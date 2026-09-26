"""Härtung der Suchoberfläche (fremde Seiten, kaputte Eingaben, Symlink-Wettlauf, belegter Port)
und robuste Testdatei für »belegsuche test«."""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from belegsuche import server as srv
from belegsuche.evaluate import load_queries
from belegsuche.search import Searcher


@pytest.fixture()
def running(indexed, monkeypatch):
    root, files, db_path, con, _ = indexed
    calls = []
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: calls.append(["browser", url]))
    searcher = Searcher(db_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(searcher, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"port": port, "calls": calls, "con": con, "files": files, "root": root,
           "db": db_path, "searcher": searcher, "httpd": httpd}
    httpd.shutdown()
    httpd.server_close()


def req(port, method, path, body=None, headers=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = raw if raw is not None else (json.dumps(body) if body is not None else None)
    c.request(method, path, body=data, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    r = c.getresponse()
    return r.status, r.read(), r.headers


def raw_request(port, data: bytes, timeout=5) -> bytes:
    """Schickt Rohdaten, ohne die Senderichtung zu schließen, und liest die Antwort."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
        return out


def file_id(con, name):
    return con.execute("SELECT id FROM files WHERE relpath LIKE ?", (f"%{name}",)).fetchone()[0]


# --- Fremde Webseiten, Einrahmen, Sicherheits-Header ------------------------------------------

@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_foreign_sites_get_403_everywhere(running, site):
    port, con, calls = running["port"], running["con"], running["calls"]
    fid = file_id(con, "Dokument (3).pdf")
    for path in ["/", "/api/search?q=Heizung", "/api/status", f"/datei/{fid}"]:
        st, body, _ = req(port, "GET", path, headers={"Sec-Fetch-Site": site})
        assert st == 403, path
        assert b"%PDF" not in body
    st, _, _ = req(port, "POST", "/api/open", {"id": fid}, {"Sec-Fetch-Site": site, "X-Belegsuche": "1"})
    assert st == 403 and not calls


@pytest.mark.parametrize("site", [None, "same-origin", "none"])
def test_own_tab_and_address_bar_allowed(running, site):
    port, con = running["port"], running["con"]
    h = {"Sec-Fetch-Site": site} if site else {}
    st, body, _ = req(port, "GET", "/api/search?q=RE-2021%2F00487", headers=h)
    assert st == 200 and json.loads(body)["hits"][0]["filename"] == "Dokument (3).pdf"
    st, body, _ = req(port, "GET", f"/datei/{file_id(con, 'Dokument (3).pdf')}", headers=h)
    assert st == 200 and body.startswith(b"%PDF")


def test_security_headers_on_every_response(running):
    port, con = running["port"], running["con"]
    fid = file_id(con, "Dokument (3).pdf")
    cases = [("/", {}), ("/api/search?q=Heizung", {}), ("/api/status", {}), (f"/datei/{fid}", {}),
             ("/datei/abc", {}), ("/gibt-es-nicht", {}), ("/api/search?q=x", {"Sec-Fetch-Site": "cross-site"})]
    for path, h in cases:
        _, _, headers = req(port, "GET", path, headers=h)
        assert headers["X-Frame-Options"] == "DENY", path
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"], path
        assert headers["X-Content-Type-Options"] == "nosniff", path
    _, _, headers = req(port, "GET", "/")
    csp = headers["Content-Security-Policy"]
    # Inline-Script/-Style der Oberfläche bleiben erlaubt, sonst nichts von außen
    assert "default-src 'none'" in csp and "script-src 'unsafe-inline'" in csp
    assert "style-src 'unsafe-inline'" in csp and "connect-src 'self'" in csp


# --- Kaputte IDs und Nicht-Objekt-JSON -------------------------------------------------------

@pytest.mark.parametrize("raw", [
    b"[1]", b'"abc"', b"1e400", b"null", b"{", b"\xff\xfe", b"[" * 5000,
    b'{"id": 1e400}', b'{"id": 99999999999999999999}', b'{"id": 1e20}', b'{"id": 9223372036854775808}',
    b'{"id": true}', b'{"id": 1.9}', b'{"id": " 3 "}', b'{"id": "3"}', b'{"id": 0}', b'{"id": -1}', b"{}",
])
def test_open_rejects_malformed_body(running, raw):
    st, body, _ = req(running["port"], "POST", "/api/open", raw=raw, headers={"X-Belegsuche": "1"})
    assert st == 400 and json.loads(body)["error"]
    assert not running["calls"]


def test_open_accepts_largest_sqlite_id(running):
    st, body, _ = req(running["port"], "POST", "/api/open", raw=b'{"id": 9223372036854775807}',
                      headers={"X-Belegsuche": "1"})
    assert st == 404 and not running["calls"]


def test_datei_rejects_malformed_ids(running):
    port, con = running["port"], running["con"]
    fid = file_id(con, "Dokument (3).pdf")
    for path in ["/datei/99999999999999999999", "/datei/1234567890123456789", f"/datei/+{fid}",
                 f"/datei/{fid}.0", f"/datei/{fid}/x", "/datei/0", "/datei/", "/datei/abc", "/datei/%31"]:
        st, body, _ = req(port, "GET", path)
        assert st == 400, path
        assert b"%PDF" not in body


def test_internal_error_gives_json_500_without_traceback(running, monkeypatch, capsys):
    def boom(*a, **kw):
        raise OverflowError("kaputt")
    monkeypatch.setattr(running["searcher"], "search", boom)
    st, body, headers = req(running["port"], "GET", "/api/search?q=geheimer+Name")
    assert st == 500 and json.loads(body) == {"error": "interner Fehler"}
    assert headers["X-Frame-Options"] == "DENY"
    err = capsys.readouterr().err
    assert "Fehler bei GET /api/search: OverflowError" in err
    assert "Traceback" not in err and "geheimer" not in err


# --- Content-Length und hängende Verbindungen ------------------------------------------------

def test_negative_or_huge_content_length_rejected(running):
    port, con = running["port"], running["con"]
    fid = file_id(con, "Dokument (3).pdf")
    body = json.dumps({"id": fid}).encode()
    for length in ["-1", "20000", "abc"]:
        head = (f"POST /api/open HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nX-Belegsuche: 1\r\n"
                f"Content-Length: {length}\r\n\r\n").encode()
        # Senderichtung bleibt offen: ohne Prüfung würde der Server bis zum Verbindungsende lesen
        out = raw_request(port, head + body)
        assert out.startswith(b"HTTP/1.0 400"), (length, out[:80])
    assert not running["calls"]


def test_handler_has_socket_timeout(running, monkeypatch, capsys):
    handler = running["httpd"].RequestHandlerClass
    assert handler.timeout == 15
    monkeypatch.setattr(handler, "timeout", 0.5)
    port = running["port"]
    t0 = time.monotonic()
    # halber Kopf und unvollständiger Körper: der Server gibt die Verbindung selbst auf
    for data in [b"GET /api/sta", f"POST /api/open HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nX-Belegsuche: 1\r\n"
                                  f"Content-Length: 100\r\n\r\n{{\"id\"".encode()]:
        assert raw_request(port, data, timeout=5) == b""
    assert time.monotonic() - t0 < 4
    assert "Traceback" not in capsys.readouterr().err
    assert not running["calls"]


# --- Symlink-Wettlauf zwischen Prüfung und Öffnen ---------------------------------------------

def test_resolve_returns_real_path(running):
    con, files = running["con"], running["files"]
    link = running["root"].parent / "verknuepfung"
    link.symlink_to(running["root"])
    con.execute("UPDATE roots SET path = ?", (str(link),))
    con.commit()
    fid = file_id(con, "Dokument (3).pdf")
    path, err = srv._resolve(running["db"], fid)
    assert err is None and path == os.path.realpath(files["heizung"])
    st, _, _ = req(running["port"], "POST", "/api/open", {"id": fid}, {"X-Belegsuche": "1"})
    assert st == 200 and running["calls"][0] == ["open", os.path.realpath(files["heizung"])]


def _race(monkeypatch, swap):
    """Führt swap() genau zwischen Pfadprüfung und Öffnen aus."""
    orig = srv._resolve

    def racing(db_path, fid):
        result = orig(db_path, fid)
        swap()
        return result
    monkeypatch.setattr(srv, "_resolve", racing)


def test_swapped_symlink_is_not_followed(running, monkeypatch, tmp_path):
    target = running["files"]["heizung"]
    secret = tmp_path / "draussen" / "geheim.pdf"
    secret.parent.mkdir()
    secret.write_bytes(b"%PDF GEHEIM")
    tmp_link = target.with_name("tausch")

    def swap():
        tmp_link.symlink_to(secret)
        os.replace(tmp_link, target)
    _race(monkeypatch, swap)
    st, body, _ = req(running["port"], "GET", f"/datei/{file_id(running['con'], 'Dokument (3).pdf')}")
    assert st == 404 and b"GEHEIM" not in body


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW_ANY"), reason="nur macOS")
def test_swapped_parent_folder_is_not_followed(running, monkeypatch, tmp_path):
    folder = running["files"]["heizung"].parent
    outside = tmp_path / "draussen"
    outside.mkdir()
    (outside / "Dokument (3).pdf").write_bytes(b"%PDF GEHEIM")

    def swap():
        folder.rename(folder.with_name("weg"))
        folder.symlink_to(outside, target_is_directory=True)
    _race(monkeypatch, swap)
    st, body, _ = req(running["port"], "GET", f"/datei/{file_id(running['con'], 'Dokument (3).pdf')}")
    assert st == 404 and b"GEHEIM" not in body


def test_swapped_fifo_does_not_block(running, monkeypatch):
    target = running["files"]["heizung"]

    def swap():
        target.unlink()
        os.mkfifo(target)
    _race(monkeypatch, swap)
    st, body, _ = req(running["port"], "GET", f"/datei/{file_id(running['con'], 'Dokument (3).pdf')}")
    assert st == 404


# --- Belegter Port, Port 0 --------------------------------------------------------------------

def test_serve_port_in_use_gives_hint(indexed, monkeypatch, capsys):
    db_path = indexed[2]
    opened = []
    monkeypatch.setattr(srv.webbrowser, "open", opened.append)
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **kw: pytest.fail("kein Programmstart"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert srv.serve(db_path, port=port, open_browser=True) == 1
    url = f"http://127.0.0.1:{port}/"
    out = capsys.readouterr().out
    assert f"Belegsuche läuft vermutlich schon: {url}" in out and "--port" in out
    assert opened == [url]


def test_serve_port_zero_uses_real_port(indexed, monkeypatch, capsys):
    db_path = indexed[2]
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: pytest.fail("kein Browser"))
    servers, result = [], []

    class Capture(ThreadingHTTPServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            servers.append(self)
    monkeypatch.setattr(srv, "ThreadingHTTPServer", Capture)
    t = threading.Thread(target=lambda: result.append(srv.serve(db_path, port=0, open_browser=False)), daemon=True)
    t.start()
    for _ in range(100):
        if servers:
            break
        time.sleep(0.05)
    port = servers[0].server_address[1]
    assert port != 0
    st, body, _ = req(port, "GET", "/")
    assert st == 200 and b"Belegsuche" in body
    servers[0].shutdown()
    t.join(5)
    assert result == [0]
    assert f"http://127.0.0.1:{port}/" in capsys.readouterr().out


# --- Oberfläche bei Fehlern --------------------------------------------------------------------

UI = srv.UI_FILE.read_text(encoding="utf-8")

NODE_PRELUDE = r"""
const els = {};
function mkEl(id) {
  return {id, value: '', innerHTML: '', className: '', children: [],
          // wie im Browser: textContent und innerHTML sind derselbe Inhalt
          get textContent() { return this.innerHTML.replace(/<[^>]*>/g, '').replace(/&lt;/g, '<').replace(/&amp;/g, '&'); },
          set textContent(v) { this.innerHTML = String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;'); },
          addEventListener() {}, appendChild(c) { if (!this.children.includes(c)) this.children.push(c); },
          querySelector(sel) { return this.children.find(c => '.' + c.className === sel) || null; }};
}
globalThis.document = {getElementById: (id) => els[id] ||= mkEl(id), createElement: () => mkEl(null)};
let handler = async () => { throw new TypeError('Failed to fetch'); };
globalThis.fetch = (url, opts) => handler(url, opts);
const okJson = (obj) => ({ok: true, status: 200, json: async () => obj});
"""

NODE_SCENARIO = r"""
(async () => {
  const res = {};
  const hit = {id: 1, filename: 'alt.pdf', folder: 'x', root: '/r', page: 1, pages: 1, snippet: 's',
               matched: [], missing: [], ocr: false, available: true, duplicates: [], pdf: true, note: 'x'};
  const good = async () => okJson({query: 'a', ms: 3, unknown: [], hits: [hit]});
  const view = () => ({results: $('results').innerHTML, meta: $('meta').textContent});

  handler = good; $('q').value = 'Heizung'; await search();
  res.first = view();
  handler = async () => { throw new TypeError('Failed to fetch'); };
  $('q').value = 'Optionstest'; await search();
  res.offline = view();

  handler = good; await search();
  handler = async () => ({ok: false, status: 500, json: async () => ({error: 'interner Fehler'})});
  await search();
  res.http500 = view();

  // veraltete Antwort: die erste Anfrage scheitert erst, nachdem die zweite fertig ist
  let fail;
  handler = () => new Promise((_, rej) => { fail = rej; });
  const p1 = search();
  handler = good; await search();
  fail(new TypeError('Failed to fetch')); await p1;
  res.stale = view();

  const actions = mkEl(null), btn = {parentElement: actions};
  handler = async () => { throw new TypeError('Failed to fetch'); };
  await openFile(1, false, btn);
  res.openOffline = actions.children.map(c => c.textContent);
  handler = async () => ({ok: false, status: 404, json: async () => ({error: 'Unbekannte Datei'})});
  await openFile(1, false, btn);
  res.open404 = actions.children.map(c => c.textContent);
  handler = async () => ({ok: false, status: 502, json: async () => { throw new SyntaxError('kein JSON'); }});
  await openFile(1, false, btn);
  res.open502 = actions.children.map(c => c.textContent);
  handler = async () => okJson({ok: true});
  await openFile(1, false, btn);
  res.openOk = actions.children.map(c => c.textContent);
  console.log(JSON.stringify(res));
})();
"""


def test_ui_script_handles_failed_fetch():
    script = re.search(r"<script>(.*)</script>", UI, re.S).group(1)
    body = script.split("async function search()")[1]
    assert "Suche fehlgeschlagen" in body and "läuft „belegsuche start“ noch?" in script
    assert "resp.ok" in body and "catch" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node nicht installiert")
def test_ui_clears_old_hits_on_error():
    script = re.search(r"<script>(.*)</script>", UI, re.S).group(1)
    p = subprocess.run(["node", "-e", NODE_PRELUDE + script + NODE_SCENARIO],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    res = json.loads(p.stdout.strip().splitlines()[-1])
    msg = "Suche fehlgeschlagen – läuft „belegsuche start“ noch?"
    assert "alt.pdf" in res["first"]["results"]
    assert res["offline"] == {"results": "", "meta": msg}
    assert res["http500"] == {"results": "", "meta": msg}
    assert "alt.pdf" in res["stale"]["results"] and res["stale"]["meta"].startswith("1 Treffer")
    assert res["openOffline"] == ["Öffnen fehlgeschlagen – läuft „belegsuche start“ noch?"]
    assert res["open404"] == ["Unbekannte Datei"]
    assert res["open502"] == ["Öffnen fehlgeschlagen (Fehler 502)"]
    assert res["openOk"] == [""]


# --- Testdatei für »belegsuche test« -----------------------------------------------------------

def _csv(tmp_path, content, encoding="utf-8"):
    p = tmp_path / "fragen.csv"
    p.write_bytes(content if isinstance(content, bytes) else content.encode(encoding))
    return p


def test_queries_extra_fields_go_to_note(tmp_path):
    p = _csv(tmp_path, "suche;datei;seite;notiz\r\nHeizung;a.pdf;1;Nummer; mit Bindestrich\r\nMüller;b.pdf;;\r\n")
    rows = load_queries(p)
    assert [r["suche"] for r in rows] == ["Heizung", "Müller"]
    assert rows[0]["notiz"] == "Nummer; mit Bindestrich" and None not in rows[0]


@pytest.mark.parametrize("encoding", ["cp1252", "mac_roman", "utf-16", "utf-8-sig"])
def test_queries_common_encodings(tmp_path, encoding):
    text = "suche;datei;notiz\nMüller Straße Größe;Ordner/Übersicht.pdf;für Ärger\n"
    rows = load_queries(_csv(tmp_path, text, encoding))
    assert rows == [{"suche": "Müller Straße Größe", "datei": "Ordner/Übersicht.pdf", "notiz": "für Ärger"}]


def test_queries_other_delimiters_and_leading_blank_lines(tmp_path):
    assert load_queries(_csv(tmp_path, "\n\nsuche,datei\nHeizung,a.pdf\n"))[0]["datei"] == "a.pdf"
    assert load_queries(_csv(tmp_path, "Suche\tDatei\nHeizung\ta.pdf\n"))[0]["suche"] == "Heizung"


@pytest.mark.parametrize("content, found", [
    ("", "nichts"),
    ("\n \n", "nichts"),
    ("suche\nHeizung\n", "gefunden: suche"),
    ("suche;pfad\nHeizung;a.pdf\n", "gefunden: suche, pfad"),
    ("Wartung Heizung;2021/a.pdf;1\n", "gefunden: Wartung Heizung, 2021/a.pdf, 1"),
])
def test_queries_missing_columns_explained(tmp_path, content, found):
    with pytest.raises(ValueError, match="Erwartet Spalten suche;datei") as e:
        load_queries(_csv(tmp_path, content))
    assert found in str(e.value)


def test_queries_header_without_rows(tmp_path):
    with pytest.raises(ValueError, match="Keine Suchfragen"):
        load_queries(_csv(tmp_path, "suche;datei;seite\n;;\n"))


def test_queries_missing_file_passes_through(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_queries(tmp_path / "gibtsnicht.csv")
