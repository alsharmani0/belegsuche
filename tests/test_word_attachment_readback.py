"""Textbasierte Word-Anlagen: Indextext, Originalprüfung und sichere Textausgabe zusammen prüfen."""
from contextlib import closing
from pathlib import Path

import pytest

from belegsuche import cli, db, server
from belegsuche.indexer import run_index
from .conftest import BytesEngine, make_eml


def test_text_word_attachment_is_returned_as_plain_text_after_source_check(tmp_path):
    root = tmp_path / "archiv"
    original = b"<html><body><p>Leistungsnachweis Tiefblau</p></body></html>"
    source = make_eml(root / "test.eml", attachments=[("Nachweis.doc", original, "application/msword")])
    index = tmp_path / "test.db"
    with closing(db.connect(index)) as con:
        run_index(con, [str(root)], engine=BytesEngine(), log=lambda _: None)
        file_id, part = con.execute("SELECT file_id, part_no FROM parts WHERE name='Nachweis.doc'").fetchone()
    before = source.read_bytes()
    value, kind, filename, roots = server._read_part(index, file_id, part)
    assert (value.decode().strip(), kind, filename) == ("Leistungsnachweis Tiefblau", "txt", "Nachweis.txt")
    assert [Path(r) for r in roots] == [root.resolve()]
    assert source.read_bytes() == before

    make_eml(source, attachments=[("Nachweis.doc", original.replace(b"Tiefblau", b"Goldgelb"), "application/msword")])
    with pytest.raises(server.PartError) as exc:
        server._read_part(index, file_id, part)
    assert exc.value.status == 409


def test_cli_exposes_retryable_part_and_clears_hint_after_recovery(tmp_path, capsys, monkeypatch):
    from .test_mail_index_repairs import make_partial
    root, _, index, con = make_partial(tmp_path)
    con.close()
    monkeypatch.setenv("BELEGSUCHE_DB", str(index))
    assert cli.main(["bericht"]) == 0
    report = capsys.readouterr().out
    assert "Vorübergehende Anhangfehler: 1" in report and "Anhang 1 vorübergehend nicht lesbar" in report
    monkeypatch.setattr("belegsuche.ocr.make_engine", lambda _: BytesEngine())
    assert cli.main(["index", str(root), "--parallel", "1", "--fehler-wiederholen"]) == 0
    capsys.readouterr()
    assert cli.main(["bericht"]) == 0
    assert "Vorübergehende Anhangfehler" not in capsys.readouterr().out
