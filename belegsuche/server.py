"""Lokale Suchoberfläche im Browser (nur auf diesem Mac erreichbar: 127.0.0.1).

E-Mails: „Mail öffnen“ öffnet die .eml mit Apple Mail. „Anhang öffnen“ liest die Mail neu (nur lesend), sucht den
Teil über parts.mime_path, prüft die sha256 und schreibt ihn schreibgeschützt in einen eigenen Ordner im
System-Temp (nie neben die Mail, nie in einen eingelesenen Ordner). Die Kopie trägt die Quarantäne-Markierung
von macOS wie ein Anhang aus Apple Mail (Word öffnet sie in der geschützten Ansicht). Ein DOMUS-Platzhalter
öffnet die Datei, auf die er verweist. /datei/<id>/teil/<n> liefert PDF- und Bild-Anhänge direkt aus dem
Speicher (für „Seite Y im Browser“). Mails, Word-Dateien und alle Typen außerhalb einer kleinen Liste gehen nur
als Download mit CSP sandbox hinaus, nie als Seite im Ursprung der Oberfläche.

Word-Dateien öffnen ebenfalls als schreibgeschützte Kopie im System-Temp: Word legt sonst eine Sperrdatei (~$…)
neben das Original im eingelesenen Ordner und erlaubt Speichern ins Original. „Im Finder zeigen“ zeigt das Original.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import db as dbm
from .search import Searcher

UI_FILE = Path(__file__).with_name("ui.html")
UI_ASSETS = {"/assets/glasarchiv.webp": ("glasarchiv.webp", "image/webp"),
             "/assets/marke.svg": ("marke.svg", "image/svg+xml"),
             "/favicon.ico": ("marke.svg", "image/svg+xml")}
MAX_ID = 2**63 - 1                     # größter Wert, den SQLite als INTEGER speichert
MAX_BODY = 10_000
# Anfragen fremder Webseiten abweisen: der eigene Browser-Tab schickt same-origin,
# Adresszeile/Lesezeichen/webbrowser.open schicken none, ältere Browser gar nichts.
ALLOWED_FETCH_SITES = (None, "same-origin", "none")
CSP_DEFAULT = "frame-ancestors 'none'"
# Downloads (Mails, Word, unbekannte Typen): selbst wenn ein Browser sie doch anzeigt, läuft darin nichts
CSP_ATTACHMENT = "sandbox; default-src 'none'; frame-ancestors 'none'"
CSP_IMAGE = "sandbox; frame-ancestors 'none'"
CSP_UI = ("default-src 'none'; img-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
          "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# Pfad darf nirgends (macOS: auch nicht in übergeordneten Ordnern) ein Symlink sein; O_NOFOLLOW_ANY
# und O_NOFOLLOW zusammen ergeben EINVAL. O_NONBLOCK, damit eine untergeschobene Named Pipe das
# Öffnen nicht blockiert (bei normalen Dateien wirkungslos).
OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW_ANY", os.O_NOFOLLOW)
# Neue Temp-Datei: nur neu anlegen (keine vorhandene, kein Symlink überschreiben)
WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

# /datei/<id>: nur diese Typen werden im Browser angezeigt, alles andere ist ein Download
INLINE_FILE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "image/tiff",
                     "image/bmp", "image/heic", "image/heif"}
DOWNLOAD_KINDS = ("mail", "word")
# Anhänge: Art nur an den ersten Bytes (mail/extract.sniff), nie an der Angabe in der Mail
PART_EXT = {"pdf": ".pdf", "png": ".png", "jpeg": ".jpg", "gif": ".gif", "tiff": ".tif", "heic": ".heic",
            "bmp": ".bmp", "webp": ".webp", "rtf": ".rtf", "docx": ".docx", "odt": ".odt", "doc": ".doc",
            "txt": ".txt"}
PART_INLINE = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif",
               "webp": "image/webp"}
OPENABLE_PARTS = ("pdf", "image", "word", "text")      # parts.kind, die sich öffnen lassen
MAX_PART_NO = 10_000
ATTACH_PREFIX = "belegsuche-anhang-"
ATTACH_MAX_AGE = 24 * 3600            # ältere Reste im Temp-Ordner räumt der nächste Start weg
ATTACH_MANIFEST = ".belegsuche-kopie.json"
ATTACH_FORMAT = "belegsuche-temp-copy-v1"
MAX_NAME_CHARS = 100
MAIL_APP = "com.apple.mail"
MAIL_CHANGED = "Mail geändert – neu einlesen"
QUARANTINE_ATTR = b"com.apple.quarantine"
MAX_COPY_BYTES = 200 * 1024 * 1024    # Word-Dateien größer als das werden nicht kopiert (word.MAX_WORD_BYTES)


def _file_row(db_path: Path, file_id: int):
    con = dbm.connect(db_path, readonly=True)
    try:
        return con.execute(
            "SELECT f.relpath, f.status, f.kind, r.path AS root FROM files f JOIN roots r ON r.id = f.root_id"
            " WHERE f.id = ?", (file_id,)).fetchone()
    finally:
        con.close()


def _file_kind(db_path: Path, file_id: int) -> str | None:
    row = _file_row(db_path, file_id)
    return row["kind"] if row is not None else None


def _resolve(db_path: Path, file_id: int) -> tuple[str | None, str | None]:
    """(echter Pfad ohne Symlinks, Fehlermeldung). Nur Dateien aus dem Index, nur innerhalb ihres Ordners."""
    row = _file_row(db_path, file_id)
    if row is None or row["status"] not in dbm.SEARCHABLE_STATUS:
        return None, "Unbekannte Datei"
    if not os.path.isdir(row["root"]):
        return None, f"Ordner nicht verbunden: {row['root']} – SSD anschließen?"
    full = os.path.join(row["root"], row["relpath"])
    real_root = os.path.realpath(row["root"])
    real = os.path.realpath(full)
    if not real.startswith(real_root.rstrip("/") + "/"):
        return None, "Pfad liegt außerhalb des Ordners"
    if not os.path.isfile(real):
        return None, "Datei nicht mehr vorhanden (verschoben oder gelöscht?) – neu einlesen"
    return real, None


def _valid_id(v) -> bool:
    return type(v) is int and 1 <= v <= MAX_ID


def _valid_part(v) -> bool:
    return type(v) is int and 1 <= v <= MAX_PART_NO


# --- Dateinamen und Temp-Ordner -------------------------------------------------------------------

def _clean_name(name: str | None) -> str:
    """Nur der Basisname, ohne Steuer- und Formatzeichen (auch Richtungswechsel U+202E), ohne führende Punkte."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
    name = re.sub(r"\s+", " ", name.replace(":", "_")).strip()
    return name.lstrip(". ").rstrip(". ")


