"""Was nach dem ersten Einlesen passiert: neue, geänderte, verschobene, gelöschte Dateien,
abgezogene SSD, Abbruch mitten im Lauf."""
import os
import shutil
import time

import pytest

from belegsuche import db as dbm
from belegsuche.indexer import process_pending, run_index
from belegsuche.search import Searcher

from .conftest import CountingEngine, digital_pdf, scan_pdf


def quiet(_):
    pass


def status_of(con, relpath):
    return con.execute("SELECT status FROM files WHERE relpath = ?", (relpath,)).fetchone()[0]


def names(db_path, q):
    return [h.filename for h in Searcher(db_path).search(q).hits]


def test_new_file_is_added(indexed):
    root, _, db_path, con, _ = indexed
    digital_pdf(root / "Diverses" / "neu.pdf", [["Dachdecker Kowalski", "Rechnung D-9001", "Betrag 2.450,00 €"]])
    scans, ps = run_index(con, [], engine=CountingEngine(), log=quiet)
    assert scans[0].new == 1 and ps.done == 1
    assert names(db_path, "Kowalski") == ["neu.pdf"]


def test_changed_file_is_reindexed(indexed):
    root, files, db_path, con, _ = indexed
    time.sleep(0.01)
    digital_pdf(files["mueller"], [["Elektro Müller KG", "Rechnung INV-99999", "Betrag 10,00 €"]])
    scans, _ = run_index(con, [], engine=CountingEngine(), log=quiet)
    assert scans[0].changed == 1
    assert names(db_path, "INV-99999") == ["Unbenannt.pdf"]
    assert "Unbenannt.pdf" not in names(db_path, "INV-88213")


@pytest.mark.vision
def test_moved_file_needs_no_new_ocr(indexed):
    root, files, db_path, con, _ = indexed
    target = root / "2021" / "verschoben.pdf"
    shutil.move(files["scan"], target)
    engine = CountingEngine()
    scans, ps = run_index(con, [], engine=engine, log=quiet)
    assert scans[0].missing == 1 and scans[0].new == 1
    assert engine.calls == 0 and ps.reused == 1          # Text wurde übernommen, keine OCR
    hits = Searcher(db_path).search("Schornsteinfeger Birkenallee").hits
    assert [h.filename for h in hits] == ["verschoben.pdf"]


def test_duplicate_is_grouped(indexed):
    root, files, db_path, con, _ = indexed
    shutil.copy2(files["heizung"], root / "Diverses" / "Kopie.pdf")
    engine = CountingEngine()
    run_index(con, [], engine=engine, log=quiet)
    hits = Searcher(db_path).search("RE-2021/00487").hits
    assert len(hits) == 1 and len(hits[0].duplicates) == 1


def test_deleted_file_disappears(indexed):
    root, files, db_path, con, _ = indexed
    files["hauswart"].unlink()
    scans, _ = run_index(con, [], engine=CountingEngine(), log=quiet)
    assert scans[0].missing == 1
    assert "20220630_1422.pdf" not in names(db_path, "Hauswart Lindenweg")


def test_ssd_unplugged_changes_nothing(indexed, tmp_path):
    root, _, db_path, con, _ = indexed
    before = dict(con.execute("SELECT relpath, status FROM files").fetchall())
    offline = tmp_path / "abgezogen"
    root.rename(offline)                                   # SSD "abziehen"
    scans, ps = run_index(con, [], engine=CountingEngine(), log=quiet)
    assert scans[0].online is False
    assert dict(con.execute("SELECT relpath, status FROM files").fetchall()) == before
    hits = Searcher(db_path).search("RE-2021/00487").hits  # Suche geht weiter …
    assert hits and hits[0].available is False             # … Öffnen erst nach dem Anschließen
    offline.rename(root)
    assert Searcher(db_path).search("RE-2021/00487").hits[0].available is True


