"""Ordinary fictional copies, metadata errors and conservative session cleanup; no native app starts."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import http.client
import json
import os
import stat
import tempfile
import threading
import time
from email import policy
from email.message import EmailMessage
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from belegsuche import db as dbm
from belegsuche import server as srv
from belegsuche.search import Searcher

from .conftest import digital_pdf, docx_bytes


@pytest.fixture()
def temp_area(tmp_path, monkeypatch):
    area = tmp_path / "systemtmp"
    area.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(area))
    return area


def xattr(path, value=None):
    """Independent fixture helper for real harmless macOS quarantine metadata."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = os.open(path, os.O_RDONLY)
    try:
        if value is not None:
            setter = libc.fsetxattr
            setter.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                               ctypes.c_uint32, ctypes.c_int]
            assert setter(fd, srv.QUARANTINE_ATTR, value, len(value), 0, 0) == 0
        getter = libc.fgetxattr
        getter.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                           ctypes.c_uint32, ctypes.c_int]
        getter.restype = ctypes.c_ssize_t
        buffer = ctypes.create_string_buffer(1024)
        size = getter(fd, srv.QUARANTINE_ATTR, buffer, len(buffer), 0, 0)
        return buffer.raw[:size] if size >= 0 else None
    finally:
        os.close(fd)


@pytest.fixture()
def ordinary_server(tmp_path, temp_area, monkeypatch):
    root = tmp_path / "Belege"
    root.mkdir()
    word = root / "Fiktiver Vertrag.docx"
    word.write_bytes(docx_bytes(["Fiktiver Wartungsvertrag. Alle Angaben frei erfunden."]))
    pdf = tmp_path / "fiktive-quelle.pdf"
    digital_pdf(pdf, [["Fiktive Wartungsrechnung. Ausschliesslich lokale Testdaten."]])
    mail = EmailMessage(policy=policy.SMTP)
    mail["Subject"] = "Fiktive Unterlagen"
    mail["From"] = "demo@wartung.example"
    mail.set_content("Fiktive Unterlagen ohne echte Daten.")
    mail.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename="Fiktiver Beleg.pdf")
    eml = root / "Fiktive Mail.eml"
    eml.write_bytes(mail.as_bytes())
    linked = root / "Fiktiver Verweis.eml"
    linked.write_bytes(mail.as_bytes())
    database = tmp_path / "index.db"
    con = dbm.connect(database)
    rid = con.execute("INSERT INTO roots(path) VALUES (?)", (str(root),)).lastrowid
    for fid, path, kind in [(1, eml, "mail"), (2, word, "word"), (3, linked, "mail")]:
        con.execute("INSERT INTO files(id,root_id,relpath,status,kind) VALUES (?,?,?,'done',?)",
                    (fid, rid, path.name, kind))
    con.execute("INSERT INTO parts(file_id,part_no,kind,name,sha256,mime_path) VALUES (1,1,'pdf',?,?,?)",
                ("Fiktiver Beleg.pdf", hashlib.sha256(pdf.read_bytes()).hexdigest(), "2"))
    con.execute("INSERT INTO parts(file_id,part_no,kind,name,note) VALUES (3,1,'link',?,?)",
                (word.name, word.name))
    con.commit()
    con.close()
    calls = []
    monkeypatch.setattr(srv.subprocess, "run", lambda command, check=False: calls.append(command))
    manager = srv.AttachmentTemp()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(Searcher(database), port, manager)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()

    def post(body):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST", "/api/open", body=json.dumps(body), headers={"X-Belegsuche": "1"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    original = word.read_bytes(), word.stat().st_mtime_ns, stat.S_IMODE(word.stat().st_mode)
    try:
        yield {"post": post, "calls": calls, "word": word, "root": root, "temp": temp_area}
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(5)
        manager.cleanup()
        assert (word.read_bytes(), word.stat().st_mtime_ns, stat.S_IMODE(word.stat().st_mode)) == original


def test_required_attachment_quarantine_failure_does_not_open(ordinary_server, monkeypatch):
    monkeypatch.setattr(srv, "set_quarantine", lambda fd: False)
    status, body = ordinary_server["post"]({"id": 1, "part": 1})
    assert status == 500 and "Sicherheitsmarkierung" in body["error"]
    assert not ordinary_server["calls"]
    assert list(ordinary_server["temp"].iterdir()) == []


@pytest.mark.parametrize("selector", [{"id": 2}, {"id": 3, "part": 1}])
def test_existing_word_quarantine_survives_copy_and_link(ordinary_server, selector):
    original = ordinary_server["word"]
    quarantine = b"0081;00000001;Fiktiver Download;"
    xattr(original, quarantine)
    status, body = ordinary_server["post"](selector)
    assert status == 200, body
    copy = Path(ordinary_server["calls"][-1][1])
    assert copy != original and copy.read_bytes() == original.read_bytes()
    assert xattr(copy) == quarantine and xattr(original) == quarantine
    assert stat.S_IMODE(copy.stat().st_mode) == 0o400


def test_word_without_quarantine_remains_an_unmarked_copy(ordinary_server):
    assert xattr(ordinary_server["word"]) is None
    assert ordinary_server["post"]({"id": 2}) == (200, {"ok": True})
    assert xattr(ordinary_server["calls"][-1][1]) is None


def test_existing_word_quarantine_write_failure_does_not_open(ordinary_server, monkeypatch):
    xattr(ordinary_server["word"], b"0081;00000001;Fiktiver Download;")
    monkeypatch.setattr(srv, "_set_quarantine_value", lambda fd, value: False)
    status, body = ordinary_server["post"]({"id": 2})
    assert status == 500 and "Sicherheitsmarkierung" in body["error"]
    assert not ordinary_server["calls"] and list(ordinary_server["temp"].iterdir()) == []


