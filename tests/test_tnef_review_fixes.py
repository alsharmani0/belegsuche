"""Normale TNEF-Varianten: getrennte HTML-Codepage und Dateiname neben dem Anzeigelabel."""
from __future__ import annotations

import hashlib
import http.client
import struct
import threading
from http.server import ThreadingHTTPServer

import pytest

from belegsuche import db as dbm
from belegsuche import mail, server, tnef
from belegsuche.indexer import run_index
from belegsuche.search import Searcher

from .conftest import (BytesEngine, PT_BINARY, PT_LONG, PT_STRING8, PT_UNICODE, make_eml, mapi_props,
                       tnef_attachment, tnef_attr, tnef_bytes)


def _stream(props, oem=1252):
    data = tnef.SIGNATURE + struct.pack("<H", 0)
    data += tnef_attr(1, 0x00089006, b"\x00\x00\x01\x00")
    if oem is not None:
        data += tnef_attr(1, 0x00069007, struct.pack("<II", oem, 0))
    return data + tnef_attr(1, 0x00069003, mapi_props(props))


def test_binary_html_has_its_own_codepage_without_changing_ansi_mapi_strings(tmp_path):
    expected = "Kündigung für Müller – März 2025"
    data = tnef_bytes(
        [tnef_attachment(b"Notiz", "März.txt", name8=True)], codepage=65001,
        html=f"<p>{expected}</p>".encode("utf-8"),
        extra_props=[(PT_LONG, tnef.PR_MESSAGE_CODEPAGE, 1252),
                     (PT_STRING8, tnef.PR_SENDER_NAME, "Jörg Müller".encode("cp1252"))])
    parsed = tnef.parse(data)
    assert parsed.notes == []
    assert parsed.codepage == 1252 and parsed.internet_codepage == 65001
    assert parsed.sender == "Jörg Müller"
    assert parsed.attachments[0].file_name == "März.txt"
    assert parsed.html == f"<p>{expected}</p>"
    path = make_eml(tmp_path / "mail.eml", text="", attachments=[("winmail.dat", data, "application/ms-tnef")])
    result = mail.extract_mail(path, BytesEngine())
    assert expected in result.pages[0].text
    assert "KÃ" not in result.pages[0].text


@pytest.mark.parametrize("ptype", [PT_STRING8, PT_UNICODE])
def test_string_html_keeps_its_mapi_encoding(ptype):
    expected = "<p>März für Müller</p>"
    value = expected.encode("cp1252") if ptype == PT_STRING8 else expected
    parsed = tnef.parse(_stream([(PT_LONG, tnef.PR_MESSAGE_CODEPAGE, 1252),
                                 (PT_LONG, tnef.PR_INTERNET_CPID, 65001),
                                 (ptype, tnef.PR_HTML, value)]))
    assert parsed.html == expected and not parsed.notes


@pytest.mark.parametrize("oem,message_cp,encoding", [(65001, 1252, "utf-8"), (None, 1252, "cp1252"),
                                                    (None, None, "cp1252")])
def test_binary_html_without_internet_codepage_uses_transport_then_message_then_default(oem, message_cp, encoding):
    expected = "<p>März für Müller</p>"
    props = [(PT_BINARY, tnef.PR_HTML, expected.encode(encoding))]
    if message_cp is not None:
        props.insert(0, (PT_LONG, tnef.PR_MESSAGE_CODEPAGE, message_cp))
    parsed = tnef.parse(_stream(props, oem=oem))
    assert parsed.html == expected and not parsed.notes


def test_actual_filename_is_separate_from_display_label():
    attachment = tnef.Attachment(filename="NOTIZ.TXT", display_name="Leistungsbeschreibung", title="NOTIZ.TXT")
    assert attachment.name == "Leistungsbeschreibung"
    assert attachment.file_name == "NOTIZ.TXT"
    attachment.long_name = "Wartungsnotiz März.txt"
    assert attachment.name == attachment.file_name == "Wartungsnotiz März.txt"


@pytest.mark.parametrize("extension,content,mime", [
    ("TXT", b"Ventiltausch im Birkenhof, Vorgang FIK-2025-021.\n", "text/plain"),
    ("HTML", b"<p>Ventiltausch im <b>Birkenhof</b>, Vorgang FIK-2025-021.</p>", "text/html"),
])
def test_display_label_without_extension_keeps_text_searchable_and_safely_downloadable(tmp_path, extension,
                                                                                     content, mime):
    stream = tnef_bytes([tnef_attachment(content, f"NOTIZ.{extension}", variant="C",
                                         extra=[(PT_UNICODE, tnef.PR_DISPLAY_NAME, "Leistungsbeschreibung")])])
    source = tmp_path / "source"
    path = make_eml(source / "mail.eml", text="Anbei die Unterlagen",
                    attachments=[("winmail.dat", stream, "application/ms-tnef")])
    original = path.read_bytes()
    database = tmp_path / "index.db"
    con = dbm.connect(database)
    try:
        run_index(con, [str(source)], engine=BytesEngine(), log=lambda _: None)
        row = con.execute("SELECT t.*, f.id AS file_id FROM parts t JOIN files f ON f.id=t.file_id"
                          " WHERE t.mime_path='2!1'").fetchone()
        assert (row["kind"], row["name"], row["mime"], row["n_pages"]) == (
            "text", "Leistungsbeschreibung", mime, 1)
        hit = Searcher(database).search("Ventiltausch").hits[0]
        assert hit.part_no == row["part_no"] and hit.part_name == "Leistungsbeschreibung"
        assert mail.find_part(original, row["mime_path"]) == content
        opened, kind, name, _ = server._read_part(database, row["file_id"], row["part_no"])
        assert opened == content and kind == "txt" and name == "Leistungsbeschreibung.txt"
        assert hashlib.sha256(opened).hexdigest() == row["sha256"]
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = server.make_handler(Searcher(database), port)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", f"/datei/{row['file_id']}/teil/{row['part_no']}")
            response = connection.getresponse()
            assert response.status == 200 and response.read() == content
            assert response.getheader("Content-Type") == "application/octet-stream"
            assert response.getheader("Content-Disposition").startswith("attachment;")
            assert "Leistungsbeschreibung.txt" in response.getheader("Content-Disposition")
            connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=5)
            httpd.RequestHandlerClass.temp.cleanup()
        assert path.read_bytes() == original
    finally:
        con.close()
