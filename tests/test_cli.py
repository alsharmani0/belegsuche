"""Kommandozeile: --db an beiden Stellen, fehlender Index, ordner umziehen, Sperre, Steuerzeichen."""
import pytest

from belegsuche import cli
from belegsuche.indexer import acquire_lock

from .conftest import digital_pdf


def run(capsys, *argv):
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def test_db_before_and_after_subcommand(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "a.pdf", [["Dachdecker Kowalski", "Rechnung D-9001"]])
    db = str(tmp_path / "i.db")
    rc, _ = run(capsys, "index", str(root), "--db", db, "--parallel", "1")
    assert rc == 0
    rc, out = run(capsys, "--db", db, "suche", "Kowalski")
    assert rc == 0 and "a.pdf" in out
    rc, out = run(capsys, "suche", "D-9001", "--db", db)
    assert rc == 0 and "a.pdf" in out


def test_missing_index_gives_hint_not_traceback(tmp_path, capsys):
    db = str(tmp_path / "nichtda.db")
    for cmd in (["suche", "x"], ["bericht"], ["ordner"]):
        rc, out = run(capsys, "--db", db, *cmd)
        assert rc == 1 and "Noch kein Index" in out


def test_eval_with_bad_csv(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "a.pdf", [["Hallo Beleg"]])
    db = str(tmp_path / "i.db")
    run(capsys, "--db", db, "index", str(root), "--parallel", "1")
    rc, out = run(capsys, "--db", db, "test", str(tmp_path / "fehlt.csv"))
    assert rc == 2 and "nicht gefunden" in out
    (tmp_path / "leer.csv").write_text("a;b\n1;2\n", encoding="utf-8")
    rc, out = run(capsys, "--db", db, "test", str(tmp_path / "leer.csv"))
    assert rc == 2 and "suche;datei" in out


def test_move_root_errors(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    digital_pdf(a / "x.pdf", [["Hallo Beleg eins"]])
    digital_pdf(b / "y.pdf", [["Hallo Beleg zwei"]])
    db = str(tmp_path / "i.db")
    run(capsys, "--db", db, "index", str(a), "--parallel", "1")
    run(capsys, "--db", db, "index", str(b), "--parallel", "1")
    rc, out = run(capsys, "--db", db, "ordner", "umziehen", "7", str(b))
    assert rc == 1 and "Unbekannte" in out
    rc, out = run(capsys, "--db", db, "ordner", "umziehen", "1", str(b))
    assert rc == 1 and "bereits" in out


def test_second_index_run_is_refused(tmp_path):
    db = tmp_path / "i.db"
    lock = acquire_lock(db)  # noqa: F841
    try:
        cli.main(["--db", str(db), "index"])
        raised = False
    except SystemExit as e:
        raised = "läuft bereits" in str(e)
    assert raised


@pytest.mark.vision
def test_control_characters_are_not_printed(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "Rechnung\x1b[2J\x1b[31mROT.pdf", [["Terminaltest Beleg"]])
    db = str(tmp_path / "i.db")
    run(capsys, "--db", db, "index", str(root), "--parallel", "1")
    rc, out = run(capsys, "--db", db, "suche", "Terminaltest")
    assert rc == 0 and "\x1b" not in out and "ROT.pdf" in out


def test_report_keeps_line_breaks(tmp_path, capsys):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "a.pdf", [["Hallo Beleg"]])
    db = str(tmp_path / "i.db")
    run(capsys, "--db", db, "index", str(root), "--parallel", "1")
    rc, out = run(capsys, "--db", db, "bericht")
    assert rc == 0 and "\\n" not in out and len(out.splitlines()) > 5
