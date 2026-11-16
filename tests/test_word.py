"""Word-Dateien: docx direkt aus dem ZIP, doc/rtf/odt über textutil nur per stdin/stdout.

Alle Testdateien entstehen in tmp_path (docx per zipfile, die übrigen mit macOS textutil aus einer Textdatei)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from belegsuche import db as dbm
from belegsuche import word
from belegsuche.extract import NOTE_PREFIX, ExtractError, extract_with_notes
from belegsuche.indexer import PERMANENT_ERRORS, run_index
from belegsuche.search import Searcher

from .conftest import BytesEngine, docx_bytes, textutil_word, tree_state

needs_textutil = pytest.mark.skipif(not Path(word.TEXTUTIL).exists(), reason="textutil (macOS) fehlt")


def quiet(_):
    pass


def test_docx_text_with_header_footer_footnote_table_and_page_break(tmp_path):
    f = tmp_path / "Brief.docx"
    f.write_bytes(docx_bytes(["Mietvertrag Lindenweg 3", "Kaltmiete 650,00 EUR", "Anlage Hausordnung"],
                             header="Hausverwaltung Nord GmbH", footer="IBAN DE00 1234 5678 9012 3456 78",
                             footnote="Gilt ab 01.04.2024", page_break_before=2,
                             table=[["Position", "Betrag"], ["Nebenkosten", "180,00"]],
                             fallback_dup="Textfeld Wohnung 4"))
    pages, notes = extract_with_notes(f, None)
    assert [p.page_no for p in pages] == [1, 2] and {p.source for p in pages} == {"text"} and notes == []
    assert pages[0].text.startswith("Hausverwaltung Nord GmbH")                  # Kopfzeile vorn auf Seite 1
    assert "Kaltmiete 650,00 EUR" in pages[0].text and "IBAN DE00" in pages[0].text   # Fußzeile auf Seite 1
    assert "Anlage Hausordnung" in pages[1].text and "Gilt ab 01.04.2024" in pages[1].text
    assert "Nebenkosten\t180,00" in pages[1].text                                # Tabellenzellen
    assert pages[1].text.count("Textfeld Wohnung 4") == 1                        # mc:Fallback nicht doppelt


@needs_textutil
@pytest.mark.parametrize("name", ["Alt.doc", "Brief.rtf", "Text.odt", "Neu.docx", "Vorlage.dot"])
def test_word_formats_via_textutil_and_zip(tmp_path, name):
    src = name if not name.endswith(".dot") else "Vorlage.doc"
    f = textutil_word(tmp_path, src, "Wärmetechnik Sommer Angebot 4711\nKesselwartung 312,50 EUR\n")
    if name.endswith(".dot"):
        f = f.rename(tmp_path / name)
    pages, _ = extract_with_notes(f, None)
    text = "\n".join(p.text for p in pages)
    assert "Wärmetechnik Sommer Angebot 4711" in text and "312,50" in text


@needs_textutil
def test_doc_page_break_gives_pages(tmp_path):
    f = textutil_word(tmp_path, "Zwei.doc", "Seite eins Heizung\n\fSeite zwei Dachrinne\n")
    pages, _ = extract_with_notes(f, None)
    assert [p.page_no for p in pages] == [1, 2]
    assert "Heizung" in pages[0].text and "Dachrinne" in pages[1].text


def test_textutil_gets_only_stdin_never_the_original_path(tmp_path, monkeypatch):
    f = tmp_path / "Alt.doc"
    f.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + "WordDocument".encode("utf-16-le") + bytes(500))
    seen = {}

    def fake_run(args, **kw):
        seen["args"], seen["input"], seen["cwd"] = args, kw.get("input"), kw.get("cwd")
        assert kw.get("timeout") == word.TEXTUTIL_TIMEOUT
        return subprocess.CompletedProcess(args, 0, "Gelesen über stdin\n".encode(), b"")

    monkeypatch.setattr(word.subprocess, "run", fake_run)
    pages, _ = extract_with_notes(f, None)
    assert pages[0].text == "Gelesen über stdin"
    assert "-stdin" in seen["args"] and "-stdout" in seen["args"] and "-format" in seen["args"]
    assert not any(str(f) in a or f.name in a for a in seen["args"])           # nie den Pfad des Originals
    assert seen["input"] == f.read_bytes() and seen["cwd"] != str(tmp_path)


def test_doc_that_is_really_docx_is_read_from_zip(tmp_path, monkeypatch):
    f = tmp_path / "Eigentlich-docx.doc"
    f.write_bytes(docx_bytes(["Abrechnung Hausmeister Petersen"]))
    monkeypatch.setattr(word.subprocess, "run", lambda *a, **k: pytest.fail("textutil nicht nötig"))
    pages, _ = extract_with_notes(f, None)
    assert pages[0].text == "Abrechnung Hausmeister Petersen"


def test_empty_word_file_is_one_empty_page_with_note(tmp_path):
    f = tmp_path / "Leer.docx"
    f.write_bytes(b"")
    pages, notes = extract_with_notes(f, None)
    assert [(p.page_no, p.text) for p in pages] == [(1, "")] and notes == [f"{NOTE_PREFIX} leere Datei (0 Byte)"]


def test_docx_with_only_images_gets_note_and_no_ocr(tmp_path):
    f = tmp_path / "Scan.docx"
    f.write_bytes(docx_bytes([""], images=2))
    pages, notes = extract_with_notes(f, BytesEngine())
    assert pages[0].text == "" and len(notes) == 1 and "fast nur Bilder" in notes[0]


@needs_textutil
def test_textutil_timeout_and_error_give_clear_messages(tmp_path, monkeypatch):
    f = tmp_path / "Alt.doc"
    f.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + "WordDocument".encode("utf-16-le") + bytes(500))
    with pytest.raises(ExtractError, match="beschädigt"):        # textutil meldet Fehler nur auf stderr
        extract_with_notes(f, None)
    slow = tmp_path / "langsam.sh"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setattr(word, "TEXTUTIL", str(slow))
    monkeypatch.setattr(word, "TEXTUTIL_TIMEOUT", 1)
    with pytest.raises(ExtractError, match="Zeitlimit"):
        extract_with_notes(f, None)


def test_password_protected_docx_is_permanent_error(tmp_path):
    f = tmp_path / "Geheim.docx"
    f.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + "EncryptedPackage".encode("utf-16-le") + bytes(500))
    with pytest.raises(ExtractError) as e:
        extract_with_notes(f, None)
    assert str(e.value) == word.PASSWORD_MSG and word.PASSWORD_MSG in PERMANENT_ERRORS


def test_broken_docx_and_xml_entities_are_rejected(tmp_path):
    f = tmp_path / "Kaputt.docx"
    f.write_bytes(docx_bytes(["Text"])[:200])
    with pytest.raises(ExtractError, match="beschädigt|nicht lesbar"):
        extract_with_notes(f, None)
    bomb = tmp_path / "Bombe.docx"
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
    bomb.write_bytes(buf.getvalue())
    with pytest.raises(ExtractError, match="ungewöhnlich"):
        extract_with_notes(bomb, None)


@needs_textutil
def test_index_word_files_searchable_and_originals_untouched(tmp_path):
    root = tmp_path / "ordner"
    root.mkdir()
    (root / "Mietvertrag.docx").write_bytes(docx_bytes(["Mietvertrag Birkenallee 21", "Kaution 1.950,00 EUR"],
                                                       header="Verwaltung Sonnenhof"))
    textutil_word(root, "Altbrief.doc", "Kündigung Garage 7 zum 30.06.2019\n")
    (root / "~$Mietvertrag.docx").write_bytes(b"\x0bFrau Beispiel" + bytes(150))   # Sperrdatei von Word
    (root / "Leer.doc").write_bytes(b"")
    before = tree_state(root)
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert tree_state(root) == before                                             # nichts verändert, nichts dazu
    rows = {r["relpath"]: r for r in con.execute("SELECT relpath, kind, status, error, ocr_pages FROM files")}
    assert set(rows) == {"Mietvertrag.docx", "Altbrief.doc", "Leer.doc"}           # ~$-Datei ignoriert
    assert {r["kind"] for r in rows.values()} == {"word"} and {r["status"] for r in rows.values()} == {"done"}
    assert rows["Leer.doc"]["error"] == f"{NOTE_PREFIX} leere Datei (0 Byte)"
    assert all(r["ocr_pages"] == 0 for r in rows.values()) and ps.ocr_pages == 0
    s = Searcher(tmp_path / "i.db")
    assert [h.filename for h in s.search("Kaution Birkenallee").hits] == ["Mietvertrag.docx"]
    assert [h.filename for h in s.search("Sonnenhof").hits] == ["Mietvertrag.docx"]   # Kopfzeile
    hit = s.search("Kündigung Garage").hits[0]
    assert hit.filename == "Altbrief.doc" and hit.kind == "word"
    con.close()


@needs_textutil
def test_textutil_never_writes_next_to_original(tmp_path):
    root = tmp_path / "ordner"
    root.mkdir()
    f = textutil_word(root, "Brief.rtf", "Rechnung Gärtnerei Grünwerk\n")
    before = sorted(os.listdir(root))
    pages, _ = extract_with_notes(f, None)
    assert "Grünwerk" in pages[0].text and sorted(os.listdir(root)) == before
    assert shutil.which("textutil") or Path(word.TEXTUTIL).exists()
