"""Inkrementeller Index: Fälle, die bisher ungetestet waren (wiederhergestellte Datei, reparierte
Datei, viele fehlende Dateien, ignorierte System-/Temp-Dateien, Symlinks, verschwindende Unterordner).
Schnell: Ersatz-OCR, die mitzählt."""
import os
import shutil
import time

from belegsuche import db as dbm
from belegsuche.indexer import run_index
from belegsuche.ocr import OcrResult
from belegsuche.search import Searcher

from .conftest import digital_pdf, text_image, tree_state

BON = "Quittung Baumarkt Nord Silikon Schrauben Summe 11,48 EUR"


class CountingFake:
    def __init__(self, text=BON):
        self.text, self.calls = text, 0

    def recognize_image(self, img):
        self.calls += 1
        return OcrResult(self.text, 0.9)

    def recognize_file(self, path):
        return self.recognize_image(None)


def quiet(_):
    pass


def status(con):
    return {r["relpath"]: r["status"] for r in con.execute("SELECT relpath, status FROM files")}


def names(db, q):
    return [h.filename for h in Searcher(db).search(q).hits]


def test_geloeschte_und_wiederhergestellte_datei_braucht_keine_neue_ocr(tmp_path):
    root = tmp_path / "k"
    root.mkdir()
    text_image(["Baumarkt Nord"], size=(300, 200), font_size=20).save(root / "bon.png")
    digital_pdf(root / "r.pdf", [["Dachdecker Kowalski Rechnung D-9001", "Sehr geehrte Damen und Herren"]])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    eng = CountingFake()
    run_index(con, [str(root)], engine=eng, log=quiet)
    assert eng.calls == 1 and names(db, "Silikon") == ["bon.png"]

    backup = tmp_path / "bon.png.bak"
    shutil.move(root / "bon.png", backup)
    scans, _ = run_index(con, [], engine=eng, log=quiet)
    assert scans[0].missing == 1 and status(con)["bon.png"] == "missing"
    assert names(db, "Silikon") == []

    shutil.move(backup, root / "bon.png")
    os.utime(root / "bon.png")                            # beim Zurückkopieren ändert sich oft die Zeit
    scans, ps = run_index(con, [], engine=eng, log=quiet)
    assert scans[0].restored == 1 and ps.reused == 1
    assert eng.calls == 1                                 # keine zweite Texterkennung
    assert status(con)["bon.png"] == "done" and names(db, "Silikon") == ["bon.png"]
    con.close()


