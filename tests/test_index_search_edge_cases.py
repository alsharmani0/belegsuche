"""Regressionstests: Randfälle beim Einlesen, bei der Sperre und bei der Suche."""
import os
import subprocess
import sys

import pytest
from PIL import Image

from belegsuche import db as dbm
from belegsuche.indexer import acquire_lock, run_index
from belegsuche.ocr import OcrResult
from belegsuche.search import Searcher


class ColorOCR:
    """Liest statt Text die Farbe: rot = alter Inhalt, sonst neuer Inhalt."""

    def recognize_file(self, path):
        with Image.open(path) as im:
            tag = "ALTKENNUNG" if im.getpixel((0, 0)) == (255, 0, 0) else "NEUKENNUNG"
        return OcrResult(f"Rechnung Firma {tag} Musterstadt Belegnummer 123456789 Betrag 99 Euro", 0.99)

    def recognize_image(self, img):
        return OcrResult("fallback", 0.99)


def quiet(_):
    pass


def test_foreign_folder_with_same_names_is_not_taken_as_moved_ssd(tmp_path):
    old, other = tmp_path / "ssd-alt", tmp_path / "fremd"
    old.mkdir(), other.mkdir()
    for i in range(4):
        for folder, color in ((old, (255, 0, 0)), (other, (0, 255, 0))):
            f = folder / f"bon{i}.bmp"
            Image.new("RGB", (20, 20), color).save(f)
            os.utime(f, (1700000000, 1700000000))
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(old)], engine=ColorOCR(), log=quiet)
    old.rename(tmp_path / "abgezogen")
    log = []
    run_index(con, [str(other)], engine=ColorOCR(), log=log.append)
    assert con.execute("SELECT count(*) FROM roots").fetchone()[0] == 2          # eigener Ordner
    assert not any("umgezogen" in m for m in log)
    hits = Searcher(tmp_path / "i.db").search("NEUKENNUNG").hits
    assert hits and all(h.root == str(other.resolve()) for h in hits)


def test_real_moved_ssd_is_still_recognized(tmp_path):
    old = tmp_path / "SSD"
    old.mkdir()
    for i in range(4):
        Image.new("RGB", (20, 20), (255, 0, 0)).save(old / f"bon{i}.bmp")
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(old)], engine=ColorOCR(), log=quiet)
    new = tmp_path / "SSD 1"
    old.rename(new)
    log = []
    _, ps = run_index(con, [str(new)], engine=ColorOCR(), log=log.append)
    assert con.execute("SELECT count(*) FROM roots").fetchone()[0] == 1 and any("umgezogen" in m for m in log)
    assert ps.done == 0


def test_empty_mount_point_with_single_file_changes_nothing(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    Image.new("RGB", (20, 20), (255, 0, 0)).save(mount / "bon.bmp")
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(mount)], engine=ColorOCR(), log=quiet)
    mount.rename(tmp_path / "abgezogen")
    mount.mkdir()                                           # leerer Einhängepunkt bleibt zurück
    scans, _ = run_index(con, [], engine=ColorOCR(), log=quiet)
    assert scans[0].missing == 0 and scans[0].missing_held_back == 1
    assert con.execute("SELECT status FROM files").fetchone()[0] == "done"
    assert Searcher(tmp_path / "i.db").search("ALTKENNUNG").hits


def test_lock_is_shared_through_symlink_alias(tmp_path):
    db = tmp_path / "same.db"
    db.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(db)
    first = acquire_lock(db)
    try:
        code = f"from belegsuche.indexer import acquire_lock; acquire_lock({str(alias)!r})"
        second = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert second.returncode != 0 and "läuft bereits" in second.stderr
    finally:
        first.close()


# --- Suche (direkt in die Datenbank geschrieben) --------------------------------------------

def _setup(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    con = dbm.connect(tmp_path / "i.db")
    rid = con.execute("INSERT INTO roots(path) VALUES (?)", (str(root),)).lastrowid
    return root, con, rid


def _add(root, con, rid, relpath, texts, sha, mtime=1):
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(texts), encoding="utf-8")
    fid = con.execute("INSERT INTO files(root_id, relpath, ext, kind, size, mtime, sha256, status, pages)"
                      " VALUES (?, ?, '.pdf', 'pdf', ?, ?, ?, 'done', ?)",
                      (rid, relpath, path.stat().st_size, mtime, sha, len(texts))).lastrowid
    for n, body in enumerate(texts, 1):
        con.execute("INSERT INTO pages(file_id, page_no, text) VALUES (?, ?, ?)", (fid, n, body))
    dbm.index_file_text(con, fid, relpath)
    return fid


def test_many_identical_copies_do_not_crowd_out_other_invoice(tmp_path):
    root, con, rid = _setup(tmp_path)
    for i in range(310):
        _add(root, con, rid, f"kopien/Kopie{i:03}.pdf", ["Heizung Rechnung"], "gleich", i + 2)
    _add(root, con, rid, "einzeln/Anderer.pdf", ["Heizung Rechnung mit anderer Leistung"], "anders")
    con.commit()
    hits = Searcher(tmp_path / "i.db").search("Heizung", limit=30).hits
    assert "Anderer.pdf" in [h.filename for h in hits]
    assert len(hits) == 2 and max(len(h.duplicates) for h in hits) == 309


