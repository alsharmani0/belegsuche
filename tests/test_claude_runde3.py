"""Runde 3: Funde F5, F6, F7, F10 aus dem Code-Review vom 27.09.2026 (05-belegsuche-code-review.md)
und F4, F5 aus dem Testbericht (06-belegsuche-neue-tests.md).

Alle Daten liegen in tmp_path; der echte Index unter ~/Library/Application Support/Belegsuche wird nie
berührt (BELEGSUCHE_DB zeigt per autouse-Fixture auf tmp_path)."""
import http.client
import json
import multiprocessing as mp
import os
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from PIL import Image

from belegsuche import cli
from belegsuche import db as dbm
from belegsuche import indexer
from belegsuche import server as srv
from belegsuche.indexer import run_index
from belegsuche.ocr import OcrResult
from belegsuche.search import Searcher, parse_query

from .conftest import digital_pdf
from .test_claude_search import FILLER, build


@pytest.fixture(autouse=True)
def _nie_echter_index(tmp_path, monkeypatch):
    monkeypatch.setenv("BELEGSUCHE_DB", str(tmp_path / "env-index.db"))


def quiet(_):
    pass


# --- 05-F5: Zeitlimit für pdfium/Vision -------------------------------------------------------

@pytest.mark.vision
def test_haengende_datei_blockiert_den_parallelen_lauf_nicht(tmp_path, monkeypatch):
    root = tmp_path / "k"
    root.mkdir()
    for i in range(4):
        digital_pdf(root / f"r{i}.pdf", [[f"Rechnung Nummer {i} Wärmetechnik Brandl", *FILLER]])
    # r1.pdf "hängt" im C-Code (hält den GIL) – so wie ein hängender pdfium- oder Vision-Aufruf
    monkeypatch.setenv("BELEGSUCHE_TEST_HANG_ON", "r1.pdf")
    monkeypatch.setattr(indexer, "STALL_TIMEOUT", 3)
    con = dbm.connect(tmp_path / "i.db")
    t0 = time.time()
    _, ps = run_index(con, [str(root)], log=quiet, workers=2)
    assert time.time() - t0 < 90                      # ohne Zeitlimit: wartet unbegrenzt
    rows = {r["relpath"]: r for r in con.execute("SELECT relpath, status, error, attempts FROM files")}
    assert rows["r1.pdf"]["status"] == "error" and "Zeitlimit" in rows["r1.pdf"]["error"]
    for n in ("r0.pdf", "r2.pdf", "r3.pdf"):
        assert rows[n]["status"] == "done", n
        assert rows[n]["attempts"] == 0, n             # unschuldige Dateien: kein Versuch verbraucht
    assert ps.done == 3 and ps.errors == 1 and not ps.interrupted
    con.close()


class _LangsameOCR:
    """Ersatz-OCR für den Arbeitsprozess: jede Seite dauert `sekunden`."""

    def __init__(self, sekunden):
        self.sekunden = sekunden

    def recognize_image(self, img):
        time.sleep(self.sekunden)
        return OcrResult("Rechnung Wärmetechnik Brandl Heizungswartung Musterstadt " * 3, 0.95)

    def recognize_file(self, path):
        return self.recognize_image(None)


def _job_im_kindprozess(path, marker, stall_timeout, sekunden, ergebnis):
    indexer._WORKER_ENGINE = _LangsameOCR(sekunden)
    pages, _ = indexer._extract_job(path, marker, stall_timeout)
    with open(ergebnis, "w") as f:
        f.write(str(len(pages)))


def _mehrseitiges_tiff(path, n):
    frames = [Image.new("RGB", (400, 200), "white") for _ in range(n)]
    frames[0].save(path, save_all=True, append_images=frames[1:])


def _starte_job(tmp_path, sekunden, stall_timeout=1.5, seiten=5):
    tif = tmp_path / "fax.tif"
    _mehrseitiges_tiff(tif, seiten)
    marker, ergebnis = tmp_path / "fax.zeitlimit", tmp_path / "ergebnis.txt"
    p = mp.get_context("spawn").Process(target=_job_im_kindprozess,
                                        args=(str(tif), str(marker), stall_timeout, sekunden, str(ergebnis)))
    p.start()
    p.join(60)
    if p.is_alive():
        p.kill()
    return p.exitcode, marker, ergebnis