def test_word_metadata_read_error_is_distinct_and_prevents_open(ordinary_server, monkeypatch):
    def unavailable(fd):
        raise OSError(errno.EACCES, "Fiktiver Lesefehler")
    monkeypatch.setattr(srv, "_get_quarantine", unavailable)
    status, body = ordinary_server["post"]({"id": 2})
    assert status == 500 and "Originals konnte nicht gelesen" in body["error"]
    assert not ordinary_server["calls"] and list(ordinary_server["temp"].iterdir()) == []


def test_quarantine_is_read_from_the_open_original_fd(ordinary_server, monkeypatch):
    expected = ordinary_server["word"].stat()
    seen = []
    def read_metadata(fd):
        actual = os.fstat(fd)
        seen.append((actual.st_dev, actual.st_ino))
        return None
    monkeypatch.setattr(srv, "_get_quarantine", read_metadata)
    assert ordinary_server["post"]({"id": 2})[0] == 200
    assert seen == [(expected.st_dev, expected.st_ino)]


@pytest.mark.parametrize("sizes, code", [([0], errno.EINVAL), ([4, 2], errno.EAGAIN)])
def test_empty_or_changed_quarantine_is_a_read_error(monkeypatch, sizes, code):
    class Getter:
        def __call__(self, *args):
            return sizes.pop(0)
    class Library:
        fgetxattr = Getter()
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: Library())
    with pytest.raises(OSError) as error:
        srv._get_quarantine(123)
    assert error.value.errno == code


def test_manifest_records_owned_directory_file_and_content(temp_area):
    manager = srv.AttachmentTemp()
    path = Path(manager.write("Fiktiver Beleg.txt", b"Fiktive Testdaten", [], quarantine=False))
    record = json.loads((path.parent / srv.ATTACH_MANIFEST).read_text())
    assert record["file_identity"] == [path.stat().st_dev, path.stat().st_ino]
    assert record["directory_identity"] == [path.parent.stat().st_dev, path.parent.stat().st_ino]
    assert record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    manager.cleanup(roots=[])
    assert list(temp_area.iterdir()) == []


def test_old_unmanaged_directory_is_never_deleted(temp_area):
    directory = temp_area / "belegsuche-anhang-fiktives-archiv"
    directory.mkdir()
    original = directory / "Fiktiver Beleg.txt"
    original.write_text("Fiktive Archivdatei")
    old = time.time() - 48 * 3600
    os.utime(directory, (old, old))
    assert srv.AttachmentTemp.cleanup_stale(roots=[]) == 0
    assert original.read_text() == "Fiktive Archivdatei"


def test_active_session_is_skipped_even_when_old(temp_area):
    manager = srv.AttachmentTemp()
    path = Path(manager.write("Fiktiver Beleg.txt", b"Fiktiv", [], quarantine=False))
    assert srv.AttachmentTemp.cleanup_stale(max_age=0, now=time.time() + 48 * 3600, roots=[]) == 0
    assert path.read_bytes() == b"Fiktiv"
    manager.cleanup(roots=[])
    assert list(temp_area.iterdir()) == []


def test_only_finished_owned_unchanged_session_is_cleaned(temp_area):
    manager = srv.AttachmentTemp()
    path = Path(manager.write("Fiktiver Beleg.txt", b"Fiktiv", [], quarantine=False))
    manager.cleanup(remove=False)
    assert path.is_file()
    assert srv.AttachmentTemp.cleanup_stale(max_age=0) == 0   # aktuelle Archivgrenzen fehlen
    assert srv.AttachmentTemp.cleanup_stale(max_age=0, roots=[]) == 1
    assert list(temp_area.iterdir()) == []


@pytest.mark.parametrize("root_position", ["ancestor", "same", "descendant"])
def test_regular_and_stale_cleanup_preserve_archive_overlap(temp_area, root_position):
    manager = srv.AttachmentTemp()
    path = Path(manager.write("Fiktiver Beleg.txt", b"Fiktiv", [], quarantine=False))
    root = {"ancestor": temp_area, "same": path.parent,
            "descendant": path.parent / "Eingelesener Ordner"}[root_position]
    manager.cleanup(roots=[str(root)])
    assert path.read_bytes() == b"Fiktiv"
    assert srv.AttachmentTemp.cleanup_stale(max_age=0, roots=[str(root)]) == 0
    assert path.read_bytes() == b"Fiktiv"


def test_manual_addition_is_preserved_without_recursive_cleanup(temp_area):
    manager = srv.AttachmentTemp()
    path = Path(manager.write("Fiktiver Beleg.txt", b"Fiktiv", [], quarantine=False))
    note = path.parent / "Manuell gespeicherte Notiz.txt"
    note.write_text("Fiktive eigene Notiz")
    manager.cleanup(roots=[])
    assert path.read_bytes() == b"Fiktiv" and note.read_text() == "Fiktive eigene Notiz"
    assert srv.AttachmentTemp.cleanup_stale(max_age=0, roots=[]) == 0
    assert note.read_text() == "Fiktive eigene Notiz"


def test_edited_copy_is_kept_after_close_and_on_next_start(temp_area):
    manager = srv.AttachmentTemp()
    path = Path(manager.write("Notiz.txt", b"Entwurf A", [], quarantine=False))
    # Eine bewusst bearbeitete Kopie ist keine unveränderte Programmdatei mehr, auch bei gleicher Größe.
    path.chmod(0o600)
    path.write_bytes(b"Entwurf B")
    path.chmod(0o400)
    manager.cleanup(roots=[])
    assert path.read_bytes() == b"Entwurf B"
    assert srv.AttachmentTemp.cleanup_stale(max_age=0, roots=[]) == 0
    assert path.read_bytes() == b"Entwurf B"
