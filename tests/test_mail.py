"""E-Mails (.eml): Kopf, Mailtext, Anhänge, DOMUS-Platzhalter, Wiederverwendung, Grenzen und Rückfälle.

Nur erfundene Daten (Namen erfunden, Adressen auf .example). Alles liegt in tmp_path; der echte Index wird nie
berührt (BELEGSUCHE_DB zeigt per autouse-Fixture auf tmp_path)."""
import io
import os
import socket
import tempfile
from datetime import datetime
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from belegsuche import db as dbm
from belegsuche import indexer
from belegsuche import mail as mailmod
from belegsuche.extract import NOTE_PREFIX, extract_full
from belegsuche.indexer import add_root, run_index, scan_root

from .conftest import BytesEngine, digital_pdf, docx_bytes, make_eml, scan_pdf, text_image, tree_state


@pytest.fixture(autouse=True)
def _nie_echter_index(tmp_path, monkeypatch):
    monkeypatch.setenv("BELEGSUCHE_DB", str(tmp_path / "env-index.db"))


def quiet(_):
    pass


def pdf_bytes(tmp_path, pages, name="r.pdf", password=None) -> bytes:
    p = tmp_path / name
    digital_pdf(p, pages, password=password)
    return p.read_bytes()


def png_bytes(lines=("Baumarkt Nord",), size=(900, 700)) -> bytes:
    buf = io.BytesIO()
    text_image(list(lines), size=size, font_size=36).save(buf, "PNG")
    return buf.getvalue()


def logo_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (120, 40), "navy").save(buf, "PNG")
    return buf.getvalue()


PLACEHOLDER = "Die Datei '{}' wurde in die Dokumentenverwaltung von DOMUS CRM verschoben. 03.02.2023 10:15 EB"


# --- Schema, Format 10, Wiederaufnahme --------------------------------------------------------------------

def _old_index_with_one_file(tmp_path, fmt="9"):
    root = tmp_path / "ordner"
    root.mkdir()
    digital_pdf(root / "a.pdf", [["Dachdecker Kowalski Rechnung 4711"]])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    con.execute("DROP TABLE parts")
    con.execute("DROP TABLE mails")
    dbm.set_meta(con, "index_format", fmt)
    con.commit()
    con.close()
    return root, db


def test_format_9_is_upgraded_without_rebuilding_the_search_index(tmp_path, monkeypatch):
    _, db = _old_index_with_one_file(tmp_path, "9")

    def no_rebuild(con):
        raise AssertionError("kein Neuaufbau bei Format 9")

    monkeypatch.setattr(dbm, "rebuild_search_index", no_rebuild)
    con = dbm.connect(db)
    assert dbm.get_meta(con, "index_format") == dbm.INDEX_FORMAT
    assert dbm.has_table(con, "parts") and dbm.has_table(con, "mails")
    con.close()
    from belegsuche.search import Searcher
    assert [h.filename for h in Searcher(db).search("Kowalski 4711").hits] == ["a.pdf"]


def test_older_format_still_rebuilds_and_newer_is_rejected(tmp_path, monkeypatch):
    _, db = _old_index_with_one_file(tmp_path, "8")
    calls = []
    real = dbm.rebuild_search_index
    monkeypatch.setattr(dbm, "rebuild_search_index", lambda con: (calls.append(1), real(con)))
    dbm.connect(db).close()
    assert calls == [1]
    con = dbm.connect(db)
    dbm.set_meta(con, "index_format", str(int(dbm.INDEX_FORMAT) + 1))
    con.commit()
    con.close()
    with pytest.raises(dbm.IncompatibleIndexError, match="neuer"):
        dbm.connect(db)


def test_parts_and_mails_are_deleted_with_their_file(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "Emails" / "m.eml", text="Hallo", attachments=[("a.txt", b"Notiz Kellerschluessel", "text/plain")])
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert con.execute("SELECT count(*) FROM parts").fetchone()[0] == 2
    con.execute("DELETE FROM files")
    con.commit()
    assert con.execute("SELECT count(*) FROM parts").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM mails").fetchone()[0] == 0


def test_scan_reactivates_skipped_mail_and_word_even_if_unchanged(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "Emails" / "m.eml", text="Hallo")
    (root / "Brief.docx").write_bytes(docx_bytes(["Brief"]))
    (root / "Liste.xlsx").write_bytes(b"PK\x03\x04 kein excel")
    digital_pdf(root / "a.pdf", [["Rechnung"]])
    con = dbm.connect(tmp_path / "i.db")
    rid = add_root(con, str(root), log=quiet)
    # Stand einer älteren Programmversion: .eml und .docx als „nicht unterstützt“ eingetragen, Größe/Zeit gleich
    for rel in ("Emails/m.eml", "Brief.docx", "Liste.xlsx"):
        st = (root / rel).stat()
        con.execute("INSERT INTO files(root_id, relpath, ext, kind, size, mtime, status, error, seen_at)"
                    " VALUES (?, ?, ?, 'unsupported', ?, ?, 'skipped', 'Dateityp nicht unterstützt', 0)",
                    (rid, rel, os.path.splitext(rel)[1], st.st_size, st.st_mtime))
    con.commit()
    logs = []
    st_ = scan_root(con, rid, str(root), log=logs.append)
    rows = {r["relpath"]: tuple(r)[1:] for r in con.execute("SELECT relpath, kind, status, error FROM files")}
    assert rows["Emails/m.eml"] == ("mail", "pending", None)
    assert rows["Brief.docx"] == ("word", "pending", None)
    assert rows["Liste.xlsx"] == ("unsupported", "skipped", "Dateityp nicht unterstützt")
    assert rows["a.pdf"] == ("pdf", "pending", None)
    assert st_.reactivated == 2 and st_.unsupported == 1 and st_.new == 1
    # zweiter Durchgang: nichts mehr zu reaktivieren
    assert scan_root(con, rid, str(root), log=quiet).reactivated == 0