def test_zeitlimit_gilt_je_seite_nicht_fuer_die_ganze_datei(tmp_path):
    # 5 Seiten à 0,6 s = 3 s insgesamt, Zeitlimit 1,5 s: lange Dateien mit zügigen Seiten laufen durch
    code, marker, ergebnis = _starte_job(tmp_path, sekunden=0.6)
    assert code == 0
    assert ergebnis.read_text() == "5"
    assert not marker.exists()                         # keine Zeitüberschreitung gemeldet


def test_seite_ueber_dem_zeitlimit_beendet_den_arbeitsprozess(tmp_path):
    code, marker, ergebnis = _starte_job(tmp_path, sekunden=4, seiten=2)
    assert code not in (0, None)                       # vom Wächter beendet, nicht erst nach 8 s fertig
    assert not ergebnis.exists()
    assert indexer._timed_out(str(marker))             # der Hauptprozess erkennt die Zeitüberschreitung


# --- 05-F6: lokaler Server – fremde Webseiten (Origin/Referer, Host) ---------------------------

@pytest.fixture()
def server(tmp_path, monkeypatch):
    build(tmp_path, {"heizung.pdf": ["Rechnung Wärmetechnik Brandl Heizungswartung", *FILLER]})
    calls = []
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    searcher = Searcher(tmp_path / "i.db")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(searcher, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    con = sqlite3.connect(tmp_path / "i.db")
    fid = con.execute("SELECT id FROM files WHERE relpath = 'heizung.pdf'").fetchone()[0]
    con.close()
    yield port, fid, calls
    httpd.shutdown()
    httpd.server_close()


def _req(port, method, path, headers=None, body=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, r.read()


def _open(port, fid, headers):
    return _req(port, "POST", "/api/open", {"X-Belegsuche": "1", "Content-Type": "application/json", **headers},
                {"id": fid})


FREMD = ["http://evil.example", "https://evil.example", "http://127.0.0.1:1", "null", "http://127.0.0.1.evil.example"]


@pytest.mark.parametrize("origin", FREMD)
def test_fremde_herkunft_wird_auch_ohne_sec_fetch_site_abgelehnt(server, origin):
    # ältere Browser schicken kein Sec-Fetch-Site, aber Origin – bei POST immer
    port, fid, calls = server
    st, _ = _open(port, fid, {"Origin": origin})
    assert st == 403 and calls == []
    st, body = _req(port, "GET", "/api/search?q=Heizung", {"Origin": origin})
    assert st == 403 and b"heizung" not in body.lower()


@pytest.mark.parametrize("referer", ["http://evil.example/seite.html", "http://127.0.0.1:1/", "file:///tmp/x.html",
                                     "kaputt[::"])
def test_fremde_seite_als_referer_wird_abgelehnt(server, referer):
    port, fid, calls = server
    for path in ("/", "/api/search?q=Heizung", "/api/status", f"/datei/{fid}"):
        st, body = _req(port, "GET", path, {"Referer": referer})
        assert st == 403, path
    st, _ = _open(port, fid, {"Referer": referer})
    assert st == 403 and calls == []


def test_dns_rebinding_mit_passender_herkunft_bleibt_abgelehnt(server):
    port, fid, calls = server
    evil = f"evil.example:{port}"
    st, _ = _req(port, "POST", "/api/open", {"X-Belegsuche": "1", "Origin": f"http://{evil}"}, {"id": fid},
                 host=evil)
    assert st == 403 and calls == []
    st, _ = _req(port, "GET", "/api/search?q=Heizung", {"Sec-Fetch-Site": "same-origin"}, host=evil)
    assert st == 403


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_eigene_oberflaeche_darf_weiterhin_alles(server, host):
    port, fid, calls = server
    hp = f"{host}:{port}"
    own = {"Origin": f"http://{hp}", "Referer": f"http://{hp}/", "Sec-Fetch-Site": "same-origin"}
    st, body = _req(port, "GET", "/api/search?q=Heizung", {"Referer": f"http://{hp}/"}, host=hp)
    assert st == 200 and json.loads(body)["hits"][0]["filename"] == "heizung.pdf"
    st, _ = _req(port, "GET", f"/datei/{fid}", {"Referer": f"http://{hp}/"}, host=hp)
    assert st == 200
    st, body = _req(port, "POST", "/api/open", {"X-Belegsuche": "1", **own}, {"id": fid}, host=hp)
    assert st == 200 and json.loads(body) == {"ok": True}
    assert len(calls) == 1 and calls[0][0] == "open"


def test_start_aus_adresszeile_oder_starter_ohne_herkunft_bleibt_erlaubt(server):
    # webbrowser.open / .command-Starter / Lesezeichen: weder Origin noch Referer
    port, _, _ = server
    for h in ({}, {"Sec-Fetch-Site": "none"}):
        st, body = _req(port, "GET", "/", h)
        assert st == 200 and b"<html" in body.lower()


# --- 05-F7: Nur-Lese-Verbindung mit Sonderzeichen im Pfad --------------------------------------

@pytest.mark.parametrize("ordner", ["Belege #2", "Frage?ja", "100% Belege", "mit  Leerzeichen", "a%23b", "Ümlaut"])
def test_nur_lese_verbindung_mit_sonderzeichen_im_pfad(tmp_path, ordner):
    db = tmp_path / ordner / "index.db"
    con = dbm.connect(db)
    con.execute("INSERT INTO roots(path) VALUES ('/x')")
    con.commit()
    con.close()
    vorher = sorted(os.listdir(tmp_path))
    ro = dbm.connect(db, readonly=True)
    try:
        assert ro.execute("SELECT path FROM roots").fetchone()[0] == "/x"      # die echte Datenbank
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE fremd(x)")                                 # wirklich nur lesend
    finally:
        ro.close()
    assert sorted(os.listdir(tmp_path)) == vorher                                # keine fremde Datei angelegt


def test_suche_ueber_index_mit_raute_im_pfad(tmp_path, capsys):
    sub = tmp_path / "Belege #2"
    sub.mkdir()
    build(sub, {"heizung.pdf": ["Rechnung Wärmetechnik Brandl Heizungswartung", *FILLER]})
    rc = cli.main(["--db", str(sub / "i.db"), "suche", "Heizungswartung"])
    assert rc == 0 and "heizung.pdf" in capsys.readouterr().out


# --- 05-F10: geänderte Dateien bleiben bis zum Neu-Einlesen auffindbar --------------------------

def test_geaenderte_datei_bleibt_bis_zum_neu_einlesen_auffindbar(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    f = root / "angebot.pdf"
    digital_pdf(f, [["Angebot Dachdecker Kowalski", "Dachrinne erneuern", *FILLER]])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=None, log=quiet, workers=1)
    time.sleep(0.01)
    digital_pdf(f, [["Angebot Dachdecker Kowalski", "Schornsteinkopf verkleiden", *FILLER]])
    scans, ps = run_index(con, [], engine=None, log=quiet, workers=1, limit=0)   # erkannt, noch nicht neu gelesen
    assert scans[0].changed == 1 and ps.done == 0
    fid = con.execute("SELECT id FROM files WHERE relpath = 'angebot.pdf'").fetchone()[0]

    hits = Searcher(db).search("Dachrinne Kowalski").hits
    assert [h.filename for h in hits] == ["angebot.pdf"]                 # bisheriger Text gilt weiter …
    assert hits[0].outdated                                              # … mit Hinweis
    path, err = srv._resolve(db, fid)
    assert err is None and path == os.path.realpath(f)                   # Öffnen geht weiterhin
    assert cli.main(["--db", str(db), "suche", "Dachrinne"]) == 0
    assert "wird neu eingelesen" in capsys.readouterr().out

    run_index(con, [], engine=None, log=quiet, workers=1)                # jetzt neu eingelesen
    assert Searcher(db).search("Dachrinne").hits == []
    hits = Searcher(db).search("Schornsteinkopf Kowalski").hits
    assert [h.filename for h in hits] == ["angebot.pdf"] and not hits[0].outdated
    con.close()


@pytest.mark.parametrize("geaendert", ["a/kopie.pdf", "b/kopie.pdf"])
def test_bei_inhaltsgleichen_kopien_ist_die_aktuelle_der_haupttreffer(tmp_path, geaendert):
    # Prüfer-Korrektur: eine Kopie wurde geändert (noch nicht neu eingelesen), die andere nicht.
    # Haupttreffer (Öffnen) muss die unveränderte Kopie sein – deren Inhalt passt wirklich zur Suche.
    s = build(tmp_path, {"a/kopie.pdf": ["Angebot Dachdecker Kowalski Dachrinne", *FILLER],
                         "b/kopie.pdf": ["Angebot Dachdecker Kowalski Dachrinne", *FILLER]})
    con = sqlite3.connect(tmp_path / "i.db")
    con.execute("UPDATE files SET sha256 = 'gleich'")
    con.execute("UPDATE files SET status = 'pending' WHERE relpath = ?", (geaendert,))
    con.commit()
    con.close()
    hits = Searcher(tmp_path / "i.db").search("Dachrinne Kowalski").hits
    assert len(hits) == 1
    assert hits[0].relpath != geaendert and not hits[0].outdated
    assert len(hits[0].duplicates) == 1


def test_neue_noch_nicht_eingelesene_datei_ist_nicht_oeffenbar_ohne_index(tmp_path):
    # Gegenprobe: ohne früheren Text gibt es nichts zu finden
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "neu.pdf", [["Angebot Dachdecker Kowalski", *FILLER]])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=None, log=quiet, workers=1, limit=0)
    assert Searcher(db).search("Kowalski").hits == []
    con.close()


