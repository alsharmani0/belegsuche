"""Einlesen: Ordner durchgehen, neue und geänderte Dateien verarbeiten.

- Fortsetzbar: jede Datei wird einzeln abgeschlossen; nach Abbruch geht es beim
  nächsten Start mit der nächsten offenen Datei weiter.
- Originale werden nur gelesen.
- Ist der Ordner (z. B. die SSD) nicht erreichbar, wird nichts als gelöscht markiert.
  Verschwindet er mitten im Lauf, wird angehalten; die offenen Dateien bleiben offen.
- Verschobene oder doppelte Dateien werden am Inhalt (SHA-256) erkannt und nicht neu erkannt (OCR).
"""
from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Callable

from . import db as dbm
from .extract import IMAGE_EXT, NOTE_PREFIX, PDF_EXT, ExtractError, PageText, extract_with_notes

IGNORED_DIRS = {
    ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100",
    "$RECYCLE.BIN", "System Volume Information", "__MACOSX",
}
IGNORED_FILES = {"Thumbs.db", "desktop.ini", "Icon\r"}
MAX_ATTEMPTS = 3
MISSING_SAFETY_RATIO = 0.5   # mehr als die Hälfte verschwunden? -> vermutlich falscher/leerer Ordner
PERMANENT_ERRORS = ("PDF ist passwortgeschützt",)   # hier hilft Wiederholen nicht
MOVED_SAMPLE = 50            # Stichprobe, um einen umgezogenen Ordner (umbenannte SSD) zu erkennen …
MOVED_MIN_RATIO = 0.8        # … so viel davon muss am neuen Ort liegen
BUMP_EVERY_FILES = 50        # laufende Suchserver sehen Neues spätestens nach so vielen Dateien …
BUMP_EVERY_SECONDS = 60      # … oder so vielen Sekunden
MAX_TASKS_PER_WORKER = 50    # Arbeitsprozess danach neu starten (gibt Speicher von Vision/pdfium sicher frei)
ROOT_LOST_MSG = ("Ordner nicht mehr erreichbar (SSD getrennt?) – Einlesen angehalten. "
                 "Nach dem Anschließen erneut starten.")


def _ignored(name: str) -> bool:
    return name.startswith(".") or name.startswith("~$") or name in IGNORED_FILES or name in IGNORED_DIRS


def kind_for(ext: str) -> str:
    if ext in PDF_EXT:
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    return "unsupported"


# --- eingelesene Ordner ------------------------------------------------------------------

def normalize_root(path: str | Path) -> str:
    """Einheitliche Schreibweise: ~ und Symlinks aufgelöst (/tmp -> /private/tmp), Unicode NFC."""
    return unicodedata.normalize("NFC", os.path.realpath(os.path.expanduser(str(path))))


def _ident(path: str) -> tuple[int, int] | None:
    try:
        s = os.stat(path)
    except OSError:
        return None
    return (s.st_dev, s.st_ino) if stat.S_ISDIR(s.st_mode) else None


def _ancestors(path: str) -> set[tuple[int, int]]:
    """Dateikennungen aller Elternordner (ohne den Ordner selbst)."""
    out = set()
    path = os.path.realpath(path)
    while (parent := os.path.dirname(path)) != path:
        path = parent
        if i := _ident(path):
            out.add(i)
    return out


def _relation(new: str, old: str) -> str | None:
    """Lage von Ordner `new` zu `old`: 'same', 'inside' (new liegt in old), 'contains' oder None."""
    a, b = _ident(new), _ident(old)
    if a and b:   # beide erreichbar: Dateikennung zählt (Groß/Klein, NFC/NFD, Symlinks egal)
        if a == b:
            return "same"
        if b in _ancestors(new):
            return "inside"
        if a in _ancestors(old):
            return "contains"
        return None
    new, old = normalize_root(new).rstrip("/"), normalize_root(old).rstrip("/")
    if new == old:
        return "same"
    if new.startswith(old + "/"):
        return "inside"
    if old.startswith(new + "/"):
        return "contains"
    return None


def _conflict(con, p: str, exclude_id: int | None = None):
    """(Zeile, Lage) des eingetragenen Ordners, der p entspricht oder sich damit überschneidet."""
    found = None, None
    for r in con.execute("SELECT id, path FROM roots ORDER BY id").fetchall():
        if r["id"] == exclude_id:
            continue
        rel = _relation(p, r["path"])
        if rel == "same":
            return r, rel
        if rel and found[0] is None:
            found = r, rel
    return found


