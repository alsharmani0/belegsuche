"""Hängende Datei im parallelen Lauf: erst allein wiederholen, Stapelspur sichern, Rückfall später nachholen.

Im echten Lauf über alle Domus-Mails hingen zwei Mails je 10 Minuten, die allein in 1–2 Sekunden durchgingen.
Ein Hänger unter Last darf deshalb nicht gleich die Anhänge kosten.
"""
import time

import pytest

from belegsuche import db as dbm
from belegsuche import indexer
from belegsuche import mail as mailmod
from belegsuche.indexer import count_safe_mails, run_index
from belegsuche.search import Searcher

from .conftest import digital_pdf, make_eml


def quiet(_):
    pass


def _mail_mit_pdf(root, tmp_path, name="haengt.eml"):
    digital_pdf(tmp_path / "anhang.pdf", [["Abrechnung Kanalreinigung Rohrblitz Auftrag 77341"]])
    make_eml(root / name, subject="Hänger", text="Mailtext Hänger Kowalski",
             attachments=[("Rechnung.pdf", (tmp_path / "anhang.pdf").read_bytes(), "application/pdf")])
    for i in range(2):
        digital_pdf(root / f"r{i}.pdf", [[f"Rechnung Nummer {i} Wärmetechnik Brandl"]])


def _rows(con):
    return {r["relpath"]: r for r in con.execute("SELECT id, relpath, status, error, attempts FROM files")}


@pytest.mark.vision
def test_einmaliger_haenger_einer_mail_wird_allein_wiederholt_mit_anhang(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    _mail_mit_pdf(root, tmp_path)
    monkeypatch.setenv("BELEGSUCHE_TEST_HANG_ON", "haengt.eml")
    monkeypatch.setenv("BELEGSUCHE_TEST_HANG_ONCE_FLAG", str(tmp_path / "einmal.flag"))
    monkeypatch.setattr(indexer, "STALL_TIMEOUT", 3)
    logs = []
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], log=logs.append, workers=2)
    rows = _rows(con)
    assert rows["haengt.eml"]["status"] == "done" and rows["haengt.eml"]["error"] is None
    notes = [r[0] for r in con.execute("SELECT note FROM parts WHERE file_id = ?", (rows["haengt.eml"]["id"],))]
    assert mailmod.SAFE_NOTE not in notes                           # Anhang gelesen, kein Rückfall
    assert any("wird allein wiederholt" in s for s in logs)
    assert ps.errors == 0 and count_safe_mails(con) == 0
    hits = Searcher(tmp_path / "i.db").search("Rohrblitz 77341").hits
    assert hits and hits[0].relpath.endswith("haengt.eml") and hits[0].part_name == "Rechnung.pdf"
    # Stapelspur bleibt neben dem Index, obwohl der Ordner mit den Markierungsdateien am Ende gelöscht wird
    log = (tmp_path / indexer.HANG_LOG).read_text(encoding="utf-8")
    assert "haengt.eml" in log and "erster Versuch" in log and "Zeitlimit 3 s" in log
    con.close()


@pytest.mark.vision
def test_einmaliger_haenger_einer_pdf_wird_allein_wiederholt(tmp_path, monkeypatch):
    root = tmp_path / "k"
    root.mkdir()
    for i in range(3):
        digital_pdf(root / f"r{i}.pdf", [[f"Rechnung Nummer {i} Wärmetechnik Brandl"]])
    monkeypatch.setenv("BELEGSUCHE_TEST_HANG_ON", "r1.pdf")
    monkeypatch.setenv("BELEGSUCHE_TEST_HANG_ONCE_FLAG", str(tmp_path / "einmal.flag"))
    monkeypatch.setattr(indexer, "STALL_TIMEOUT", 3)
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], log=quiet, workers=2)
    assert {k: r["status"] for k, r in _rows(con).items()} == {"r0.pdf": "done", "r1.pdf": "done", "r2.pdf": "done"}
    assert ps.errors == 0
    con.close()


@pytest.mark.vision
def test_dauerhafter_haenger_rueckfall_und_spaeter_nachholen(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    _mail_mit_pdf(root, tmp_path)
    monkeypatch.setenv("BELEGSUCHE_TEST_HANG_ON", "haengt.eml")
    monkeypatch.setattr(indexer, "STALL_TIMEOUT", 3)
    monkeypatch.setattr(indexer, "STALL_TIMEOUT_RETRY", 2)
    con = dbm.connect(tmp_path / "i.db")
    t0 = time.time()
    _, ps = run_index(con, [str(root)], log=quiet, workers=2)
    assert time.time() - t0 < 90
    rows = _rows(con)
    assert rows["haengt.eml"]["status"] == "done" and rows["haengt.eml"]["error"] == mailmod.SAFE_MAIL_NOTE
    assert rows["r0.pdf"]["status"] == rows["r1.pdf"]["status"] == "done"
    assert count_safe_mails(con) == 1
    log = (tmp_path / indexer.HANG_LOG).read_text(encoding="utf-8")
    assert "erster Versuch, Zeitlimit 3 s" in log and "Wiederholung allein, Zeitlimit 2 s" in log

    # Später (Hänger vorbei): --fehler-wiederholen liest die Mail ganz, die übrigen Dateien bleiben unberührt
    monkeypatch.delenv("BELEGSUCHE_TEST_HANG_ON")
    _, ps2 = run_index(con, [], log=quiet, workers=2, retry=True)
    rows2 = _rows(con)
    assert ps2.retried == 1 and ps2.done == 1
    assert rows2["haengt.eml"]["status"] == "done" and rows2["haengt.eml"]["error"] is None
    assert count_safe_mails(con) == 0
    hits = Searcher(tmp_path / "i.db").search("Rohrblitz 77341").hits
    assert hits and hits[0].relpath.endswith("haengt.eml") and hits[0].part_name == "Rechnung.pdf"
    con.close()


@pytest.mark.vision
def test_gleiche_kopie_uebernimmt_keinen_rueckfall_stand(tmp_path, monkeypatch):
    """Eine bytegleiche Kopie einer nur mit Mailtext gelesenen Mail wird ganz gelesen, nicht übernommen."""
    root = tmp_path / "ordner"
    _mail_mit_pdf(root, tmp_path)
    monkeypatch.setenv("BELEGSUCHE_TEST_HANG_ON", "haengt.eml")
    monkeypatch.setattr(indexer, "STALL_TIMEOUT", 2)
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], log=quiet, workers=2)
    assert count_safe_mails(con) == 1
    monkeypatch.delenv("BELEGSUCHE_TEST_HANG_ON")
    (root / "kopie.eml").write_bytes((root / "haengt.eml").read_bytes())
    run_index(con, [str(root)], log=quiet, workers=2)
    rows = _rows(con)
    assert rows["kopie.eml"]["status"] == "done" and rows["kopie.eml"]["error"] is None
    assert count_safe_mails(con) == 1                               # nur das Original wartet noch
    con.close()


def test_fehler_wiederholen_ohne_rueckfall_mails_aendert_nichts(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", text="Mailtext normal")
    con = dbm.connect(tmp_path / "i.db")
    from .conftest import BytesEngine
    run_index(con, [str(root)], log=quiet, engine=BytesEngine())
    assert count_safe_mails(con) == 0
    assert indexer.retry_errors(con) == 0
    assert con.execute("SELECT status FROM files").fetchone()[0] == "done"
    con.close()
