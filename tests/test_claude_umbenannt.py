"""Umbenannte oder innerhalb des Ordners verschobene Dateien: Der alte Eintrag „fehlt“ wird nach dem Verarbeiten
entfernt, der Text übernommen. Anlass: Beim Kopieren über die Windows App wurden Umlaute in Dateinamen beschädigt
(„šbergabe“ statt „Übergabe“); nach dem Zurückbenennen standen die alten Namen dauerhaft als „fehlen“ im Bericht.

Gelöschte Dateien ohne Gegenstück, echte Dubletten und Einträge bei abgezogener SSD bleiben unberührt.
Schnell: Ersatz-OCR, die mitzählt."""
import os
import shutil
import unicodedata

import pytest

from belegsuche import cli
from belegsuche import db as dbm
from belegsuche.indexer import process_pending, prune_renamed, run_index
from belegsuche.ocr import OcrResult
from belegsuche.report import build_report, format_report, repair_umlauts
from belegsuche.search import Searcher

from .conftest import digital_pdf, text_image

BON = "Quittung Baumarkt Nord Silikon Schrauben Summe 11,48 EUR"
ALT = "šbergabe.png"            # „Übergabe.png“, beim Kopieren aus CP850 als cp1252 gelesen
NEU = "Übergabe.png"


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


def nfc(s):
    return unicodedata.normalize("NFC", s)


def status(con):
    return {nfc(r["relpath"]): r["status"] for r in con.execute("SELECT relpath, status FROM files")}


def names(db, q):
    return [nfc(h.relpath) for h in Searcher(db).search(q).hits]


def no_orphans(con):
    """Keine Seiten ohne Datei, keine Suchzeilen ohne Seite."""
    return (con.execute("SELECT count(*) FROM pages WHERE file_id NOT IN (SELECT id FROM files)").fetchone()[0] == 0
            and con.execute("SELECT count(*) FROM fts WHERE rowid NOT IN (SELECT id FROM pages)").fetchone()[0] == 0
            and con.execute("SELECT count(*) FROM fts_doc WHERE rowid NOT IN (SELECT id FROM files)").fetchone()[0] == 0)


@pytest.fixture()
def ordner(tmp_path):
    """Ordner mit einem Foto (braucht Texterkennung) unter beschädigtem Namen und einem digitalen PDF."""
    root = tmp_path / "k"
    root.mkdir()
    text_image(["Baumarkt Nord"], size=(300, 200), font_size=20).save(root / ALT)
    digital_pdf(root / "r.pdf", [["Dachdecker Kowalski Rechnung D-9001", "Sehr geehrte Damen und Herren"]])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    eng = CountingFake()
    run_index(con, [str(root)], engine=eng, log=quiet)
    assert eng.calls == 1 and names(db, "Silikon") == [ALT]
    yield root, db, con
    con.close()


@pytest.mark.parametrize("ziel", [NEU, f"2021/Objekte/{NEU}"], ids=["umbenannt", "in Unterordner verschoben"])
def test_umbenannte_datei_fehlt_nicht_und_braucht_keine_neue_texterkennung(ordner, ziel):
    root, db, con = ordner
    (root / ziel).parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / ALT, root / ziel)
    eng = CountingFake()
    log = []
    scans, ps = run_index(con, [], engine=eng, log=log.append)
    assert scans[0].missing == 1 and scans[0].new == 1         # beim Durchgehen: alter Name weg, neuer da …
    assert scans[0].renamed == 1                               # … nach dem Verarbeiten als Umbenennung erkannt
    assert eng.calls == 0 and ps.reused == 1                   # Inhalt übernommen, keine neue Texterkennung
    assert status(con) == {"r.pdf": "done", ziel: "done"}      # kein Eintrag „fehlt“ mehr
    assert names(db, "Silikon") == [ziel]
    assert any(ALT in s and ziel in s and "umbenannt/verschoben" in s for s in log)
    assert no_orphans(con)
    r = build_report(con)
    assert r["status"].get("missing", 0) == 0 and r["duplicates"] == 0 and r["problems"] == []
    assert "fehlen im Ordner:        0" in format_report(r)
    # nächster Lauf: nichts mehr zu tun
    scans, ps = run_index(con, [], engine=eng, log=quiet)
    assert scans[0].missing == 0 and scans[0].renamed == 0 and ps.done == 0