def test_run_index_logs_reactivated_files(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", text="Hallo Welt")
    con = dbm.connect(tmp_path / "i.db")
    rid = add_root(con, str(root), log=quiet)
    st = (root / "m.eml").stat()
    con.execute("INSERT INTO files(root_id, relpath, ext, kind, size, mtime, status) VALUES (?, 'm.eml', '.eml',"
                " 'unsupported', ?, ?, 'skipped')", (rid, st.st_size, st.st_mtime))
    con.commit()
    logs = []
    _, ps = run_index(con, [str(root)], engine=BytesEngine(), log=logs.append)
    assert any("1 bisher nicht unterstützte Dateien (E-Mail, Word) werden jetzt eingelesen" in s for s in logs)
    assert ps.done == 1


# --- Mailkopf und Mailtext --------------------------------------------------------------------------------

def test_header_and_body_on_page_one(tmp_path):
    p = make_eml(tmp_path / "m.eml", subject="Rechnung Heizungswartung Musterstr. 12",
                 cc="Buchhaltung <buha@hv-nord.example>", text="Sehr geehrte Damen und Herren,\nanbei die Rechnung.",
                 headers={"Message-ID": "<abc123@firma.example>"})
    res = extract_full(p, None)
    assert len(res.pages) == 1 and res.pages[0].source == "text"
    text = res.pages[0].text
    for line in ("Betreff: Rechnung Heizungswartung Musterstr. 12", "Von: Erika Beispiel <erika@firma.example>",
                 "An: Hausverwaltung Nord <info@hv-nord.example>", "Cc: Buchhaltung <buha@hv-nord.example>",
                 "Datum: 12.03.2021 14:05", "anbei die Rechnung."):
        assert line in text
    m = res.mail
    assert m.subject == "Rechnung Heizungswartung Musterstr. 12" and m.sender == "Erika Beispiel <erika@firma.example>"
    assert m.recipients == "Hausverwaltung Nord <info@hv-nord.example>, Buchhaltung <buha@hv-nord.example>"
    assert m.sent_at == "2021-03-12T14:05:00+01:00" and m.message_id == "<abc123@firma.example>"
    assert [(t.part_no, t.kind, t.first_page, t.n_pages) for t in res.parts] == [(0, "body", 1, 1)]


def test_html_only_mail_without_tags_scripts_and_network(tmp_path, monkeypatch):
    html = ("<html><head><title>Kopf geheim</title><style>p {color: red}</style>"
            "<script>alert('boese')</script></head><body><p>Guten Tag,</p><p>die Wartung kostet "
            "<b>312,50&nbsp;&euro;</b> &amp; wird am 05.04.2024 erledigt.</p>"
            "<img src='https://tracker.example/pixel.gif'><table><tr><td>Pos</td><td>Betrag</td></tr></table>"
            "</body></html>")
    p = make_eml(tmp_path / "m.eml", html=html)

    def no_network(*a, **k):
        raise AssertionError("kein Netzzugriff beim Lesen einer Mail")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    body = extract_full(p, None).pages[0].text.split("\n\n", 1)[1]      # nach dem Mailkopf
    assert "die Wartung kostet 312,50 € & wird am 05.04.2024 erledigt." in body
    assert "<" not in body and "alert" not in body and "color" not in body and "Kopf geheim" not in body
    assert "Pos Betrag" in body


def test_plain_text_preferred_over_html_alternative(tmp_path):
    p = make_eml(tmp_path / "m.eml", text="Nur Text Fassung Kowalski", html="<p>HTML Fassung Kowalski</p>")
    text = extract_full(p, None).pages[0].text
    assert "Nur Text Fassung" in text and "HTML Fassung" not in text


def test_empty_plain_alternative_falls_back_to_html(tmp_path):
    p = make_eml(tmp_path / "m.eml", text="\n", html="<p>Nur im HTML steht Kowalski</p>")
    assert "Nur im HTML steht Kowalski" in extract_full(p, None).pages[0].text


def test_charsets_unknown_missing_and_cp1252(tmp_path):
    body = "Müllgebühren für Straße 3: 45,00 €".encode("cp1252")
    raw = (b"From: a@b.example\r\nSubject: =?iso-8859-1?q?Geb=FChren?=\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: text/plain\r\nContent-Transfer-Encoding: 8bit\r\n\r\n" + body + b"\r\n")
    (tmp_path / "ohne.eml").write_bytes(raw)
    res = extract_full(tmp_path / "ohne.eml", None)
    assert "Müllgebühren für Straße 3: 45,00 €" in res.pages[0].text and res.mail.subject == "Gebühren"
    raw2 = raw.replace(b"Content-Type: text/plain", b"Content-Type: text/plain; charset=x-erfunden-42")
    (tmp_path / "unbekannt.eml").write_bytes(raw2)
    assert "Müllgebühren" in extract_full(tmp_path / "unbekannt.eml", None).pages[0].text
    raw3 = raw.replace(b"Content-Type: text/plain", b"Content-Type: text/plain; charset=iso-8859-1")
    (tmp_path / "latin.eml").write_bytes(raw3)
    assert "45,00 €" in extract_full(tmp_path / "latin.eml", None).pages[0].text


def test_missing_date_uses_file_time_and_draft_is_marked(tmp_path):
    p = make_eml(tmp_path / "m.eml", date=None, text="Entwurf Text", headers={"X-Unsent": "1"})
    os.utime(p, (1_700_000_000, 1_700_000_000))
    res = extract_full(p, None)
    assert "(Speicherdatum der Datei, nicht das Sendedatum)" in res.pages[0].text
    assert "Entwurf (nicht gesendet)" in res.pages[0].text      # ohne Sendevermerk des CRM (test_mail_head_notes)
    shown = datetime.fromtimestamp(1_700_000_000).strftime("%d.%m.%Y")
    assert shown in res.pages[0].text                   # nur angezeigt: kein Sendedatum (siehe test_mail_review)
    assert res.mail.sent_at is None


def test_empty_and_non_mime_eml(tmp_path):
    (tmp_path / "leer.eml").write_bytes(b"")
    res = extract_full(tmp_path / "leer.eml", None)
    assert [p.text for p in res.pages] == [""] and res.notes == [f"{NOTE_PREFIX} leere Datei (0 Byte)"]
    (tmp_path / "text.eml").write_bytes("Einfach nur Text Kellerschlüssel ohne Kopf\n".encode())
    res = extract_full(tmp_path / "text.eml", None)
    assert "Kellerschlüssel" in res.pages[0].text


def test_control_characters_removed_and_names_nfc(tmp_path):
    p = make_eml(tmp_path / "m.eml", subject="Rechnung\x1b[31m rot ‮gnunhceR", text="x",
                 sender="Jörg Müller <jm@firma.example>")
    res = extract_full(p, None)
    assert "\x1b" not in res.mail.subject and "‮" not in res.mail.subject
    assert res.mail.sender == "Jörg Müller <jm@firma.example>"
    assert "\x1b" not in res.pages[0].text and "‮" not in res.pages[0].text


def test_broken_related_part_does_not_lose_the_text(tmp_path):
    # leeres multipart/related mit einer Grenze, die nicht vorkommt: get_body() bricht dort ab
    raw = (b"From: a@b.example\r\nSubject: Kaputt\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: multipart/mixed; boundary=\"aussen\"\r\n\r\n"
           b"--aussen\r\nContent-Type: multipart/related; boundary=\"fehlt\"\r\n\r\n\r\n"
           b"--aussen\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nDer Text bleibt lesbar Kowalski\r\n"
           b"--aussen--\r\n")
    (tmp_path / "k.eml").write_bytes(raw)
    res = extract_full(tmp_path / "k.eml", None)
    assert "Der Text bleibt lesbar Kowalski" in res.pages[0].text


# --- Anhänge -------------------------------------------------------------------------------------------------

def test_attachments_by_magic_bytes_pages_and_parts(tmp_path):
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-77 Seite eins Wärmetechnik Sommer GmbH"],
                               ["Seite zwei Kesselwartung Brennwertkessel Abgasmessung"],
                               ["Seite drei Gesamtbetrag 980,00 EUR inklusive Mehrwertsteuer"]])
    p = make_eml(tmp_path / "m.eml", text="Anbei", attachments=[
        ("", pdf, "application/octet-stream"),                                  # ohne Namen, nur octet-stream
        ("Foto", png_bytes(), "application/octet-stream"),
        ("Brief.docx", docx_bytes(["Mietbescheinigung Lindenweg"]), "application/octet-stream"),
        ("Notiz.txt", "Schlüssel liegt beim Hausmeister".encode(), "text/plain"),
    ])
    eng = BytesEngine("Kassenbon Baumarkt Nord Schrauben Silikon Summe 11,48 EUR")
    res = extract_full(p, eng)
    kinds = [(t.part_no, t.kind, t.first_page, t.n_pages) for t in res.parts]
    assert kinds == [(0, "body", 1, 1), (1, "pdf", 2, 3), (2, "image", 5, 1), (3, "word", 6, 1), (4, "text", 7, 1)]
    assert [p.page_no for p in res.pages] == list(range(1, 8))
    assert "Kesselwartung" in res.pages[2].text and res.pages[4].text.startswith("Kassenbon Baumarkt")
    assert res.pages[4].source == "ocr" and res.pages[5].source == "text"
    assert "Mietbescheinigung Lindenweg" in res.pages[5].text and "Hausmeister" in res.pages[6].text
    assert eng.calls == 1                                                       # nur das Foto per OCR
    assert all(t.sha256 and len(t.sha256) == 64 for t in res.parts[1:])
    assert "Anhänge: Foto, Brief.docx, Notiz.txt" in res.pages[0].text


