"""CLI regressions using only disposable files."""
import os
import tempfile
from contextlib import closing
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


@pytest.mark.parametrize("action", ["index", "update", "remove", "move"])
def test_archive_guard_precedes_lock_creation(tmp_path, monkeypatch, action):
    archive = tmp_path / "archiv"
    temp = archive / "tmp"
    temp.mkdir(parents=True)
    path = make_index(tmp_path)
    if action in ("update", "remove"):
        with closing(db.connect(path)) as con:
            con.execute("UPDATE roots SET path=?", (str(archive),))
            con.commit()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    args = {"index": ["index", str(archive)], "update": ["index"],
            "remove": ["ordner", "entfernen", "1"], "move": ["ordner", "umziehen", "1", str(archive)]}
    with pytest.raises(SystemExit, match="Temp-Ordner"):
        cli.main(["--db", str(path), *args[action]])
    assert list(temp.iterdir()) == []


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


def test_repeating_removal_does_not_remove_a_new_archive(tmp_path):
    path = make_index(tmp_path)
    new_root = tmp_path / "neuer-ordner"
    new_root.mkdir()
    assert cli.main(["--db", str(path), "ordner", "entfernen", "1"]) == 0
    assert cli.main(["--db", str(path), "index", str(new_root), "--limit", "0"]) == 0
    assert cli.main(["--db", str(path), "ordner", "entfernen", "1"]) == 1
    with closing(db.connect(path, readonly=True)) as con:
        assert con.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 1


def test_interrupted_index_does_not_claim_completion(tmp_path, capsys):
    with patch.object(indexer, "run_index", return_value=([], indexer.ProcessStats(interrupted=True))):
        assert cli.main(["--db", str(tmp_path / "i.db"), "index"]) == 130
    assert "Fertig:" not in capsys.readouterr().out


@pytest.mark.parametrize("args", [["ordner", "entfernen", "1", "anderer-ordner"],
                                 ["ordner", "entfernen", str(2**63)],
                                 ["test", "fragen.csv", "--k", "0"], ["test", "fragen.csv", "--k", "51"]])
def test_invalid_arguments_fail_before_database_access(args, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(tmp_path / "nicht-vorhanden.db"), *args])
    assert exc.value.code == 2