def test_empty_mountpoint_is_not_treated_as_deleted(tmp_path):
    """Leerer Ordner statt SSD (z. B. falscher Mount): nicht alles als gelöscht markieren."""
    root = tmp_path / "viele"
    root.mkdir()
    for i in range(25):
        digital_pdf(root / f"d{i}.pdf", [[f"Dokument Nummer {i}", "Inhalt Testbeleg"]])
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=CountingEngine(), log=quiet)
    for p in list(root.iterdir())[:20]:
        p.unlink()
    scans, _ = run_index(con, [], engine=CountingEngine(), log=quiet)
    assert scans[0].missing == 0 and scans[0].missing_held_back == 20
    scans, _ = run_index(con, [], engine=CountingEngine(), confirm_missing=True, log=quiet)
    assert scans[0].missing == 20


@pytest.mark.vision
def test_resume_after_interrupt(tmp_path):
    root = tmp_path / "scans"
    root.mkdir()
    for i in range(3):
        # genug Text, damit die OCR ohne zweiten Versuch (Kontrastverstärkung) auskommt
        scan_pdf(root / f"s{i}.pdf", [[f"Quittung Nummer Q-{i}000", "Firma Testbau Musterstadt GmbH",
                                       "Lieferung Baustoffe Lindenweg 3"]])
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], engine=CountingEngine(fail_after=1), log=quiet)
    assert ps.interrupted and ps.done == 1
    assert status_of(con, "s0.pdf") == "done"
    assert status_of(con, "s1.pdf") == "pending"            # abgebrochene Datei bleibt offen
    engine = CountingEngine()
    _, ps = run_index(con, [], engine=engine, log=quiet)
    assert ps.done == 2 and engine.calls == 2                # s0 wird nicht noch einmal erkannt
    assert {status_of(con, f"s{i}.pdf") for i in range(3)} == {"done"}


def test_repeated_crash_marks_error(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    digital_pdf(root / "a.pdf", [["Hallo Welt Beleg"]])
    con = dbm.connect(tmp_path / "i.db")
    from belegsuche.indexer import add_root, scan_root
    rid = add_root(con, root)
    scan_root(con, rid, str(root), log=quiet)
    con.execute("UPDATE files SET status='processing', attempts=3")   # 3× hart abgestürzt
    con.commit()
    process_pending(con, engine=CountingEngine(), log=quiet)
    assert con.execute("SELECT status, error FROM files").fetchone()[0] == "error"


def test_unreadable_subfolder_is_not_marked_missing(indexed):
    root, files, db_path, con, _ = indexed
    locked = root / "Scans unsortiert"
    os.chmod(locked, 0)
    try:
        scans, _ = run_index(con, [], engine=CountingEngine(), log=quiet)
        assert scans[0].missing == 0 and scans[0].dir_errors
    finally:
        os.chmod(locked, 0o755)


@pytest.mark.vision
def test_parallel_matches_sequential(corpus, tmp_path):
    root, _ = corpus
    con_a = dbm.connect(tmp_path / "a.db")
    run_index(con_a, [str(root)], log=quiet, workers=1)
    con_b = dbm.connect(tmp_path / "b.db")
    _, ps = run_index(con_b, [str(root)], log=quiet, workers=3)
    q = "SELECT relpath, status, pages, text_chars > 0 FROM files ORDER BY relpath"
    assert con_a.execute(q).fetchall() == con_b.execute(q).fetchall()
    assert ps.done == 6 and ps.errors == 2
    assert names(tmp_path / "b.db", "Aufzug Wartung Am Hang")[0] == "scan0001.pdf"


@pytest.mark.vision
def test_parallel_worker_crash_is_isolated(corpus, tmp_path, monkeypatch):
    root, files = corpus
    monkeypatch.setenv("BELEGSUCHE_TEST_CRASH_ON", "Scan_00487.pdf")
    con = dbm.connect(tmp_path / "c.db")
    _, ps = run_index(con, [str(root)], log=quiet, workers=2)
    st = dict(con.execute("SELECT relpath, status FROM files").fetchall())
    assert st["Scans unsortiert/Scan_00487.pdf"] == "error"          # nur die schuldige Datei
    for k in ("heizung", "hauswart", "mueller", "bon", "sammel"):
        assert st[str(files[k].relative_to(root))] == "done", k