def test_small_inline_logos_and_duplicate_images_are_not_recognized_again(tmp_path):
    logo = logo_bytes()
    photo = png_bytes(["Schadensfoto Keller"])
    p = make_eml(tmp_path / "m.eml", html="<p>Gruß</p>",
                 inline=[(f"image{i:03d}.png", logo, "image/png") for i in range(20)],
                 attachments=[("Foto1.png", photo, "image/png"), ("Foto2.png", photo, "image/png")])
    eng = BytesEngine()
    res = extract_full(p, eng)
    assert eng.calls == 1                                       # 20 Logos: keine OCR; gleiches Foto nur einmal
    assert [(t.kind, t.n_pages, t.note) for t in res.parts[1:]] == [
        ("image", 1, None), ("image", 0, "gleicher Inhalt wie Anhang 1")]


def test_domus_placeholder_becomes_link_to_file_in_activity_folder(tmp_path):
    root = tmp_path / "ordner"
    act = root / "Vorgaenge" / "090001"
    act.mkdir(parents=True)
    digital_pdf(act / "Rechnung 4711.pdf", [["Rechnung 4711"]])
    p = make_eml(act / "Emails" / "x.eml", text="Siehe Anlage", attachments=[
        ("Rechnung 4711.pdf.txt", PLACEHOLDER.format("Rechnung 4711.pdf").encode(), "application/octet-stream"),
        ("Weg.pdf.txt", PLACEHOLDER.format("Weg.pdf").encode(), "application/octet-stream"),
        ("rechnung 4711.PDF.txt", PLACEHOLDER.format("rechnung 4711.PDF").encode(), "text/plain"),
    ])
    res = extract_full(p, BytesEngine(), root=str(root))
    links = [(t.kind, t.name, t.n_pages, t.note) for t in res.parts[1:]]
    assert links == [("link", "Rechnung 4711.pdf", 0, "Vorgaenge/090001/Rechnung 4711.pdf"),
                     ("link", "Weg.pdf", 0, None),
                     ("link", "rechnung 4711.PDF", 0, "Vorgaenge/090001/Rechnung 4711.pdf")]   # ohne Groß/Klein
    assert "Anhang in DOMUS abgelegt: Rechnung 4711.pdf" in res.pages[0].text
    assert "Anhang in DOMUS abgelegt: Weg.pdf" in res.pages[0].text
    assert "Anhänge:" not in res.pages[0].text and len(res.pages) == 1 and res.notes == []


