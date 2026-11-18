"""Targeted mail-index repairs; fictional documents and isolated databases only."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from belegsuche import db as dbm, indexer, mail as mailmod, tnef
from belegsuche.search import Searcher
from .conftest import (PT_UNICODE, BytesEngine, digital_pdf, make_eml, textutil_word, tnef_attachment,
                       tnef_bytes, tree_state)


def quiet(_):
    pass


def photo():
    out = io.BytesIO()
    Image.new("RGB", (900, 700), "white").save(out, "PNG")
    return out.getvalue()


class UnavailableOCR(BytesEngine):
    def recognize_image(self, _):
        self.calls += 1
        raise RuntimeError("Vision-Fehler: vorübergehend nicht verfügbar")

    recognize_file = recognize_image
    recognize_bytes = recognize_image


def make_partial(tmp_path):
    root = tmp_path / "ordner"
    path = make_eml(root / "a.eml", subject="Erfundene Rechnung", text="Bitte bearbeiten.",
                    attachments=[("Rechnung.png", photo(), "image/png")])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    indexer.run_index(con, [str(root)], engine=UnavailableOCR(), log=quiet)
    return root, path, db, con


def test_retry_reads_transient_failed_attachment_and_keeps_originals(tmp_path):
    root, _, db, con = make_partial(tmp_path)
    assert indexer.count_retryable_mails(con) == 1
    before = tree_state(root)
    good = BytesEngine("Rechnung Fantasiebau Dachrinne Nummer 9472 Summe 814,35 EUR")
    _, ps = indexer.run_index(con, [], engine=good, retry=True, log=quiet)
    assert ps.retried == 1 and ps.done == 1 and good.calls == 1
    assert con.execute("SELECT n_pages,retryable FROM parts WHERE kind='image'").fetchone()[:] == (1, 0)
    assert indexer.count_retryable_mails(con) == 0
    assert Searcher(db).search("Dachrinne 9472").hits
    assert tree_state(root) == before
    con.close()


@pytest.mark.parametrize("note,expected", [
    ("nicht lesbar (RuntimeError)", True),
    ("nicht lesbar (TimeoutError); Hinweis des Konverters", True),
    ("Bild nicht lesbar (Vision-Fehler: vorübergehend)", True),
    ("Word-Datei: Zeitlimit beim Lesen überschritten (60 s)", True),
    ("PDF ist passwortgeschützt", False),
    ("Word-Datei beschädigt oder nicht lesbar", False),
    ("Word-Datei nicht lesbar (unbekanntes Format)", False),
    ("nicht lesbar (ValueError)", False),
    ("nicht ausgewertet (.xlsx)", False),
    ("nicht ausgewertet (zu groß)", False),
    ("Bild zu klein für die Texterkennung", False),
])
def test_legacy_retry_classification_does_not_promote_ordinary_skips(note, expected):
    assert mailmod.legacy_retryable_part_note(note) is expected


def test_partial_mail_is_not_a_cache_source_for_its_identical_copy(tmp_path):
    root, path, _, con = make_partial(tmp_path)
    (root / "kopie.eml").write_bytes(path.read_bytes())
    good = BytesEngine()
    _, ps = indexer.run_index(con, [], engine=good, log=quiet)
    assert ps.done == 1 and ps.reused == 0 and good.calls == 1
    assert con.execute("SELECT n_pages FROM parts WHERE file_id=(SELECT id FROM files WHERE relpath='kopie.eml')"
                       " AND kind='image'").fetchone()[0] == 1
    # A later explicit retry can use the now complete attachment, without new OCR.
    _, ps = indexer.run_index(con, [], engine=good, retry=True, log=quiet)
    assert ps.retried == 1 and ps.done == 1 and good.calls == 1
    con.close()


def test_legacy_retry_notes_are_migrated_conservatively(tmp_path):
    root, _, db, con = make_partial(tmp_path)
    con.execute("UPDATE parts SET note='nicht lesbar (RuntimeError)' WHERE kind='image'")
    # Mimic the pre-repair schema: neither structured marker existed.
    for column in ("retryable", "content_revision"):
        if column in {r[1] for r in con.execute("PRAGMA table_info(parts)")}:
            con.execute(f"ALTER TABLE parts DROP COLUMN {column}")
    dbm.set_meta(con, "index_format", "11")
    con.commit()
    con.close()
    con = dbm.connect(db)
    assert con.execute("SELECT retryable FROM parts WHERE kind='image'").fetchone()[0] == 1
    _, ps = indexer.run_index(con, [str(root)], engine=BytesEngine(), retry=True, log=quiet)
    assert ps.retried == 1 and ps.done == 1
    con.close()


def test_permanent_and_unsupported_parts_are_not_retried(tmp_path):
    root = tmp_path / "ordner"
    pdf = tmp_path / "verschluesselt.pdf"
    digital_pdf(pdf, [["Vertrauliche Rechnung"]], password="1234")
    make_eml(root / "a.eml", text="Bitte ablegen.", attachments=[
        ("geheim.pdf", pdf.read_bytes(), "application/pdf"),
        ("Liste.xlsx", b"PK\x03\x04 keine Word-Datei", "application/octet-stream"),
        ("Logo.png", b"", "image/png")])
    con = dbm.connect(tmp_path / "i.db")
    indexer.run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert con.execute("SELECT sum(retryable) FROM parts").fetchone()[0] == 0
    _, ps = indexer.run_index(con, [], engine=BytesEngine(), retry=True, log=quiet)
    assert ps.retried == 0 and ps.done == 0
    con.close()


@pytest.mark.parametrize("previous_format", ["10", "11"])
def test_neutral_old_tnef_pdf_is_recovered_once_without_rebuild(tmp_path, monkeypatch, previous_format):
    from .test_winmail import _old_behaviour
    root = tmp_path / "ordner"
    pdfs = []
    for i, text in enumerate(("Rechnung Dachdecker Fiktion 4711", "Zweite Rechnung Dachrinne 9472")):
        p = tmp_path / f"r{i}.pdf"
        digital_pdf(p, [[text, "Sehr geehrte Damen und Herren, die Rechnung betrifft das erfundene Wohnhaus."]])
        pdfs.append(p.read_bytes())
    container = tnef_bytes([tnef_attachment(pdfs[0], "Dach.pdf"), tnef_attachment(pdfs[1], "Rinne.pdf")])
    make_eml(root / "a.eml", text="Anbei die Unterlagen.",
             attachments=[("ATT0001.dat", container, "application/octet-stream")])
    # A genuine PDF with the same neutral filename must remain unchanged.
    make_eml(root / "echt.eml", text="Anbei ein echtes PDF.",
             attachments=[("ATT0002.dat", pdfs[0], "application/octet-stream")])
    db = tmp_path / "i.db"
    with monkeypatch.context() as m:
        _old_behaviour(m)
        con = dbm.connect(db)
        indexer.run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
        assert con.execute("SELECT kind FROM parts WHERE name='ATT0001.dat'").fetchone()[0] == "pdf"
        dbm.set_meta(con, "index_format", previous_format)
        con.commit()
        con.close()
    before = tree_state(root)
    monkeypatch.setattr(dbm, "rebuild_search_index", lambda _: pytest.fail("kein Vollneuaufbau"))
    con = dbm.connect(db)
    assert dbm.get_meta(con, "index_format") == dbm.INDEX_FORMAT
    assert con.execute("SELECT count(*) FROM files WHERE status!='done'").fetchone()[0] == 0
    _, ps = indexer.run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert ps.done == 1 and ps.errors == 0
    assert Searcher(db).search("Dachrinne 9472").hits
    _, ps = indexer.run_index(con, [], engine=BytesEngine(), log=quiet)
    assert ps.done == 0
    assert tree_state(root) == before
    con.close()


@pytest.mark.skipif(not Path("/usr/bin/textutil").exists(), reason="macOS textutil")
def test_legacy_skipped_doc_is_reclassified_and_read(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    doc = textutil_word(tmp_path, "brief.doc", "Besprechungsprotokoll Fiktion Wasserschaden Keller 7719")
    make_eml(root / "a.eml", text="Anbei das Protokoll.",
             attachments=[("brief.doc", doc.read_bytes(), "application/octet-stream")])
    real = mailmod._classify_content
    db = tmp_path / "i.db"
    with monkeypatch.context() as m:
        def old(att, depth):
            att = real(att, depth)
            if att.name == "brief.doc":
                att.kind, att.note = "skipped", "nicht ausgewertet (.doc)"
            return att
        m.setattr(mailmod, "_classify_content", old)
        m.setattr(dbm, "INDEX_FORMAT", "11")
        con = dbm.connect(db)
        indexer.run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
        con.close()
    con = dbm.connect(db)
    _, ps = indexer.run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert ps.done == 1 and ps.errors == 0
    assert con.execute("SELECT kind,n_pages FROM parts WHERE name='brief.doc'").fetchone()[:] == ("word", 1)
    assert Searcher(db).search("Wasserschaden 7719").hits
    con.close()


def test_late_placeholder_target_is_linked_without_reextracting_mail(tmp_path):
    root, other = tmp_path / "ordner", tmp_path / "anderer_ordner"
    placeholder = "Die Datei 'Rechnung.pdf' wurde in die Dokumentenverwaltung von DOMUS CRM verschoben. 03.02.2023 10:15 EB"
    for r in (root, other):
        make_eml(r / "Vorgang" / "Emails" / "a.eml", subject="Vorgang Kolibri", text="Bitte die Rechnung bearbeiten.",
                 attachments=[("Rechnung.pdf.txt", placeholder.encode(), "application/octet-stream")])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    eng = BytesEngine()
    indexer.run_index(con, [str(root), str(other)], engine=eng, log=quiet)
    for r in (root, other):
        digital_pdf(r / "Vorgang" / "Rechnung.pdf", [["Rechnung Installateur Fiktion Nummer 4719", "Rohrreparatur und Material."]])
    _, ps = indexer.run_index(con, [str(root)], engine=eng, log=quiet)
    assert ps.done == 1 and eng.calls == 0
    rows = con.execute("SELECT r.path,t.note FROM parts t JOIN files f ON f.id=t.file_id JOIN roots r ON r.id=f.root_id"
                       " WHERE t.kind='link' ORDER BY r.id").fetchall()
    assert [tuple(r) for r in rows] == [(str(root), "Vorgang/Rechnung.pdf"), (str(other), None)]
    assert next(h for h in Searcher(db).search("Kolibri").hits if h.root == str(root)).links[0]["file_id"]
    con.close()


def test_old_opened_tnef_is_refreshed_once_and_other_attachment_pages_are_reused(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    payload = b"Leistungsbeschreibung Auffahrt Pflasterarbeiten Auftrag 7729"
    container = tnef_bytes([tnef_attachment(payload, "NOTIZ.TXT", variant="C",
                                           extra=[(PT_UNICODE, 0x3001, "Leistungsbeschreibung")])])
    make_eml(root / "a.eml", text="Anbei die Leistungsbeschreibung.", attachments=[
        ("winmail.dat", container, "application/ms-tnef"), ("Foto.png", photo(), "image/png")])
    db = tmp_path / "i.db"
    old_ocr = BytesEngine()
    with monkeypatch.context() as m:
        m.setattr(dbm, "INDEX_FORMAT", "11")
        m.setattr(tnef.Attachment, "file_name", property(lambda self: self.name))
        con = dbm.connect(db)
        indexer.run_index(con, [str(root)], engine=old_ocr, log=quiet)
        assert con.execute("SELECT n_pages FROM parts WHERE name='Leistungsbeschreibung'").fetchone()[0] == 0
        assert old_ocr.calls == 1
        con.close()
    con = dbm.connect(db)
    good = BytesEngine()
    _, ps = indexer.run_index(con, [str(root)], engine=good, log=quiet)
    assert ps.done == 1 and good.calls == 0
    assert con.execute("SELECT kind,n_pages FROM parts WHERE name='Leistungsbeschreibung'").fetchone()[:] == ("text", 1)
    assert Searcher(db).search("Auffahrt 7729").hits
    _, ps = indexer.run_index(con, [], engine=good, log=quiet)
    assert ps.done == 0
    con.close()


def test_clear_named_old_pdf_does_not_trigger_content_inspection(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    pdf = tmp_path / "original.pdf"
    digital_pdf(pdf, [["Rechnung Installateur Fiktion Nummer 4719", "Rohrreparatur und Material."]])
    make_eml(root / "a.eml", text="Anbei die Rechnung.",
             attachments=[("Rechnung.pdf", pdf.read_bytes(), "application/octet-stream")])
    db = tmp_path / "i.db"
    with monkeypatch.context() as m:
        m.setattr(dbm, "INDEX_FORMAT", "11")
        con = dbm.connect(db)
        indexer.run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
        con.close()
    con = dbm.connect(db)
    monkeypatch.setattr(mailmod, "find_part", lambda *_: pytest.fail("normale PDF nicht neu parsen"))
    _, ps = indexer.run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert ps.done == 0
    con.close()


def test_old_opened_tnef_outside_selected_root_is_not_a_cache_donor(tmp_path, monkeypatch):
    first, second = tmp_path / "eins", tmp_path / "zwei"
    payload = b"Leistungsbeschreibung Auffahrt Pflasterarbeiten Auftrag 7729"
    container = tnef_bytes([tnef_attachment(payload, "NOTIZ.TXT", variant="C",
                                           extra=[(PT_UNICODE, 0x3001, "Leistungsbeschreibung")])])
    path = make_eml(first / "a.eml", text="Anbei die Leistungsbeschreibung.", attachments=[
        ("winmail.dat", container, "application/ms-tnef"), ("Foto.png", photo(), "image/png")])
    db = tmp_path / "i.db"
    with monkeypatch.context() as m:
        m.setattr(dbm, "INDEX_FORMAT", "11")
        m.setattr(tnef.Attachment, "file_name", property(lambda self: self.name))
        con = dbm.connect(db)
        indexer.run_index(con, [str(first)], engine=BytesEngine(), log=quiet)
        con.close()
    second.mkdir()
    (second / "kopie.eml").write_bytes(path.read_bytes())
    con = dbm.connect(db)
    good = BytesEngine()
    _, ps = indexer.run_index(con, [str(second)], engine=good, log=quiet)
    assert ps.done == 1 and ps.reused == 0 and good.calls == 0
    rows = con.execute("SELECT r.path,t.kind,t.n_pages FROM parts t JOIN files f ON f.id=t.file_id"
                       " JOIN roots r ON r.id=f.root_id WHERE t.name='Leistungsbeschreibung' ORDER BY r.id").fetchall()
    assert [tuple(r) for r in rows] == [(str(first), "skipped", 0), (str(second), "text", 1)]
    _, ps = indexer.run_index(con, [str(first)], engine=good, log=quiet)
    assert ps.done == 1 and good.calls == 0
    _, ps = indexer.run_index(con, [], engine=good, log=quiet)
    assert ps.done == 0
    con.close()


def test_retryable_classifier_does_not_retry_a_decoder_format_oserror():
    from belegsuche.extract import ExtractError
    try:
        try:
            raise OSError("cannot identify image file")
        except OSError as source:
            raise ExtractError("Bild nicht lesbar (cannot identify image file)") from source
    except ExtractError as error:
        assert not mailmod.retryable_part_error(error)


def test_multiple_unresolved_links_share_one_directory_listing(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    act = root / "Vorgang"
    attachments = []
    for name in ("Dach.pdf", "Rinne.pdf"):
        placeholder = f"Die Datei '{name}' wurde in die Dokumentenverwaltung von DOMUS CRM verschoben. 03.02.2023 10:15 EB"
        attachments.append((name + ".txt", placeholder.encode(), "application/octet-stream"))
    for path in (act / "Emails" / "a.eml", act / "Emails" / "b.eml", act / "c.eml"):
        make_eml(path, text="Bitte die Unterlagen bearbeiten.", attachments=attachments)
    con = dbm.connect(tmp_path / "i.db")
    indexer.run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    for name in ("Dach.pdf", "Rinne.pdf"):
        digital_pdf(act / name, [[f"Rechnung Dachdecker Fiktion {name}", "Material und Reparatur des Wohnhauses."]])
    rid = con.execute("SELECT id FROM roots").fetchone()[0]
    indexer.scan_root(con, rid, str(root), log=quiet)
    calls = []
    real_scandir = mailmod.os.scandir

    def counted_scandir(path):
        calls.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(mailmod.os, "scandir", counted_scandir)
    assert indexer._refresh_unresolved_links(con, [rid]) == 6
    assert calls == [act]
    assert {r[0] for r in con.execute("SELECT note FROM parts WHERE kind='link'")} == {
        "Vorgang/Dach.pdf", "Vorgang/Rinne.pdf"}
    assert dbm.linked_file_id(con, rid, "Vorgang/Dach.pdf") != dbm.linked_file_id(con, rid, "Vorgang/Rinne.pdf")
    con.close()