# --- 06-F4: „EUR“/„Euro“ neben dem Betrag ist kein eigener Suchbegriff --------------------------

@pytest.mark.parametrize("q", ["Euro 500", "euro 500", "EUR 500", "500 Euro", "Heizung Euro 500", "Euro 1.500"])
def test_waehrungswort_neben_ganzem_betrag_ist_kein_suchbegriff(q):
    labels = [g.label for g in parse_query(q)]
    assert "euro" not in labels and "eur" not in labels, labels
    assert sum(1 for g in parse_query(q) if g.kind == "amount") == 1


def test_euro_vor_ganzem_betrag_findet_nur_den_passenden_beleg(tmp_path):
    s = build(tmp_path, {
        "ziel.pdf": ["Rechnung Wärmetechnik Brandl", "Gesamtbetrag 500,00 €", *FILLER],
        "woertlich.pdf": ["Quittung Hausmeister Petersen", "Betrag Euro 500 bar erhalten", *FILLER],
        "anderer.pdf": ["Rechnung Elektro Müller", "Summe in Euro: 120,00", *FILLER],
    })
    resp = s.search("Euro 500")
    names = [h.relpath for h in resp.hits]
    assert set(names[:2]) == {"ziel.pdf", "woertlich.pdf"}
    assert all(h.missing == [] for h in resp.hits[:2])
    assert "anderer.pdf" not in names                  # enthält nur „Euro“, nicht den Betrag