def test_signatures_appledouble_and_unsupported_attachments(tmp_path):
    p = make_eml(tmp_path / "m.eml", text="Siehe Anlagen", attachments=[
        ("smime.p7s", b"0\x82\x05" + bytes(300), "application/pkcs7-signature"),
        ("._Rechnung.pdf", b"\x00\x05\x16\x07" + bytes(200), "application/octet-stream"),
        ("winmail.dat", b"\x78\x9f\x3e\x22" + bytes(200), "application/ms-tnef"),
        ("Liste.xlsx", b"PK\x03\x04" + bytes(200), "application/octet-stream"),
        ("Video.mov", b"\x00\x00\x00\x14ftypqt  " + bytes(200), "video/quicktime"),
    ])
    res = extract_full(p, BytesEngine())
    assert [(t.name, t.kind, t.n_pages) for t in res.parts[1:]] == [
        ("winmail.dat", "tnef", 0), ("Liste.xlsx", "skipped", 0), ("Video.mov", "skipped", 0)]
    # kaputte winmail.dat (nur die Kennung, danach Nullen): Hinweis am Teil, kein Fehler der Mail
    assert res.parts[1].note.startswith("nicht lesbar (beschädigt")
    assert all(t.note.startswith("nicht ausgewertet") for t in res.parts[2:])
    assert res.notes == []                                       # übliche Auslassungen: kein Hinweis an der Mail


def test_password_pdf_attachment_is_a_part_note_not_a_mail_error(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", text="Passwort folgt", attachments=[
        ("geheim.pdf", pdf_bytes(tmp_path, [["Vertraulich"]], password="1234"), "application/pdf")])
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert ps.done == 1 and ps.errors == 0
    assert con.execute("SELECT status, error FROM files").fetchone()[:] == ("done", None)
    assert con.execute("SELECT kind, n_pages, note FROM parts WHERE part_no = 1").fetchone()[:] == (
        "pdf", 0, "PDF ist passwortgeschützt")


