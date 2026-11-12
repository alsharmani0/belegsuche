"""Isolated regression cases from the November index/database review."""
from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from belegsuche import db as dbm
from belegsuche import indexer
from belegsuche.extract import PageText
from belegsuche.search import Searcher


def quiet(_):
    pass


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    """No user archives or actual OCR: each synthetic PDF contains its own text."""
    root = tmp_path / "belege"
    root.mkdir()
    calls = []

    def extract(path, engine):
        calls.append(path)
        return [PageText(1, path.read_text(), "text", None)], []

    monkeypatch.setattr(indexer, "extract_with_notes", extract)
    db = tmp_path / "index.db"
    con = dbm.connect(db)
    yield root, db, con, calls
    con.close()


@pytest.mark.parametrize("status", ["pending", "processing"])
@pytest.mark.parametrize("old_format", ["1", "8"])
def test_format_rebuild_preserves_searchable_previous_text(synthetic, status, old_format):
    root, db, con, _ = synthetic
    (root / "rechnung.pdf").write_text("Dachdecker Kowalski Rechnung")
    indexer.run_index(con, [str(root)], engine=object(), log=quiet)
    con.execute("UPDATE files SET status=?", (status,))
    dbm.set_meta(con, "index_format", old_format)
    con.commit()
    reopened = dbm.connect(db)
    reopened.close()
    hits = Searcher(db).search("Kowalski").hits
    assert [h.filename for h in hits] == ["rechnung.pdf"]
    assert hits[0].outdated


def test_transient_file_stat_error_preserves_previous_search(synthetic, monkeypatch):
    root, db, con, _ = synthetic
    unreadable = root / "rechnung.pdf"
    unreadable.write_text("Dachdecker Kowalski Rechnung")
    (root / "andere.pdf").write_text("Gartenpflege Grünwerk Rechnung")
    indexer.run_index(con, [str(root)], engine=object(), log=quiet)
    real_stat = os.stat

    def stat_with_transient_error(path, *args, **kwargs):
        if Path(path) == unreadable and kwargs.get("follow_symlinks") is False:
            raise PermissionError(13, "Nicht lesbar", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(indexer.os, "stat", stat_with_transient_error)
    scans, _ = indexer.run_index(con, [], engine=object(), log=quiet)
    assert scans[0].dir_errors
    assert scans[0].missing == 0
    assert con.execute("SELECT status FROM files WHERE relpath='rechnung.pdf'").fetchone()[0] == "done"
    assert [h.filename for h in Searcher(db).search("Kowalski").hits] == ["rechnung.pdf"]


def test_mass_directory_move_prunes_proven_renames_despite_missing_safeguard(synthetic):
    root, db, con, calls = synthetic
    old = root / "alt"
    old.mkdir()
    for i in range(24):
        (old / f"rechnung{i}.pdf").write_text(f"Dachdecker Kowalski Rechnung Nummer {i}")
    indexer.run_index(con, [str(root)], engine=object(), log=quiet)
    old.rename(root / "neu")
    calls.clear()
    scans, ps = indexer.run_index(con, [], engine=object(), log=quiet)
    assert calls == [] and ps.reused == 24
    assert scans[0].renamed == 24
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 24
    assert all(r[0].startswith("neu/") for r in con.execute("SELECT relpath FROM files"))
    assert len(Searcher(db).search("Kowalski", limit=100).hits) == 24


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("key,value", [("schema", str(dbm.SCHEMA_VERSION + 1)),
                                      ("index_format", str(int(dbm.INDEX_FORMAT) + 1)),
                                      ("index_format", "unknown")])
def test_incompatible_index_is_rejected_without_changes(tmp_path, readonly, key, value):
    db = tmp_path / "future.db"
    con = dbm.connect(db)
    dbm.set_meta(con, key, value)
    con.commit()
    con.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with pytest.raises(dbm.IncompatibleIndexError, match="unterstützt|neuer"):
        dbm.connect(db, readonly=readonly)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0] == value
    con.close()


@pytest.mark.parametrize("readonly", [False, True])
def test_unrelated_sqlite_database_is_not_adopted(tmp_path, readonly):
    db = tmp_path / "andere.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE rechnungen (id INTEGER PRIMARY KEY, betrag INTEGER)")
    con.execute("INSERT INTO rechnungen VALUES (1, 42)")
    con.commit()
    con.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with pytest.raises(dbm.IncompatibleIndexError, match="Belegsuche"):
        dbm.connect(db, readonly=readonly)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    con = sqlite3.connect(db)
    assert con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("rechnungen",)]
    assert con.execute("SELECT * FROM rechnungen").fetchall() == [(1, 42)]
    con.close()


