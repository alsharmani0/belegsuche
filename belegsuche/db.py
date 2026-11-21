"""SQLite-Datenbank: Dateiliste, Seitentexte und Volltextindex (FTS5).

Die Datenbank liegt standardmäßig auf dem Mac, nicht auf dem durchsuchten Laufwerk.
So bleibt der Ordner mit den Originalen unverändert, und die Suche funktioniert
auch, wenn die SSD gerade nicht angeschlossen ist.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = 1
# Format der Suchtexte (Zusatz-Tokens, Dokument-Index). Ändert es sich, wird der Suchindex aus den
# gespeicherten Seitentexten neu aufgebaut – ohne neue Texterkennung.
INDEX_FORMAT = "13"          # TNEF-HTML/RTF und tatsächliche Dateinamen gezielt erneut lesen
MAIL_CLASSIFICATION_REVISION = 12
MAIL_CONTENT_REVISION = 13
# Von diesen Formaten aus ändern sich die vorhandenen Suchzeilen nicht: nur das Format vermerken, kein Neuaufbau
# (der Index kann über 100.000 Seiten haben; die neuen Tabellen legt SCHEMA an)
NO_REBUILD_FROM = {"9", "10", "11", "12"}
# Teile, die eine frühere Version nur vermerkt hat und die diese Version liest (Format 10 → 11: winmail.dat). Mails
# mit so einem Teil liest der nächste Lauf von „belegsuche index“ neu (mark_outdated_mails); ihre übrigen Seiten
# übernimmt das Neulesen über die sha256 der Anhänge, ohne neue Texterkennung. Bis dahin gilt ihr bisheriger Stand.
LEGACY_CLASSIFICATION_NOTE = "nicht ausgewertet (ältere Anhang-Erkennung)"
TRUNCATED_REUSE_NOTE = "unvollständig übernommen (älterer gekürzter Seitenbestand)"
# Mailkopf mit einem Vermerk einer früheren Version („Entwurf“ für gesendete CRM-Mails, alter Datumszusatz). Nur
# „belegsuche index --mailkopf-erneuern“ setzt diesen Hinweis (indexer.flag_stale_mail_heads), nie ein Lauf von selbst.
STALE_MAIL_HEAD_NOTE = "Mailkopf mit Vermerk einer früheren Version"
OUTDATED_PART_NOTES = ("nicht ausgewertet (winmail.dat aus Outlook)", LEGACY_CLASSIFICATION_NOTE,
                       TRUNCATED_REUSE_NOTE, STALE_MAIL_HEAD_NOTE)
TNEF_TYPES = ("application/ms-tnef", "application/vnd.ms-tnef")
OUTDATED_IN_SQL = "(" + ", ".join("?" * len(OUTDATED_PART_NOTES)) + ")"
FILE_TOKEN = "0f"            # + files.id, steht in der path-Spalte jeder Seite
# Dateien, die Suche und Oberfläche anzeigen und öffnen: fertig eingelesen – oder seitdem geändert und noch
# nicht neu eingelesen (bis dahin gilt der bisherige Text; er wird erst beim Neu-Einlesen ersetzt)
SEARCHABLE_STATUS = ("done", "pending", "processing")
SEARCHABLE_SQL = "(" + ", ".join(f"'{s}'" for s in SEARCHABLE_STATUS) + ")"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS roots (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    added_at REAL
);

CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    root_id    INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    relpath    TEXT NOT NULL,
    ext        TEXT,
    kind       TEXT,              -- pdf | image | mail | word | unsupported
    size       INTEGER,
    mtime      REAL,
    sha256     TEXT,
    status     TEXT NOT NULL,     -- pending | processing | done | error | skipped | missing
    error      TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    pages      INTEGER,
    ocr_pages  INTEGER NOT NULL DEFAULT 0,
    text_chars INTEGER NOT NULL DEFAULT 0,
    min_conf   REAL,
    indexed_at REAL,
    seen_at    REAL,
    UNIQUE (root_id, relpath)
);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS files_status ON files(status);

CREATE TABLE IF NOT EXISTS pages (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    page_no  INTEGER NOT NULL,         -- ab 1
    text     TEXT NOT NULL,
    source   TEXT,                     -- text | ocr | text+ocr
    ocr_conf REAL,
    UNIQUE (file_id, page_no)
);

-- rowid = pages.id
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    path, body, tokenize = "unicode61 remove_diacritics 0"
);
CREATE VIRTUAL TABLE IF NOT EXISTS vocab USING fts5vocab(fts, 'row');

-- rowid = files.id, nur mehrseitige Dokumente: findet Belege, deren Suchbegriffe auf verschiedenen Seiten stehen
CREATE VIRTUAL TABLE IF NOT EXISTS fts_doc USING fts5(body, tokenize = "unicode61 remove_diacritics 0");

-- Teile einer E-Mail: 0 = Mailkopf + Mailtext (Seite 1), danach die Anhänge. Die Seiten in `pages` sind
-- fortlaufend nummeriert; ein Teil belegt die Seiten first_page .. first_page + n_pages - 1.
CREATE TABLE IF NOT EXISTS parts (
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    part_no    INTEGER NOT NULL,
    first_page INTEGER,                 -- NULL, wenn der Teil keine eigenen Seiten hat
    n_pages    INTEGER NOT NULL DEFAULT 0,
    kind       TEXT NOT NULL,           -- body | pdf | image | word | text | mail | link | tnef | skipped
    name       TEXT,
    mime       TEXT,
    size       INTEGER,
    sha256     TEXT,
    mime_path  TEXT,                    -- Lage im MIME-Baum, „!k“ = Anhang k einer winmail.dat (mail.find_part)
    note       TEXT,                    -- Hinweis (zu groß, nicht ausgewertet …); bei 'link': Pfad der Datei
    retryable  INTEGER NOT NULL DEFAULT 0, -- technischer Teilfehler, der explizit wiederholt werden kann
    content_revision INTEGER NOT NULL DEFAULT 0, -- gezielte Alt-Anhangprüfung schon erfolgt
    PRIMARY KEY (file_id, part_no)
);
CREATE INDEX IF NOT EXISTS parts_sha ON parts(sha256);

CREATE TABLE IF NOT EXISTS mails (
    file_id    INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    subject    TEXT,
    sender     TEXT,
    recipients TEXT,
    sent_at    TEXT,                    -- ISO 8601 aus dem Date-Kopf; NULL, wenn er fehlt
    message_id TEXT
);
"""


