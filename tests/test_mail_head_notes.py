"""Vermerke im Mailkopf: „Aus dem CRM gesendet“ statt „Entwurf“, Speicherdatum statt Sendedatum und das gezielte
Erneuern älterer Indexstände (index --mailkopf-erneuern).

Nur erfundene Daten, Adressen auf .example; jede Datenbank liegt in tmp_path.
"""
import io
import os
import time
from email import policy
from email.message import EmailMessage

import pytest
from PIL import Image

from belegsuche import cli
from belegsuche import db as dbm
from belegsuche import mail as mailmod
from belegsuche.extract import extract_full
from belegsuche.indexer import flag_stale_mail_heads, run_index
from belegsuche.search import NO_DATE_HIT_SUFFIX, Searcher

from .conftest import BytesEngine, make_eml, tree_state

CRM_SENT = {"X-Unsent": "1", "X-tblEMail-Typ": "Sent"}
STORED = time.mktime((2023, 2, 3, 10, 0, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def _nie_echter_index(tmp_path, monkeypatch):
    monkeypatch.setenv("BELEGSUCHE_DB", str(tmp_path / "env-index.db"))


def quiet(_):
    pass


def head_lines(path) -> list[str]:
    return extract_full(path, None).pages[0].text.split("\n\n")[0].split("\n")


def photo() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (900, 700), "white").save(out, "PNG")
    return out.getvalue()


# --- Vermerk zu X-Unsent ------------------------------------------------------------------------------------

def test_mail_sent_from_crm_is_not_called_a_draft(tmp_path):
    lines = head_lines(make_eml(tmp_path / "m.eml", text="Bitte um Rückruf.", headers=CRM_SENT))
    assert mailmod.CRM_SENT_LINE in lines and mailmod.CRM_SENT_LINE == "Aus dem CRM gesendet"
    assert mailmod.DRAFT_LINE not in lines


@pytest.mark.parametrize("typ", ["sent", "SENT", " Sent "])
def test_crm_type_is_compared_without_case_and_spaces(tmp_path, typ):
    lines = head_lines(make_eml(tmp_path / "m.eml", headers={"X-Unsent": "1", "X-tblEMail-Typ": typ}))
    assert mailmod.CRM_SENT_LINE in lines and mailmod.DRAFT_LINE not in lines


@pytest.mark.parametrize("typ", [None, "", "Draft", "Received", "Sent later"])
def test_unsent_without_crm_type_sent_stays_a_draft(tmp_path, typ):
    headers = {"X-Unsent": "1"} if typ is None else {"X-Unsent": "1", "X-tblEMail-Typ": typ}
    lines = head_lines(make_eml(tmp_path / "m.eml", headers=headers))
    assert mailmod.DRAFT_LINE in lines and mailmod.CRM_SENT_LINE not in lines


@pytest.mark.parametrize("headers", [{}, {"X-Unsent": "0"}, {"X-Unsent": "0", "X-tblEMail-Typ": "Sent"},
                                     {"X-tblEMail-Typ": "Sent"}])
def test_no_note_without_unsent_flag(tmp_path, headers):
    lines = head_lines(make_eml(tmp_path / "m.eml", headers=headers))
    assert mailmod.DRAFT_LINE not in lines and mailmod.CRM_SENT_LINE not in lines


def test_attached_crm_mail_gets_the_same_note(tmp_path):
    inner = EmailMessage(policy=policy.SMTP)
    inner["Subject"], inner["From"], inner["To"] = "Antwort", "buero@hv-nord.example", "erika@firma.example"
    inner["X-Unsent"], inner["X-tblEMail-Typ"] = "1", "Sent"
    inner.set_content("Der Termin ist bestätigt.")
    res = extract_full(make_eml(tmp_path / "m.eml", text="zur Kenntnis", attachments=[inner]), None)
    nested = res.pages[1].text.split("\n\n")[0].split("\n")
    assert nested[0] == "Weitergeleitete Mail" and mailmod.CRM_SENT_LINE in nested
    assert mailmod.CRM_SENT_LINE not in res.pages[0].text


# --- Ersatzdatum ---------------------------------------------------------------------------------------------

def test_missing_date_is_labelled_as_storage_date_not_sent_date(tmp_path):
    p = make_eml(tmp_path / "m.eml", date=None, text="Antwort", headers=CRM_SENT)
    os.utime(p, (STORED, STORED))
    res = extract_full(p, None)
    lines = res.pages[0].text.split("\n\n")[0].split("\n")
    assert "Datum: 03.02.2023 10:00 (Speicherdatum der Datei, nicht das Sendedatum)" in lines
    assert mailmod.CRM_SENT_LINE in lines
    assert res.mail.sent_at is None


def test_hit_line_says_storage_date_not_sent_date(tmp_path):
    root = tmp_path / "ordner"
    p = make_eml(root / "m.eml", date=None, subject="Zählerstand Birkenweg", text="Antwort", headers=CRM_SENT)
    os.utime(p, (STORED, STORED))
    make_eml(root / "n.eml", subject="Ablesung Birkenweg", text="Termin")
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    con.close()
    hits = {h.filename: h for h in Searcher(db).search("Birkenweg").hits}
    assert NO_DATE_HIT_SUFFIX == " (Speicherdatum, nicht Sendedatum)"
    assert hits["m.eml"].mail_date == "03.02.2023 10:00" + NO_DATE_HIT_SUFFIX
    assert hits["n.eml"].mail_date == "12.03.2021 14:05"


# --- Vermerke werden angezeigt, zählen aber nicht als Mailtext -----------------------------------------------

def test_notes_are_shown_but_not_searchable(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "a.eml", subject="Zählerstand", text="Bitte um Rückruf.", headers=CRM_SENT)
    make_eml(root / "b.eml", subject="Angebot", text="Dachrinne erneuern.", headers={"X-Unsent": "1"})
    make_eml(root / "text.eml", subject="Mahnung", text="Der Entwurf wurde aus dem CRM gesendet.")
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    shown = dict(con.execute("SELECT f.relpath, p.text FROM pages p JOIN files f ON f.id = p.file_id"))
    con.close()
    assert mailmod.CRM_SENT_LINE in shown["a.eml"] and mailmod.DRAFT_LINE in shown["b.eml"]
    for query in ("gesendet", "CRM", "Entwurf"):
        assert [h.filename for h in Searcher(db).search(query).hits] == ["text.eml"], query
    assert [h.filename for h in Searcher(db).search("Rückruf").hits] == ["a.eml"]


# --- älteren Indexstand erneuern -----------------------------------------------------------------------------

def old_index(tmp_path):
    """Index, dessen Seitentexte die Vermerke einer früheren Version tragen (im Index nachgestellt)."""
    root = tmp_path / "ordner"
    inner = EmailMessage(policy=policy.SMTP)
    inner["Subject"], inner["From"], inner["To"] = "Antwort", "buero@hv-nord.example", "erika@firma.example"
    inner["X-Unsent"], inner["X-tblEMail-Typ"] = "1", "Sent"
    inner.set_content("Der Termin ist bestätigt.")
    undated = [make_eml(root / "V1" / "Emails" / "crm.eml", date=None, subject="Zählerstand",
                        text="Bitte um Rückruf.", headers=CRM_SENT,
                        attachments=[("Kassenbon.png", photo(), "image/png")]),
               make_eml(root / "ohne.eml", date=None, subject="Notiz", text="Ohne Datum abgelegt.")]
    for p in undated:
        os.utime(p, (STORED, STORED))
    make_eml(root / "crm-datum.eml", subject="Ablesung", text="Termin am Dienstag.", headers=CRM_SENT)
    make_eml(root / "entwurf.eml", subject="Angebot", text="Noch nicht fertig.", headers={"X-Unsent": "1"})
    make_eml(root / "weiter.eml", subject="Weiterleitung", text="zur Kenntnis", attachments=[inner])
    make_eml(root / "normal.eml", subject="Eingang", text="Eingang bestätigt.")
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    engine = BytesEngine()
    run_index(con, [str(root)], engine=engine, log=quiet)
    con.execute("UPDATE pages SET text = replace(replace(text, ?, ?), ?, ?)",
                (mailmod.CRM_SENT_LINE, mailmod.DRAFT_LINE, mailmod.NO_DATE_SUFFIX, mailmod.OLD_NO_DATE_SUFFIX))
    for fid, relpath in con.execute("SELECT id, relpath FROM files").fetchall():
        dbm.index_file_text(con, fid, relpath)
    con.commit()
    return root, db, con, engine


def texts(con) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel, text in con.execute("SELECT f.relpath, p.text FROM pages p JOIN files f ON f.id = p.file_id"
                                 " ORDER BY f.relpath, p.page_no"):
        out[rel] = out.get(rel, "") + text + "\f"
    return out


def test_plain_index_run_keeps_old_notes(tmp_path):
    root, db, con, engine = old_index(tmp_path)
    before = texts(con)
    _, ps = run_index(con, [], engine=engine, log=quiet)
    assert ps.done == 0 and texts(con) == before
    assert mailmod.DRAFT_LINE in before["V1/Emails/crm.eml"]
    assert con.execute("SELECT count(*) FROM parts WHERE note = ?", (dbm.STALE_MAIL_HEAD_NOTE,)).fetchone()[0] == 0
    con.close()


def test_old_date_suffix_stays_out_of_the_search_text(tmp_path):
    root, db, con, _ = old_index(tmp_path)
    con.close()
    assert Searcher(db).search("03.02.2023").hits == []
    assert Searcher(db).search("gesendet").hits == []           # alter Vermerk „Entwurf (nicht gesendet)“


def test_refresh_rereads_only_mails_with_old_notes(tmp_path):
    root, db, con, engine = old_index(tmp_path)
    before, calls = tree_state(root), engine.calls
    log: list[str] = []
    _, ps = run_index(con, [], engine=engine, refresh_mail_heads=True, log=log.append)
    assert ps.done == 5 and ps.errors == 0
    assert any("5 E-Mail(s) mit einem Kopfvermerk einer früheren Version werden neu gelesen" in x for x in log)
    now = texts(con)
    crm = now["V1/Emails/crm.eml"]
    assert mailmod.CRM_SENT_LINE in crm and mailmod.DRAFT_LINE not in crm
    assert f"Datum: 03.02.2023 10:00 {mailmod.NO_DATE_SUFFIX}" in crm and mailmod.OLD_NO_DATE_SUFFIX not in crm
    assert "Kassenbon" in crm                                   # Seite des Anhangs übernommen …
    assert engine.calls == calls                                # … ohne neue Texterkennung
    assert mailmod.CRM_SENT_LINE in now["crm-datum.eml"] and mailmod.DRAFT_LINE not in now["crm-datum.eml"]
    assert mailmod.NO_DATE_SUFFIX in now["ohne.eml"] and mailmod.OLD_NO_DATE_SUFFIX not in now["ohne.eml"]
    assert mailmod.CRM_SENT_LINE in now["weiter.eml"] and mailmod.DRAFT_LINE not in now["weiter.eml"]
    assert mailmod.DRAFT_LINE in now["entwurf.eml"]             # echter Entwurf bleibt einer
    assert con.execute("SELECT count(*) FROM parts WHERE note = ?", (dbm.STALE_MAIL_HEAD_NOTE,)).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM files WHERE status != 'done'").fetchone()[0] == 0
    assert tree_state(root) == before                           # Originale unverändert
    con.close()
    assert [h.filename for h in Searcher(db).search("Kassenbon Baumarkt").hits] == ["crm.eml"]
    assert Searcher(db).search("gesendet").hits == []


def test_refresh_again_only_checks_real_drafts(tmp_path):
    root, db, con, engine = old_index(tmp_path)
    run_index(con, [], engine=engine, refresh_mail_heads=True, log=quiet)
    assert flag_stale_mail_heads(con) == 1                      # nur entwurf.eml trägt noch „Entwurf“
    con.rollback()
    _, ps = run_index(con, [], engine=engine, refresh_mail_heads=True, log=quiet)
    assert ps.done == 1
    _, ps = run_index(con, [], engine=engine, log=quiet)
    assert ps.done == 0
    con.close()


def test_interrupted_refresh_continues_without_the_switch(tmp_path):
    root, db, con, engine = old_index(tmp_path)
    _, ps = run_index(con, [], engine=engine, refresh_mail_heads=True, limit=2, log=quiet)
    assert ps.done == 2
    log: list[str] = []
    _, ps = run_index(con, [], engine=engine, log=log.append)
    assert ps.done == 3 and any("3 E-Mail(s) mit einem Kopfvermerk" in x for x in log)
    assert all(mailmod.OLD_NO_DATE_SUFFIX not in t for t in texts(con).values())
    con.close()


def test_cli_switch_refreshes_old_notes(tmp_path, capsys):
    root, db, con, _ = old_index(tmp_path)
    con.close()
    assert cli.main(["--db", str(db), "index", "--parallel", "1"]) == 0
    assert "Kopfvermerk" not in capsys.readouterr().out
    assert cli.main(["--db", str(db), "index", "--mailkopf-erneuern", "--parallel", "1"]) == 0
    assert "5 E-Mail(s) mit einem Kopfvermerk einer früheren Version werden neu gelesen" in capsys.readouterr().out
    con = dbm.connect(db, readonly=True)
    assert mailmod.CRM_SENT_LINE in texts(con)["V1/Emails/crm.eml"]
    con.close()
