"""Kommandozeile: Pfade, die bisher ungetestet waren (ordner entfernen/liste, bericht --csv,
test --json, index --limit/--parallel, BELEGSUCHE_DB, Fehlerfälle).

Alle Läufe arbeiten in tmp_path; BELEGSUCHE_DB zeigt immer auf tmp_path, damit ein vergessenes
--db nie den echten Index unter ~/Library/Application Support/Belegsuche berührt."""
import csv
import json
from pathlib import Path

import pytest

from belegsuche import cli
from belegsuche import db as dbm

from .conftest import digital_pdf, tree_state


@pytest.fixture(autouse=True)
def _nie_echter_index(tmp_path, monkeypatch):
    monkeypatch.setenv("BELEGSUCHE_DB", str(tmp_path / "env-index.db"))


def run(capsys, *argv):
    rc = cli.main([str(a) for a in argv])
    return rc, capsys.readouterr().out


def zwei_ordner(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    digital_pdf(a / "dach.pdf", [["Dachdecker Kowalski", "Rechnung D-9001", "Betrag 2.450,00 €"]])
    digital_pdf(b / "garten.pdf", [["Gartenpflege Grünwerk", "Rasenmähen Lindenweg 3", "Betrag 145,00 €"]])
    return a, b


# --- ordner / ordner entfernen -----------------------------------------------------------------

def test_ordner_entfernen_nimmt_nur_diesen_ordner_aus_dem_index(tmp_path, capsys):
    a, b = zwei_ordner(tmp_path)
    db = tmp_path / "i.db"
    before = {**{f"a/{k}": v for k, v in tree_state(a).items()}, **{f"b/{k}": v for k, v in tree_state(b).items()}}
    assert run(capsys, "--db", db, "index", a, "--parallel", "1")[0] == 0
    assert run(capsys, "--db", db, "index", b, "--parallel", "1")[0] == 0
    rc, out = run(capsys, "--db", db, "ordner")
    assert rc == 0 and out.count("verbunden") == 2 and "1:" in out and "2:" in out

    rc, out = run(capsys, "--db", db, "ordner", "entfernen", "1")
    assert rc == 0 and "entfernt" in out and "Originale unberührt" in out

    rc, out = run(capsys, "--db", db, "suche", "Kowalski")
    assert rc == 1 and "dach.pdf" not in out
    rc, out = run(capsys, "--db", db, "suche", "Grünwerk")
    assert rc == 0 and "garten.pdf" in out
    rc, out = run(capsys, "--db", db, "ordner")
    assert "1:" not in out and "2:" in out
    con = dbm.connect(db)
    try:
        # keine Waisen: Dateien, Seiten und Suchzeilen des entfernten Ordners sind weg
        assert con.execute("SELECT count(*) FROM files WHERE root_id = 1").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM pages p LEFT JOIN files f ON f.id = p.file_id"
                           " WHERE f.id IS NULL").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM fts").fetchone()[0] == \
            con.execute("SELECT count(*) FROM pages").fetchone()[0]
    finally:
        con.close()
    after = {**{f"a/{k}": v for k, v in tree_state(a).items()}, **{f"b/{k}": v for k, v in tree_state(b).items()}}
    assert after == before                               # Originale unverändert


def test_entfernter_ordner_kann_neu_eingelesen_werden(tmp_path, capsys):
    a, _ = zwei_ordner(tmp_path)
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", a, "--parallel", "1")
    run(capsys, "--db", db, "ordner", "entfernen", "1")
    rc, _ = run(capsys, "--db", db, "index", a, "--parallel", "1")
    assert rc == 0
    rc, out = run(capsys, "--db", db, "suche", "Kowalski")
    assert rc == 0 and "dach.pdf" in out


def test_ordner_entfernen_unbekannte_id_und_ohne_id(tmp_path, capsys):
    a, _ = zwei_ordner(tmp_path)
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", a, "--parallel", "1")
    rc, out = run(capsys, "--db", db, "ordner", "entfernen", "9")
    assert rc == 1 and "Unbekannte Ordner-ID 9" in out
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(db), "ordner", "entfernen"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(db), "ordner", "umziehen", "1"])
    assert e.value.code == 2


def test_ordner_liste_zeigt_nicht_verbundenen_ordner(tmp_path, capsys):
    a, _ = zwei_ordner(tmp_path)
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", a, "--parallel", "1")
    a.rename(tmp_path / "a_abgezogen")
    rc, out = run(capsys, "--db", db, "ordner")
    assert rc == 0 and "NICHT verbunden" in out
    # README: Suche funktioniert weiter, auch wenn der Ordner nicht verbunden ist
    rc, out = run(capsys, "--db", db, "suche", "Kowalski")
    assert rc == 0 and "dach.pdf" in out and "[nicht verbunden]" in out