def safe_filename(name: str | None, ext: str) -> str:
    """Dateiname für eine Temp-Kopie: bereinigter Basisname, gekürzt, Endung aus den ersten Bytes (ext)."""
    stem = _clean_name(name)
    base, old = os.path.splitext(stem)
    if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", old) and re.search(r"[A-Za-z]", old):
        stem = base                              # angegebene Endung zählt nicht, die ersten Bytes zählen
    stem = stem[:MAX_NAME_CHARS].rstrip(". ") or "Anhang"
    return stem + ext


def _content_disposition(kind: str, name: str) -> str:
    plain = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;%' else "_" for c in name) or "datei"
    return f"{kind}; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"


_inside = dbm.path_inside


def set_quarantine(fd: int) -> bool:
    """Eine neue macOS-Quarantäne-Markierung setzen; False, wenn sie nicht gesetzt werden konnte."""
    value = f"0081;{int(time.time()):08x};Belegsuche;".encode("ascii")
    return _set_quarantine_value(fd, value)


def _set_quarantine_value(fd: int, value: bytes) -> bool:
    """Quarantäne am geöffneten Dateideskriptor setzen, auch unverändert vom Original."""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fsetxattr = libc.fsetxattr
    except (OSError, AttributeError):
        return False
    fsetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                          ctypes.c_int]
    fsetxattr.restype = ctypes.c_int
    return fsetxattr(fd, QUARANTINE_ATTR, value, len(value), 0, 0) == 0


def _get_quarantine(fd: int) -> bytes | None:
    """Original-Markierung vom geöffneten FD lesen; nur ENOATTR/ENODATA bedeutet „keine vorhanden“."""
    import ctypes
    try:
        get = ctypes.CDLL(None, use_errno=True).fgetxattr
    except (OSError, AttributeError) as e:
        raise OSError(errno.ENOTSUP, "Quarantäne-Markierung kann nicht gelesen werden") from e
    get.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                   ctypes.c_int]
    get.restype = ctypes.c_ssize_t
    size = get(fd, QUARANTINE_ATTR, None, 0, 0, 0)
    if size < 0:
        code = ctypes.get_errno()
        if code in (getattr(errno, "ENOATTR", None), getattr(errno, "ENODATA", None)):
            return None
        raise OSError(code, os.strerror(code))
    if size == 0:
        raise OSError(errno.EINVAL, "Quarantäne-Markierung ist leer")
    if size > 65536:
        raise OSError(errno.E2BIG, "Quarantäne-Markierung ist zu groß")
    buffer = ctypes.create_string_buffer(size)
    actual = get(fd, QUARANTINE_ATTR, buffer, len(buffer), 0, 0)
    if actual < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    if actual != size:
        raise OSError(errno.EAGAIN, "Quarantäne-Markierung wurde während des Lesens geändert")
    return buffer.raw[:actual]


