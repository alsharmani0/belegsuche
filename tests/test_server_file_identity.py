"""Stale browser actions must never resolve to a newly indexed document."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from belegsuche import cli, db, server
from belegsuche.indexer import run_index
from belegsuche.search import Searcher

from .conftest import digital_pdf


def test_removed_archive_links_cannot_open_replacement_file(tmp_path, monkeypatch):
    old_root, new_root = tmp_path / "vorher", tmp_path / "nachher"
    old_root.mkdir()
    new_root.mkdir()
    old_file, new_file = old_root / "alt.pdf", new_root / "neu.pdf"
    digital_pdf(old_file, [["Alterbeleg Heizungswartung Rechnung A-12345", "Erfundene Firma Beispiel"]])
    digital_pdf(new_file, [["Neuerbeleg Gartenpflege Rechnung N-98765", "Andere erfundene Firma Beispiel"]])
    old_bytes, new_bytes = old_file.read_bytes(), new_file.read_bytes()

    class NoOcr:
        def recognize_image(self, image):
            raise AssertionError("Die Testbelege haben eine digitale Textebene")

    db_path = tmp_path / "index.db"
    con = db.connect(db_path)
    run_index(con, [str(old_root)], engine=NoOcr(), log=lambda _: None)
    root_id = con.execute("SELECT id FROM roots").fetchone()[0]
    native_calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **kw: native_calls.append(a))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(Searcher(db_path), port)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()

    def request(path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST" if body is not None else "GET", path,
                               body=json.dumps(body) if body is not None else None,
                               headers={"X-Belegsuche": "1"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        status, body = request("/api/search?q=Alterbeleg")
        assert status == 200
        stale_id = json.loads(body)["hits"][0]["id"]
        assert request(f"/datei/{stale_id}") == (200, old_bytes)

        # A browser tab keeps its old result while another terminal changes the index.
        assert cli.main(["--db", str(db_path), "ordner", "entfernen", str(root_id)]) == 0
        run_index(con, [str(new_root)], engine=NoOcr(), log=lambda _: None)

        assert request(f"/datei/{stale_id}")[0] == 404
        assert request("/api/open", {"id": stale_id})[0] == 404
        assert request("/api/open", {"id": stale_id, "reveal": True})[0] == 404
        assert not native_calls

        status, body = request("/api/search?q=Neuerbeleg")
        assert status == 200
        current_id = json.loads(body)["hits"][0]["id"]
        assert current_id != stale_id
        assert request(f"/datei/{current_id}") == (200, new_bytes)
        assert old_file.read_bytes() == old_bytes and new_file.read_bytes() == new_bytes
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(5)
        con.close()