def test_nested_mails_up_to_depth_two(tmp_path):
    pdf = pdf_bytes(tmp_path, [["Weitergeleitete Rechnung Gartenpflege GW-5521"]])
    inner2 = EmailMessage(policy=policy.SMTP)
    inner2["Subject"], inner2["From"] = "Ganz innen", "c@x.example"
    inner2.set_content("Tiefe zwei Text Dachrinne")
    inner2.add_attachment(EmailMessage(policy=policy.SMTP))           # Tiefe 3: nicht mehr gelesen
    inner = EmailMessage(policy=policy.SMTP)
    inner["Subject"], inner["From"], inner["Date"] = "Ursprüngliche Rechnung", "b@x.example", \
        "Mon, 01 Jul 2024 09:00:00 +0200"
    inner.set_content("Bitte bezahlen")
    inner.add_attachment(pdf, maintype="application", subtype="pdf", filename="GW-5521.pdf")
    inner.add_attachment(inner2)
    as_file = EmailMessage(policy=policy.SMTP)
    as_file["Subject"], as_file["From"] = "Als Datei", "d@x.example"
    as_file.set_content("Angehängte eml-Datei Treppenhaus")
    p = make_eml(tmp_path / "m.eml", text="Zur Info", attachments=[
        inner, ("Weiterleitung.eml", as_file.as_bytes(), "application/octet-stream")])
    res = extract_full(p, BytesEngine())
    rows = [(t.part_no, t.kind, t.first_page, t.n_pages, t.mime_path) for t in res.parts]
    assert rows[:4] == [(0, "body", 1, 1, ""), (1, "mail", 2, 1, "2"), (2, "pdf", 3, 1, "2/2"),
                        (3, "mail", 4, 1, "2/3")]
    assert rows[4][1:4] == ("skipped", None, 0) and rows[4][4] == "2/3/2"        # Tiefe 3
    assert rows[5] == (5, "mail", 5, 1, "3")
    assert "Betreff: Ursprüngliche Rechnung" in res.pages[1].text and "Bitte bezahlen" in res.pages[1].text
    assert "GW-5521" in res.pages[2].text and "Dachrinne" in res.pages[3].text
    assert "Treppenhaus" in res.pages[4].text
    data = p.read_bytes()
    assert mailmod.find_part(data, "2/2") == pdf                                    # Anhang wiederfinden
    assert b"Treppenhaus" in mailmod.find_part(data, "3")
    assert mailmod.find_part(data, "9") is None and mailmod.find_part(data, "x/../1") is None


def test_limits_parts_size_and_pages(tmp_path, monkeypatch):
    p = make_eml(tmp_path / "m.eml", text="Viele Anhänge", attachments=[
        (f"n{i}.txt", f"Notiz Nummer {i}".encode(), "text/plain") for i in range(5)])
    monkeypatch.setattr(mailmod, "MAX_MAIL_PARTS", 2)
    res = extract_full(p, None)
    assert len(res.parts) == 3 and res.notes == [f"{NOTE_PREFIX} nur die ersten 2 Anhänge ausgewertet (3 weitere nicht)"]
    monkeypatch.setattr(mailmod, "MAX_MAIL_PARTS", 60)
    monkeypatch.setattr(mailmod, "MAX_ATTACHMENT_BYTES", 10)
    res = extract_full(p, None)
    assert {t.note for t in res.parts[1:]} == {"nicht ausgewertet (zu groß: 0 MB)"}
    monkeypatch.setattr(mailmod, "MAX_ATTACHMENT_BYTES", 50 * 1024 * 1024)
    monkeypatch.setattr(mailmod, "MAX_MAIL_PAGES", 3)
    res = extract_full(p, None)
    assert len(res.pages) == 3 and any("Seitenlimit" in n for n in res.notes)


# --- Einlesen in den Index ----------------------------------------------------------------------------------

def test_index_mail_writes_pages_parts_and_mail_row(tmp_path):
    root = tmp_path / "ordner"
    pdf = pdf_bytes(tmp_path, [["Rechnung Nr. RE-2024/118", "Gesamtbetrag 1.234,56 €"]])
    make_eml(root / "Emails" / "m.eml", subject="Ihre Rechnung", text="Anbei die Rechnung",
             attachments=[("Rechnung.pdf", pdf, "application/pdf")])
    before = tree_state(root)
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert tree_state(root) == before
    f = con.execute("SELECT id, kind, status, pages, ocr_pages, error FROM files").fetchone()
    assert tuple(f)[1:] == ("mail", "done", 2, 0, None)
    assert con.execute("SELECT subject, sender FROM mails").fetchone()[:] == ("Ihre Rechnung",
                                                                             "Erika Beispiel <erika@firma.example>")
    assert [tuple(r) for r in con.execute("SELECT part_no, kind, first_page, n_pages, name FROM parts")] == [
        (0, "body", 1, 1, None), (1, "pdf", 2, 1, "Rechnung.pdf")]
    paths = [r[0] for r in con.execute("SELECT f.path FROM fts f JOIN pages p ON p.id = f.rowid ORDER BY p.page_no")]
    assert all("erika" in pth and "rechnung" in pth for pth in paths)               # Mailkopf in der path-Spalte
    assert "pdf" not in paths[0].split() and "rechnung" in paths[1]                 # Anhangname auf Seite 2


