"""Suchoberfläche: Endpunkte und Fehlerpfade, die bisher ungetestet waren
(/api/status bei abgezogener SSD, limit-Parameter, Öffnen/Ausliefern bei fehlendem Ordner oder
gelöschter Datei, Bilder, unbekannte POST-Pfade). Schnell: Ersatz-OCR statt Vision."""
import http.client
import json
import os
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from belegsuche import db as dbm
from belegsuche import server as srv
from belegsuche.indexer import run_index
from belegsuche.ocr import OcrResult
from belegsuche.search import Searcher

from .conftest import digital_pdf, text_image


class FakeEngine:
    def recognize_image(self, img):
        return OcrResult("Quittung Baumarkt Nord Silikon Schrauben Summe 11,48 EUR", 0.9)

    def recognize_file(self, path):
        return self.recognize_image(None)


@pytest.fixture()
def server(tmp_path, monkeypatch):
    root = tmp_path / "belege"
    (root / "2021").mkdir(parents=True)
    for i in range(5):
        digital_pdf(root / "2021" / f"rechnung{i}.pdf",
                    [[f"Dachdecker Kowalski Rechnung D-900{i}", "Objekt Lindenweg 3, Musterstadt"]])
    text_image(["Baumarkt Nord"], size=(300, 200), font_size=20).save(root / "bon.png")
    db_path = tmp_path / "i.db"
    con = dbm.connect(db_path)
    run_index(con, [str(root)], engine=FakeEngine(), log=lambda s: None)
    calls = []
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: calls.append(["browser", url]))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(Searcher(db_path), port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"port": port, "calls": calls, "con": con, "root": root, "tmp": tmp_path}
    httpd.shutdown()
    httpd.server_close()
    con.close()