def find_root_conflict(con, path: str | Path, exclude_id: int | None = None) -> str | None:
    """Meldung, wenn der Ordner schon eingetragen ist oder sich mit einem eingetragenen überschneidet."""
    r, rel = _conflict(con, normalize_root(path), exclude_id)
    if rel == "same":
        return f"Dieser Ordner ist bereits eingetragen (Ordner {r['id']}: {r['path']})."
    if rel:
        return f"Überschneidet sich mit bereits eingelesenem Ordner {r['id']}: {r['path']}"
    return None


def _moved_root(con, p: str, log: Callable[[str], None] = print):
    """Nicht erreichbarer Ordner, dessen Dateien jetzt unter p liegen (z. B. SSD als 'SSD 1' eingehängt)?

    Geprüft wird am Inhalt (SHA-256 einer Stichprobe), nicht nur an Name und Größe – sonst würde ein
    fremder Ordner mit gleich benannten Dateien fälschlich die alten Suchtexte erben. Passen mehrere
    Ordner, wird nichts automatisch umgezogen.
    """
    matches = []
    for r in con.execute("SELECT id, path FROM roots ORDER BY id").fetchall():
        if os.path.isdir(r["path"]):
            continue
        sample = con.execute("SELECT relpath, size, sha256 FROM files WHERE root_id = ? AND status = 'done'"
                             " AND sha256 IS NOT NULL ORDER BY random() LIMIT ?", (r["id"], MOVED_SAMPLE)).fetchall()
        if not sample:
            continue
        same_name = same_content = 0
        for f in sample:
            full = os.path.join(p, f["relpath"])
            try:
                if os.stat(full).st_size != f["size"]:
                    continue
                same_name += 1
                same_content += sha256_file(full) == f["sha256"]
            except OSError:
                pass
        if same_content >= MOVED_MIN_RATIO * len(sample):
            matches.append(r)
        elif same_name >= MOVED_MIN_RATIO * len(sample):
            log(f"Hinweis: {p} hat dieselben Dateinamen wie der nicht verbundene Ordner {r['id']} ({r['path']}), "
                "aber anderen Inhalt – wird als eigener Ordner eingelesen.")
    if len(matches) > 1:
        log(f"Hinweis: {p} passt zu mehreren nicht verbundenen Ordnern ({', '.join(str(m['id']) for m in matches)}) – "
            "nicht automatisch umgezogen. Von Hand: belegsuche ordner umziehen ID NEUER_PFAD")
        return None
    return matches[0] if matches else None


def add_root(con, path: str | Path, log: Callable[[str], None] = print) -> int:
    """Ordner eintragen. Ist er schon eingetragen (auch unter anderer Schreibweise), dessen ID."""
    p = normalize_root(path)
    if not os.path.isdir(p):
        raise SystemExit(f"Ordner nicht gefunden: {p}")
    r, rel = _conflict(con, p)
    if rel == "same":
        return r["id"]
    if rel:
        raise SystemExit(f"Überschneidet sich mit bereits eingelesenem Ordner {r['id']}: {r['path']}")
    old = _moved_root(con, p, log)
    if old is not None:
        con.execute("UPDATE roots SET path = ? WHERE id = ?", (p, old["id"]))
        con.commit()
        log(f"Ordner {old['id']} ist offenbar umgezogen (umbenannte SSD?): {old['path']} → {p}. "
            "Bereits eingelesene Dateien werden übernommen.")
        return old["id"]
    cur = con.execute("INSERT INTO roots(path, added_at) VALUES (?, ?)", (p, time.time()))
    con.commit()
    return cur.lastrowid


def move_root(con, root_id: int, new_path: str | Path) -> str | None:
    """Eingetragenen Ordner auf einen neuen Ort umstellen. None = erledigt, sonst Fehlermeldung."""
    if con.execute("SELECT 1 FROM roots WHERE id = ?", (root_id,)).fetchone() is None:
        return f"Unbekannte Ordner-ID {root_id} (siehe: belegsuche ordner)."
    p = normalize_root(new_path)
    if not os.path.isdir(p):
        return f"Ordner nicht gefunden: {p}"
    msg = find_root_conflict(con, p, exclude_id=root_id)
    if msg:
        return msg
    try:
        con.execute("UPDATE roots SET path = ? WHERE id = ?", (p, root_id))
        con.commit()
    except sqlite3.IntegrityError:
        con.rollback()
        return f"Dieser Ordner ist bereits eingetragen: {p}"
    return None