def test_ordner_umziehen_erfolgreich(tmp_path, capsys):
    a, _ = zwei_ordner(tmp_path)
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", a, "--parallel", "1")
    neu = tmp_path / "SSD 1"
    a.rename(neu)
    rc, out = run(capsys, "--db", db, "ordner", "umziehen", "1", neu)
    assert rc == 0 and "zeigt jetzt auf" in out
    rc, out = run(capsys, "--db", db, "index", "--parallel", "1")
    assert rc == 0 and "0 neu" in out
    rc, out = run(capsys, "--db", db, "suche", "Kowalski")
    assert rc == 0 and "SSD 1" in out and "[nicht verbunden]" not in out


# --- index -------------------------------------------------------------------------------------

def test_index_nicht_vorhandener_ordner(tmp_path, capsys):
    db = tmp_path / "i.db"
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(db), "index", str(tmp_path / "gibtsnicht"), "--parallel", "1"])
    assert "Ordner nicht gefunden" in str(e.value.code)
    con = dbm.connect(db)
    try:
        assert con.execute("SELECT count(*) FROM roots").fetchone()[0] == 0
    finally:
        con.close()


def test_index_limit_ist_fortsetzbar(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    for i in range(3):
        digital_pdf(root / f"r{i}.pdf", [[f"Beleg Nummer {i}", "Wärmetechnik Brandl GmbH"]])
    db = tmp_path / "i.db"
    rc, out = run(capsys, "--db", db, "index", root, "--parallel", "1", "--limit", "1")
    assert rc == 0 and "Fertig: 1 Dateien" in out
    rc, out = run(capsys, "--db", db, "bericht")
    assert rc == 0 and "noch offen:              2" in out
    rc, out = run(capsys, "--db", db, "index", "--parallel", "1")
    assert rc == 0 and "Fertig: 2 Dateien" in out
    rc, out = run(capsys, "--db", db, "bericht")
    assert "noch offen:              0" in out and "durchsuchbar:            3" in out


@pytest.mark.xfail(strict=True, reason="Bug: --limit mit negativer Zahl wird als Python-Slice todo[:-1] "
                                         "ausgewertet und verarbeitet alle Dateien bis auf die letzte")
def test_index_negatives_limit_verarbeitet_nichts_oder_wird_abgelehnt(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    for i in range(3):
        digital_pdf(root / f"r{i}.pdf", [[f"Beleg Nummer {i}", "Wärmetechnik Brandl GmbH"]])
    db = tmp_path / "i.db"
    try:
        run(capsys, "--db", db, "index", root, "--parallel", "1", "--limit", "-1")
    except SystemExit:
        return                                           # Ablehnen wäre in Ordnung
    con = dbm.connect(db)
    try:
        done = con.execute("SELECT count(*) FROM files WHERE status = 'done'").fetchone()[0]
    finally:
        con.close()
    assert done == 0                                     # "höchstens -1 Dateien" = keine


def test_index_parallel_null_wird_als_eins_behandelt(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "r.pdf", [["Dachdecker Kowalski", "Rechnung D-9001"]])
    db = tmp_path / "i.db"
    rc, out = run(capsys, "--db", db, "index", root, "--parallel", "0")
    assert rc == 0 and "Fertig: 1 Dateien" in out


def test_index_ohne_ordner_und_ohne_bekannte_ordner(tmp_path, capsys):
    rc, out = run(capsys, "--db", tmp_path / "i.db", "index", "--parallel", "1")
    assert rc == 0 and "Fertig: 0 Dateien" in out


def test_belegsuche_db_umgebungsvariable_ist_standard(tmp_path, capsys, monkeypatch):
    env_db = tmp_path / "anderswo" / "index.db"
    monkeypatch.setenv("BELEGSUCHE_DB", str(env_db))
    assert dbm.default_db_path() == env_db
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "r.pdf", [["Dachdecker Kowalski", "Rechnung D-9001"]])
    rc, _ = run(capsys, "index", root, "--parallel", "1")
    assert rc == 0 and env_db.exists()
    rc, out = run(capsys, "suche", "Kowalski")
    assert rc == 0 and "r.pdf" in out


# --- suche -------------------------------------------------------------------------------------

def test_suche_ohne_treffer_rc1_und_unbekannte_woerter(tmp_path, capsys):
    a, _ = zwei_ordner(tmp_path)
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", a, "--parallel", "1")
    rc, out = run(capsys, "--db", db, "suche", "Zzyzxquux")
    assert rc == 1 and "Nichts gefunden." in out and "Kommt in keinem Dokument vor: zzyzxquux" in out
    rc, out = run(capsys, "--db", db, "suche", "Kowalski", "Zzyzxquux")
    assert rc == 0 and "dach.pdf" in out and "Kommt in keinem Dokument vor: zzyzxquux" in out
    rc, out = run(capsys, "--db", db, "suche", "Kowalski", "Grünwerk")    # Grünwerk steht in b (nicht eingelesen)
    assert rc == 0 and "dach.pdf" in out


def test_suche_limit(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    for i in range(4):
        digital_pdf(root / f"r{i}.pdf", [[f"Dachdecker Kowalski Rechnung {i}"]])
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", root, "--parallel", "1")
    rc, out = run(capsys, "--db", db, "suche", "Kowalski", "--limit", "2")
    assert rc == 0 and "2 Treffer" in out


# --- bericht --csv -----------------------------------------------------------------------------

def test_bericht_csv_mit_problemfaellen(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "gut.pdf", [["Dachdecker Kowalski", "Rechnung D-9001"]])
    digital_pdf(root / "geheim.pdf", [["Vertraulich"]], password="1234")
    good = (root / "gut.pdf").read_bytes()
    (root / "kaputt.pdf").write_bytes(good[: len(good) // 3])
    (root / "Notiz.docx").write_bytes(b"PK\x03\x04 kein echtes word")
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", root, "--parallel", "1")
    ziel = tmp_path / "probleme.csv"
    rc, out = run(capsys, "--db", db, "bericht", "--csv", ziel)
    assert rc == 0 and "Problemliste gespeichert" in out
    assert "nicht unterstützt:       1  (.docx 1)" in out
    raw = ziel.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                # Excel erkennt UTF-8 an der BOM
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines(), delimiter=";"))
    assert rows[0] == ["pfad", "status", "grund"]
    by_name = {Path(r[0]).name: r for r in rows[1:]}
    assert by_name["geheim.pdf"][1:] == ["error", "PDF ist passwortgeschützt"]
    assert by_name["kaputt.pdf"][1] == "error" and by_name["kaputt.pdf"][2].startswith("PDF beschädigt")
    assert "gut.pdf" not in by_name
    assert (root / "kaputt.pdf").read_bytes() == good[: len(good) // 3]   # Original unverändert


# --- test --json -------------------------------------------------------------------------------

def test_test_befehl_mit_json_und_ursachen(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "dach.pdf", [["Dachdecker Kowalski", "Rechnung D-9001", "Sehr geehrte Damen und Herren"],
                                           ["Anlage Aufmaß der Dachflächen", "Mit freundlichen Grüßen"]])
    digital_pdf(root / "geheim.pdf", [["Vertraulich"]], password="1234")
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", root, "--parallel", "1")
    fragen = tmp_path / "fragen.csv"
    fragen.write_text("suche;datei;seite;notiz\n"
                      "Kowalski;dach.pdf;1;ok\n"
                      "Aufmaß;dach.pdf;1;falsche Seite\n"
                      "Hubschrauber;dach.pdf;;Wortwahl\n"
                      "Vertraulich;geheim.pdf;;Text/Format\n"
                      "Kowalski;gibtsnicht.pdf;;Index\n", encoding="utf-8")
    out_json = tmp_path / "ergebnis.json"
    rc, out = run(capsys, "--db", db, "test", fragen, "--json", out_json)
    assert rc == 0
    assert "Platz 1:        2/5 (40%)" in out and "Falsche Seite:  1" in out
    data = json.loads(out_json.read_text(encoding="utf-8"))
    s, res = data["summary"], data["results"]
    assert s["anfragen"] == 5 and s["platz_1"] == 2 and s["nicht_gefunden"] == 3
    assert s["seite_falsch"] == 1
    assert s["ursachen"] == {"Wortwahl": 1, "Text/Format": 1, "Index": 1}
    kat = [r["kategorie"] for r in res]
    assert kat == ["ok", "ok", "Wortwahl", "Text/Format", "Index"]
    assert res[1]["ursache"] == "gefunden, aber Seite 2 statt 1"
    assert res[3]["ursache"] == "Fehler beim Einlesen: PDF ist passwortgeschützt"
    assert res[0]["notiz"] == "ok"


def test_test_befehl_k_parameter(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    for i in range(3):
        digital_pdf(root / f"r{i}.pdf", [[f"Dachdecker Kowalski Rechnung {i}"]])
    digital_pdf(root / "ziel.pdf", [["Dachdecker Kowalski Rechnung", "Dachrinne"]])
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", root, "--parallel", "1")
    fragen = tmp_path / "f.csv"
    fragen.write_text("suche;datei\nDachrinne Kowalski;ziel.pdf\n", encoding="utf-8")
    rc, out = run(capsys, "--db", db, "test", fragen, "--k", "1")
    assert rc == 0 and "Unter Top 1:    1/1 (100%)" in out


# --- start -------------------------------------------------------------------------------------

def test_start_mit_unmoeglichem_port(tmp_path, capsys, monkeypatch):
    from belegsuche import server as srv
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: pytest.fail("kein Browser"))
    a, _ = zwei_ordner(tmp_path)
    db = tmp_path / "i.db"
    run(capsys, "--db", db, "index", a, "--parallel", "1")
    rc, out = run(capsys, "--db", db, "start", "--port", "70000", "--kein-browser")
    assert rc == 1 and "--port" in out
