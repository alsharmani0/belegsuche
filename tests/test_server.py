import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from belegsuche import server as srv
from belegsuche.search import Searcher


@pytest.fixture()
def running(indexed, monkeypatch):
    _, files, db_path, con, _ = indexed
    calls = []
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(Searcher(db_path), 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(Searcher(db_path), port)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port, calls, con, files
    httpd.shutdown()


def req(port, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}"}
    h.update(headers or {})
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, r.read()


def test_ui_and_search(running):
    port, *_ = running
    st, body = req(port, "GET", "/")
    assert st == 200 and b"Belegsuche" in body
    st, body = req(port, "GET", "/api/search?q=RE-2021%2F00487")
    data = json.loads(body)
    assert st == 200 and data["hits"][0]["filename"] == "Dokument (3).pdf"


def test_rejects_foreign_host(running):
    port, *_ = running
    st, _ = req(port, "GET", "/api/search?q=x", host="evil.example:80")
    assert st == 403


def test_open_requires_header_and_known_id(running):
    port, calls, con, _ = running
    fid = con.execute("SELECT id FROM files WHERE relpath LIKE '%Dokument (3).pdf'").fetchone()[0]
    st, _ = req(port, "POST", "/api/open", {"id": fid})
    assert st == 403 and not calls
    st, _ = req(port, "POST", "/api/open", {"id": 99999}, {"X-Belegsuche": "1"})
    assert st == 404 and not calls
    st, _ = req(port, "POST", "/api/open", {"id": fid, "reveal": True}, {"X-Belegsuche": "1"})
    assert st == 200 and calls[0][:2] == ["open", "-R"] and calls[0][2].endswith("Dokument (3).pdf")


def test_serves_only_indexed_files(running):
    port, _, con, _ = running
    fid = con.execute("SELECT id FROM files WHERE relpath LIKE '%Dokument (3).pdf'").fetchone()[0]
    st, body = req(port, "GET", f"/datei/{fid}")
    assert st == 200 and body.startswith(b"%PDF")
    err_id = con.execute("SELECT id FROM files WHERE status = 'error'").fetchone()[0]
    st, _ = req(port, "GET", f"/datei/{err_id}")
    assert st == 404