def acquire_lock(db_path: str | Path) -> IO:
    """Sperre gegen zwei gleichzeitige Einlese-Läufe auf denselben Index.

    Die Sperrdatei hängt an der Datenbankdatei selbst (Gerät + Inode) und liegt in einem gemeinsamen
    Ordner – ein Symlink, ein anders geschriebener Pfad oder ein harter Link in einem anderen Ordner
    teilen sich dieselbe Sperre. Das zurückgegebene Handle offen halten, solange eingelesen wird.
    """
    import fcntl
    import tempfile
    p = Path(db_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    real = Path(os.path.realpath(p))
    real.touch(exist_ok=True)            # leere Datei ist für SQLite eine gültige neue Datenbank
    st = os.stat(real)
    lock_dir = Path(tempfile.gettempdir()) / "belegsuche-sperren"
    lock_dir.mkdir(exist_ok=True)
    f = open(lock_dir / f"{st.st_dev}-{st.st_ino}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
            return f   # Dateisystem ohne Sperren: ohne Sperre weiter
        f.close()
        raise SystemExit("Einlesen läuft bereits (anderes Fenster?).")
    return f


# --- Ordner durchgehen -------------------------------------------------------------------

@dataclass
class ScanStats:
    root: str
    online: bool = True
    seen: int = 0
    new: int = 0
    changed: int = 0
    restored: int = 0
    missing: int = 0
    missing_held_back: int = 0
    unsupported: int = 0
    dir_errors: list[str] = field(default_factory=list)


def _delete_fts_for_file(con, file_id: int) -> None:
    dbm.delete_search_rows(con, file_id)


# Hinweise (z. B. "nur die ersten 1000 Seiten") bleiben stehen, bis die Datei neu eingelesen ist
_KEEP_NOTE = f"error=CASE WHEN error LIKE '{NOTE_PREFIX}%' THEN error END"


def scan_root(con, root_id: int, root_path: str, confirm_missing: bool = False,
              log: Callable[[str], None] = print) -> ScanStats:
    st_ = ScanStats(root_path)
    if not os.path.isdir(root_path):
        st_.online = False
        log(f"Ordner nicht erreichbar, übersprungen (nichts geändert): {root_path}")
        return st_
    now = time.time()
    known = {r["relpath"]: r for r in con.execute(
        "SELECT id, relpath, size, mtime, status FROM files WHERE root_id = ?", (root_id,))}
    seen: set[str] = set()
    error_dirs: list[str] = []

    def onerror(err: OSError):
        error_dirs.append(os.path.relpath(err.filename, root_path) if err.filename else "?")
        st_.dir_errors.append(f"{err.filename}: {err.strerror}")

    batch = 0
    for dirpath, dirnames, filenames in os.walk(root_path, onerror=onerror, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not _ignored(d))
        for name in sorted(filenames):
            if _ignored(name):
                continue
            full = os.path.join(dirpath, name)
            try:
                s = os.stat(full, follow_symlinks=False)
            except OSError as e:
                st_.dir_errors.append(f"{full}: {e.strerror}")
                continue
            if not stat.S_ISREG(s.st_mode):
                continue
            rel = os.path.relpath(full, root_path)
            seen.add(rel)
            st_.seen += 1
            ext = os.path.splitext(name)[1].lower()
            kind = kind_for(ext)
            row = known.get(rel)
            if row is None:
                status = "pending" if kind != "unsupported" else "skipped"
                con.execute(
                    "INSERT INTO files(root_id, relpath, ext, kind, size, mtime, status, error, seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (root_id, rel, ext, kind, s.st_size, s.st_mtime, status,
                     "Dateityp nicht unterstützt" if kind == "unsupported" else None, now))
                if kind == "unsupported":
                    st_.unsupported += 1
                else:
                    st_.new += 1
            else:
                changed = row["size"] != s.st_size or abs((row["mtime"] or 0) - s.st_mtime) > 1e-3
                if kind == "unsupported":
                    con.execute("UPDATE files SET size=?, mtime=?, status='skipped', seen_at=? WHERE id=?",
                                (s.st_size, s.st_mtime, now, row["id"]))
                    st_.unsupported += 1
                elif row["status"] == "missing":
                    con.execute(f"UPDATE files SET size=?, mtime=?, status='pending', attempts=0, {_KEEP_NOTE},"
                                " seen_at=? WHERE id=?", (s.st_size, s.st_mtime, now, row["id"]))
                    st_.restored += 1
                elif changed:
                    con.execute(f"UPDATE files SET size=?, mtime=?, status='pending', attempts=0, {_KEEP_NOTE},"
                                " seen_at=? WHERE id=?", (s.st_size, s.st_mtime, now, row["id"]))
                    st_.changed += 1
                else:
                    con.execute("UPDATE files SET seen_at=? WHERE id=?", (now, row["id"]))
            batch += 1
            if batch % 2000 == 0:
                con.commit()
    con.commit()

    gone = [r for rel, r in known.items() if rel not in seen and r["status"] != "missing"]
    # Dateien unter Ordnern, die nicht gelesen werden konnten, gelten nicht als verschwunden
    if error_dirs:
        prefixes = tuple(d.rstrip("/") + "/" for d in error_dirs if d not in (".", "?"))
        if "." in error_dirs or "?" in error_dirs:
            gone = []
        else:
            gone = [r for r in gone if not r["relpath"].startswith(prefixes)]
    alive = sum(1 for r in known.values() if r["status"] != "missing")
    if gone and st_.seen == 0 and not confirm_missing:
        # Ordner ist da, aber leer: typischer leerer Einhängepunkt einer nicht angeschlossenen SSD
        st_.missing_held_back = len(gone)
        log(f"Achtung: {root_path} ist leer, bekannt sind {alive} Dateien – vermutlich ist die SSD nicht richtig "
            "angeschlossen. Nichts wurde als gelöscht markiert. Falls der Ordner wirklich geleert wurde: "
            "erneut mit --fehlende-bestaetigen starten.")
        gone = []
    if gone and alive >= 20 and len(gone) > MISSING_SAFETY_RATIO * alive and not confirm_missing:
        st_.missing_held_back = len(gone)
        log(f"Achtung: {len(gone)} von {alive} bekannten Dateien fehlen in {root_path}. "
            "Das sieht nach einem falschen oder unvollständigen Ordner aus – nichts wurde als gelöscht markiert. "
            "Falls die Dateien wirklich weg sind: erneut mit --fehlende-bestaetigen starten.")
        gone = []
    for r in gone:
        _delete_fts_for_file(con, r["id"])
        con.execute("UPDATE files SET status='missing' WHERE id=?", (r["id"],))
    st_.missing = len(gone)
    if gone:
        dbm.bump_generation(con)
    con.commit()
    return st_


# --- Dateien verarbeiten -----------------------------------------------------------------

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _write_pages(con, file_id: int, relpath: str, pages: list[PageText]) -> None:
    _delete_fts_for_file(con, file_id)
    con.execute("DELETE FROM pages WHERE file_id = ?", (file_id,))
    for p in pages:
        text = unicodedata.normalize("NFC", p.text)   # zerlegte Umlaute (u + ¨) aus PDF-Textebenen zusammenfügen
        con.execute("INSERT INTO pages(file_id, page_no, text, source, ocr_conf) VALUES (?, ?, ?, ?, ?)",
                    (file_id, p.page_no, text, p.source, p.ocr_conf))
    dbm.index_file_text(con, file_id, relpath)


def _pages_of(con, file_id: int) -> list[PageText]:
    return [PageText(r["page_no"], r["text"], r["source"], r["ocr_conf"])
            for r in con.execute("SELECT page_no, text, source, ocr_conf FROM pages WHERE file_id = ? ORDER BY page_no",
                                 (file_id,))]


def _note_of(con, file_id: int) -> str | None:
    row = con.execute("SELECT error FROM files WHERE id = ?", (file_id,)).fetchone()
    return row[0] if row and row[0] and row[0].startswith(NOTE_PREFIX) else None


@dataclass
class ProcessStats:
    done: int = 0
    reused: int = 0
    errors: int = 0
    pages: int = 0
    ocr_pages: int = 0
    skipped_offline: int = 0
    interrupted: bool = False
    root_lost: bool = False      # Ordner (SSD) mitten im Lauf verschwunden -> angehalten
    retried: int = 0             # Dateien mit Fehler, die erneut versucht wurden
    seconds: float = 0.0


def retry_errors(con, include_permanent: bool = False) -> int:
    """Dateien mit Fehler wieder als offen markieren (z. B. nach abgezogener SSD oder OCR-Aussetzer).

    Ohne include_permanent bleiben Fehler stehen, bei denen Wiederholen nicht hilft (passwortgeschützt).
    Rückgabe: Anzahl der Dateien.
    """
    sql = "UPDATE files SET status='pending', attempts=0, error=NULL WHERE status='error'"
    args: tuple = ()
    if not include_permanent:
        sql += f" AND coalesce(error, '') NOT IN ({','.join('?' * len(PERMANENT_ERRORS))})"
        args = PERMANENT_ERRORS
    n = con.execute(sql, args).rowcount
    con.commit()
    return n


def _reset_crashed(con) -> None:
    """Dateien, bei denen ein früherer Lauf hart abgebrochen ist (Absturz, Strom weg)."""
    con.execute("UPDATE files SET status='error', error='Programm ist bei dieser Datei mehrfach abgebrochen'"
                " WHERE status='processing' AND attempts >= ?", (MAX_ATTEMPTS,))
    con.execute("UPDATE files SET status='pending' WHERE status='processing'")
    con.commit()


def _root_gone(root: str) -> bool:
    """Ordner weg oder leer (SSD abgezogen, nur noch der leere Einhängepunkt)?"""
    try:
        with os.scandir(root) as it:
            return next(it, None) is None
    except OSError:
        return True


class _RootLost(Exception):
    """Der Ordner ist mitten im Lauf verschwunden."""


def _prepare(con, r):
    """Markiert die Datei als 'in Arbeit', berechnet den Hash und prüft, ob der Text schon bekannt ist.

    Rückgabe: (stat, sha256, pages oder None, übernommen?, Hinweis oder None)
    """
    fid = r["id"]
    con.execute("UPDATE files SET status='processing', attempts=attempts+1 WHERE id=?", (fid,))
    con.commit()
    full = os.path.join(r["root"], r["relpath"])
    s = os.stat(full)
    sha = sha256_file(full)
    if r["sha256"] == sha and con.execute("SELECT 1 FROM pages WHERE file_id=? LIMIT 1", (fid,)).fetchone():
        return s, sha, _pages_of(con, fid), True, _note_of(con, fid)
    src = con.execute(
        "SELECT f.id FROM files f WHERE f.sha256 = ? AND f.id != ? AND f.status IN ('done', 'missing')"
        " AND EXISTS (SELECT 1 FROM pages p WHERE p.file_id = f.id) LIMIT 1", (sha, fid)).fetchone()
    if src:
        return s, sha, _pages_of(con, src[0]), True, _note_of(con, src[0])
    return s, sha, None, False, None


class _Progress:
    def __init__(self, con, ps: ProcessStats, total: int, log):
        self.con, self.ps, self.total, self.log = con, ps, total, log
        self.i = 0
        self.t_start = time.time()
        self.unbumped = 0            # Änderungen am Suchindex, die Suchserver noch nicht gemeldet bekamen
        self.t_bump = self.t_start

    def _tick(self):
        self.i += 1
        if self.i % 25 == 0 and self.i < self.total:
            elapsed = time.time() - self.t_start
            self.log(f"   … {self.i}/{self.total} Dateien, noch ca. {elapsed / self.i * (self.total - self.i) / 60:.0f} min")

    def _changed(self):
        """Laufende Suchserver regelmäßig neu laden lassen, nicht erst am Ende eines tagelangen Laufs."""
        self.unbumped += 1
        if self.unbumped >= BUMP_EVERY_FILES or time.time() - self.t_bump >= BUMP_EVERY_SECONDS:
            self.bump()

    def bump(self):
        dbm.bump_generation(self.con)
        self.unbumped = 0
        self.t_bump = time.time()

    def finish(self, r, s, sha, pages: list[PageText], reused: bool, t0: float, note: str | None = None):
        con, ps = self.con, self.ps
        _write_pages(con, r["id"], r["relpath"], pages)
        ocr_pages = sum(1 for p in pages if p.source != "text")
        confs = [p.ocr_conf for p in pages if p.ocr_conf is not None]
        chars = sum(len(p.text.strip()) for p in pages)
        con.execute(
            "UPDATE files SET status='done', error=?, attempts=0, sha256=?, size=?, mtime=?, pages=?,"
            " ocr_pages=?, text_chars=?, min_conf=?, indexed_at=? WHERE id=?",
            (note, sha, s.st_size, s.st_mtime, len(pages), ocr_pages, chars, min(confs) if confs else None,
             time.time(), r["id"]))
        self._changed()
        con.commit()
        ps.done += 1
        ps.reused += reused
        ps.pages += len(pages)
        ps.ocr_pages += 0 if reused else ocr_pages
        info = "übernommen (gleicher Inhalt schon bekannt)" if reused else f"{len(pages)} S., {ocr_pages} per OCR"
        if chars == 0:
            info += ", KEIN TEXT"
        if note:
            info += f" – {note}"
        self.log(f"[{self.i + 1}/{self.total}] {r['relpath']} – {info} ({time.time() - t0:.1f}s)")
        self._tick()

    def fail(self, r, e: BaseException):
        """Datei als fehlerhaft/verschwunden markieren. Ist der ganze Ordner weg: _RootLost."""
        con = self.con
        con.rollback()
        if _root_gone(r["root"]):
            # SSD abgezogen o. Ä.: kein Fehler der Datei – offen lassen und anhalten
            self.back_to_pending(r)
            raise _RootLost(r["root"])
        if isinstance(e, FileNotFoundError):
            _delete_fts_for_file(con, r["id"])
            con.execute("UPDATE files SET status='missing' WHERE id=?", (r["id"],))
            self._changed()
            con.commit()
            self.log(f"[{self.i + 1}/{self.total}] {r['relpath']} – verschwunden, übersprungen")
        else:
            msg = str(e) if isinstance(e, ExtractError) else f"{type(e).__name__}: {e}"
            con.execute("UPDATE files SET status='error', error=? WHERE id=?", (msg[:500], r["id"]))
            con.commit()
            self.ps.errors += 1
            self.log(f"[{self.i + 1}/{self.total}] {r['relpath']} – FEHLER: {msg}")
        self._tick()

    def back_to_pending(self, r):
        """Datei, die gerade in Arbeit ist, wieder als offen markieren (Versuch zählt nicht)."""
        self.con.rollback()
        self.con.execute("UPDATE files SET status='pending', attempts=MAX(attempts-1, 0)"
                         " WHERE id=? AND status='processing'", (r["id"],))
        self.con.commit()

    def stop(self, msg: str, root_lost: bool = False):
        self.ps.interrupted = True
        self.ps.root_lost = self.ps.root_lost or root_lost
        self.log(msg)


def _todo(con, ps: ProcessStats, limit: int | None):
    rows = con.execute(
        "SELECT f.id, f.relpath, f.sha256, f.attempts, r.path AS root FROM files f JOIN roots r ON r.id = f.root_id"
        " WHERE f.status = 'pending' ORDER BY r.id, f.relpath").fetchall()
    online: dict[str, bool] = {}
    todo = []
    for r in rows:
        if r["root"] not in online:
            online[r["root"]] = os.path.isdir(r["root"])
        if online[r["root"]]:
            todo.append(r)
        else:
            ps.skipped_offline += 1
    return todo[:limit] if limit is not None else todo


def process_pending(con, engine_name: str = "apple", limit: int | None = None,
                    log: Callable[[str], None] = print, engine=None, workers: int = 1) -> ProcessStats:
    """Verarbeitet alle offenen Dateien. `workers` > 1: Texterkennung in mehreren Prozessen parallel."""
    ps = ProcessStats()
    t_start = time.time()
    _reset_crashed(con)
    todo = _todo(con, ps, limit)
    prog = _Progress(con, ps, len(todo), log)
    if todo:
        if workers > 1 and engine is None:
            _process_parallel(con, todo, engine_name, workers, prog)
        else:
            if engine is None:
                from .ocr import make_engine
                engine = make_engine(engine_name)
            _process_sequential(con, todo, engine, prog)
    if prog.unbumped:
        prog.bump()
        con.commit()
    ps.seconds = time.time() - t_start
    return ps


def _process_sequential(con, todo, engine, prog: _Progress) -> None:
    try:
        for r in todo:
            t0 = time.time()
            try:
                if _root_gone(r["root"]):
                    raise _RootLost(r["root"])
                s, sha, pages, reused, note = _prepare(con, r)
                if pages is None:
                    pages, notes = extract_with_notes(Path(r["root"], r["relpath"]), engine)
                    note = "; ".join(notes) or None
                prog.finish(r, s, sha, pages, reused, t0, note)
            except KeyboardInterrupt:
                prog.back_to_pending(r)
                prog.stop("Unterbrochen. Beim nächsten Start geht es mit dieser Datei weiter.")
                return
            except _RootLost:
                raise
            except Exception as e:  # jede Datei einzeln: ein Fehler stoppt nicht den ganzen Lauf
                prog.fail(r, e)
    except _RootLost:
        prog.stop(ROOT_LOST_MSG, root_lost=True)


# --- parallele Verarbeitung -------------------------------------------------------------

_WORKER_ENGINE = None


def _init_worker(engine_name: str) -> None:
    global _WORKER_ENGINE
    import signal
    # Ctrl+C, Terminal-Fenster schließen, Abmelden: das regelt der Hauptprozess (setzt offene
    # Dateien zurück und beendet die Arbeitsprozesse). Sonst zählte jedes Schließen als Absturz.
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)
    from .ocr import make_engine
    _WORKER_ENGINE = make_engine(engine_name)