def default_db_path() -> Path:
    env = os.environ.get("BELEGSUCHE_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "Belegsuche" / "index.db"


class IncompatibleIndexError(sqlite3.DatabaseError):
    """Fremde oder von dieser Programmversion nicht lesbare Datenbank; niemals automatisch ändern."""


def _check_compatible(con: sqlite3.Connection, *, allow_empty: bool) -> None:
    objects = con.execute("SELECT name, type FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
    tables = {r[0] for r in objects if r[1] == "table"}
    if not objects and allow_empty:
        # acquire_lock legt eine leere Datei an; eine neue SQLite-Datenbank hat noch keine Tabellen.
        return
    if not {"meta", "roots", "files", "pages", "fts", "vocab"}.issubset(tables):
        raise IncompatibleIndexError("Diese Datenbank ist kein unterstützter Belegsuche-Index.")
    try:
        schema = get_meta(con, "schema")
        fmt = get_meta(con, "index_format")
    except sqlite3.DatabaseError as e:
        raise IncompatibleIndexError("Diese Datenbank ist kein unterstützter Belegsuche-Index.") from e
    if schema is not None and schema != str(SCHEMA_VERSION):
        raise IncompatibleIndexError(f"Datenbankschema {schema!r} wird nicht unterstützt "
                                     f"(diese Version: {SCHEMA_VERSION}).")
    if fmt is not None:
        if not isinstance(fmt, str) or not fmt.isascii() or not fmt.isdecimal():
            raise IncompatibleIndexError(f"Indexformat {fmt!r} wird nicht unterstützt.")
        version = fmt.lstrip("0") or "0"
        if (len(version), version) > (len(INDEX_FORMAT), INDEX_FORMAT):
            raise IncompatibleIndexError(f"Dieser Index ist neuer (Format {fmt}) als diese Belegsuche "
                                         f"(Format {INDEX_FORMAT}). Bitte die Belegsuche aktualisieren.")


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        # als URI maskiert: "#", "?", "%" oder Leerzeichen im Pfad dürfen die Nur-Lese-Option nicht abschneiden
        uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        _check_compatible(con, allow_empty=not readonly)
        if not readonly:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            _upgrade_part_columns(con)
            con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', '0')")
            _init_persistent_ids(con)
            con.commit()
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=NORMAL")
        if not readonly and get_meta(con, "index_format") != INDEX_FORMAT:
            # Ein zweiter Start kann dasselbe alte Format gesehen haben. Nach dem Warten
            # auf den Schreibzugriff erneut prüfen, bevor wir den gesamten Index aufbauen.
            con.execute("BEGIN IMMEDIATE")
            _check_compatible(con, allow_empty=False)
            old = get_meta(con, "index_format")
            if old != INDEX_FORMAT:
                set_meta(con, "index_format", INDEX_FORMAT)
                if old not in NO_REBUILD_FROM and con.execute("SELECT 1 FROM pages LIMIT 1").fetchone():
                    print("Suchindex wird auf das neue Format umgestellt (einmalig, ohne neue Texterkennung) …",
                          file=sys.stderr)
                    rebuild_search_index(con)
                if old == "10":
                    _flag_tnef_read_as_pdf(con)
                _flag_legacy_retryable_parts(con)
            con.commit()
        return con
    except BaseException:
        con.close()
        raise


def _init_persistent_ids(con: sqlite3.Connection) -> None:
    """Ordnerbefehle und Browserlinks behalten ihre IDs auch nach Löschung und Wiederanlauf."""
    for table, key in (("roots", "root_id_highwater"), ("files", "file_id_highwater")):
        con.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_id_highwater AFTER INSERT ON {table} BEGIN
            INSERT INTO meta(key,value) VALUES ('{key}', CAST(NEW.id AS TEXT))
            ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER), NEW.id) AS TEXT);
        END""")
        con.execute(f"""INSERT INTO meta(key,value) VALUES (?, (SELECT CAST(coalesce(MAX(id),0) AS TEXT) FROM {table}))
            ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER),
            CAST(excluded.value AS INTEGER)) AS TEXT)""", (key,))


def _upgrade_part_columns(con: sqlite3.Connection) -> None:
    con.execute("BEGIN IMMEDIATE")
    columns = {r[1] for r in con.execute("PRAGMA table_info(parts)")}
    for name in ("retryable", "content_revision"):
        if name not in columns:
            con.execute(f"ALTER TABLE parts ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")


def _flag_legacy_retryable_parts(con: sqlite3.Connection) -> None:
    from .mail import legacy_retryable_part_note
    for file_id, part_no, note in con.execute(
            "SELECT file_id,part_no,note FROM parts WHERE retryable=0 AND n_pages=0"
            " AND kind IN ('pdf','image','word') AND note IS NOT NULL").fetchall():
        if legacy_retryable_part_note(note):
            con.execute("UPDATE parts SET retryable=1 WHERE file_id=? AND part_no=?", (file_id, part_no))


def _flag_tnef_read_as_pdf(con: sqlite3.Connection) -> int:
    """Einmalig bei der Umstellung 10 → 11: Format 10 hielt eine kleine winmail.dat, deren erster Anhang ein PDF
    ist, für ein PDF („%PDF-“ in den ersten 1024 Bytes, vor der TNEF-Kennung geprüft) und las nur dieses PDF. Solche
    Teile bekommen den Hinweis einer nur vermerkten winmail.dat – dann liest der nächste Lauf die Mail neu, mit allen
    Anhängen darin. Nur hier, nicht bei jedem Lauf: ein echtes PDF namens winmail.dat würde sonst immer wieder neu
    gelesen. Rückgabe: Anzahl."""
    if not has_table(con, "parts"):
        return 0
    return con.execute(
        "UPDATE parts SET note = ? WHERE kind = 'pdf' AND (lower(coalesce(mime, '')) IN (?, ?)"
        " OR lower(coalesce(name, '')) = 'winmail.dat')", (OUTDATED_PART_NOTES[0], *TNEF_TYPES)).rowcount


def mark_outdated_mails(con: sqlite3.Connection, root_ids: list[int] | None = None) -> int:
    """Fertige Mails, die ein Teil aus einer früheren Version enthalten (OUTDATED_PART_NOTES, z. B. eine nur
    vermerkte winmail.dat), zum Neueinlesen vormerken – nur in den Ordnern root_ids (None: alle). Aufruf zu Beginn
    von „belegsuche index“, nicht schon bei der Umstellung: bis dahin gelten sie als fertig, nicht als geändert.
    Wiederholbar: neu eingelesene Mails haben solche Teile nicht mehr. Rückgabe: Anzahl."""
    if not has_table(con, "parts"):
        return 0
    scope, args = "", ()
    if root_ids is not None:
        scope, args = f" AND root_id IN ({','.join('?' * len(root_ids))})", tuple(root_ids)
    return con.execute(f"UPDATE files SET status = 'pending', attempts = 0 WHERE status = 'done'{scope} AND id IN"
                       f" (SELECT file_id FROM parts WHERE note IN {OUTDATED_IN_SQL})",
                       (*args, *OUTDATED_PART_NOTES)).rowcount


def has_outdated_parts(con: sqlite3.Connection, file_id: int) -> bool:
    """Hat die Datei einen Teil, den eine frühere Version nur vermerkt hat (dann gilt ihr Stand nicht weiter)?"""
    return con.execute(f"SELECT 1 FROM parts WHERE file_id = ? AND note IN {OUTDATED_IN_SQL} LIMIT 1",
                       (file_id, *OUTDATED_PART_NOTES)).fetchone() is not None


def delete_search_rows(con: sqlite3.Connection, file_id: int) -> None:
    con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM pages WHERE file_id = ?)", (file_id,))
    con.execute("DELETE FROM fts_doc WHERE rowid = ?", (file_id,))


def german_date(iso: str | None, with_time: bool = True) -> str:
    """ISO-Zeitpunkt (mails.sent_at) als „12.03.2021 14:05“; leer, wenn nicht lesbar."""
    from datetime import datetime
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    return dt.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def _mail_path_texts(con: sqlite3.Connection, file_id: int):
    """Für E-Mails: Mailkopf als Suchtext, die Seitenbereiche der Anhänge mit ihrem Namen, die Namen der Anhänge
    ohne eigene Seiten (Platzhalter, nicht ausgewertet) und die Seiten mit Mailkopf (Mailtext, angehängte Mails)."""
    try:
        row = con.execute("SELECT subject, sender, recipients, sent_at FROM mails WHERE file_id = ?",
                          (file_id,)).fetchone()
    except sqlite3.OperationalError:     # Index ohne Tabelle mails (nur lesend geöffnet, vor Format 10)
        return None
    if row is None:
        return None
    from .normalize import index_text
    # sent_at fehlt bei Mails ohne Date-Kopf: dann steht kein (Ersatz-)Datum im Suchtext
    head = index_text(" ".join(x for x in (row[0], row[1], row[2], german_date(row[3], False)) if x))
    ranges, unpaged, head_pages = [], [], set()
    for first, n, name, kind, part_no in con.execute(
            "SELECT first_page, n_pages, name, kind, part_no FROM parts WHERE file_id = ? ORDER BY part_no",
            (file_id,)):
        if n > 0 and first is not None:
            if part_no == 0 or kind == "mail":
                head_pages.update(range(first, first + n))
            if part_no > 0 and name:
                ranges.append((first, first + n - 1, name))
        elif part_no > 0 and name:
            unpaged.append(name)
    return head, ranges, unpaged, head_pages


def index_file_text(con: sqlite3.Connection, file_id: int, relpath: str) -> None:
    """Suchindex einer Datei aus ihren gespeicherten Seitentexten (neu) schreiben.

    Bei E-Mails stehen Betreff, Absender, Empfänger und Datum in der path-Spalte aller Seiten (auch der
    Anhangseiten) und dazu der Name des Anhangs, zu dem die Seite gehört: „Absender + Betrag im Anhang“ passt so
    auch auf Seite 3 eines Anhangs (Treffer in der path-Spalte zählen abgewertet, siehe search.PATH_FACTOR).
    Die Zeilen „Anhänge: …“ und „Anhang in DOMUS abgelegt: …“ im Kopf von Seite 1 zählen nicht als Mailtext: die
    Namen stehen in der path-Spalte der Anhangseiten bzw. (Anhänge ohne eigene Seiten) von Seite 1. Sonst stünde
    die Mail bei der Suche nach einem Dateinamen vor der Datei selbst."""
    from .normalize import index_text, path_index_text
    delete_search_rows(con, file_id)
    ptext = f"{path_index_text(relpath)} {FILE_TOKEN}{file_id}"   # Dateikennung: Seiten einer Datei gezielt abfragen
    mail = _mail_path_texts(con, file_id)
    if mail is not None:
        from .mail import index_body_text
    bodies = []
    for pid, page_no, text in con.execute("SELECT id, page_no, text FROM pages WHERE file_id = ? ORDER BY page_no",
                                          (file_id,)).fetchall():
        path = ptext
        if mail is not None:
            head, ranges, unpaged, head_pages = mail
            if page_no in head_pages:
                text = index_body_text(text)
            name = next((n for a, b, n in ranges if a <= page_no <= b), "")
            names = " ".join(unpaged) if page_no == 1 else name
            path = " ".join(x for x in (ptext, head, path_index_text(names) if names else "") if x)
        body = index_text(text)
        bodies.append(body)
        con.execute("INSERT INTO fts(rowid, path, body) VALUES (?, ?, ?)", (pid, path, body))
    if len(bodies) > 1:
        con.execute("INSERT INTO fts_doc(rowid, body) VALUES (?, ?)", (file_id, " ".join(bodies)))


def linked_file_id(con: sqlite3.Connection, root_id: int, relpath: str) -> int | None:
    """Datei im Index, auf die ein DOMUS-Platzhalter verweist (parts.note): gleicher Ordner, gleicher Pfad – auch
    bei anderer Unicode-Schreibweise der Umlaute (NFC/NFD) und anderer Groß-/Kleinschreibung."""
    import unicodedata
    row = con.execute(f"SELECT id FROM files WHERE root_id = ? AND relpath = ? AND status IN {SEARCHABLE_SQL}",
                      (root_id, relpath)).fetchone()
    if row:
        return row[0]
    want = unicodedata.normalize("NFC", relpath).casefold()
    # Kandidaten im selben Ordner; Umlaute im Pfad können anders geschrieben sein: nur der ASCII-Anfang zählt
    head = os.path.dirname(relpath)
    head = head[:next((i for i, c in enumerate(head) if not c.isascii()), len(head))]
    for fid, rel in con.execute(f"SELECT id, relpath FROM files WHERE root_id = ? AND relpath >= ? AND relpath < ?"
                                f" AND status IN {SEARCHABLE_SQL}", (root_id, head, head + "\U0010ffff")):
        if unicodedata.normalize("NFC", rel).casefold() == want:
            return fid
    return None


def path_inside(path: str, root: str) -> bool:
    """Liegt path in root (oder ist root)? Ohne Symlinks, auch bei anderer Groß-/Kleinschreibung."""
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    for a, b in ((rp, rr), (rp.casefold(), rr.casefold())):
        if a == b or a.startswith(b.rstrip("/") + "/"):
            return True
    if os.path.isdir(rr):
        p = rp
        while True:
            try:
                if os.path.exists(p) and os.path.samefile(p, rr):
                    return True
            except OSError:
                pass
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
    return False


def has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def rebuild_search_index(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM fts")
    con.execute("DELETE FROM fts_doc")
    for f in con.execute(f"SELECT id, relpath FROM files WHERE status IN {SEARCHABLE_SQL}").fetchall():
        index_file_text(con, f["id"], f["relpath"])
    bump_generation(con)
    con.commit()


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value))


def bump_generation(con: sqlite3.Connection) -> None:
    """Signalisiert laufenden Suchservern, dass sich der Index geändert hat."""
    con.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'")