def test_euro_als_einziges_wort_bleibt_suchbegriff():
    assert [g.label for g in parse_query("Euro Heizung")] == ["euro", "heizung"]


# --- 06-F5: --limit mit negativer Zahl ---------------------------------------------------------

def _drei_belege(tmp_path):
    build(tmp_path, {f"r{i}.pdf": [f"Rechnung Wärmetechnik Brandl Nummer {i}", *FILLER] for i in range(3)})
    return tmp_path / "i.db"


@pytest.mark.parametrize("wert", ["-1", "0", "abc"])
def test_suche_limit_ungueltig_wird_abgelehnt(tmp_path, capsys, wert):
    db = _drei_belege(tmp_path)
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(db), "suche", "Brandl", "--limit", wert])
    assert e.value.code == 2
    assert "--limit" in capsys.readouterr().err


def test_suche_limit_begrenzt_die_treffer(tmp_path, capsys):
    db = _drei_belege(tmp_path)
    assert cli.main(["--db", str(db), "suche", "Brandl", "--limit", "2"]) == 0
    assert "2 Treffer" in capsys.readouterr().out


@pytest.mark.parametrize("wert", ["-1", "-5"])
def test_index_limit_negativ_wird_abgelehnt(tmp_path, capsys, wert):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "r.pdf", [["Rechnung Wärmetechnik Brandl", *FILLER]])
    db = tmp_path / "i.db"
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(db), "index", str(root), "--parallel", "1", "--limit", wert])
    assert e.value.code == 2
    assert not db.exists() or dbm.connect(db).execute("SELECT count(*) FROM files").fetchone()[0] == 0