def _extract_job(path: str) -> tuple[list[PageText], list[str]]:
    crash_on = os.environ.get("BELEGSUCHE_TEST_CRASH_ON")   # nur für Tests: harten Absturz simulieren
    if crash_on and path.endswith(crash_on):
        os._exit(1)
    return extract_with_notes(Path(path), _WORKER_ENGINE)


def _start_resource_tracker() -> None:
    """Hilfsprozess von multiprocessing so starten, dass auch er SIGHUP (Fenster schließen) übersteht."""
    import signal
    from multiprocessing import resource_tracker
    try:
        old = signal.signal(signal.SIGHUP, signal.SIG_IGN)   # wird beim Start vererbt
    except ValueError:   # nicht im Hauptthread
        return
    try:
        resource_tracker.ensure_running()
    finally:
        signal.signal(signal.SIGHUP, old)


def _make_pool(engine_name: str, workers: int):
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    _start_resource_tracker()
    kw = {"max_tasks_per_child": MAX_TASKS_PER_WORKER} if sys.version_info >= (3, 11) else {}
    return ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"),
                               initializer=_init_worker, initargs=(engine_name,), **kw)


def _kill_pool(pool) -> None:
    for p in list(getattr(pool, "_processes", {}).values()):
        try:
            p.kill()   # SIGKILL: die Arbeitsprozesse ignorieren SIGTERM
        except Exception:
            pass
    pool.shutdown(wait=False, cancel_futures=True)