def test_terms_on_different_pages_found_despite_many_single_matches(tmp_path):
    root, con, rid = _setup(tmp_path)
    for i in range(3100):
        _add(root, con, rid, f"gleich/V{i:04}.pdf", ["Brandl"], f"v{i}")
        _add(root, con, rid, f"gleich/O{i:04}.pdf", ["Birkenallee"], f"o{i}")
    target = _add(root, con, rid, "lange/gesuchte_rechnung.pdf",
                  ["Brandl " + "erlaeuterung " * 50, "Birkenallee " + "erlaeuterung " * 50], "ziel")
    con.commit()
    hits = Searcher(tmp_path / "i.db").search("Brandl Birkenallee", limit=30).hits
    assert hits[0].file_id == target and hits[0].missing == []


def test_quoted_phrase_matches_folder_name(tmp_path):
    root, con, rid = _setup(tmp_path)
    _add(root, con, rid, "Musterstrasse 12/Rechnung.pdf", ["Leistung Dachreparatur"], "p")
    con.commit()
    s = Searcher(tmp_path / "i.db")
    assert [h.filename for h in s.search("Musterstrasse 12").hits] == ["Rechnung.pdf"]
    assert [h.filename for h in s.search('"Musterstrasse 12"').hits] == ["Rechnung.pdf"]


def test_old_index_format_is_rebuilt_without_ocr(tmp_path):
    root, con, rid = _setup(tmp_path)
    _add(root, con, rid, "a.pdf", ["Gesamtbetrag 1.234,56 €"], "x")
    con.execute("UPDATE fts SET body = 'gesamtbetrag 1 234 56 amt1234c56'")   # altes Tokenformat
    dbm.set_meta(con, "index_format", "1")
    con.commit()
    con.close()
    dbm.connect(tmp_path / "i.db").close()                                     # Umstellung beim Öffnen
    assert [h.filename for h in Searcher(tmp_path / "i.db").search("1234,56").hits] == ["a.pdf"]


# --- Sperre über harte Links, Umstellung alter Indizes, leere Rückseiten ---------------------

def test_lock_is_shared_through_hardlink_in_other_folder(tmp_path):
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    db = tmp_path / "a" / "index.db"
    db.touch()
    os.link(db, tmp_path / "b" / "index.db")
    first = acquire_lock(db)
    try:
        code = f"from belegsuche.indexer import acquire_lock; acquire_lock({str(tmp_path / 'b' / 'index.db')!r})"
        second = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert second.returncode != 0 and "läuft bereits" in second.stderr
    finally:
        first.close()


def test_cli_search_migrates_old_index_first(tmp_path, capsys, monkeypatch):
    from belegsuche import cli
    from belegsuche import search as srch
    monkeypatch.setattr(srch, "GROUP_FETCH_MAX_DF", 1)     # wie bei großem Bestand: nur wenige Kandidaten je Begriff
    monkeypatch.setattr(srch, "CANDIDATE_LIMIT", 5)
    root, con, rid = _setup(tmp_path)
    for i in range(30):
        _add(root, con, rid, f"x/V{i}.pdf", ["Brandl"], f"v{i}")
        _add(root, con, rid, f"x/O{i}.pdf", ["Birkenallee"], f"o{i}")
    _add(root, con, rid, "ziel/rechnung.pdf", ["Brandl Firma", "Birkenallee Objekt"], "ziel")
    con.execute("DROP TABLE fts_doc")                 # Stand vor dem Dokument-Index
    con.execute("DELETE FROM meta WHERE key = 'index_format'")
    con.commit()
    con.close()
    rc = cli.main(["--db", str(tmp_path / "i.db"), "suche", "Brandl", "Birkenallee", "--limit", "3"])
    out = capsys.readouterr().out
    assert rc == 0 and "rechnung.pdf" in out.splitlines()[0]
    con = dbm.connect(tmp_path / "i.db", readonly=True)
    assert dbm.get_meta(con, "index_format") == dbm.INDEX_FORMAT
    assert con.execute("SELECT count(*) FROM fts_doc").fetchone()[0] == 1


@pytest.mark.vision
def test_report_names_blank_back_pages(tmp_path):
    from PIL import Image as PILImage

    from belegsuche.report import build_report, format_report
    from .conftest import INVOICE_HEIZUNG, text_image
    root = tmp_path / "k"
    root.mkdir()
    front = text_image(INVOICE_HEIZUNG).convert("RGB")
    back = PILImage.new("RGB", front.size, "white")
    front.save(root / "scan.pdf", "PDF", resolution=200, save_all=True, append_images=[back])
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], log=quiet)
    rep = build_report(con)
    reasons = [p["grund"] for p in rep["problems"]]
    assert rep["blank_pages"] == 1 and rep["low_conf_pages"] == 0
    assert any("leere Seite 2" in g for g in reasons) and not any("0%" in g for g in reasons)
    assert "leer: 1" in format_report(rep)