def test_geloeschte_datei_ohne_gegenstueck_zaehlt_weiter_als_fehlt(ordner):
    root, db, con = ordner
    (root / ALT).unlink()
    scans, _ = run_index(con, [], engine=CountingFake(), log=quiet)
    assert scans[0].missing == 1 and scans[0].renamed == 0
    assert status(con)[ALT] == "missing"
    assert names(db, "Silikon") == []
    r = build_report(con)
    assert r["status"]["missing"] == 1
    assert "fehlen im Ordner:        1" in format_report(r)
    assert [nfc(os.path.basename(p["pfad"])) for p in r["problems"] if p["status"] == "missing"] == [ALT]
    # auch spätere Läufe lassen ihn stehen
    scans, _ = run_index(con, [], engine=CountingFake(), log=quiet)
    assert scans[0].renamed == 0 and status(con)[ALT] == "missing"


def test_echte_dubletten_bleiben_unangetastet(tmp_path):
    root = tmp_path / "k"
    root.mkdir()
    text_image(["Baumarkt Nord"], size=(300, 200), font_size=20).save(root / "a.png")
    shutil.copy2(root / "a.png", root / "Kopie.png")
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    assert build_report(con)["duplicates"] == 1
    scans, _ = run_index(con, [], engine=CountingFake(), log=quiet)   # ohne Änderung: beide bleiben
    assert scans[0].renamed == 0 and status(con) == {"a.png": "done", "Kopie.png": "done"}

    os.rename(root / "a.png", root / "b.png")
    eng = CountingFake()
    scans, _ = run_index(con, [], engine=eng, log=quiet)
    assert scans[0].renamed == 1 and eng.calls == 0
    assert status(con) == {"b.png": "done", "Kopie.png": "done"}       # die echte Dublette ist noch da
    assert build_report(con)["duplicates"] == 1
    assert no_orphans(con)
    con.close()


def test_alter_pfad_noch_belegt_bleibt_stehen(tmp_path):
    """Liegt unter dem alten Pfad wieder (oder noch) eine Datei, wird der Eintrag nicht entfernt."""
    root = tmp_path / "k"
    root.mkdir()
    text_image(["Baumarkt Nord"], size=(300, 200), font_size=20).save(root / "a.png")
    shutil.copy2(root / "a.png", root / "Kopie.png")
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    rid = con.execute("SELECT id FROM roots").fetchone()[0]
    # z. B. früher als fehlend markiert, weil der Ordner kurz nicht lesbar war; a.png liegt aber wieder da
    con.execute("UPDATE files SET status = 'missing' WHERE relpath = 'a.png'")
    con.commit()
    assert prune_renamed(con, rid, str(root), log=quiet) == 0
    assert status(con) == {"a.png": "missing", "Kopie.png": "done"}
    con.close()


def test_gegenstueck_noch_nicht_verarbeitet_dann_im_naechsten_lauf(ordner):
    """Erst wenn die Datei unter dem neuen Namen fertig ist, wird der alte Eintrag entfernt."""
    root, db, con = ordner
    os.rename(root / ALT, root / NEU)
    scans, ps = run_index(con, [], engine=CountingFake(), log=quiet, limit=0)   # z. B. abgebrochen
    assert ps.done == 0 and scans[0].renamed == 0
    assert status(con) == {"r.pdf": "done", ALT: "missing", NEU: "pending"}
    eng = CountingFake()
    scans, ps = run_index(con, [], engine=eng, log=quiet)
    assert scans[0].renamed == 1 and eng.calls == 0 and ps.reused == 1      # Text kam vom alten Eintrag
    assert status(con) == {"r.pdf": "done", NEU: "done"}
    assert names(db, "Silikon") == [NEU]


