"""Isolated regressions for cache completeness and long result snippets."""
from __future__ import annotations

import io

from PIL import Image

from belegsuche import db as dbm
from belegsuche import extract as extractmod
from belegsuche import indexer
from belegsuche import mail as mailmod
from belegsuche import tnef
from belegsuche.search import Searcher, make_snippet

from .conftest import BytesEngine, PT_LONG, digital_pdf, make_eml, tnef_bytes, tree_state


def test_truncated_standalone_file_does_not_supply_complete_attachment_cache(tmp_path, monkeypatch):
    root = tmp_path / "belege"
    root.mkdir()
    images = [Image.new("RGB", (400, 400), color) for color in ("white", "red", "green")]
    stream = io.BytesIO()
    images[0].save(stream, format="TIFF", save_all=True, append_images=images[1:])
    payload = stream.getvalue()
    (root / "fax.tif").write_bytes(payload)
    db = tmp_path / "index.db"
    con = dbm.connect(db)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(extractmod, "MAX_IMAGE_PAGES", 1)
            indexer.run_index(con, [str(root)], engine=BytesEngine(), log=lambda _: None)
        assert con.execute("SELECT pages,error FROM files").fetchone()[:] == (
            1, "Hinweis: nur die ersten 1 von 3 Seiten eingelesen")
        make_eml(root / "fax.eml", text="Anbei das vollständige Fax.",
                 attachments=[("fax.tif", payload, "image/tiff")])
        before = tree_state(root)
        engine = BytesEngine()
        _, result = indexer.run_index(con, [], engine=engine, log=lambda _: None)
        attachment = con.execute("SELECT n_pages,note FROM parts WHERE kind='image'").fetchone()
        assert attachment[0] == 3
        assert attachment[1] != mailmod.REUSED_NOTE
        assert engine.calls == 3 and result.ocr_pages == 3
        assert tree_state(root) == before
    finally:
        con.close()


def test_snippet_window_scores_terms_that_will_actually_be_visible():
    text = "Rechnung " * 10000 + "Kolibri Dachwartung Rechnung"
    html, plain = make_snippet(text, {"rechnung", "kolibri", "dachwartung"}, width=100)
    assert "<mark>Kolibri</mark>" in html
    assert "<mark>Dachwartung</mark>" in html
    assert "»Kolibri«" in plain and "»Dachwartung«" in plain


def test_file_id_highwater_survives_cascade_delete_and_reopen(tmp_path):
    db = tmp_path / "index.db"
    con = dbm.connect(db)
    root = con.execute("INSERT INTO roots(path) VALUES ('synthetic')").lastrowid
    # Eine bestehende Datei-ID bleibt auch nach Entfernen ihres gesamten Ordners verbraucht.
    con.execute("INSERT INTO files(id,root_id,relpath,status) VALUES (50,?,'alt.pdf','done')", (root,))
    con.commit()
    con.execute("DELETE FROM roots WHERE id=?", (root,))
    con.commit()
    con.close()
    root_dir = tmp_path / "belege"
    make_eml(root_dir / "neu.eml", text="Neue vollständig andere Mail.")
    con = dbm.connect(db)
    try:
        indexer.run_index(con, [str(root_dir)], engine=BytesEngine(), log=lambda _: None)
        assert con.execute("SELECT id FROM files").fetchone()[0] == 51
        # Abgebrochene Änderungen verbrauchen keine sichtbaren IDs und ändern den Zähler nicht.
        rid = con.execute("SELECT id FROM roots").fetchone()[0]
        before = dbm.get_meta(con, "file_id_highwater")
        con.execute("INSERT INTO files(id,root_id,relpath,status) VALUES (100,?,'rollback.pdf','pending')", (rid,))
        con.rollback()
        assert dbm.get_meta(con, "file_id_highwater") == before == "51"
    finally:
        con.close()


def test_existing_file_ids_initialize_highwater_before_deletion(tmp_path):
    db = tmp_path / "legacy.db"
    con = dbm.connect(db)
    rid = con.execute("INSERT INTO roots(path) VALUES ('synthetic')").lastrowid
    con.execute("INSERT INTO files(id,root_id,relpath,status) VALUES (75,?,'alt.pdf','done')", (rid,))
    con.execute("DROP TRIGGER IF EXISTS files_id_highwater")
    con.execute("DELETE FROM meta WHERE key='file_id_highwater'")
    con.commit()
    con.close()
    con = dbm.connect(db)
    try:
        assert con.execute("SELECT id FROM files").fetchone()[0] == 75
        assert dbm.get_meta(con, "file_id_highwater") == "75"
    finally:
        con.close()


