"""Local UI assets must ship with the package and remain readable over HTTP."""
from __future__ import annotations

import fnmatch
import http.client
import re
import threading
import tomllib
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from belegsuche import server as srv


def test_ui_references_only_declared_packaged_assets():
    project = Path(__file__).resolve().parents[1]
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["belegsuche"]
    references = set(re.findall(r'(?:src|href)="(/assets/[^"#]+)"',
                                srv.UI_FILE.read_text(encoding="utf-8")))
    assert references and references <= srv.UI_ASSETS.keys()
    for route in references:
        name, _ = srv.UI_ASSETS[route]
        assert any(fnmatch.fnmatch(name, pattern) for pattern in patterns), name
        assert srv.UI_FILE.with_name(name).is_file(), name
    assert "img-src 'self'" in srv.CSP_UI


@pytest.fixture()
def asset_server(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = server.server_address[1]
    # UI/asset requests never need an index or private archive.
    server.RequestHandlerClass = srv.make_handler(SimpleNamespace(db_path=tmp_path / "unused.db"), port)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


@pytest.mark.parametrize("route", tuple(srv.UI_ASSETS))
def test_packaged_ui_asset_is_served_with_its_type(asset_server, route):
    connection = http.client.HTTPConnection("127.0.0.1", asset_server, timeout=5)
    try:
        connection.request("GET", route)
        response = connection.getresponse()
        body = response.read()
        name, content_type = srv.UI_ASSETS[route]
        assert response.status == 200
        assert response.headers["Content-Type"] == content_type
        assert response.headers["Cache-Control"] == "no-store"
        assert int(response.headers["Content-Length"]) == len(body)
        assert body == srv.UI_FILE.with_name(name).read_bytes()
    finally:
        connection.close()