def test_attachment_equal_to_indexed_pdf_is_not_recognized_again(tmp_path):
    root = tmp_path / "ordner"
    (root / "090001").mkdir(parents=True)
    scan_pdf(root / "090001" / "Scan_0001.pdf", [["Servicerechnung 77100001 Liegenschaft 42"]])
    scanned = (root / "090001" / "Scan_0001.pdf").read_bytes()
    con = dbm.connect(tmp_path / "i.db")
    eng = BytesEngine("Servicerechnung 77100001 Liegenschaft 42 Erfasst")
    run_index(con, [str(root)], engine=eng, log=quiet)
    assert eng.calls == 1
    make_eml(root / "090001" / "Emails" / "a.eml", text="Anbei", attachments=[("Scan.pdf", scanned,
                                                                               "application/octet-stream")])
    _, ps = run_index(con, [], engine=eng, log=quiet)
    assert eng.calls == 1 and ps.ocr_pages == 0                            # Seiten übernommen statt OCR
    row = con.execute("SELECT t.n_pages, t.note FROM parts t WHERE t.part_no = 1").fetchone()
    assert tuple(row) == (1, mailmod.REUSED_NOTE)
    assert con.execute("SELECT ocr_pages FROM files WHERE kind = 'mail'").fetchone()[0] == 1
    # zweite Mail mit demselben Anhang: Seiten aus dem Anhang der ersten Mail
    os.unlink(root / "090001" / "Scan_0001.pdf")
    con.execute("DELETE FROM files WHERE kind = 'pdf'")
    con.commit()
    make_eml(root / "Emails" / "b.eml", text="Nochmal", attachments=[("Kopie.pdf", scanned, "application/pdf")])
    run_index(con, [], engine=eng, log=quiet)
    assert eng.calls == 1
    pages = [r[0] for r in con.execute("SELECT p.text FROM pages p JOIN files f ON f.id = p.file_id"
                                       " WHERE f.relpath = 'Emails/b.eml' ORDER BY p.page_no")]
    assert pages[1] == "Servicerechnung 77100001 Liegenschaft 42 Erfasst"


def test_mails_come_last_and_standalone_file_reuses_attachment_pages(tmp_path):
    root = tmp_path / "ordner"
    scan_pdf(tmp_path / "s.pdf", [["Servicerechnung 77100002"]])
    scanned = (tmp_path / "s.pdf").read_bytes()
    act = root / "090001"
    make_eml(act / "Emails" / "a.eml", text="Anbei", attachments=[("Scan.pdf", scanned, "application/pdf")])
    act.joinpath("Zahlung.pdf").write_bytes(scanned)          # sortiert nach „Emails/…“, wird trotzdem zuerst gelesen
    con = dbm.connect(tmp_path / "i.db")
    eng = BytesEngine("Servicerechnung 77100002 Liegenschaft 42 Erfasst per Texterkennung")
    logs = []
    _, ps = run_index(con, [str(root)], engine=eng, log=logs.append)
    assert eng.calls == 1 and ps.reused == 0
    order = [ln.split("] ", 1)[1].split(" – ")[0] for ln in logs if ln.startswith("[")]
    assert order == ["090001/Zahlung.pdf", "090001/Emails/a.eml"]
    assert con.execute("SELECT note FROM parts WHERE part_no = 1").fetchone()[0] == mailmod.REUSED_NOTE
    # umgekehrt: Inhalt nur als Anhang bekannt, dann taucht die Datei auf -> übernommen, keine OCR
    con.execute("DELETE FROM files WHERE kind = 'pdf'")
    con.commit()
    act.joinpath("Zahlung.pdf").unlink()
    act.joinpath("Kopie").mkdir()
    act.joinpath("Kopie", "Beleg.pdf").write_bytes(scanned)
    _, ps = run_index(con, [], engine=eng, log=quiet)
    assert eng.calls == 1 and ps.reused == 1
    text = con.execute("SELECT p.text FROM pages p JOIN files f ON f.id = p.file_id WHERE f.kind = 'pdf'").fetchone()[0]
    assert text.startswith("Servicerechnung 77100002")


def test_identical_eml_copy_reuses_pages_parts_and_mail(tmp_path):
    root = tmp_path / "ordner"
    p = make_eml(root / "a" / "m.eml", subject="Kopie", text="Hallo", attachments=[
        ("Foto.png", png_bytes(), "image/png"),
        ("Rechnung.pdf.txt", PLACEHOLDER.format("Rechnung.pdf").encode(), "application/octet-stream")])
    (root / "b").mkdir()
    (root / "b" / "m.eml").write_bytes(p.read_bytes())
    eng = BytesEngine()
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], engine=eng, log=quiet)
    assert ps.done == 2 and ps.reused == 1 and eng.calls == 1
    q = "SELECT part_no, kind, first_page, n_pages, name, sha256 FROM parts WHERE file_id = ? ORDER BY part_no"
    ids = [r[0] for r in con.execute("SELECT id FROM files ORDER BY relpath")]
    assert [tuple(r) for r in con.execute(q, (ids[0],))] == [tuple(r) for r in con.execute(q, (ids[1],))]
    assert con.execute("SELECT count(*) FROM mails").fetchone()[0] == 2