class TempRefused(Exception):
    """Temp-Ordner läge in einem eingelesenen Ordner – dann wird nichts geschrieben."""


class QuarantineError(OSError):
    """Eine erforderliche Sicherheitsmarkierung konnte nicht gesetzt werden."""


def _archive_overlap(path: str, roots: list[str]) -> bool:
    return any(_inside(path, root) or _inside(root, path) for root in roots)


def _remove_copy(directory: str, manifest_fd: int, roots: list[str]) -> bool:
    """Nur die im eigenen Manifest belegte unveränderte Kopie entfernen; nie unbekannte Inhalte rekursiv."""
    if _archive_overlap(directory, roots):
        return False
    directory_fd = os.open(directory, OPEN_FLAGS | os.O_DIRECTORY)
    try:
        ds, ms = os.fstat(directory_fd), os.fstat(manifest_fd)
        if not stat.S_ISDIR(ds.st_mode) or ds.st_uid != os.getuid() or stat.S_IMODE(ds.st_mode) != 0o700:
            return False
        marker = os.stat(ATTACH_MANIFEST, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(ms.st_mode) or ms.st_uid != os.getuid() or stat.S_IMODE(ms.st_mode) != 0o600 \
                or (marker.st_dev, marker.st_ino) != (ms.st_dev, ms.st_ino) or ms.st_size > 2048:
            return False
        os.lseek(manifest_fd, 0, os.SEEK_SET)
        try:
            record = json.loads(os.read(manifest_fd, 2049))
        except (ValueError, RecursionError):
            return False
        if not isinstance(record, dict) or record.get("format") != ATTACH_FORMAT \
                or record.get("directory") != os.path.basename(directory) \
                or record.get("directory_identity") != [ds.st_dev, ds.st_ino]:
            return False
        name = record.get("name")
        size = record.get("size")
        if not isinstance(name, str) or not name or name != _clean_name(name) or name == ATTACH_MANIFEST \
                or "/" in name or "\\" in name or type(size) is not int or not 0 <= size <= MAX_COPY_BYTES:
            return False
        if set(os.listdir(directory_fd)) != {ATTACH_MANIFEST, name}:
            return False
        fd = os.open(name, OPEN_FLAGS, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as copy:
            fs = os.fstat(fd)
            if not stat.S_ISREG(fs.st_mode) or fs.st_uid != os.getuid() or fs.st_size != size \
                    or stat.S_IMODE(fs.st_mode) != 0o400 \
                    or record.get("file_identity") != [fs.st_dev, fs.st_ino]:
                return False
            digest = hashlib.file_digest(copy, "sha256").hexdigest()
        if digest != record.get("sha256"):
            return False
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        current_directory = os.stat(directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (fs.st_dev, fs.st_ino) \
                or (current_directory.st_dev, current_directory.st_ino) != (ds.st_dev, ds.st_ino) \
                or _archive_overlap(directory, roots):
            return False
        os.unlink(name, dir_fd=directory_fd)
        os.unlink(ATTACH_MANIFEST, dir_fd=directory_fd)
        current_directory = os.stat(directory, follow_symlinks=False)
        if (current_directory.st_dev, current_directory.st_ino) == (ds.st_dev, ds.st_ino):
            os.rmdir(directory)
        return True
    finally:
        os.close(directory_fd)


class AttachmentTemp:
    """Temp-Kopien geöffneter Anhänge: je Anhang ein eigener Ordner (0700) unter $TMPDIR, Datei 0400.
    Nur unveränderte eigene Kopien werden entfernt; belegte inaktive Reste auch beim nächsten Start."""

    def __init__(self):
        self._dirs: list[tuple[str, int, list[str]]] = []
        self._lock = threading.Lock()
        self._closed = False

    def write(self, name: str, data: bytes, roots: list[str], quarantine: bool | bytes = True) -> str:
        """Kopie und Besitzmanifest anlegen. Gehaltener flock kennzeichnet die aktive Sitzung."""
        if not name or name != _clean_name(name) or name == ATTACH_MANIFEST:
            raise OSError(errno.EINVAL, "Ungültiger Dateiname für die Temp-Kopie")
        base = os.path.realpath(tempfile.gettempdir())
        for r in roots:
            if _inside(base, r):
                raise TempRefused(r)
        with self._lock:
            if self._closed:
                raise OSError(errno.ECANCELED, "Suchoberfläche wird beendet")
            d = tempfile.mkdtemp(prefix=ATTACH_PREFIX, dir=base)
            marker_fd, identity = None, None
            try:
                os.chmod(d, 0o700)
                if _archive_overlap(d, roots):
                    raise TempRefused(d)
                marker_fd = os.open(os.path.join(d, ATTACH_MANIFEST),
                                    (WRITE_FLAGS & ~os.O_WRONLY) | os.O_RDWR, 0o600)
                fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                path = os.path.join(d, name)
                fd = os.open(path, WRITE_FLAGS, 0o600)
                with os.fdopen(fd, "wb") as f:
                    fs = os.fstat(fd)
                    identity = fs.st_dev, fs.st_ino
                    ds = os.stat(d, follow_symlinks=False)
                    record = {"format": ATTACH_FORMAT, "directory": os.path.basename(d), "name": name,
                              "directory_identity": [ds.st_dev, ds.st_ino],
                              "file_identity": list(identity), "size": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()}
                    with os.fdopen(os.dup(marker_fd), "wb") as marker:
                        marker.write(json.dumps(record, ensure_ascii=True).encode("ascii"))
                    f.write(data)
                    f.flush()
                    marked = (_set_quarantine_value(fd, quarantine) if isinstance(quarantine, bytes)
                              else set_quarantine(fd) if quarantine else True)
                    if not marked:
                        raise QuarantineError(errno.EACCES, "Erforderliche Quarantäne-Markierung konnte nicht gesetzt werden")
                    os.fchmod(fd, 0o400)
                self._dirs.append((d, marker_fd, list(roots)))
                return path
            except BaseException:
                # Nur genau die gerade von uns angelegte Datei entfernen, niemals fremde Inhalte rekursiv.
                try:
                    if identity is not None:
                        current = os.stat(os.path.join(d, name), follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == identity:
                            os.unlink(os.path.join(d, name))
                    if marker_fd is not None:
                        current = os.stat(os.path.join(d, ATTACH_MANIFEST), follow_symlinks=False)
                        ms = os.fstat(marker_fd)
                        if (current.st_dev, current.st_ino) == (ms.st_dev, ms.st_ino):
                            os.unlink(os.path.join(d, ATTACH_MANIFEST))
                    os.rmdir(d)
                except OSError:
                    pass
                finally:
                    if marker_fd is not None:
                        os.close(marker_fd)
                raise

    def cleanup(self, roots: list[str] | None = None, *, remove: bool = True) -> None:
        with self._lock:
            dirs, self._dirs = self._dirs, []
            self._closed = True
        for directory, fd, saved_roots in dirs:
            try:
                if remove:
                    _remove_copy(directory, fd, saved_roots + (roots or []))
            except OSError:
                pass
            finally:
                os.close(fd)

    @staticmethod
    def cleanup_stale(max_age: float = ATTACH_MAX_AGE, now: float | None = None,
                      *, roots: list[str] | None = None) -> int:
        """Nur belegte, unveränderte Reste beendeter Sitzungen außerhalb sämtlicher Archivwurzeln entfernen."""
        if roots is None:                       # Ohne aktuelle Wurzeln gibt es keinen Löschungsnachweis.
            return 0
        base = os.path.realpath(tempfile.gettempdir())
        now = time.time() if now is None else now
        removed = 0
        try:
            entries = list(os.scandir(base))
        except OSError:
            return 0
        for e in entries:
            if not e.name.startswith(ATTACH_PREFIX):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or now - st.st_mtime < max_age \
                    or _archive_overlap(e.path, roots):
                continue
            fd = None
            try:
                fd = os.open(os.path.join(e.path, ATTACH_MANIFEST), os.O_RDWR | OPEN_FLAGS)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # aktive andere Sitzung niemals aufräumen
                removed += _remove_copy(e.path, fd, roots)
            except OSError:
                pass
            finally:
                if fd is not None:
                    os.close(fd)
        return removed


class PartError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _part_type(data: bytes, part_kind: str) -> str | None:
    """Art eines Anhangs an den ersten Bytes (Schlüssel in PART_EXT) oder None (wird nicht geöffnet)."""
    from .extract import sniff
    magic = sniff(data)
    if magic in PART_EXT:
        return magic
    if magic == "zip":
        from .word import word_zip_kind
        return word_zip_kind(data)
    if magic == "ole":
        from .word import is_word_ole
        return "doc" if is_word_ole(data) else None
    if magic is None and part_kind == "text":
        return "txt"                          # auch HTML-Anhänge: als reiner Text, nie als Seite
    return None


def _read_part(db_path: Path, file_id: int, part_no: int) -> tuple[bytes, str, str, list[str]]:
    """Anhang part_no der Mail file_id aus der Mail neu lesen (nur lesend) und gegen parts.sha256 prüfen.
    Rückgabe: (Inhalt, Art nach den ersten Bytes, bereinigter Dateiname, Pfade der eingelesenen Ordner)."""
    from .mail import MAX_MAIL_BYTES, MAX_TEXT_ATTACHMENT_BYTES, find_part
    path, err = _resolve(db_path, file_id)
    if err:
        raise PartError(404, err)
    con = dbm.connect(db_path, readonly=True)
    try:
        kind = con.execute("SELECT kind FROM files WHERE id = ?", (file_id,)).fetchone()
        if kind is None or kind[0] != "mail":
            raise PartError(400, "Kein Anhang: die Datei ist keine E-Mail")
        row = None
        if dbm.has_table(con, "parts"):
            row = con.execute("SELECT kind, name, sha256, mime_path FROM parts WHERE file_id = ? AND part_no = ?",
                              (file_id, part_no)).fetchone()
        roots = [r[0] for r in con.execute("SELECT path FROM roots")]
    finally:
        con.close()
    if row is None:
        raise PartError(404, "Anhang nicht gefunden – neu einlesen")
    if row["kind"] not in OPENABLE_PARTS:
        raise PartError(415, "Anhänge dieser Art werden nicht geöffnet")
    try:
        fd = os.open(path, OPEN_FLAGS)
    except OSError:
        raise PartError(404, "Datei nicht lesbar (verschoben oder ersetzt?) – neu einlesen") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PartError(404, "Keine normale Datei")
        if st.st_size > MAX_MAIL_BYTES:
            raise PartError(413, "Mail zu groß zum Öffnen eines Anhangs – Mail öffnen")
        raw = f.read(MAX_MAIL_BYTES + 1)
    data = find_part(raw, row["mime_path"] or "")
    del raw
    if data is None or not row["sha256"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise PartError(409, MAIL_CHANGED)
    kind = _part_type(data, row["kind"])
    if kind is None and row["kind"] == "word" and len(data) <= MAX_TEXT_ATTACHMENT_BYTES:
        # HTML/WordML/Text unter der Endung .doc: keine aktive Datei an Word oder den
        # Browser übergeben. Erst die Originalbytes prüfen (oben), dann nur den Text liefern.
        from .extract import ExtractError, sniff
        from .word import text_word_kind, word_text
        if sniff(data) is None and text_word_kind(data) is not None:
            try:
                data = "\f".join(word_text(data, [])).encode("utf-8")
            except (ExtractError, UnicodeError) as e:
                raise PartError(415, "Text dieses Word-Anhangs ist nicht lesbar") from e
            kind = "txt"
    if kind is None:
        raise PartError(415, "Anhänge dieser Art werden nicht geöffnet")
    return data, kind, safe_filename(row["name"], PART_EXT[kind]), roots


def _link_target(db_path: Path, file_id: int, part_no: int) -> int | None:
    """DOMUS-Platzhalter part_no der Mail file_id: id der verlinkten Datei im Index. None, wenn der Teil kein
    Platzhalter ist; PartError, wenn die Datei nicht (mehr) im Vorgangsordner bzw. im Index ist."""
    con = dbm.connect(db_path, readonly=True)
    try:
        if not dbm.has_table(con, "parts"):
            return None
        row = con.execute("SELECT t.kind, t.note, f.root_id FROM parts t JOIN files f ON f.id = t.file_id"
                          " WHERE t.file_id = ? AND t.part_no = ? AND f.kind = 'mail'", (file_id, part_no)).fetchone()
        if row is None or row["kind"] != "link":
            return None
        target = dbm.linked_file_id(con, row["root_id"], row["note"]) if row["note"] else None
    finally:
        con.close()
    if target is None:
        raise PartError(404, "Die in DOMUS abgelegte Datei liegt nicht im Vorgangsordner (oder ist nicht eingelesen)")
    return target


def _read_original(path: str, limit: int) -> tuple[bytes, bytes | None]:
    """Inhalt und bestehende Quarantäne desselben geöffneten Originals nur lesend übernehmen."""
    try:
        fd = os.open(path, OPEN_FLAGS)
    except OSError:
        raise PartError(404, "Datei nicht lesbar (verschoben oder ersetzt?) – neu einlesen") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PartError(404, "Keine normale Datei")
        if st.st_size > limit:
            raise PartError(413, "Datei zu groß für eine Kopie – im Finder zeigen und von Hand öffnen")
        try:
            quarantine = _get_quarantine(fd)
        except OSError as e:
            raise PartError(500, "Sicherheitsmarkierung des Originals konnte nicht gelesen werden "
                            f"({e.strerror or e}) – Datei nicht geöffnet") from None
        data = f.read(limit + 1)
        if len(data) > limit:
            raise PartError(413, "Datei zu groß für eine Kopie – im Finder zeigen und von Hand öffnen")
        return data, quarantine


def _roots(db_path: Path) -> list[str]:
    con = dbm.connect(db_path, readonly=True)
    try:
        return [r[0] for r in con.execute("SELECT path FROM roots")]
    finally:
        con.close()


def make_handler(searcher: Searcher, port: int, attachments: AttachmentTemp | None = None):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Belegsuche"
        timeout = 15                   # halbe oder hängende Anfragen belegen nicht ewig einen Thread
        _sent = False                  # Kopfzeilen schon gesendet? (dann keine 500-Antwort mehr möglich)
        _csp = CSP_DEFAULT

        def log_message(self, fmt, *args):  # ruhig bleiben
            pass

        def end_headers(self):
            # gilt für jede Antwort, auch für Fehlerseiten von BaseHTTPRequestHandler
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", self._csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")
            self._sent = True
            super().end_headers()

        def _host_ok(self) -> bool:
            # Schutz gegen DNS-Rebinding: nur Anfragen an die lokale Adresse beantworten
            return self.headers.get("Host", "") in allowed_hosts

        def _origin_ok(self) -> bool:
            # Zusätzlich zu Sec-Fetch-Site (fehlt in älteren Browsern): Browser nennen bei Anfragen aus
            # einer Seite deren Herkunft (Origin, bei POST immer) bzw. die Seite selbst (Referer). Stammt
            # sie nicht von dieser Oberfläche, wird abgelehnt – auch "null" (Sandbox, file://).
            # Fehlen beide (Adresszeile, Lesezeichen, webbrowser.open), bleibt es erlaubt.
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                return False
            referer = self.headers.get("Referer")
            if referer is not None:
                try:
                    u = urlparse(referer)
                except ValueError:
                    return False
                if f"{u.scheme}://{u.netloc}" not in allowed_origins:
                    return False
            return True

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, route):
            self._sent, self._csp = False, CSP_DEFAULT
            try:
                if not self._host_ok() or self.headers.get("Sec-Fetch-Site") not in ALLOWED_FETCH_SITES \
                        or not self._origin_ok():
                    return self._json({"error": "forbidden"}, 403)
                route(urlparse(self.path))
            except (ConnectionError, TimeoutError):
                self.close_connection = True       # Browser hat abgebrochen oder Anfrage hängt
            except Exception as e:
                where = self.path.split("?", 1)[0][:200]   # ohne Suchbegriffe
                print(f"Belegsuche: Fehler bei {self.command} {where}: {type(e).__name__}: {e}", file=sys.stderr)
                self.close_connection = True
                if not self._sent:
                    try:
                        self._json({"error": "interner Fehler"}, 500)
                    except OSError:
                        pass

        def do_GET(self):
            self._handle(self._get)

        def do_POST(self):
            self._handle(self._post)

        def _get(self, url):
            if url.path == "/":
                body = UI_FILE.read_bytes()
                self._csp = CSP_UI
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path in UI_ASSETS:
                name, content_type = UI_ASSETS[url.path]
                body = UI_FILE.with_name(name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/search":
                qs = parse_qs(url.query)
                q = (qs.get("q") or [""])[0][:300]
                try:
                    limit = max(1, min(100, int((qs.get("limit") or ["30"])[0])))
                except ValueError:
                    limit = 30
                resp = searcher.search(q, limit=limit)
                self._json({
                    "query": resp.query, "ms": round(resp.ms), "unknown": resp.unknown,
                    "hits": [{
                        "id": h.file_id, "filename": h.filename, "folder": h.folder, "root": h.root,
                        "page": h.page_no, "pages": h.pages, "score": h.score, "snippet": h.snippet_html,
                        "matched": h.matched, "missing": h.missing, "ocr": h.ocr, "available": h.available,
                        "duplicates": h.duplicates, "pdf": h.filename.lower().endswith(".pdf"),
                        "outdated": h.outdated, "kind": h.kind, "part_no": h.part_no, "part_name": h.part_name,
                        "part_kind": h.part_kind, "part_from": h.part_from, "part_page": h.part_page,
                        "part_pages": h.part_pages,
                        "mail_subject": h.mail_subject, "mail_from": h.mail_from, "mail_date": h.mail_date,
                        "links": h.links,
                    } for h in resp.hits]})
            elif url.path == "/api/status":
                con = dbm.connect(searcher.db_path, readonly=True)
                try:
                    con.execute("BEGIN")
                    roots = [{"path": r["path"], "online": os.path.isdir(r["path"])}
                             for r in con.execute("SELECT path FROM roots ORDER BY id")]
                    # Geänderte Dateien behalten bis zum erneuten Einlesen ihren Suchtext.
                    # Neue Dateien ohne Seiten sind dagegen noch nicht durchsuchbar.
                    done = con.execute(
                        f"SELECT count(*) FROM files f WHERE f.status IN {dbm.SEARCHABLE_SQL}"
                        " AND EXISTS (SELECT 1 FROM pages p WHERE p.file_id = f.id)"
                    ).fetchone()[0]
                finally:
                    con.close()
                self._json({"roots": roots, "files": done})
            elif url.path.startswith("/datei/"):
                m = re.fullmatch(r"([0-9]{1,18})(?:/teil/([0-9]{1,5}))?", url.path[len("/datei/"):])
                if not m or int(m.group(1)) < 1 or (m.group(2) is not None and not _valid_part(int(m.group(2)))):
                    return self._json({"error": "bad id"}, 400)
                if m.group(2) is not None:
                    try:
                        target = _link_target(searcher.db_path, int(m.group(1)), int(m.group(2)))
                    except PartError as e:
                        return self._json({"error": e.msg}, e.status)
                    if target is not None:             # Platzhalter: zur verlinkten Datei
                        self.send_response(303)
                        self.send_header("Location", f"/datei/{target}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    return self._send_part(int(m.group(1)), int(m.group(2)))
                fid = int(m.group(1))
                path, err = _resolve(searcher.db_path, fid)
                if err:
                    return self._json({"error": err}, 404)
                # geprüften Pfad ohne Symlinks öffnen und nur das so geöffnete Objekt ausliefern:
                # ein zwischen Prüfung und Öffnen eingetauschter Symlink wird nicht verfolgt
                try:
                    fd = os.open(path, OPEN_FLAGS)
                except OSError:
                    return self._json({"error": "Datei nicht lesbar (verschoben oder ersetzt?) – neu einlesen"}, 404)
                with os.fdopen(fd, "rb") as f:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        return self._json({"error": "Keine normale Datei"}, 404)
                    ctype = mimetypes.guess_type(path)[0]
                    inline = ctype in INLINE_FILE_TYPES and _file_kind(searcher.db_path, fid) not in DOWNLOAD_KINDS
                    if not inline:                 # Mail, Word, Unbekanntes: nur als Download, nie als Seite
                        self._csp = CSP_ATTACHMENT
                    self.send_response(200)
                    self.send_header("Content-Type", ctype if inline else "application/octet-stream")
                    self.send_header("Content-Length", str(st.st_size))
                    self.send_header("Content-Disposition", "inline" if inline else
                                     _content_disposition("attachment", _clean_name(os.path.basename(path)) or "datei"))
                    self.end_headers()
                    left = st.st_size
                    while left > 0 and (chunk := f.read(min(1 << 16, left))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
            else:
                self._json({"error": "not found"}, 404)

        def _send_part(self, file_id: int, part_no: int):
            """Anhang aus dem Speicher: PDF und gängige Bilder zur Ansicht, alles andere nur als Download."""
            try:
                data, kind, name, _ = _read_part(searcher.db_path, file_id, part_no)
            except PartError as e:
                return self._json({"error": e.msg}, e.status)
            ctype = PART_INLINE.get(kind)
            self._csp = CSP_DEFAULT if kind == "pdf" else CSP_IMAGE if ctype else CSP_ATTACHMENT
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", _content_disposition("inline" if ctype else "attachment", name))
            self.end_headers()
            self.wfile.write(data)

        def _open_part(self, file_id: int, part_no: int):
            try:
                data, _, name, roots = _read_part(searcher.db_path, file_id, part_no)
            except PartError as e:
                return self._json({"error": e.msg}, e.status)
            return self._open_copy(name, data, roots, quarantine=True)

        def _open_copy(self, name: str, data: bytes, roots: list[str], quarantine: bool | bytes):
            """Schreibgeschützte Kopie im System-Temp anlegen und mit dem Standardprogramm öffnen."""
            try:
                path = temp.write(name, data, roots, quarantine=quarantine)
            except TempRefused:
                return self._json({"error": "Temp-Ordner liegt in einem eingelesenen Ordner – Datei nicht "
                                            "geöffnet (TMPDIR prüfen)"}, 500)
            except QuarantineError:
                return self._json({"error": "Erforderliche Sicherheitsmarkierung konnte nicht gesetzt werden – "
                                            "Datei nicht geöffnet"}, 500)
            except OSError as e:
                return self._json({"error": f"Kopie konnte nicht bereitgestellt werden ({e.strerror or e})"}, 500)
            code = _returncode(subprocess.run(["open", path], check=False))
            if code:
                return self._json({"error": f"Öffnen fehlgeschlagen (Programm meldet Fehler {code})"}, 500)
            self._json({"ok": True})

        def _open_word(self, path: str):
            """Word-Original als schreibgeschützte Kopie öffnen (keine Sperrdatei, kein Speichern ins Original)."""
            try:
                data, quarantine = _read_original(path, MAX_COPY_BYTES)
            except PartError as e:
                return self._json({"error": e.msg}, e.status)
            return self._open_copy(_clean_name(os.path.basename(path)) or "Dokument", data,
                                   _roots(searcher.db_path), quarantine=quarantine if quarantine is not None else False)

        def _post(self, url):
            if self.headers.get("X-Belegsuche") != "1":
                return self._json({"error": "forbidden"}, 403)
            if url.path != "/api/open":
                return self._json({"error": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= MAX_BODY:
                    raise ValueError
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, RecursionError):
                return self._json({"error": "bad request"}, 400)
            if not isinstance(data, dict) or not _valid_id(data.get("id")):
                return self._json({"error": "bad request"}, 400)
            part = data.get("part")
            if part is not None and not _valid_part(part):
                return self._json({"error": "bad request"}, 400)
            reveal = data.get("reveal") is True
            fid = data["id"]
            if part is not None:
                try:
                    target = _link_target(searcher.db_path, fid, part)
                except PartError as e:
                    return self._json({"error": e.msg}, e.status)
                if target is not None:                   # Platzhalter: die verlinkte Datei öffnen bzw. zeigen
                    fid, part = target, None
            if part is not None and not reveal:
                return self._open_part(fid, part)
            path, err = _resolve(searcher.db_path, fid)
            if err:
                return self._json({"error": err}, 404)
            kind = _file_kind(searcher.db_path, fid)
            if reveal:                                   # bei Mails und Anhängen: die .eml im Finder
                code = _returncode(subprocess.run(["open", "-R", path], check=False))
            elif kind == "word":
                return self._open_word(path)
            elif kind == "mail":
                # mit Apple Mail; geht das nicht (z. B. Mail fehlt), mit dem Standardprogramm für .eml
                code = _returncode(subprocess.run(["open", "-b", MAIL_APP, path], check=False))
                if code:
                    code = _returncode(subprocess.run(["open", path], check=False))
            else:
                code = _returncode(subprocess.run(["open", path], check=False))
            if code:
                # z. B. kein Programm für diesen Dateityp oder Datei gesperrt
                msg = (f"Im Finder zeigen fehlgeschlagen (Programm meldet Fehler {code})" if reveal else
                       f"Öffnen fehlgeschlagen (Programm meldet Fehler {code}) – "
                       "Datei im Finder zeigen und von Hand öffnen")
                return self._json({"error": msg}, 500)
            self._json({"ok": True})

    temp = attachments if attachments is not None else AttachmentTemp()
    Handler.temp = temp
    return Handler


def _returncode(result) -> int:
    """Rückgabewert eines Programmstarts; None/ohne returncode gilt als Erfolg (0)."""
    code = getattr(result, "returncode", 0)
    return code if isinstance(code, int) else 0


def serve(db_path: Path, port: int = 8765, open_browser: bool = True) -> int:
    """Startet die Oberfläche; 0 nach Ctrl+C, 1 wenn der Port nicht zu öffnen ist."""
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), None)
    except (OSError, OverflowError) as e:
        if getattr(e, "errno", None) == errno.EADDRINUSE:
            print(f"Port {port} ist belegt. Eine laufende Suche im vorhandenen Fenster nutzen "
                  "oder einen anderen Port mit --port wählen.")
        else:
            print(f"Port {port} lässt sich nicht öffnen: {getattr(e, 'strerror', None) or e} "
                  f"– anderen Port mit --port wählen")
        return 1
    browser_timer = None
    attachments = AttachmentTemp()
    old_handlers = _stop_on_hangup()
    try:
        try:
            AttachmentTemp.cleanup_stale(roots=_roots(db_path))
        except (OSError, sqlite3.Error):
            pass
        port = httpd.server_address[1]          # bei --port 0 vergibt das System den Port
        httpd.RequestHandlerClass = make_handler(Searcher(db_path), port, attachments)
        url = f"http://127.0.0.1:{port}/"
        print(f"Belegsuche läuft: {url}  (beenden mit Ctrl+C)")
        if open_browser:
            browser_timer = threading.Timer(0.5, lambda: webbrowser.open(url))
            browser_timer.daemon = True
            browser_timer.start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        if browser_timer is not None:
            browser_timer.cancel()
        httpd.server_close()
        try:
            roots = _roots(db_path)
        except (OSError, sqlite3.Error):
            attachments.cleanup(remove=False)      # Ohne aktuelle Archivgrenzen nur die Sitzung freigeben.
        else:
            attachments.cleanup(roots=roots)
    return 0


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def _stop_on_hangup() -> dict:
    """Terminal-Fenster geschlossen (SIGHUP) oder Abmelden (SIGTERM): wie Ctrl+C beenden, damit die Kopien
    geöffneter Anhänge (persönliche Daten) weggeräumt werden. Nur im Hauptthread möglich."""
    old = {}
    for sig in (signal.SIGHUP, signal.SIGTERM):
        try:
            old[sig] = signal.signal(sig, _raise_interrupt)
        except ValueError:                      # nicht im Hauptthread (Tests): nichts ändern
            break
    return old
