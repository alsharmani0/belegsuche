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
INDEX_FORMAT = "7"
FILE_TOKEN = "0f"            # + files.id, steht in der path-Spalte jeder Seite

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
    kind       TEXT,              -- pdf | image | unsupported
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
"""


def default_db_path() -> Path:
    env = os.environ.get("BELEGSUCHE_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "Belegsuche" / "index.db"


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', '0')")
        con.commit()
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")
    if not readonly and get_meta(con, "index_format") != INDEX_FORMAT:
        if con.execute("SELECT 1 FROM pages LIMIT 1").fetchone():
            print("Suchindex wird auf das neue Format umgestellt (einmalig, ohne neue Texterkennung) …",
                  file=sys.stderr)
            rebuild_search_index(con)
        set_meta(con, "index_format", INDEX_FORMAT)
        con.commit()
    return con


def delete_search_rows(con: sqlite3.Connection, file_id: int) -> None:
    con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM pages WHERE file_id = ?)", (file_id,))
    con.execute("DELETE FROM fts_doc WHERE rowid = ?", (file_id,))


def index_file_text(con: sqlite3.Connection, file_id: int, relpath: str) -> None:
    """Suchindex einer Datei aus ihren gespeicherten Seitentexten (neu) schreiben."""
    from .normalize import index_text, path_index_text
    delete_search_rows(con, file_id)
    ptext = f"{path_index_text(relpath)} {FILE_TOKEN}{file_id}"   # Dateikennung: Seiten einer Datei gezielt abfragen
    bodies = []
    for pid, text in con.execute("SELECT id, text FROM pages WHERE file_id = ? ORDER BY page_no", (file_id,)).fetchall():
        body = index_text(text)
        bodies.append(body)
        con.execute("INSERT INTO fts(rowid, path, body) VALUES (?, ?, ?)", (pid, ptext, body))
    if len(bodies) > 1:
        con.execute("INSERT INTO fts_doc(rowid, body) VALUES (?, ?)", (file_id, " ".join(bodies)))


def rebuild_search_index(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM fts")
    con.execute("DELETE FROM fts_doc")
    for f in con.execute("SELECT id, relpath FROM files WHERE status = 'done'").fetchall():
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
