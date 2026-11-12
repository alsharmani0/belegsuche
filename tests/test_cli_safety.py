"""CLI regressions using only disposable files."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from belegsuche import cli, db, indexer


def make_index(tmp_path):
    path = tmp_path / "index.db"
    con = db.connect(path)
    con.execute("INSERT INTO roots(path) VALUES (?)", (str(tmp_path / "belege"),))
    con.commit()
    con.close()
    return path


@pytest.mark.parametrize("command", ["bericht", "ordner"])
def test_current_index_can_be_read_without_write_permission(tmp_path, command, capsys):
    path = make_index(tmp_path)
    path.chmod(0o444)
    try:
        if os.access(path, os.W_OK):
            pytest.skip("Dieser Benutzer kann schreibgeschützte Dateien ändern")
        assert cli.main(["--db", str(path), command]) == 0
        assert not capsys.readouterr().err
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("destination", ["db", "hardlink", "wal", "original", "symlink"])
def test_report_export_cannot_clobber_index_or_original(tmp_path, destination):
    path = make_index(tmp_path)
    docs = tmp_path / "belege"
    docs.mkdir()
    receipt = docs / "rechnung.pdf"
    receipt.write_bytes(b"%PDF-1.7\nOriginal")
    output = {"db": path, "wal": Path(str(path) + "-wal"), "original": receipt}.get(
        destination, tmp_path / "ausgabe.csv")
    if destination == "hardlink":
        os.link(path, output)
    if destination == "symlink":
        output.symlink_to(receipt)
    assert cli.main(["--db", str(path), "bericht", "--csv", str(output)]) != 0
    assert receipt.read_bytes() == b"%PDF-1.7\nOriginal"
    con = db.connect(path, readonly=True)
    try:
        assert con.execute("SELECT count(*) FROM roots").fetchone()[0] == 1
    finally:
        con.close()


def test_json_export_preserves_questions(tmp_path):
    path = make_index(tmp_path)
    questions = tmp_path / "fragen.csv"
    data = b"suche;datei\nWartung;rechnung.pdf\n"
    questions.write_bytes(data)
    assert cli.main(["--db", str(path), "test", str(questions), "--json", str(questions)]) != 0
    assert questions.read_bytes() == data


def test_index_inside_archive_is_rejected_before_creating_files(tmp_path):
    archive = tmp_path / "Belege"
    archive.mkdir()
    path = archive / "unbeabsichtigt" / "index.db"
    assert cli.main(["--db", str(path), "index", str(archive)]) != 0
    assert list(archive.iterdir()) == []


def test_failed_export_preserves_old_report(tmp_path):
    path = make_index(tmp_path)
    old = tmp_path / "bericht.csv"
    old.write_text("voriger Bericht")

    def failed_write(report, output):
        output.write_text("unvollständig")
        raise OSError("Kein Platz mehr")

    with patch("belegsuche.report.write_problems_csv", failed_write):
        assert cli.main(["--db", str(path), "bericht", "--csv", str(old)]) != 0
    assert old.read_text() == "voriger Bericht"
    assert not list(tmp_path.glob(".belegsuche-*"))


def test_root_mutation_is_refused_while_indexing(tmp_path):
    path = make_index(tmp_path)
    with indexer.acquire_lock(path):
        with pytest.raises(SystemExit, match="läuft bereits"):
            cli.main(["--db", str(path), "ordner", "entfernen", "1"])
    con = db.connect(path, readonly=True)
    try:
        assert con.execute("SELECT count(*) FROM roots").fetchone()[0] == 1
    finally:
        con.close()


def test_interrupted_index_does_not_claim_completion(tmp_path, capsys):
    with patch.object(indexer, "run_index", return_value=([], indexer.ProcessStats(interrupted=True))):
        assert cli.main(["--db", str(tmp_path / "i.db"), "index"]) == 130
    assert "Fertig:" not in capsys.readouterr().out


@pytest.mark.parametrize("args", [["ordner", "entfernen", "1", "anderer-ordner"],
                                 ["test", "fragen.csv", "--k", "0"], ["test", "fragen.csv", "--k", "51"]])
def test_invalid_arguments_fail_before_database_access(args, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(tmp_path / "nicht-vorhanden.db"), *args])
    assert exc.value.code == 2