def req(port, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, r.read(), r.headers


def fid(con, name):
    return con.execute("SELECT id FROM files WHERE relpath LIKE ?", (f"%{name}",)).fetchone()[0]


# --- /api/search -------------------------------------------------------------------------------

def test_search_liefert_trefferfelder(server):
    st, body, headers = req(server["port"], "GET", "/api/search?q=" + quote("Kowalski D-9003"))
    assert st == 200 and headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == "no-store"
    data = json.loads(body)
    h = data["hits"][0]
    assert h["filename"] == "rechnung3.pdf" and h["folder"] == "2021" and h["page"] == 1
    assert h["pdf"] is True and h["available"] is True and h["ocr"] is False
    assert "<mark>" in h["snippet"] and h["missing"] == []
    assert set(h) >= {"id", "root", "pages", "score", "matched", "duplicates"}


@pytest.mark.parametrize("limit,erwartet", [("1", 1), ("2", 2), ("0", 1), ("-5", 1), ("abc", 5), ("1e3", 5),
                                            ("100000", 5), ("", 5)])
def test_search_limit_wird_begrenzt(server, limit, erwartet):
    st, body, _ = req(server["port"], "GET", f"/api/search?q=Kowalski&limit={limit}")
    assert st == 200
    assert len(json.loads(body)["hits"]) == erwartet


def test_search_sehr_lange_und_leere_anfrage(server):
    st, body, _ = req(server["port"], "GET", "/api/search?q=" + quote("Kowalski " * 2000))
    assert st == 200 and json.loads(body)["hits"]
    for q in ("", "%20%20", quote('"'), quote("*"), quote("NEAR(a b)")):
        st, body, _ = req(server["port"], "GET", f"/api/search?q={q}")
        assert st == 200, q
        assert isinstance(json.loads(body)["hits"], list)
    st, body, _ = req(server["port"], "GET", "/api/search")          # ganz ohne q
    assert st == 200 and json.loads(body)["hits"] == []


def test_bild_treffer_ist_ocr_und_kein_pdf(server):
    st, body, _ = req(server["port"], "GET", "/api/search?q=Silikon")
    h = json.loads(body)["hits"][0]
    assert h["filename"] == "bon.png" and h["ocr"] is True and h["pdf"] is False


# --- /api/status -------------------------------------------------------------------------------

def test_status_zeigt_dateien_und_verbindung(server):
    st, body, _ = req(server["port"], "GET", "/api/status")
    data = json.loads(body)
    assert st == 200 and data["files"] == 6
    assert data["roots"] == [{"path": os.path.realpath(server["root"]), "online": True}]
    server["root"].rename(server["tmp"] / "abgezogen")
    st, body, _ = req(server["port"], "GET", "/api/status")
    assert st == 200 and json.loads(body)["roots"][0]["online"] is False


# --- abgezogene SSD / gelöschte Datei --------------------------------------------------------

def test_abgezogene_ssd_suche_geht_oeffnen_nicht(server):
    port, con, calls = server["port"], server["con"], server["calls"]
    f = fid(con, "rechnung2.pdf")
    server["root"].rename(server["tmp"] / "abgezogen")
    # README: Die Suche funktioniert weiter, nur Öffnen geht erst nach dem Anschließen
    st, body, _ = req(port, "GET", "/api/search?q=" + quote("D-9002"))
    h = json.loads(body)["hits"][0]
    assert st == 200 and h["filename"] == "rechnung2.pdf" and h["available"] is False
    st, body, _ = req(port, "GET", f"/datei/{f}")
    assert st == 404 and "nicht verbunden" in json.loads(body)["error"]
    st, body, _ = req(port, "POST", "/api/open", {"id": f}, {"X-Belegsuche": "1"})
    assert st == 404 and "nicht verbunden" in json.loads(body)["error"]
    assert calls == []


def test_geloeschte_datei_wird_nicht_ausgeliefert(server):
    port, con, calls = server["port"], server["con"], server["calls"]
    f = fid(con, "rechnung1.pdf")
    os.remove(server["root"] / "2021" / "rechnung1.pdf")
    st, body, _ = req(port, "GET", f"/datei/{f}")
    assert st == 404 and "nicht mehr vorhanden" in json.loads(body)["error"]
    st, body, _ = req(port, "POST", "/api/open", {"id": f, "reveal": True}, {"X-Belegsuche": "1"})
    assert st == 404 and calls == []


def test_bild_wird_mit_passendem_typ_ausgeliefert(server):
    f = fid(server["con"], "bon.png")
    st, body, headers = req(server["port"], "GET", f"/datei/{f}")
    assert st == 200 and headers["Content-Type"] == "image/png" and body.startswith(b"\x89PNG")
    assert headers["Content-Disposition"] == "inline"
    assert int(headers["Content-Length"]) == len(body) == (server["root"] / "bon.png").stat().st_size


def test_oeffnen_ohne_reveal_nutzt_open(server):
    f = fid(server["con"], "rechnung0.pdf")
    st, body, _ = req(server["port"], "POST", "/api/open", {"id": f, "reveal": "ja"}, {"X-Belegsuche": "1"})
    # nur reveal=true (JSON-Boolean) zeigt im Finder, alles andere öffnet die Datei
    assert st == 200 and json.loads(body) == {"ok": True}
    assert server["calls"][0][0] == "open" and server["calls"][0][1].endswith("rechnung0.pdf")


# --- unbekannte Pfade --------------------------------------------------------------------------

def test_unbekannte_pfade(server):
    port = server["port"]
    st, body, _ = req(port, "GET", "/index.html")
    assert st == 404 and json.loads(body)["error"] == "not found"
    st, _, _ = req(port, "POST", "/api/search", {"q": "x"}, {"X-Belegsuche": "1"})
    assert st == 404
    st, _, _ = req(port, "POST", "/api/search", {"q": "x"})
    assert st == 403
    st, _, _ = req(port, "GET", "/datei/")
    assert st == 400
    st, _, _ = req(port, "GET", "/api/search?q=x", host="localhost:%d" % port)
    assert st == 200                                      # localhost ist erlaubt
    st, _, _ = req(port, "GET", "/api/search?q=x", host="127.0.0.1")   # ohne Port: nicht erlaubt
    assert st == 403
    assert server["calls"] == []