def test_format12_tnef_body_is_recovered_once_without_repeating_image_ocr(tmp_path, monkeypatch):
    root = tmp_path / "belege"
    expected = "Kündigung Müller März 2025"
    stream = tnef_bytes([], html=f"<p>{expected}</p>".encode("utf-16-le"), codepage=1200,
                        extra_props=[(PT_LONG, tnef.PR_MESSAGE_CODEPAGE, 1252)])
    photo = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(photo, "PNG")
    make_eml(root / "brief.eml", text="", attachments=[
        ("winmail.dat", stream, "application/ms-tnef"), ("Foto.png", photo.getvalue(), "image/png")])
    db = tmp_path / "index.db"
    with monkeypatch.context() as patch:
        patch.setattr(dbm, "INDEX_FORMAT", "12")
        patch.setattr(tnef, "_decode8", lambda raw, cp: tnef.decode_cp(
            bytes(raw).split(b"\x00", 1)[0], tnef.codec_name(cp)))
        con = dbm.connect(db)
        engine = BytesEngine()
        indexer.run_index(con, [str(root)], engine=engine, log=lambda _: None)
        assert engine.calls == 1
        assert expected not in con.execute("SELECT text FROM pages WHERE page_no=1").fetchone()[0]
        assert {r[0] for r in con.execute("SELECT content_revision FROM parts")} == {12}
        con.close()
    before = tree_state(root)
    monkeypatch.setattr(dbm, "rebuild_search_index", lambda _: (_ for _ in ()).throw(
        AssertionError("Gezielte Reparatur braucht keinen Vollneuaufbau")))
    con = dbm.connect(db)
    try:
        engine = BytesEngine()
        _, result = indexer.run_index(con, [str(root)], engine=engine, log=lambda _: None)
        assert result.done == 1 and result.errors == 0 and engine.calls == 0
        assert expected in con.execute("SELECT text FROM pages WHERE page_no=1").fetchone()[0]
        assert Searcher(db).search("Kündigung Müller März 2025").hits
        _, result = indexer.run_index(con, [], engine=engine, log=lambda _: None)
        assert result.done == 0 and engine.calls == 0
        assert tree_state(root) == before
    finally:
        con.close()


def test_format13_does_not_reinspect_neutral_pdf_verified_at_format12(tmp_path, monkeypatch):
    root = tmp_path / "belege"
    pdf = tmp_path / "rechnung.pdf"
    digital_pdf(pdf, [["Rechnung für Dachwartung einer erfundenen Firma 7744"]])
    make_eml(root / "brief.eml", attachments=[("ATT0001.dat", pdf.read_bytes(), "application/octet-stream")])
    db = tmp_path / "index.db"
    with monkeypatch.context() as patch:
        patch.setattr(dbm, "INDEX_FORMAT", "12")
        con = dbm.connect(db)
        indexer.run_index(con, [str(root)], engine=BytesEngine(), log=lambda _: None)
        con.close()
    monkeypatch.setattr(mailmod, "find_part", lambda *_: (_ for _ in ()).throw(
        AssertionError("Neutrales PDF wurde bereits in Revision 12 geprüft")))
    con = dbm.connect(db)
    try:
        _, result = indexer.run_index(con, [str(root)], engine=BytesEngine(), log=lambda _: None)
        assert result.done == 0
        assert con.execute("SELECT content_revision FROM parts WHERE kind='pdf'").fetchone()[0] == 12
    finally:
        con.close()


def test_old_truncated_attachments_are_repaired_without_reusing_another_short_copy(tmp_path, monkeypatch):
    root, other = tmp_path / "belege", tmp_path / "anderer_ordner"
    root.mkdir()
    pictures = [Image.new("RGB", (400, 400), color) for color in ("white", "red", "green")]
    stream = io.BytesIO()
    pictures[0].save(stream, format="TIFF", save_all=True, append_images=pictures[1:])
    payload = stream.getvalue()
    (root / "fax.tif").write_bytes(payload)
    con = dbm.connect(tmp_path / "index.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(extractmod, "MAX_IMAGE_PAGES", 1)
            indexer.run_index(con, [str(root)], engine=BytesEngine(), log=lambda _: None)
        donor = con.execute("SELECT id FROM files").fetchone()[0]
        for path in (root / "a.eml", root / "b.eml", other / "c.eml"):
            make_eml(path, text=f"Fax {path.stem}", attachments=[("fax.tif", payload, "image/tiff")])
        # Den historischen Fehler nachbilden: ein früherer Lauf hielt die eine gespeicherte Seite für vollständig.
        with monkeypatch.context() as patch:
            patch.setattr(indexer, "_pages_for_sha", lambda connection, *_: indexer._pages_of(connection, donor))
            patch.setattr(indexer, "_repair_truncated_reuse", lambda *_: 0)
            indexer.run_index(con, [str(root), str(other)], engine=BytesEngine(), log=lambda _: None)
        assert [tuple(r) for r in con.execute("SELECT n_pages,note FROM parts WHERE kind='image'")] == [
            (1, mailmod.REUSED_NOTE)] * 3
        before = tree_state(root), tree_state(other)
        engine = BytesEngine()
        _, result = indexer.run_index(con, [str(root)], engine=engine, log=lambda _: None)
        assert result.done == 2 and result.errors == 0 and engine.calls == 3
        assert [r[0] for r in con.execute("SELECT n_pages FROM parts WHERE kind='image' ORDER BY file_id")] == [3, 3, 1]
        _, result = indexer.run_index(con, [str(other)], engine=engine, log=lambda _: None)
        assert result.done == 1 and engine.calls == 3
        assert {r[0] for r in con.execute("SELECT n_pages FROM parts WHERE kind='image'")} == {3}
        _, result = indexer.run_index(con, [], engine=engine, log=lambda _: None)
        assert result.done == 0 and engine.calls == 3
        assert (tree_state(root), tree_state(other)) == before
    finally:
        con.close()