def test_lock_does_not_touch_existing_database_mtime(tmp_path):
    db = tmp_path / "existing.db"
    db.write_bytes(b"existing content")
    os.utime(db, ns=(1_000_000_000, 2_000_000_000))
    before = db.stat().st_mtime_ns
    lock = indexer.acquire_lock(db)
    lock.close()
    assert db.read_bytes() == b"existing content"
    assert db.stat().st_mtime_ns == before


def test_unavailable_lock_does_not_allow_unprotected_indexing(tmp_path, monkeypatch):
    import fcntl

    def unavailable(*_):
        raise OSError(errno.ENOSYS, "Keine Dateisperren")

    monkeypatch.setattr(fcntl, "flock", unavailable)
    with pytest.raises(SystemExit, match="Sperre|sperren"):
        indexer.acquire_lock(tmp_path / "i.db")


def test_explicit_folder_does_not_process_other_roots_pending_files(synthetic, tmp_path):
    root, _, con, calls = synthetic
    other = tmp_path / "anderer_ordner"
    other.mkdir()
    (root / "a.pdf").write_text("Dachdecker Kowalski Rechnung")
    (other / "b.pdf").write_text("Gartenpflege Grünwerk Rechnung")
    indexer.run_index(con, [str(root), str(other)], engine=object(), log=quiet, limit=0)
    _, ps = indexer.run_index(con, [str(root)], engine=object(), log=quiet)
    assert ps.done == 1
    assert [p.name for p in calls] == ["a.pdf"]
    assert dict(con.execute("SELECT relpath,status FROM files")) == {"a.pdf": "done", "b.pdf": "pending"}


def test_explicit_folder_does_not_retry_other_roots_errors(synthetic, tmp_path):
    root, _, con, _ = synthetic
    other = tmp_path / "anderer_ordner"
    other.mkdir()
    (root / "a.pdf").write_text("Dachdecker Kowalski Rechnung")
    (other / "b.pdf").write_text("Gartenpflege Grünwerk Rechnung")
    indexer.run_index(con, [str(root), str(other)], engine=object(), log=quiet, limit=0)
    con.execute("UPDATE files SET status='error', error='Vorübergehender Lesefehler', attempts=3")
    con.commit()
    _, ps = indexer.run_index(con, [str(root)], engine=object(), log=quiet, retry=True)
    assert ps.retried == 1 and ps.done == 1
    assert dict(con.execute("SELECT relpath,status FROM files")) == {"a.pdf": "done", "b.pdf": "error"}


def test_same_size_edit_with_sub_millisecond_mtime_is_reindexed(synthetic):
    root, db, con, _ = synthetic
    path = root / "rechnung.pdf"
    path.write_text("Dachdecker Kowalski Rechnung")
    indexer.run_index(con, [str(root)], engine=object(), log=quiet)
    before = path.stat()
    path.write_text("Dachdecker Hochdach Rechnung")
    assert path.stat().st_size == before.st_size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 500_000))
    scans, ps = indexer.run_index(con, [], engine=object(), log=quiet)
    assert scans[0].changed == 1 and ps.done == 1
    assert Searcher(db).search("Kowalski").hits == []
    assert [h.filename for h in Searcher(db).search("Hochdach").hits] == ["rechnung.pdf"]


def test_file_edited_during_extraction_remains_pending_without_mislabelled_text(synthetic, monkeypatch):
    root, db, con, _ = synthetic
    path = root / "a.pdf"
    original = "Dachdecker Kowalski Rechnung"
    path.write_text(original)
    real_extract = indexer.extract_with_notes

    def edit_while_extracting(path, engine):
        path.write_text("Gartenpflege Grünwerk Rechnung geändert")
        return real_extract(path, engine)

    monkeypatch.setattr(indexer, "extract_with_notes", edit_while_extracting)
    _, ps = indexer.run_index(con, [str(root)], engine=object(), log=quiet)
    assert ps.done == 0 and ps.errors == 0
    assert con.execute("SELECT status, attempts FROM files").fetchone()[:] == ("pending", 0)
    assert con.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
    monkeypatch.setattr(indexer, "extract_with_notes", real_extract)
    (root / "b.pdf").write_text(original)
    _, ps = indexer.run_index(con, [], engine=object(), log=quiet)
    assert ps.done == 2 and ps.reused == 0
    assert [h.filename for h in Searcher(db).search("Kowalski").hits] == ["b.pdf"]
    assert [h.filename for h in Searcher(db).search("Grünwerk").hits] == ["a.pdf"]