def test_renamed_eml_keeps_parts(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "alt.eml", text="Hallo", attachments=[("n.txt", "Notiz Heizöl".encode(), "text/plain")])
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    (root / "alt.eml").rename(root / "neu.eml")
    scans, _ = run_index(con, [], engine=BytesEngine(), log=quiet)
    assert scans[0].renamed == 1
    assert [r[0] for r in con.execute("SELECT f.relpath FROM files f")] == ["neu.eml"]
    assert con.execute("SELECT count(*) FROM parts").fetchone()[0] == 2
    assert con.execute("SELECT subject FROM mails").fetchone()[0] == "Rechnung"


# --- Zeitlimit und Absturz: Rückfall „nur Mailtext“ -----------------------------------------------------------

def test_safe_mode_skips_pdf_and_image_but_keeps_text(tmp_path):
    p = make_eml(tmp_path / "m.eml", text="Mailtext bleibt Kowalski", attachments=[
        ("r.pdf", pdf_bytes(tmp_path, [["Rechnung"]]), "application/pdf"),
        ("f.png", png_bytes(), "image/png"),
        ("b.docx", docx_bytes(["Word bleibt lesbar"]), "application/octet-stream")])
    eng = BytesEngine()
    res = extract_full(p, eng, safe=True)
    assert eng.calls == 0
    assert [(t.kind, t.n_pages, t.note) for t in res.parts[1:]] == [
        ("pdf", 0, mailmod.SAFE_NOTE), ("image", 0, mailmod.SAFE_NOTE), ("word", 1, None)]
    assert res.notes == [mailmod.SAFE_MAIL_NOTE] and "Kowalski" in res.pages[0].text


def test_last_attempt_of_a_mail_runs_in_safe_mode(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", text="Mailtext", attachments=[("f.png", png_bytes(), "image/png")])
    con = dbm.connect(tmp_path / "i.db")
    rid = add_root(con, str(root), log=quiet)
    scan_root(con, rid, str(root), log=quiet)
    con.execute("UPDATE files SET attempts = ?", (indexer.MAX_ATTEMPTS - 1,))   # zwei Abstürze vorher
    con.commit()
    eng = BytesEngine()
    _, ps = run_index(con, [], engine=eng, log=quiet)
    assert ps.done == 1 and eng.calls == 0
    assert con.execute("SELECT status, error FROM files").fetchone()[:] == ("done", mailmod.SAFE_MAIL_NOTE)


@pytest.mark.vision
def test_hanging_mail_in_parallel_run_falls_back_to_mail_text(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    make_eml(root / "haengt.eml", subject="Hänger", text="Mailtext Hänger Kowalski",
             attachments=[("f.png", png_bytes(), "image/png")])
    for i in range(2):
        digital_pdf(root / f"r{i}.pdf", [[f"Rechnung Nummer {i} Wärmetechnik Brandl"]])
    monkeypatch.setenv("BELEGSUCHE_TEST_HANG_ON", "haengt.eml")
    monkeypatch.setattr(indexer, "STALL_TIMEOUT", 3)
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], log=quiet, workers=2)
    rows = {r["relpath"]: r for r in con.execute("SELECT relpath, status, error FROM files")}
    assert rows["haengt.eml"]["status"] == "done" and rows["haengt.eml"]["error"] == mailmod.SAFE_MAIL_NOTE
    assert rows["r0.pdf"]["status"] == rows["r1.pdf"]["status"] == "done"
    assert ps.errors == 0 and not ps.interrupted
    con.close()


@pytest.mark.vision
def test_crashing_mail_gets_last_attempt_with_mail_text_only(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    make_eml(root / "absturz.eml", text="Mailtext Absturz", attachments=[("f.png", png_bytes(), "image/png")])
    digital_pdf(root / "gut.pdf", [["Rechnung Gartenpflege Grünwerk"]])
    monkeypatch.setenv("BELEGSUCHE_TEST_CRASH_ON", "absturz.eml")
    monkeypatch.setenv("BELEGSUCHE_TEST_CRASH_SAFE_OK", "1")      # im Rückfall-Modus stürzt sie nicht mehr ab
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], log=quiet, workers=2)
    rows = {r["relpath"]: tuple(r)[1:] for r in con.execute("SELECT relpath, status, error FROM files")}
    assert rows["absturz.eml"] == ("done", mailmod.SAFE_MAIL_NOTE)
    assert rows["gut.pdf"] == ("done", None)


# --- Bytes-Extraktion und Originalschutz ----------------------------------------------------------------------