def test_reparierte_datei_wird_ohne_fehler_wiederholen_neu_eingelesen(tmp_path):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(tmp_path / "gut.pdf", [["Dachdecker Kowalski Rechnung D-9001", "Sehr geehrte Damen und Herren"]])
    good = (tmp_path / "gut.pdf").read_bytes()
    (root / "r.pdf").write_bytes(good[: len(good) // 3])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    assert status(con)["r.pdf"] == "error"
    time.sleep(0.01)
    (root / "r.pdf").write_bytes(good)                    # Datei ersetzt (neu gescannt/heruntergeladen)
    scans, ps = run_index(con, [], engine=CountingFake(), log=quiet)
    assert scans[0].changed == 1 and ps.done == 1
    row = con.execute("SELECT status, error, attempts FROM files WHERE relpath = 'r.pdf'").fetchone()
    assert tuple(row) == ("done", None, 0)
    assert names(db, "Kowalski") == ["r.pdf"]
    con.close()


def test_viele_fehlende_dateien_werden_zurueckgehalten(tmp_path):
    root = tmp_path / "k"
    root.mkdir()
    for i in range(24):
        digital_pdf(root / f"r{i:02d}.pdf", [[f"Beleg Nummer {i} Kowalski", "Sehr geehrte Damen und Herren"]])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    for i in range(13):                                   # mehr als die Hälfte verschwindet
        os.remove(root / f"r{i:02d}.pdf")
    logs = []
    scans, _ = run_index(con, [], engine=CountingFake(), log=logs.append)
    assert scans[0].missing == 0 and scans[0].missing_held_back == 13
    assert any("fehlende-bestaetigen" in s for s in logs)
    assert list(status(con).values()).count("done") == 24
    scans, _ = run_index(con, [], engine=CountingFake(), log=quiet, confirm_missing=True)
    assert scans[0].missing == 13 and list(status(con).values()).count("missing") == 13
    assert len(Searcher(db).search("Kowalski", limit=100).hits) == 11
    con.close()


def test_wenige_fehlende_dateien_werden_sofort_als_fehlend_markiert(tmp_path):
    root = tmp_path / "k"
    root.mkdir()
    for i in range(24):
        digital_pdf(root / f"r{i:02d}.pdf", [[f"Beleg Nummer {i} Kowalski", "Sehr geehrte Damen und Herren"]])
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    for i in range(5):
        os.remove(root / f"r{i:02d}.pdf")
    scans, _ = run_index(con, [], engine=CountingFake(), log=quiet)
    assert scans[0].missing == 5 and scans[0].missing_held_back == 0
    con.close()


def test_system_und_temp_dateien_werden_ignoriert(tmp_path):
    root = tmp_path / "k"
    for d in ("__MACOSX", ".Trashes", ".Spotlight-V100", "$RECYCLE.BIN", "System Volume Information", ".versteckt",
              "echt"):
        (root / d).mkdir(parents=True)
        digital_pdf(root / d / "r.pdf", [["Dachdecker Kowalski Rechnung D-9001", "Sehr geehrte Damen und Herren"]])
    digital_pdf(root / "~$Rechnung.pdf", [["Dachdecker Kowalski temporär", "Sehr geehrte Damen und Herren"]])
    digital_pdf(root / "._Rechnung.pdf", [["Dachdecker Kowalski AppleDouble", "Sehr geehrte Damen und Herren"]])
    for n in ("Thumbs.db", "desktop.ini", "Icon\r", ".DS_Store"):
        (root / n).write_bytes(b"\x00")
    con = dbm.connect(tmp_path / "i.db")
    scans, _ = run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    assert status(con) == {"echt/r.pdf": "done"}
    assert scans[0].seen == 1 and scans[0].unsupported == 0
    con.close()


def test_symlink_auf_datei_ausserhalb_wird_nicht_eingelesen(tmp_path):
    root, outside = tmp_path / "k", tmp_path / "privat"
    root.mkdir(), outside.mkdir()
    digital_pdf(outside / "geheim.pdf", [["Kontoauszug Privat Geheimwort", "Sehr geehrte Damen und Herren"]])
    digital_pdf(root / "r.pdf", [["Dachdecker Kowalski Rechnung D-9001", "Sehr geehrte Damen und Herren"]])
    (root / "link.pdf").symlink_to(outside / "geheim.pdf")
    (root / "linkordner").symlink_to(outside, target_is_directory=True)
    before = tree_state(outside)
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    assert set(status(con)) == {"r.pdf"}
    assert names(db, "Geheimwort") == []
    assert tree_state(outside) == before
    con.close()


def test_verschwundener_unterordner_und_neuer_unterordner(tmp_path):
    root = tmp_path / "k"
    (root / "2021").mkdir(parents=True)
    for i in range(3):
        digital_pdf(root / "2021" / f"r{i}.pdf", [[f"Beleg {i} Kowalski", "Sehr geehrte Damen und Herren"]])
    for i in range(3):
        digital_pdf(root / f"s{i}.pdf", [[f"Beleg {i} Brandl", "Sehr geehrte Damen und Herren"]])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    (root / "2021").rename(root / "Archiv 2021")          # Ordner umbenannt: gleicher Inhalt, neuer Pfad
    eng = CountingFake()
    scans, ps = run_index(con, [], engine=eng, log=quiet)
    assert scans[0].missing == 3 and scans[0].new == 3 and ps.reused == 3
    hits = Searcher(db).search("Kowalski").hits
    assert sorted(h.folder for h in hits) == ["Archiv 2021"] * 3
    con.close()


def test_nicht_unterstuetzte_datei_wird_spaeter_nicht_als_fehler_gemeldet(tmp_path):
    root = tmp_path / "k"
    root.mkdir()
    (root / "Liste.xlsx").write_bytes(b"PK\x03\x04 eins")
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    time.sleep(0.01)
    (root / "Liste.xlsx").write_bytes(b"PK\x03\x04 zwei, jetzt laenger")
    scans, ps = run_index(con, [], engine=CountingFake(), log=quiet)
    assert status(con) == {"Liste.xlsx": "skipped"} and ps.errors == 0 and scans[0].unsupported == 1
    con.close()
