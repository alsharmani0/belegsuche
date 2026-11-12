"""Benign temporary fixtures for status counts, server lifecycle and UI races."""
from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest

from belegsuche import db as dbm
from belegsuche import server as srv
from belegsuche.search import Searcher


@pytest.fixture()
def local_server(tmp_path):
    root = tmp_path / "belege"
    root.mkdir()
    db_path = tmp_path / "index.db"
    con = dbm.connect(db_path)
    root_id = con.execute("INSERT INTO roots(path) VALUES (?)", (str(root),)).lastrowid
    for file_id, status, has_text in [
        (1, "done", True), (2, "pending", True), (3, "processing", True),
        (4, "pending", False), (5, "error", True), (6, "done", False),
    ]:
        name = f"beispiel{file_id}.pdf"
        (root / name).write_bytes(b"%PDF harmless temporary fixture")
        con.execute("INSERT INTO files(id, root_id, relpath, status) VALUES (?, ?, ?, ?)",
                    (file_id, root_id, name, status))
        if has_text:
            con.execute("INSERT INTO pages(file_id, page_no, text) VALUES (?, 1, ?)",
                        (file_id, "Beispiel Rechnung"))
    con.commit()
    con.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(Searcher(db_path), port)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(5)


def get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read(), response.headers
    finally:
        connection.close()


def test_status_counts_changed_documents_with_retained_text(local_server):
    code, body, _ = get(local_server, "/api/status")
    assert code == 200
    assert json.loads(body)["files"] == 3


def test_original_file_response_is_not_cached(local_server):
    code, body, headers = get(local_server, "/datei/1")
    assert code == 200 and body == b"%PDF harmless temporary fixture"
    assert headers.get_all("Cache-Control") == ["no-store"]


class FakeServer:
    server_address = ("127.0.0.1", 23456)
    closed = False

    def serve_forever(self):
        pass

    def server_close(self):
        self.closed = True


def test_server_socket_is_closed_when_initialization_fails(tmp_path, monkeypatch):
    httpd = FakeServer()
    monkeypatch.setattr(srv, "ThreadingHTTPServer", lambda *a: httpd)

    def failed_searcher(*a):
        raise OSError("temporary startup failure")

    monkeypatch.setattr(srv, "Searcher", failed_searcher)
    with pytest.raises(OSError, match="temporary startup failure"):
        srv.serve(tmp_path / "index.db", open_browser=False)
    assert httpd.closed


def test_delayed_browser_open_is_cancelled_when_server_stops(tmp_path, monkeypatch):
    httpd = FakeServer()
    timers = []

    class Timer:
        daemon = False
        started = cancelled = False

        def __init__(self, delay, callback):
            timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(srv, "ThreadingHTTPServer", lambda *a: httpd)
    monkeypatch.setattr(srv.threading, "Timer", Timer)
    assert srv.serve(tmp_path / "index.db", open_browser=True) == 0
    assert len(timers) == 1 and timers[0].daemon and timers[0].started and timers[0].cancelled
    assert httpd.closed


NODE_PRELUDE = r"""
const els = {};
function mkEl(id) {
  return {id, value: '', innerHTML: '', className: '', children: [], events: {},
    get textContent() { return this.innerHTML; },
    set textContent(value) { this.innerHTML = String(value); },
    addEventListener(name, handler) { this.events[name] = handler; },
    appendChild(child) { this.children.push(child); },
    querySelector() { return null; }};
}
globalThis.document = {getElementById: id => els[id] ||= mkEl(id), createElement: () => mkEl(null)};
let handler = async () => { throw new Error('offline'); };
globalThis.fetch = (...args) => handler(...args);
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
const ok = obj => ({ok: true, status: 200, json: async () => obj});
const hit = {id: 1, filename: 'alte-suche.pdf', folder: '', root: '/belege', page: 1, pages: 1,
  snippet: 'Beispiel', matched: [], missing: [], ocr: false, available: true, duplicates: [], pdf: true};
const answer = {hits: [hit], ms: 1, unknown: []};
"""


def run_ui(scenario):
    if not shutil.which("node"):
        pytest.skip("node nicht installiert")
    script = re.search(r"<script>(.*?)</script>", srv.UI_FILE.read_text(), re.S).group(1)
    run = subprocess.run(["node", "-e", NODE_PRELUDE + script + scenario], capture_output=True,
                         text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_ui_rejects_old_response_as_soon_as_input_changes():
    result = run_ui(r"""
    (async () => {
      await Promise.resolve();
      let finish;
      handler = () => new Promise(resolve => { finish = resolve; });
      $('q').value = 'alte Suche';
      const pending = search();
      $('q').value = 'neue Suche';
      $('q').events.input();
      finish(ok(answer));
      await pending;
      console.log(JSON.stringify({results: $('results').innerHTML, meta: $('meta').textContent}));
    })();
    """)
    assert result == {"results": "", "meta": "Suche …"}


def test_ui_empty_search_clears_hits_and_reports_unavailable_status():
    result = run_ui(r"""
    (async () => {
      await Promise.resolve();
      handler = async () => ok(answer);
      $('q').value = 'Rechnung';
      await search();
      const previous = $('results').innerHTML;
      handler = async () => ({ok: false, status: 503, json: async () => ({})});
      $('q').value = '   ';
      $('q').events.input();
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
      console.log(JSON.stringify({previous, results: $('results').innerHTML, meta: $('meta').textContent}));
    })();
    """)
    assert "alte-suche.pdf" in result["previous"]
    assert result["results"] == ""
    assert result["meta"] == "Status nicht erreichbar – läuft „belegsuche start“ noch?"