def test_pdf_bytes_same_text_as_path_and_password_error(tmp_path):
    from belegsuche.extract import ExtractError, extract_pdf
    p = tmp_path / "a.pdf"
    digital_pdf(p, [["Rechnung Dachrinne 4711"], ["Seite zwei"]])
    assert [x.text for x in extract_pdf(p.read_bytes(), None)] == [x.text for x in extract_pdf(p, None)]
    digital_pdf(tmp_path / "g.pdf", [["Geheim"]], password="1234")
    with pytest.raises(ExtractError, match="passwortgeschützt"):
        extract_pdf((tmp_path / "g.pdf").read_bytes(), None)


@pytest.mark.vision
@pytest.mark.skipif(not Path("/usr/bin/sips").exists(), reason="sips (macOS) fehlt")
def test_heic_attachment_read_from_memory_without_temp_file(tmp_path, monkeypatch):
    import subprocess
    from belegsuche.ocr import AppleVisionOCR
    png = tmp_path / "bon.png"
    png.write_bytes(png_bytes(["Kassenbon Baumarkt", "Summe 11,48 EUR"], size=(1200, 600)))
    heic = tmp_path / "bon.heic"
    subprocess.run(["/usr/bin/sips", "-s", "format", "heic", str(png), "--out", str(heic)], check=True,
                   capture_output=True)
    p = make_eml(tmp_path / "m.eml", text="Foto vom Handy", attachments=[("IMG_0001.HEIC", heic.read_bytes(),
                                                                         "application/octet-stream")])

    def no_temp(*a, **k):
        raise AssertionError("keine Temp-Datei für HEIC aus dem Speicher")

    monkeypatch.setattr(tempfile, "mkdtemp", no_temp)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", no_temp)
    res = extract_full(p, AppleVisionOCR())
    assert res.parts[1].kind == "image" and "11,48" in res.pages[1].text


class _NoBytesEngine:
    """OCR ohne recognize_bytes (wie Tesseract-Anbindungen in älteren Tests): Bilder über Pillow/sips."""

    def __init__(self):
        from belegsuche.ocr import OcrResult
        self.r = OcrResult("Bon Baumarkt Nord Summe", 0.9)
        self.sizes = []

    def recognize_image(self, img):
        self.sizes.append(img.size)
        return self.r

    def recognize_file(self, path):
        return self.r


@pytest.mark.skipif(not Path("/usr/bin/sips").exists(), reason="sips (macOS) fehlt")
def test_heic_without_vision_uses_system_temp_never_the_mail_folder(tmp_path, monkeypatch):
    import subprocess
    root = tmp_path / "ordner"
    png = tmp_path / "bon.png"
    png.write_bytes(png_bytes(["Kassenbon"], size=(1200, 600)))
    heic = tmp_path / "bon.heic"
    subprocess.run(["/usr/bin/sips", "-s", "format", "heic", str(png), "--out", str(heic)], check=True,
                   capture_output=True)
    make_eml(root / "Emails" / "m.eml", text="Foto", attachments=[("IMG.HEIC", heic.read_bytes(), "image/heic")])
    made = []
    real = tempfile.mkdtemp

    def record(*a, **k):
        d = real(*a, **k)
        made.append(d)
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", record)
    before = tree_state(root)
    con = dbm.connect(tmp_path / "i.db")
    eng = _NoBytesEngine()
    run_index(con, [str(root)], engine=eng, log=quiet)
    assert tree_state(root) == before
    assert made and all(not Path(d).resolve().is_relative_to(root.resolve()) for d in made)
    assert all(not Path(d).exists() for d in made)                       # Temp-Ordner wieder entfernt
    assert eng.sizes and con.execute("SELECT kind, n_pages FROM parts WHERE part_no = 1").fetchone()[:] == ("image", 1)


def test_attachment_shas_cover_nested_and_skip_logos(tmp_path):
    pdf = pdf_bytes(tmp_path, [["Rechnung"]])
    inner = EmailMessage(policy=policy.SMTP)
    inner["Subject"] = "innen"
    inner.set_content("x")
    inner.add_attachment(pdf, maintype="application", subtype="pdf", filename="r.pdf")
    p = make_eml(tmp_path / "m.eml", html="<p>x</p>", inline=[("logo.png", logo_bytes(), "image/png")],
                 attachments=[inner, ("n.txt", b"Notiz", "text/plain")])
    import hashlib
    assert mailmod.attachment_shas(p.read_bytes()) == [hashlib.sha256(pdf).hexdigest()]


def test_mail_db_columns_have_no_control_characters(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", subject="Betreff\x07mit\x00Klingel", text="x",
             attachments=[("Rech‮nung\x01.pdf.txt", PLACEHOLDER.format("A\x1b.pdf").encode(), "text/plain")])
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    subj = con.execute("SELECT subject FROM mails").fetchone()[0]
    name = con.execute("SELECT name FROM parts WHERE part_no = 1").fetchone()[0]
    assert subj == "Betreff mit Klingel" and "\x1b" not in name and name == "A .pdf"