@pytest.mark.parametrize("wie", ["abgezogen", "leerer Einhängepunkt"])
def test_abgezogene_ssd_aendert_nichts(ordner, tmp_path, wie):
    root, db, con = ordner
    os.rename(root / ALT, root / NEU)
    run_index(con, [], engine=CountingFake(), log=quiet, limit=0)
    process_pending(con, engine=CountingFake(), log=quiet)          # neuer Name fertig, alter Eintrag „fehlt“
    rid = con.execute("SELECT id FROM roots").fetchone()[0]
    vorher = status(con)
    assert vorher[ALT] == "missing" and vorher[NEU] == "done"
    offline = tmp_path / "abgezogen"
    root.rename(offline)
    if wie == "leerer Einhängepunkt":
        root.mkdir()
    assert prune_renamed(con, rid, str(root), log=quiet) == 0
    scans, _ = run_index(con, [], engine=CountingFake(), log=quiet)
    assert scans[0].renamed == 0 and status(con) == vorher
    if wie == "leerer Einhängepunkt":
        root.rmdir()
    offline.rename(root)                                            # wieder angeschlossen
    scans, _ = run_index(con, [], engine=CountingFake(), log=quiet)
    assert scans[0].renamed == 1 and status(con) == {"r.pdf": "done", NEU: "done"}


def test_nur_im_selben_ordner_aufraeumen(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    text_image(["Baumarkt Nord"], size=(300, 200), font_size=20).save(a / "bon.png")
    digital_pdf(a / "dach.pdf", [["Dachdecker Kowalski Rechnung D-9001"]])   # sonst wäre a danach leer
    digital_pdf(b / "garten.pdf", [["Gartenpflege Grünwerk", "Rasenmähen Lindenweg 3"]])
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(a), str(b)], engine=CountingFake(), log=quiet)
    shutil.move(a / "bon.png", b / "bon.png")                       # in einen anderen eingelesenen Ordner
    eng = CountingFake()
    scans, _ = run_index(con, [], engine=eng, log=quiet)
    assert eng.calls == 0                                           # Text trotzdem übernommen …
    assert [s.renamed for s in scans] == [0, 0]                     # … aber nichts über Ordnergrenzen entfernt
    rows = {(r["root_id"], r["relpath"]): r["status"] for r in con.execute("SELECT root_id, relpath, status FROM files")}
    assert rows == {(1, "bon.png"): "missing", (1, "dach.pdf"): "done", (2, "garten.pdf"): "done",
                    (2, "bon.png"): "done"}
    con.close()


def _case_insensitive(path) -> bool:
    probe = path / "GrossKlein"
    probe.write_bytes(b"")
    try:
        return (path / "grossklein").exists()
    finally:
        probe.unlink()


def test_nur_gross_klein_geaendert(tmp_path):
    """APFS (Standard): rechnung.png und Rechnung.png sind derselbe Pfad – der alte Eintrag ist trotzdem veraltet."""
    if not _case_insensitive(tmp_path):
        pytest.skip("Dateisystem unterscheidet Groß/Klein")
    root = tmp_path / "k"
    root.mkdir()
    text_image(["Baumarkt Nord"], size=(300, 200), font_size=20).save(root / "rechnung.png")
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    os.rename(root / "rechnung.png", root / "Rechnung.png")
    eng = CountingFake()
    scans, _ = run_index(con, [], engine=eng, log=quiet)
    assert scans[0].renamed == 1 and eng.calls == 0
    assert status(con) == {"Rechnung.png": "done"}
    assert names(db, "Silikon") == ["Rechnung.png"]
    con.close()


