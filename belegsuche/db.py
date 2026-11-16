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
INDEX_FORMAT = "10"          # E-Mails: Mailkopf und Anhangname in der path-Spalte; Tabellen parts/mails
# Von diesen Formaten aus ändern sich die vorhandenen Suchzeilen nicht: nur das Format vermerken, kein Neuaufbau
# (der Index kann über 100.000 Seiten haben; die neuen Tabellen legt SCHEMA an)
NO_REBUILD_FROM = {"9"}
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
    kind       TEXT NOT NULL,           -- body | pdf | image | word | text | mail | link | skipped
    name       TEXT,
    mime       TEXT,
    size       INTEGER,
    sha256     TEXT,
    mime_path  TEXT,                    -- Lage im MIME-Baum (siehe mail.find_part)
    note       TEXT,                    -- Hinweis (zu groß, nicht ausgewertet …); bei 'link': Pfad der Datei
    PRIMARY KEY (file_id, part_no)
);
CREATE INDEX IF NOT EXISTS parts_sha ON parts(sha256);

CREATE TABLE IF NOT EXISTS mails (
    file_id    INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    subject    TEXT,
    sender     TEXT,
    recipients TEXT,
    sent_at    TEXT,                    -- ISO 8601; fehlt Date:, die Änderungszeit der Datei
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
            con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', '0')")
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
            con.commit()
        return con
    except BaseException:
        con.close()
        raise


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


def _mail_path_texts(con: sqlite3.Connection, file_id: int) -> tuple[str, list[tuple[int, int, str]]] | None:
    """Für E-Mails: Mailkopf als Suchtext und die Seitenbereiche der Anhänge mit ihrem Namen."""
    try:
        row = con.execute("SELECT subject, sender, recipients, sent_at FROM mails WHERE file_id = ?",
                          (file_id,)).fetchone()
    except sqlite3.OperationalError:     # Index ohne Tabelle mails (nur lesend geöffnet, vor Format 10)
        return None
    if row is None:
        return None
    from .normalize import index_text
    head = index_text(" ".join(x for x in (row[0], row[1], row[2], german_date(row[3], False)) if x))
    ranges = [(r[0], r[0] + r[1] - 1, r[2]) for r in con.execute(
        "SELECT first_page, n_pages, name FROM parts WHERE file_id = ? AND n_pages > 0 AND first_page IS NOT NULL"
        " AND part_no > 0 AND name IS NOT NULL", (file_id,))]
    return head, ranges


def index_file_text(con: sqlite3.Connection, file_id: int, relpath: str) -> None:
    """Suchindex einer Datei aus ihren gespeicherten Seitentexten (neu) schreiben.

    Bei E-Mails stehen Betreff, Absender, Empfänger und Datum in der path-Spalte aller Seiten (auch der
    Anhangseiten) und dazu der Name des Anhangs, zu dem die Seite gehört: „Absender + Betrag im Anhang“ passt so
    auch auf Seite 3 eines Anhangs (Treffer in der path-Spalte zählen abgewertet, siehe search.PATH_FACTOR)."""
    from .normalize import index_text, path_index_text
    delete_search_rows(con, file_id)
    ptext = f"{path_index_text(relpath)} {FILE_TOKEN}{file_id}"   # Dateikennung: Seiten einer Datei gezielt abfragen
    mail = _mail_path_texts(con, file_id)
    bodies = []
    for pid, page_no, text in con.execute("SELECT id, page_no, text FROM pages WHERE file_id = ? ORDER BY page_no",
                                          (file_id,)).fetchall():
        body = index_text(text)
        bodies.append(body)
        path = ptext
        if mail is not None:
            head, ranges = mail
            name = next((n for a, b, n in ranges if a <= page_no <= b), "")
            path = " ".join(x for x in (ptext, head, path_index_text(name) if name else "") if x)
        con.execute("INSERT INTO fts(rowid, path, body) VALUES (?, ?, ?)", (pid, path, body))
    if len(bodies) > 1:
        con.execute("INSERT INTO fts_doc(rowid, body) VALUES (?, ?)", (file_id, " ".join(bodies)))


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