def _process_parallel(con, todo, engine_name: str, workers: int, prog: _Progress) -> None:
    from collections import deque
    from concurrent.futures import FIRST_COMPLETED, wait
    from concurrent.futures.process import BrokenProcessPool

    queue = deque(todo)
    solo: deque = deque()      # nach einem Absturz: einzeln verarbeiten, damit nur die schuldige Datei zählt
    deferred: list = []        # gleicher Inhalt ist gerade in Arbeit -> danach übernehmen
    inflight: dict = {}        # future -> (row, stat, sha, t0)
    inflight_sha: set[str] = set()
    current = None             # Datei, die der Hauptprozess gerade vorbereitet
    crashed: list = []
    pool = _make_pool(engine_name, workers)
    solo_running = False
    try:
        while queue or solo or inflight or deferred:
            while not solo_running and ((solo and not inflight) or (queue and not solo and len(inflight) < workers * 2)):
                from_solo = bool(solo)
                r = solo.popleft() if from_solo else queue.popleft()
                if _root_gone(r["root"]):
                    raise _RootLost(r["root"])
                current, t0 = r, time.time()
                try:
                    s, sha, pages, reused, note = _prepare(con, r)
                except Exception as e:
                    prog.fail(r, e)
                    current = None
                    continue
                if pages is not None:
                    prog.finish(r, s, sha, pages, reused, t0, note)
                    current = None
                    continue
                if sha in inflight_sha:
                    prog.back_to_pending(r)
                    current = None
                    deferred.append(r)
                    continue
                fut = pool.submit(_extract_job, os.path.join(r["root"], r["relpath"]))
                inflight[fut] = (r, s, sha, t0)
                current = None
                inflight_sha.add(sha)
                solo_running = from_solo
            if not inflight:
                if deferred:
                    queue.extendleft(reversed(deferred))
                    deferred = []
                    continue
                if not queue and not solo:
                    break
                continue
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            if not inflight.keys() - done:
                solo_running = False
            crashed = []
            for fut in done:
                r, s, sha, t0 = inflight.pop(fut)
                inflight_sha.discard(sha)
                try:
                    pages, notes = fut.result()
                    prog.finish(r, s, sha, pages, False, t0, "; ".join(notes) or None)
                except BrokenProcessPool:
                    crashed.append(r)
                except Exception as e:
                    prog.fail(r, e)
            if crashed:
                # Ein Prozess ist abgestürzt: alle laufenden Dateien sind betroffen
                for fut, (r, s, sha, t0) in list(inflight.items()):
                    crashed.append(r)
                inflight.clear()
                inflight_sha.clear()
                solo_running = False
                _kill_pool(pool)
                if any(_root_gone(r["root"]) for r in crashed):
                    raise _RootLost(crashed[0]["root"])   # SSD weg: die Dateien sind nicht schuld
                for r in crashed:
                    att = con.execute("SELECT attempts FROM files WHERE id=?", (r["id"],)).fetchone()[0]
                    if att >= MAX_ATTEMPTS:
                        prog.fail(r, ExtractError("Programm ist bei dieser Datei mehrfach abgebrochen"))
                    else:
                        con.execute("UPDATE files SET status='pending' WHERE id=?", (r["id"],))
                        con.commit()
                        solo.append(dict(r) | {"attempts": att})
                crashed = []
                prog.log(f"   Ein Verarbeitungsprozess ist abgestürzt – {len(solo)} Datei(en) werden einzeln wiederholt.")
                pool = _make_pool(engine_name, workers)
    except (KeyboardInterrupt, _RootLost) as e:
        # nur Dateien, die noch 'processing' sind, gehen zurück (back_to_pending prüft das)
        for r in [current, *(v[0] for v in inflight.values()), *crashed]:
            if r is not None:
                prog.back_to_pending(r)
        _kill_pool(pool)
        if isinstance(e, _RootLost):
            prog.stop(ROOT_LOST_MSG, root_lost=True)
        else:
            prog.stop("Unterbrochen. Beim nächsten Start geht es mit den offenen Dateien weiter.")
        return
    try:
        pool.shutdown(wait=True)
    except KeyboardInterrupt:   # alles erledigt, nur das Aufräumen wurde unterbrochen
        _kill_pool(pool)
        prog.ps.interrupted = True