def test_index_zusammenfassung_meldet_umbenannte(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BELEGSUCHE_DB", str(tmp_path / "env-index.db"))   # nie den echten Index berühren
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "šbergabe Protokoll.pdf", [["Übergabeprotokoll Wohnung 3", "Zählerstand Strom 4711"]])
    db = tmp_path / "i.db"
    assert cli.main(["--db", str(db), "index", str(root), "--parallel", "1"]) == 0
    os.rename(root / "šbergabe Protokoll.pdf", root / "Übergabe Protokoll.pdf")
    capsys.readouterr()
    assert cli.main(["--db", str(db), "index", "--parallel", "1"]) == 0
    out = capsys.readouterr().out
    assert "umbenannt/verschoben: 1 (alte Einträge entfernt, Text übernommen)" in out
    assert cli.main(["--db", str(db), "bericht"]) == 0
    out = capsys.readouterr().out
    assert "fehlen im Ordner:        0" in out and "beschädigten Umlauten" not in out


# --- Hinweis im Bericht: beschädigte Umlaute ----------------------------------------------------

@pytest.mark.parametrize("kaputt, richtig", [
    ("šbergabe.pdf", "Übergabe.pdf"),
    ("M„rz 2021.pdf", "März 2021.pdf"),
    ("Gr”áe.pdf", "Größe.pdf"),
    ("Heiz”l.pdf", "Heizöl.pdf"),
    ("Straáe 12.pdf", "Straße 12.pdf"),
    ("Straįe 12.pdf", "Straße 12.pdf"),                 # cp1257
    ("Žnderung.pdf", "Änderung.pdf"),
    ("™lrechnung.pdf", "Ölrechnung.pdf"),
    ("MšLLER GMBH.pdf", "MÜLLER GMBH.pdf"),
    ("\x81ber T„tigkeiten.pdf", "über Tätigkeiten.pdf"),  # ü: Byte ohne Zeichen in cp1252 -> Steuerzeichen
    ("Straše", None),                              # NFD-š mitten im Wort (wie Dušek): kein Umlaut
])
def test_beschaedigte_umlaute_werden_erkannt(kaputt, richtig):
    assert repair_umlauts(kaputt) == richtig
    assert repair_umlauts(unicodedata.normalize("NFD", kaputt)) == richtig     # macOS speichert Namen oft zerlegt


@pytest.mark.parametrize("name", ["Angebot „Dach“.pdf", "Angebot „Dach”.pdf", "Dušek.pdf", "Novák.pdf", "Málaga.pdf",
                                  "Lukács.pdf", "Brand™ Name.pdf", "Übergabe Müller.pdf", "Scan_00487.pdf"])
def test_echte_sonderzeichen_sind_keine_beschaedigten_umlaute(name):
    assert repair_umlauts(name) is None


def test_bericht_weist_auf_beschaedigte_umlaute_hin(tmp_path):
    root = tmp_path / "k"
    (root / "Žnderungen").mkdir(parents=True)
    digital_pdf(root / "šbergabe.pdf", [["Übergabeprotokoll Wohnung 3"]])
    digital_pdf(root / "Žnderungen" / "Mietvertrag.pdf", [["Nachtrag Mietvertrag"]])
    digital_pdf(root / "Žnderungen" / "Gr”áe.pdf", [["Wohnfläche 74 qm"]])
    digital_pdf(root / "Angebot „Dach“.pdf", [["Angebot Dachrinne"]])
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=CountingFake(), log=quiet)
    r = build_report(con)
    assert r["broken_names"] == 3                       # drei Dateien betroffen (zwei über den Ordnernamen)
    vorschlaege = {nfc(os.path.relpath(n["pfad"], root)): n["vorschlag"] for n in r["broken_name_list"]}
    assert vorschlaege == {"šbergabe.pdf": "Übergabe.pdf", "Žnderungen": "Änderungen",
                           "Žnderungen/Gr”áe.pdf": "Größe.pdf"}
    text = format_report(r)
    assert "Dateinamen mit beschädigten Umlauten: 3" in text and "vermutlich: Übergabe.pdf" in text
    assert os.path.exists(root / "šbergabe.pdf")       # nichts umbenannt
    con.close()