def test_unrelated_sqlite_database_with_only_a_view_is_not_adopted(tmp_path):
    db = tmp_path / "andere.db"
    con = sqlite3.connect(db)
    con.execute("CREATE VIEW andere AS SELECT 42 AS betrag")
    con.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with pytest.raises(dbm.IncompatibleIndexError, match="Belegsuche"):
        dbm.connect(db)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_format_marker_and_rebuilt_rows_are_committed_together(synthetic, monkeypatch):
    root, db, con, calls = synthetic
    (root / "rechnung.pdf").write_text("Dachdecker Kowalski Rechnung Hospitalstraße, 62")
    indexer.run_index(con, [str(root)], engine=object(), log=quiet)
    con.execute("UPDATE fts SET body='kowalski'")
    dbm.set_meta(con, "index_format", "8")
    con.commit()
    real_rebuild = dbm.rebuild_search_index
    visible = []

    def rebuild_and_observe_committed_state(connection):
        real_rebuild(connection)
        reader = dbm.connect(db, readonly=True)
        try:
            visible.append(dbm.get_meta(reader, "index_format"))
        finally:
            reader.close()

    monkeypatch.setattr(dbm, "rebuild_search_index", rebuild_and_observe_committed_state)
    dbm.connect(db).close()
    assert visible == [dbm.INDEX_FORMAT]
    assert len(calls) == 1
    assert [h.filename for h in Searcher(db).search("Hospitalstraße 62").hits] == ["rechnung.pdf"]


def test_changed_during_reindex_keeps_previous_searchable_text(synthetic, monkeypatch):
    root, db, con, _ = synthetic
    path = root / "rechnung.pdf"
    original = "Dachdecker Kowalski Rechnung"
    path.write_text(original)
    indexer.run_index(con, [str(root)], engine=object(), log=quiet)
    path.write_text("Gartenpflege Grünwerk Rechnung")
    real_extract = indexer.extract_with_notes

    def edit_while_extracting(path, engine):
        path.write_text("Elektro Müller Rechnung erneut geändert")
        return real_extract(path, engine)

    monkeypatch.setattr(indexer, "extract_with_notes", edit_while_extracting)
    _, ps = indexer.run_index(con, [], engine=object(), log=quiet)
    assert ps.done == 0 and ps.errors == 0
    assert con.execute("SELECT status, attempts FROM files").fetchone()[:] == ("pending", 0)
    hits = Searcher(db).search("Kowalski").hits
    assert [h.filename for h in hits] == ["rechnung.pdf"] and hits[0].outdated
    assert con.execute("SELECT sha256 FROM files").fetchone()[0] == hashlib.sha256(original.encode()).hexdigest()


class _ImmediatePool:
    """Completed worker futures, with deterministic changes kept in the parent process."""
    _processes = {}

    def submit(self, fn, path, *_):
        from concurrent.futures import Future
        result = Future()
        result.set_result(([PageText(1, Path(path).read_text(), "text", None)], []))
        return result

    def shutdown(self, **_):
        pass


@pytest.mark.parametrize("change,expected_status", [("edit", "pending"), ("remove", "missing")])
def test_parallel_changed_cached_duplicate_does_not_abort_other_files(synthetic, monkeypatch,
                                                                      change, expected_status):
    root, _, con, _ = synthetic
    text = "Dachdecker Kowalski Rechnung"
    (root / "a.pdf").write_text(text)
    indexer.run_index(con, [str(root)], engine=object(), log=quiet)
    duplicate = root / "b.pdf"
    duplicate.write_text(text)
    (root / "c.pdf").write_text("Gartenpflege Grünwerk Rechnung")
    real_prepare = indexer._prepare

    def change_cached_duplicate(connection, row):
        result = real_prepare(connection, row)
        if row["relpath"] == "b.pdf":
            assert result[2] is not None and result[3]
            if change == "edit":
                duplicate.write_text("Elektro Müller Rechnung geändert")
            else:
                duplicate.unlink()
        return result

    monkeypatch.setattr(indexer, "_prepare", change_cached_duplicate)
    monkeypatch.setattr(indexer, "_make_pool", lambda *_: _ImmediatePool())
    _, ps = indexer.run_index(con, [], workers=2, log=quiet)
    assert not ps.interrupted and ps.done == 1 and ps.errors == 0
    assert dict(con.execute("SELECT relpath,status FROM files")) == {
        "a.pdf": "done", "b.pdf": expected_status, "c.pdf": "done"}
    if change == "edit":
        assert con.execute("SELECT attempts FROM files WHERE relpath='b.pdf'").fetchone()[0] == 0


def test_parallel_interrupt_during_result_finish_resets_popped_file(synthetic, monkeypatch):
    root, _, con, _ = synthetic
    (root / "a.pdf").write_text("Dachdecker Kowalski Rechnung")
    (root / "b.pdf").write_text("Gartenpflege Grünwerk Rechnung")
    monkeypatch.setattr(indexer, "_make_pool", lambda *_: _ImmediatePool())

    def interrupt_finish(*_, **__):
        raise KeyboardInterrupt

    monkeypatch.setattr(indexer._Progress, "finish", interrupt_finish)
    _, ps = indexer.run_index(con, [str(root)], workers=2, log=quiet)
    assert ps.interrupted and ps.done == 0 and ps.errors == 0
    assert {tuple(r) for r in con.execute("SELECT status,attempts FROM files")} == {("pending", 0)}