def run_index(con, folders: list[str], engine_name: str = "apple", confirm_missing: bool = False,
              limit: int | None = None, log: Callable[[str], None] = print, engine=None, workers: int = 1,
              retry: bool = False):
    """Ordner eintragen, durchgehen und offene Dateien verarbeiten.

    retry: Dateien mit Fehler (außer passwortgeschützten) vorher erneut als offen markieren.
    """
    ids = [add_root(con, f, log) for f in folders]
    roots = con.execute("SELECT id, path FROM roots ORDER BY id").fetchall()
    if folders:
        roots = [r for r in roots if r["id"] in ids]
    scans = [scan_root(con, r["id"], r["path"], confirm_missing, log) for r in roots]
    for s in scans:
        if s.online:
            log(f"{s.root}: {s.seen} Dateien – {s.new} neu, {s.changed} geändert, {s.restored} wieder da, "
                f"{s.missing} fehlen, {s.unsupported} nicht unterstützt")
        for e in s.dir_errors[:10]:
            log(f"   nicht lesbar: {e}")
    retried = retry_errors(con) if retry else 0
    if retried:
        log(f"{retried} Datei(en) mit Fehler werden erneut versucht.")
    ps = process_pending(con, engine_name, limit, log, engine, workers)
    ps.retried = retried
    return scans, ps
