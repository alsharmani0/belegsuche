"""winmail.dat in E-Mails: Anhänge darin als eigene Teile (durchsuchbar, Seiten lückenlos), Übernahme bekannter
Seiten und Warten auf gleiche Inhalte, Mailtext aus der winmail.dat, Umstellung Format 10 → 11 (nur Mails mit
winmail.dat neu lesen, keine erneute Texterkennung), Öffnen (sha, 409, Quarantäne), Bericht, Suche, Oberfläche.

Nur erfundene Daten (Adressen auf .example); alles liegt in tmp_path, der echte Index wird nie berührt."""
import hashlib
import io
import json
import os
import random
import struct
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.message import EmailMessage

import pytest
from PIL import Image

from belegsuche import cli
from belegsuche import db as dbm
from belegsuche import indexer
from belegsuche import mail as mailmod
from belegsuche import server as srv
from belegsuche.extract import extract_full
from belegsuche.indexer import run_index
from belegsuche.report import build_report, format_report
from belegsuche.search import Searcher

from .conftest import (BytesEngine, digital_pdf, docx_bytes, html_rtf, lzfu, make_eml, scan_pdf, text_image,
                       tnef_attachment, tnef_attr, tnef_bytes, tree_state)
from .test_mail_open import run_ui          # vor dem Ersetzen von subprocess.run in den Server-Tests binden
from .test_mail_review import _xattr

FILL = ["Sehr geehrte Damen und Herren, anbei erhalten Sie unsere Unterlagen zur weiteren Bearbeitung.",
        "Bitte prüfen Sie die Angaben und melden Sie sich bei Rückfragen gern telefonisch bei uns."]
OLD_NOTE = "nicht ausgewertet (winmail.dat aus Outlook)"          # so stand eine winmail.dat in Format 10


@pytest.fixture(autouse=True)
def _nie_echter_index(tmp_path, monkeypatch):
    monkeypatch.setenv("BELEGSUCHE_DB", str(tmp_path / "env-index.db"))


def quiet(_):
    pass


def pdf_bytes(tmp_path, pages, name="r.pdf") -> bytes:
    p = tmp_path / "quellen" / name
    p.parent.mkdir(exist_ok=True)
    digital_pdf(p, [list(pg) + FILL for pg in pages])
    return p.read_bytes()


def scan_bytes(tmp_path, pages, name="s.pdf") -> bytes:
    """Eingescanntes PDF: braucht Texterkennung (BytesEngine zählt die Aufrufe)."""
    p = tmp_path / "quellen" / name
    p.parent.mkdir(exist_ok=True)
    scan_pdf(p, pages)
    return p.read_bytes()


def photo(lines=("Kassenbon Baumarkt Nord", "Summe 11,48 EUR")) -> bytes:
    buf = io.BytesIO()
    text_image(list(lines), size=(900, 700), font_size=36).save(buf, "PNG")
    return buf.getvalue()


def _inline_photo() -> bytes:
    data = photo()
    assert len(data) >= mailmod.MIN_INLINE_IMAGE_BYTES          # Bild im Mailtext, aber groß genug zum Erkennen
    return data


def logo(color="navy") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (120, 40), color).save(buf, "PNG")
    return buf.getvalue()


def animated_gif() -> bytes:
    """Wie im Bestand: 55×55 Pixel, 101 Einzelbilder, über 10 KB – zu klein für die Texterkennung."""
    r = random.Random(3)
    frames = [Image.frombytes("L", (55, 55), bytes(r.choice((0, 255)) for _ in range(55 * 55))).convert("P")
              for _ in range(101)]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    assert len(buf.getvalue()) >= mailmod.MIN_INLINE_IMAGE_BYTES
    return buf.getvalue()


def winmail_eml(path, tnef: bytes, *, text="Anbei die Unterlagen", subject="Rechnung Juli", extra=()):
    """Mail wie im Bestand: multipart/mixed[text/plain, application/ms-tnef (winmail.dat, base64)]."""
    return make_eml(path, subject=subject, text=text,
                    attachments=[("winmail.dat", tnef, "application/ms-tnef"), *extra])


def parts_of(con, file_id):
    return con.execute("SELECT part_no, kind, name, mime_path, first_page, n_pages, note, sha256 FROM parts"
                       " WHERE file_id = ? ORDER BY part_no", (file_id,)).fetchall()


def ids_of(con):
    return {unicodedata.normalize("NFC", r[0]): r[1] for r in con.execute("SELECT relpath, id FROM files")}


def index(tmp_path, root, engine=None, db_name="i.db", log=quiet, **kw):
    db = tmp_path / db_name
    con = dbm.connect(db)
    scans, ps = run_index(con, [str(root)], engine=engine or BytesEngine(), log=log, **kw)
    return db, con, ps


# --- Anhänge in der winmail.dat: Teile, Seiten, Suche --------------------------------------------------------

def _standard_tnef(tmp_path):
    """P1: sieben Logos (Bauform B), ein PDF (Bauform A mit WMF-Symbol und Datum), ein Foto im Mailtext ab 10 KB
    (B, versteckt), ein animiertes GIF (B), ein Word-Anhang nur mit Name in 0x3704 (C)."""
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-4711 Dachrinne Kowalski", "Betrag 1.234,56 EUR"],
                               ["Seite 2 der Rechnung Aufmaß Dachrinne Kowalski"]])
    docx = docx_bytes(["Bericht Wasserschaden Kellerraum Lindenweg"])
    logos = [tnef_attachment(logo(c), f"image00{i}.png", variant="B", cid=f"image00{i}.png@01DA")
             for i, c in enumerate(("navy", "red", "green", "blue", "gray", "black", "white"), 1)]
    atts = logos + [tnef_attachment(pdf, "Rechnung Müller.pdf", metafile=True, dates=True),
                    tnef_attachment(_inline_photo(), "image008.png", variant="B", cid="image008.png@01DA"),
                    tnef_attachment(animated_gif(), "image009.gif", variant="B", mime="image/gif",
                                    cid="image009.gif@01DA"),
                    tnef_attachment(docx, "Bericht Bäder Kellerraum.docx", variant="C")]
    return tnef_bytes(atts), pdf, docx


def test_attachments_in_winmail_are_parts_with_contiguous_pages_and_searchable(tmp_path):
    root = tmp_path / "ordner"
    tnef, pdf, docx = _standard_tnef(tmp_path)
    eml = winmail_eml(root / "090001" / "Emails" / "m.eml", tnef)
    before = tree_state(root)
    eng = BytesEngine()
    db, con, ps = index(tmp_path, root, eng)
    assert ps.done == 1 and ps.errors == 0
    fid = ids_of(con)["090001/Emails/m.eml"]
    rows = parts_of(con, fid)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        (0, "body", None, ""), (1, "tnef", "winmail.dat", "2"), (2, "pdf", "Rechnung Müller.pdf", "2!8"),
        (3, "image", "image008.png", "2!9"), (4, "word", "Bericht Bäder Kellerraum.docx", "2!11")]
    assert rows[1]["note"] == "ausgewertet: 3 Anhänge" and rows[1]["n_pages"] == 0
    assert rows[1]["sha256"] == hashlib.sha256(tnef).hexdigest()
    assert rows[2]["sha256"] == hashlib.sha256(pdf).hexdigest() and rows[4]["sha256"] == hashlib.sha256(docx).hexdigest()
    # Seiten lückenlos: Mailtext 1, PDF 2–3, Foto 4, Word 5
    paged = [(r["first_page"], r["n_pages"]) for r in rows if r["n_pages"]]
    assert paged == [(1, 1), (2, 2), (4, 1), (5, 1)]
    f = con.execute("SELECT status, error, pages FROM files WHERE id = ?", (fid,)).fetchone()
    assert tuple(f) == ("done", None, 5)
    assert eng.calls == 1                         # nur das Foto: Logos und GIF (zu klein) nicht erkannt
    page1 = con.execute("SELECT text FROM pages WHERE file_id = ? AND page_no = 1", (fid,)).fetchone()[0]
    assert "Anhänge: winmail.dat (Rechnung Müller.pdf, image008.png, Bericht Bäder Kellerraum.docx)" in page1
    s = Searcher(db)
    hit = s.search("RE-4711 Dachrinne").hits[0]
    assert (hit.filename, hit.part_no, hit.part_name, hit.part_from, hit.part_page) == (
        "m.eml", 2, "Rechnung Müller.pdf", "winmail.dat", 1)
    hit = s.search("Wasserschaden Kellerraum").hits[0]
    assert (hit.part_no, hit.part_kind, hit.part_from) == (4, "word", "winmail.dat")
    hit = s.search("Anbei Unterlagen").hits[0]
    assert hit.part_no == 0 and hit.part_from is None
    assert tree_state(root) == before


def test_attachment_shas_include_winmail_contents_and_match_the_stored_parts(tmp_path):
    tnef, pdf, docx = _standard_tnef(tmp_path)
    inner = EmailMessage(policy=policy.SMTP)
    inner["Subject"] = "Weitergeleitet"
    inner.set_content("innen")
    inner.add_attachment(tnef_bytes([tnef_attachment(scan_bytes(tmp_path, [["Mahnung MH-5510"]]), "Mahnung.pdf")]),
                         maintype="application", subtype="ms-tnef", filename="winmail.dat")
    p = winmail_eml(tmp_path / "m.eml", tnef, extra=[inner])
    res = extract_full(p, BytesEngine())
    stored = {t.sha256 for t in res.parts if t.kind in mailmod.REUSABLE_KINDS}
    assert set(mailmod.attachment_shas(p.read_bytes())) == stored and len(stored) == 4
    assert [t.mime_path for t in res.parts if t.name == "Mahnung.pdf"] == ["3/2!1"]


def test_winmail_without_attachments_logos_only_and_mail_text_unchanged(tmp_path):
    root = tmp_path / "ordner"
    winmail_eml(root / "a.eml", tnef_bytes([]), text="Zählerstand Keller 4711")
    winmail_eml(root / "b.eml", tnef_bytes([tnef_attachment(logo(), "image001.png", variant="B")]), text="Logo")
    db, con, ps = index(tmp_path, root)
    ids = ids_of(con)
    a, b = parts_of(con, ids["a.eml"]), parts_of(con, ids["b.eml"])
    assert [(r["kind"], r["note"]) for r in a[1:]] == [("tnef", "ausgewertet: keine Anhänge (nur Formatierung)")]
    assert [(r["kind"], r["note"]) for r in b[1:]] == [("tnef", "ausgewertet: nur Bilder im Mailtext (Logos), "
                                                                "keine Anhänge")]
    assert con.execute("SELECT count(*) FROM files WHERE error IS NOT NULL").fetchone()[0] == 0
    assert Searcher(db).search("Zählerstand 4711").hits[0].filename == "a.eml"


def test_disguised_tnef_two_in_one_mail_and_in_forwarded_mail(tmp_path):
    """P2: andere Namen und Typen (erkannt an den Bytes), zwei winmail.dat in einer Mail, winmail.dat in einer
    angehängten Mail; jeder Teil lässt sich über find_part mit gleicher sha256 wiederfinden."""
    pdf1 = pdf_bytes(tmp_path, [["Angebot AN-11 Dachdecker"]], "1.pdf")
    pdf2 = pdf_bytes(tmp_path, [["Auftrag AU-22 Gartenbau"]], "2.pdf")
    pdf3 = pdf_bytes(tmp_path, [["Gutschrift GS-33 Aufzug"]], "3.pdf")
    inner = EmailMessage(policy=policy.SMTP)
    inner["Subject"] = "Weitergeleitet"
    inner.set_content("innen")
    inner.add_attachment(tnef_bytes([tnef_attachment(pdf3, "Gutschrift.pdf")]), maintype="application",
                         subtype="octet-stream", filename="ATT00002.dat")
    p = make_eml(tmp_path / "m.eml", text="zwei", attachments=[
        ("ATT00001.dat", tnef_bytes([tnef_attachment(pdf1, "Angebot.pdf")]), "application/octet-stream"),
        ("winmail.dat", tnef_bytes([tnef_attachment(pdf2, "Auftrag.pdf")]), "application/ms-tnef"), inner])
    res = extract_full(p, BytesEngine())
    got = [(t.kind, t.name, t.mime_path) for t in res.parts[1:]]
    assert got == [("tnef", "ATT00001.dat", "2"), ("pdf", "Angebot.pdf", "2!1"), ("tnef", "winmail.dat", "3"),
                   ("pdf", "Auftrag.pdf", "3!1"), ("mail", "Weitergeleitete Mail.eml", "4"),
                   ("tnef", "ATT00002.dat", "4/2"), ("pdf", "Gutschrift.pdf", "4/2!1")]
    raw = p.read_bytes()
    for t in res.parts[1:]:
        data = mailmod.find_part(raw, t.mime_path)
        assert data is not None and hashlib.sha256(data).hexdigest() == t.sha256, t.mime_path
    for bad in ("2!0", "2!2", "2!x", "2!", "1!1", "2!1!1", "2!1/1", "9!1", "4/2!9", "4!1"):
        assert mailmod.find_part(raw, bad) is None, bad


def test_embedded_outlook_message_and_winmail_in_winmail(tmp_path):
    """P2: eingebettete Nachricht (Methode 5) mit eigenem Anhang, eine winmail.dat in der winmail.dat; tiefer als
    MAX_TNEF_DEPTH: Hinweis statt Lesen."""
    pdf_a = pdf_bytes(tmp_path, [["Mietvertrag MV-2023-17 Stellplatz"]], "a.pdf")
    pdf_b = pdf_bytes(tmp_path, [["Nebenkosten NK-2022 Abrechnung Hausgeld"]], "b.pdf")
    pdf_c = pdf_bytes(tmp_path, [["Ganz tief Protokoll PR-9"]], "c.pdf")
    embedded = tnef_bytes([tnef_attachment(pdf_a, "Mietvertrag.pdf")], subject="WG: Mietvertrag Stellplatz",
                          body="Hier der unterschriebene Vertrag Kowalski")
    nested = tnef_bytes([tnef_attachment(pdf_b, "Nebenkosten.pdf")])
    too_deep = tnef_bytes([tnef_attachment(None, "Ebene 3", method=5,
                                           embedded=tnef_bytes([tnef_attachment(pdf_c, "tief.pdf")]))])
    outer = tnef_bytes([tnef_attachment(None, "WG: Mietvertrag Stellplatz", method=5, embedded=embedded),
                        tnef_attachment(nested, "winmail.dat", variant="C"),
                        tnef_attachment(None, "Ebene 2", method=5, embedded=too_deep)])
    root = tmp_path / "ordner"
    p = winmail_eml(root / "m.eml", outer)
    db, con, ps = index(tmp_path, root)
    rows = parts_of(con, ids_of(con)["m.eml"])
    got = [(r["kind"], r["name"], r["mime_path"], r["note"]) for r in rows[1:]]
    assert got == [("tnef", "winmail.dat", "2", "ausgewertet: 3 Anhänge"),
                   ("mail", "WG: Mietvertrag Stellplatz", "2!1", None),
                   ("pdf", "Mietvertrag.pdf", "2!1!1", None),
                   ("tnef", "winmail.dat", "2!2", "ausgewertet: 1 Anhang"),
                   ("pdf", "Nebenkosten.pdf", "2!2!1", None),
                   ("mail", "Ebene 2", "2!3", None),
                   ("skipped", "Ebene 3", "2!3!1", "nicht ausgewertet (zu tief verschachtelte Nachricht)")]
    page = con.execute("SELECT text FROM pages WHERE file_id = ? AND page_no = ?",
                       (ids_of(con)["m.eml"], rows[2]["first_page"])).fetchone()[0]
    assert page.startswith("Weitergeleitete Mail\nBetreff: WG: Mietvertrag Stellplatz\nAnhänge: Mietvertrag.pdf")
    assert "Hier der unterschriebene Vertrag Kowalski" in page
    s = Searcher(db)
    hit = s.search("MV-2023-17").hits[0]
    assert (hit.part_name, hit.part_from) == ("Mietvertrag.pdf", "WG: Mietvertrag Stellplatz")
    assert s.search("NK-2022 Hausgeld").hits[0].part_from == "winmail.dat"
    assert not s.search("PR-9 Protokoll").hits
    raw = p.read_bytes()
    for r in rows[1:]:
        data = mailmod.find_part(raw, r["mime_path"])
        assert data is not None and (r["sha256"] is None or hashlib.sha256(data).hexdigest() == r["sha256"])


def test_mail_text_from_winmail_only_when_the_mail_has_none(tmp_path):
    """Leerer MIME-Text: Text aus der winmail.dat (RTF aus HTML gekapselt, PR_BODY); mit MIME-Text nichts doppelt."""
    rtf = lzfu(html_rtf(["Sehr geehrte Damen und Herren, die Rechnung RE-77 über 120 € liegt bei.",
                         "Mit freundlichen Grüßen Hausmeisterdienst Sonnenschein"]))
    root = tmp_path / "ordner"
    winmail_eml(root / "leer.eml", tnef_bytes([], rtf=rtf), text="")
    winmail_eml(root / "punkt.eml", tnef_bytes([], body="Klartext Wartungsvertrag Aufzug WV-12"), text=" .\n")
    winmail_eml(root / "voll.eml", tnef_bytes([], rtf=rtf), text="Anbei die Rechnung, Gruß Sonnenschein")
    db, con, ps = index(tmp_path, root)
    ids = ids_of(con)

    def page1(name):
        return con.execute("SELECT text FROM pages WHERE file_id = ? AND page_no = 1", (ids[name],)).fetchone()[0]
    leer = page1("leer.eml")
    assert "die Rechnung RE-77 über 120 € liegt bei." in leer and "<p" not in leer and "color" not in leer
    assert "Klartext Wartungsvertrag Aufzug WV-12" in page1("punkt.eml")
    voll = page1("voll.eml")
    assert "Anbei die Rechnung" in voll and "RE-77" not in voll
    assert Searcher(db).search("RE-77").hits[0].filename == "leer.eml"
    assert len(Searcher(db).search("RE-77").hits) == 1


def test_same_content_in_mail_and_in_its_winmail_is_read_once(tmp_path):
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-5 Schlüsseldienst"]])
    p = winmail_eml(tmp_path / "m.eml", tnef_bytes([tnef_attachment(pdf, "Kopie.pdf")]),
                    extra=[("Rechnung.pdf", pdf, "application/pdf")])
    res = extract_full(p, BytesEngine())
    got = [(t.kind, t.name, t.n_pages, t.note) for t in res.parts[1:]]
    assert got == [("tnef", "winmail.dat", 0, "ausgewertet: 1 Anhang"), ("pdf", "Kopie.pdf", 1, None),
                   ("pdf", "Rechnung.pdf", 0, "gleicher Inhalt wie Anhang 2")]


def test_limits_apply_inside_winmail(tmp_path, monkeypatch):
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-6"]])
    big = tnef_bytes([tnef_attachment(pdf, "a.pdf")])
    monkeypatch.setattr(mailmod, "MAX_TNEF_BYTES", len(big) - 1)
    res = extract_full(winmail_eml(tmp_path / "a.eml", big), BytesEngine())
    assert [(t.kind, t.note) for t in res.parts[1:]] == [("tnef", "nicht ausgewertet (zu groß: 0 MB)")]
    monkeypatch.undo()
    monkeypatch.setattr(mailmod, "MAX_ATTACHMENT_BYTES", len(pdf) - 1)
    res = extract_full(winmail_eml(tmp_path / "b.eml", tnef_bytes([tnef_attachment(pdf, "a.pdf")])), BytesEngine())
    assert [(t.kind, t.note) for t in res.parts[1:]][1] == ("skipped", "nicht ausgewertet (zu groß: 0 MB)")
    monkeypatch.undo()
    monkeypatch.setattr(mailmod, "MAX_MAIL_PARTS", 2)
    many = tnef_bytes([tnef_attachment(pdf_bytes(tmp_path, [[f"Beleg {i}"]], f"{i}.pdf"), f"{i}.pdf")
                       for i in range(4)])
    res = extract_full(winmail_eml(tmp_path / "c.eml", many), BytesEngine())
    assert len(res.parts) == 3 and any("nur die ersten 2 Anhänge" in n for n in res.notes)


def test_broken_and_truncated_winmail_keeps_the_mail(tmp_path):
    """P2: abgeschnittene winmail.dat: der erste Anhang wird gelesen, der zweite mit Hinweis; Zeichensalat nach der
    Kennung und riesige Längenfelder: Hinweis am Teil, kein Fehler der Mail."""
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-8 Heizung Wartung"]])
    jpeg = photo()
    good = tnef_bytes([tnef_attachment(pdf, "Heizung.pdf"), tnef_attachment(jpeg, "Foto.png")])
    cut = good[:good.index(jpeg) + 2000]
    junk = b"\x78\x9f\x3e\x22\x00\x00" + random.Random(5).randbytes(5000)
    huge = tnef_bytes([]) + struct.pack("<BII", 2, 0x0006800F, 0xFFFFFFF0) + b"%PDF-1.4" + bytes(100)
    root = tmp_path / "ordner"
    for name, data in (("cut.eml", cut), ("junk.eml", junk), ("huge.eml", huge)):
        winmail_eml(root / name, data, text=f"Mail {name}")
    db, con, ps = index(tmp_path, root)
    assert ps.errors == 0
    ids = ids_of(con)
    rows = parts_of(con, ids["cut.eml"])
    assert rows[1]["kind"] == "tnef" and "abgeschnitten" in rows[1]["note"]
    assert (rows[2]["name"], rows[2]["kind"], rows[2]["n_pages"]) == ("Heizung.pdf", "pdf", 1)
    # der Name steht hinter den Daten und fehlt deshalb; der Hinweis sagt, warum der Teil unvollständig ist
    assert rows[3]["name"] is None and "unvollständig (winmail.dat abgeschnitten)" in rows[3]["note"]
    assert Searcher(db).search("RE-8 Heizung").hits[0].part_from == "winmail.dat"
    junk_rows = parts_of(con, ids["junk.eml"])
    assert junk_rows[1]["kind"] == "tnef" and junk_rows[1]["note"].startswith(("nicht lesbar", "ausgewertet"))
    huge_rows = parts_of(con, ids["huge.eml"])
    assert "abgeschnitten" in huge_rows[1]["note"]
    assert con.execute("SELECT count(*) FROM files WHERE status != 'done' OR error IS NOT NULL").fetchone()[0] == 0


def test_big_winmail_is_fast_in_the_main_process(tmp_path):
    """Großer Strom (8,5 MB, Foto 4,5 MB, PDF, Name über 64 Zeichen, Name in NFD): der Vorabblick im Hauptprozess
    (attachment_shas) bleibt schnell; der Name kommt in NFC an."""
    big_jpeg = b"\xff\xd8\xff\xe0" + random.Random(1).randbytes(4_500_000)
    pdf = pdf_bytes(tmp_path, [[f"Sammelrechnung Blatt {i}"] for i in range(30)])
    long_name = "Sehr langer Dateiname einer Sammelrechnung fuer das Objekt Musterstrasse 12 Teil 1.pdf"
    nfd = unicodedata.normalize("NFD", "Übergabeprotokoll Wohnung 3.jpg")
    data = tnef_bytes([tnef_attachment(big_jpeg, nfd, variant="C"),
                       tnef_attachment(pdf + random.Random(2).randbytes(3_900_000), long_name)])
    p = winmail_eml(tmp_path / "m.eml", data)
    raw = p.read_bytes()
    t0 = time.time()
    shas = mailmod.attachment_shas(raw)
    assert time.time() - t0 < 3 and len(shas) == 2
    res = extract_full(p, BytesEngine())
    assert [t.name for t in res.parts[2:]] == ["Übergabeprotokoll Wohnung 3.jpg", long_name]


# --- Übernahme bekannter Seiten, gleichzeitige Mails ------------------------------------------------------------

def test_inner_pdf_takes_pages_from_standalone_file_and_other_mails(tmp_path):
    """P1: dasselbe (eingescannte) PDF als eigene Datei und in den winmail.dat zweier Mails: nur einmal erkannt."""
    scan = scan_bytes(tmp_path, [["Schornsteinfeger Gebührenbescheid 4711/22"]])
    root = tmp_path / "ordner"
    act = root / "090002"
    act.mkdir(parents=True)
    (act / "Bescheid.pdf").write_bytes(scan)
    for i in range(2):
        winmail_eml(act / "Emails" / f"m{i}.eml", tnef_bytes([tnef_attachment(scan, "Bescheid.pdf")]),
                    text=f"Mail {i}")
    eng = BytesEngine()
    db, con, ps = index(tmp_path, root, eng)
    assert ps.done == 3 and eng.calls == 1
    notes = [r[0] for r in con.execute("SELECT note FROM parts WHERE kind = 'pdf'")]
    assert notes == [mailmod.REUSED_NOTE] * 2


class _ThreadPool:
    """Arbeitsprozesse als Threads (gleicher Prozess): die Texterkennung lässt sich zählen."""
    _processes = {}

    def __init__(self, delay=0.3):
        self.ex = ThreadPoolExecutor(2)
        self.delay = delay

    def submit(self, fn, path, marker, timeout, *rest):
        def job():
            time.sleep(self.delay)
            return fn(path, None, None, *rest)
        return self.ex.submit(job)

    def shutdown(self, wait=True, cancel_futures=False):
        self.ex.shutdown(wait=wait, cancel_futures=cancel_futures)


def test_same_inner_attachment_in_parallel_mails_is_recognized_once(tmp_path, monkeypatch):
    """P1: inflight-Logik – die sha256 der Anhänge in der winmail.dat blockieren gleichzeitige Mails."""
    root = tmp_path / "ordner"
    foto = photo()
    for i in range(3):
        winmail_eml(root / f"m{i}.eml", tnef_bytes([tnef_attachment(foto, "IMG_0815.png", variant="C")]),
                    text=f"Mail {i}")
    eng = BytesEngine()
    monkeypatch.setattr(indexer, "_WORKER_ENGINE", eng)
    monkeypatch.setattr(indexer, "_make_pool", lambda *a: _ThreadPool())
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], workers=2, log=quiet)
    assert ps.done == 3 and ps.errors == 0 and eng.calls == 1
    notes = [r[0] for r in con.execute("SELECT note FROM parts WHERE kind = 'image' ORDER BY file_id")]
    assert notes.count(mailmod.REUSED_NOTE) == 2


def test_log_line_counts_attachments_from_winmail(tmp_path):
    root = tmp_path / "ordner"
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-9"]])
    winmail_eml(root / "a.eml", tnef_bytes([tnef_attachment(pdf, "a.pdf")]))
    winmail_eml(root / "b.eml", tnef_bytes([tnef_attachment(pdf, "a.pdf"), tnef_attachment(photo(), "f.png")]),
                extra=[("Brief.docx", docx_bytes(["Brief"]), "application/octet-stream")])
    log = []
    index(tmp_path, root, log=log.append)
    a = next(x for x in log if "a.eml" in x)
    b = next(x for x in log if "b.eml" in x)
    assert "1 Anhang aus winmail.dat" in a and "3 Anhänge (2 aus winmail.dat)" in b


# --- Format 10 → 11: nur Mails mit winmail.dat neu lesen, ohne neue Texterkennung -------------------------------

def _old_behaviour(monkeypatch, pdf_first=True):
    """Format 10 (Stand 64ab7d8): eine winmail.dat wurde nur vermerkt (Teil 'skipped' mit Hinweis), nicht geöffnet –
    und „%PDF-“ in den ersten 1024 Bytes wurde vor der TNEF-Kennung geprüft (eine kleine winmail.dat mit einem PDF
    vorn galt als PDF)."""
    from belegsuche import extract
    real = mailmod._classify_content
    real_sniff = mailmod.sniff

    def old(att, depth):
        att = real(att, depth)
        if att.kind == "tnef":
            att.kind, att.note = "skipped", OLD_NOTE
        return att

    def old_sniff(data):
        if bytes(data[:4]) == b"%PDF" or extract._pdf_after_preamble(bytes(data[:1024])):
            return "pdf"
        return real_sniff(data)
    monkeypatch.setattr(mailmod, "_classify_content", old)
    if pdf_first:
        monkeypatch.setattr(mailmod, "sniff", old_sniff)
    monkeypatch.setattr(dbm, "INDEX_FORMAT", "10")
    monkeypatch.setattr(dbm, "mark_outdated_mails", lambda con, root_ids=None: 0)


def _migration_corpus(tmp_path):
    root = tmp_path / "ordner"
    act = root / "090003"
    scan = scan_bytes(tmp_path, [["Wartungsrechnung Aufzug A-77/2023"]], "aufzug.pdf")
    new_pdf = pdf_bytes(tmp_path, [["Angebot Fassade AF-31 Malerbetrieb Kowalski"]], "fassade.pdf")
    foto = photo(("Schadensfoto Balkon", "Riss Brüstung"))
    tnef = tnef_bytes([tnef_attachment(logo(), "image001.png", variant="B"),
                       tnef_attachment(new_pdf, "Angebot Fassade.pdf"), tnef_attachment(foto, "Balkon.png")])
    winmail_eml(act / "Emails" / "a.eml", tnef, extra=[("Aufzug.pdf", scan, "application/pdf")])
    winmail_eml(act / "Emails" / "kopie" / "a.eml", tnef, extra=[("Aufzug.pdf", scan, "application/pdf")])
    winmail_eml(act / "Emails" / "c.eml", tnef_bytes([]), text="Nur Formatierung")
    make_eml(act / "Emails" / "b.eml", text="ohne winmail",
             attachments=[("Foto.png", photo(("Kellertür", "Schloss")), "image/png")])
    digital_pdf(act / "Plan.pdf", [["Lageplan Grundstück", *FILL]])
    return root, scan, new_pdf


def test_migration_10_to_11_rereads_only_mails_with_winmail_without_new_ocr(tmp_path, monkeypatch):
    root, scan, new_pdf = _migration_corpus(tmp_path)
    before = tree_state(root)
    _old_behaviour(monkeypatch)
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    old_eng = BytesEngine()
    _, ps = run_index(con, [str(root)], engine=old_eng, log=quiet)
    assert ps.done == 5 and dbm.get_meta(con, "index_format") == "10"
    ids = ids_of(con)
    a = parts_of(con, ids["090003/Emails/a.eml"])
    # genau der Stand, den Format 10 schreibt (vgl. Vermessung des echten Index)
    assert [(r["part_no"], r["kind"], r["name"], r["mime_path"], r["first_page"], r["n_pages"], r["note"])
            for r in a[1:2]] == [(1, "skipped", "winmail.dat", "2", None, 0, OLD_NOTE)]
    assert a[1]["sha256"] is not None
    con.close()
    old_calls = old_eng.calls
    assert old_calls == 2                         # Scan der Aufzug-Rechnung (1 Seite) und das Foto in b.eml
    monkeypatch.undo()

    con = dbm.connect(db)                          # Umstellung: die Mails gelten bis zum Einlesen als fertig
    assert dbm.get_meta(con, "index_format") == "11"
    assert con.execute("SELECT count(*) FROM files WHERE status != 'done'").fetchone()[0] == 0
    hits = Searcher(db).search("Anbei Unterlagen").hits            # bisheriger Stand bleibt durchsuchbar
    mails = [h for h in hits if h.kind == "mail"]
    assert {h.filename for h in mails} == {"a.eml"} and not any(h.outdated for h in mails)
    eng, log = BytesEngine(), []
    _, ps = run_index(con, [str(root)], engine=eng, log=log.append)
    assert ps.done == 3 and ps.errors == 0
    assert eng.calls == 1                          # nur das neue Foto in der winmail.dat; Scan übernommen
    assert any("3 E-Mail(s) mit winmail.dat werden neu gelesen" in x for x in log)
    for rel in ("090003/Emails/a.eml", "090003/Emails/kopie/a.eml"):
        rows = parts_of(con, ids[rel])
        assert [(r["kind"], r["name"], r["mime_path"]) for r in rows[1:]] == [
            ("tnef", "winmail.dat", "2"), ("pdf", "Angebot Fassade.pdf", "2!2"), ("image", "Balkon.png", "2!3"),
            ("pdf", "Aufzug.pdf", "3")]
        assert rows[4]["note"] == mailmod.REUSED_NOTE
    hits = Searcher(db).search("AF-31 Fassade").hits
    assert hits[0].part_from == "winmail.dat" and hits[0].part_name == "Angebot Fassade.pdf"
    # wiederholt: nichts mehr zu tun
    assert dbm.mark_outdated_mails(con) == 0
    _, ps = run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert ps.done == 0
    con.close()
    dbm.connect(db).close()
    assert tree_state(root) == before


def test_mails_written_by_an_old_run_after_the_switch_are_reread(tmp_path, monkeypatch):
    """Läuft noch ein Einlesen mit dem alten Programm, wenn das neue den Index schon umgestellt hat: dessen Mails
    mit vermerkter winmail.dat holt der nächste Lauf nach. (Eine winmail.dat, die das alte Programm für ein PDF
    hielt, erfasst nur die Umstellung selbst – test_migration_finds_winmail_that_format_10_read_as_pdf.)"""
    root = tmp_path / "ordner"
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-12 Glaserei"]])
    winmail_eml(root / "a.eml", tnef_bytes([tnef_attachment(pdf, "Glas.pdf")]))
    db = tmp_path / "i.db"
    con = dbm.connect(db)                        # schon Format 11
    with monkeypatch.context() as m:
        _old_behaviour(m, pdf_first=False)
        run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert parts_of(con, ids_of(con)["a.eml"])[1]["note"] == OLD_NOTE
    _, ps = run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert ps.done == 1 and Searcher(db).search("RE-12 Glaserei").hits[0].part_from == "winmail.dat"


def test_format_9_still_upgrades_and_old_program_rejects_format_11(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    winmail_eml(root / "a.eml", tnef_bytes([]))
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    con.execute("DROP TABLE parts")
    con.execute("DROP TABLE mails")
    con.execute("UPDATE files SET status = 'skipped', error = 'Dateityp nicht unterstützt' WHERE kind = 'mail'")
    con.execute("DELETE FROM pages")
    dbm.set_meta(con, "index_format", "9")
    con.commit()
    con.close()
    con = dbm.connect(db)
    assert dbm.get_meta(con, "index_format") == "11" and dbm.mark_outdated_mails(con) == 0
    _, ps = run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert ps.done == 1 and parts_of(con, ids_of(con)["a.eml"])[1]["kind"] == "tnef"
    con.close()
    monkeypatch.setattr(dbm, "INDEX_FORMAT", "10")              # die bisherige Version
    with pytest.raises(dbm.IncompatibleIndexError, match="neuer"):
        dbm.connect(db)
    with pytest.raises(dbm.IncompatibleIndexError, match="neuer"):
        dbm.connect(db, readonly=True)


def test_missing_old_mail_is_never_a_source_for_its_copy(tmp_path, monkeypatch):
    """Eine fehlende Mail im alten Stand (Format 10) darf ihrer wieder aufgetauchten Kopie nicht den alten Stand
    vererben (Übernahme bei gleicher sha256)."""
    root = tmp_path / "ordner"
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-13 Tischler"]])
    data = winmail_eml(root / "a.eml", tnef_bytes([tnef_attachment(pdf, "Tisch.pdf")])).read_bytes()
    db = tmp_path / "i.db"
    with monkeypatch.context() as m:
        _old_behaviour(m)
        con = dbm.connect(db)
        run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
        os.rename(root / "a.eml", tmp_path / "weg.eml")
        run_index(con, [str(root)], engine=BytesEngine(), log=quiet, confirm_missing=True)
        assert con.execute("SELECT status FROM files").fetchone()[0] == "missing"
        con.close()
    (root / "neu").mkdir()
    (root / "neu" / "b.eml").write_bytes(data)
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    rows = parts_of(con, ids_of(con)["neu/b.eml"])
    assert [r["kind"] for r in rows[1:]] == ["tnef", "pdf"]


# --- Öffnen: find_part, sha, 409, Quarantäne -------------------------------------------------------------------

@pytest.fixture()
def winmail_srv(tmp_path, monkeypatch):
    import http.client
    from http.server import ThreadingHTTPServer
    root = tmp_path / "ordner"
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-4711 Dachrinne Kowalski"], ["Seite 2 Aufmaß"]])
    docx = docx_bytes(["Bericht Wasserschaden Kellerraum"])
    eml = winmail_eml(root / "V" / "Emails" / "m.eml", tnef_bytes([
        tnef_attachment(logo(), "image001.png", variant="B"), tnef_attachment(pdf, "Rechnung Müller.pdf"),
        tnef_attachment(photo(), "Foto.png", variant="C"), tnef_attachment(docx, "Bericht.docx", variant="C"),
        tnef_attachment(b"MZ\x90\x00" + bytes(300), "setup.exe", variant="C")]))
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    systmp = tmp_path / "systemtmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    calls = []
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(Searcher(db), port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def req(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Host": f"127.0.0.1:{port}", "X-Belegsuche": "1"})
        r = c.getresponse()
        return r.status, r.read(), r.headers
    fid = ids_of(con)["V/Emails/m.eml"]
    parts = {r["name"]: r["part_no"] for r in parts_of(con, fid)}
    env = {"root": root, "eml": eml, "con": con, "db": db, "req": req, "calls": calls, "systmp": systmp,
           "fid": fid, "parts": parts, "pdf": pdf, "docx": docx, "before": tree_state(root)}
    yield env
    httpd.shutdown()
    httpd.server_close()
    httpd.RequestHandlerClass.temp.cleanup()


def test_open_attachment_from_winmail_as_quarantined_copy(winmail_srv):
    e = winmail_srv
    st, body, _ = e["req"]("POST", "/api/open", {"id": e["fid"], "part": e["parts"]["Rechnung Müller.pdf"]})
    assert st == 200, body
    path = e["calls"][-1][1]
    assert unicodedata.normalize("NFC", os.path.basename(path)) == "Rechnung Müller.pdf"
    assert open(path, "rb").read() == e["pdf"] and not os.path.realpath(path).startswith(str(e["root"]))
    assert _xattr(path, b"com.apple.quarantine").startswith(b"0081;")
    st, body, _ = e["req"]("POST", "/api/open", {"id": e["fid"], "part": e["parts"]["Bericht.docx"]})
    assert st == 200 and e["calls"][-1][1].endswith("Bericht.docx")
    assert tree_state(e["root"]) == e["before"]


def test_inner_pdf_served_from_memory_and_container_or_exe_refused(winmail_srv):
    e = winmail_srv
    st, body, h = e["req"]("GET", f"/datei/{e['fid']}/teil/{e['parts']['Rechnung Müller.pdf']}")
    assert st == 200 and body == e["pdf"] and h["Content-Type"] == "application/pdf"
    for name in ("winmail.dat", "setup.exe"):
        st, body = e["req"]("POST", "/api/open", {"id": e["fid"], "part": e["parts"][name]})[:2]
        assert st == 415, name
        st, raw, _ = e["req"]("GET", f"/datei/{e['fid']}/teil/{e['parts'][name]}")
        assert st == 415 and b"MZ" not in raw
    assert not e["calls"] and not os.listdir(e["systmp"])


def test_inner_part_with_wrong_sha_or_changed_mail_gives_409(winmail_srv, tmp_path):
    e = winmail_srv
    n = e["parts"]["Rechnung Müller.pdf"]
    e["con"].execute("UPDATE parts SET sha256 = ? WHERE file_id = ? AND part_no = ?", ("0" * 64, e["fid"], n))
    e["con"].commit()
    st, body = e["req"]("POST", "/api/open", {"id": e["fid"], "part": n})[:2]
    assert st == 409 and json.loads(body) == {"error": "Mail geändert – neu einlesen"}
    other = pdf_bytes(tmp_path, [["Ganz andere Rechnung"]], "x.pdf")
    winmail_eml(tmp_path / "neu.eml", tnef_bytes([tnef_attachment(other, "Rechnung Müller.pdf")]))
    os.replace(tmp_path / "neu.eml", e["eml"])
    n = e["parts"]["Bericht.docx"]
    assert e["req"]("POST", "/api/open", {"id": e["fid"], "part": n})[0] == 409
    assert e["req"]("GET", f"/datei/{e['fid']}/teil/{n}")[0] == 409
    assert not e["calls"] and not os.listdir(e["systmp"])


def test_search_json_cli_and_ui_mark_attachments_from_winmail(winmail_srv, capsys, monkeypatch):
    e = winmail_srv
    st, body, _ = e["req"]("GET", "/api/search?q=RE-4711")
    hit = json.loads(body)["hits"][0]
    assert (hit["part_name"], hit["part_from"], hit["part_page"], hit["part_pages"]) == (
        "Rechnung Müller.pdf", "winmail.dat", 1, 2)
    monkeypatch.setattr(sys, "argv", ["belegsuche", "--db", str(e["db"]), "suche", "RE-4711"])
    cli.main()
    out = unicodedata.normalize("NFC", capsys.readouterr().out)
    assert f"Anhang {hit['part_no']}: Rechnung Müller.pdf (aus winmail.dat), Seite 1/2" in out
    rows = run_ui(json.dumps([hit]), "console.log(JSON.stringify(HITS.map(hitRow)));")
    assert f"Anhang {hit['part_no']} · Rechnung Müller.pdf (aus winmail.dat) · Seite 1 von 2" in rows[0]
    assert "Anhang öffnen" in rows[0]


def test_report_counts_winmail(tmp_path):
    root = tmp_path / "ordner"
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-14"]])
    winmail_eml(root / "a.eml", tnef_bytes([tnef_attachment(pdf, "a.pdf"),
                                            tnef_attachment(b"PK\x03\x04" + bytes(40), "Liste.xlsx", variant="C")]))
    winmail_eml(root / "b.eml", tnef_bytes([]))
    winmail_eml(root / "c.eml", b"\x78\x9f\x3e\x22" + bytes(50))
    db, con, ps = index(tmp_path, root)
    r = build_report(con)
    assert r["attachments"]["tnef"] == {"n": 3, "read": 2, "logos_only": 0, "no_attachments": 1,
                                        "not_read": {"nicht lesbar": 1}, "outdated": 0}
    assert r["attachments"]["in_tnef"] == 2 and r["attachments"]["skipped"] == 1
    assert ("winmail.dat (Outlook): 2 geöffnet (davon 1 ohne Anhänge), darin 2 Anhänge (oben mitgezählt); "
            "1 nicht ausgewertet (nicht lesbar 1)") in format_report(r)


def test_report_labels_unsure_pages_in_winmail(tmp_path):
    from belegsuche.report import _part_labels
    root = tmp_path / "ordner"
    winmail_eml(root / "a.eml", tnef_bytes([tnef_attachment(photo(), "f.png", variant="C")]))
    db, con, ps = index(tmp_path, root)
    label = _part_labels(con, ids_of(con)["a.eml"])
    assert label(1) == "Mailtext" and label(2) == "Anhang 2 (aus winmail.dat) Seite 1"


def test_checksum_errors_in_winmail_are_a_note_on_the_part(tmp_path):
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-15"]])
    good = tnef_bytes([tnef_attachment(pdf, "a.pdf")])
    i = good.index(pdf) + len(pdf)
    bad = good[:i] + bytes([good[i] ^ 1]) + good[i + 1:]
    res = extract_full(winmail_eml(tmp_path / "m.eml", bad), BytesEngine())
    assert res.parts[1].note == "ausgewertet: 1 Anhang; Prüfsumme falsch (1 Attribut)"
    assert res.parts[2].kind == "pdf" and res.parts[2].n_pages == 1 and res.notes == []


def test_reference_and_ole_attachments_in_winmail(tmp_path):
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(600)
    res = extract_full(winmail_eml(tmp_path / "m.eml", tnef_bytes([
        tnef_attachment(None, "Verweis.pdf", method=2), tnef_attachment(None, "Objekt", method=6, ole=ole),
        tnef_attachment(b"0\x80" + bytes(50), "smime.p7m", variant="C")], message_class="IPM.Note.SMIME")),
        BytesEngine())
    got = [(t.kind, t.name, t.note) for t in res.parts[1:]]
    assert got[0][0] == "tnef" and "verschlüsselte Outlook-Nachricht (S/MIME)" in got[0][2]
    assert got[1:] == [("skipped", "Verweis.pdf", "nicht ausgewertet (Verweis auf eine Datei, ohne Inhalt)"),
                       ("skipped", "Objekt", "nicht ausgewertet (Office- oder Outlook-Datei)"),
                       ("skipped", "smime.p7m", "nicht ausgewertet (.p7m)")]


def test_attribute_without_tnef_signature_inside_message_attachment_is_unreadable(tmp_path):
    bad_embedded = tnef_attachment(None, "Kaputt", method=5, embedded=b"keine TNEF-Daten")
    res = extract_full(winmail_eml(tmp_path / "m.eml", tnef_bytes([bad_embedded])), BytesEngine())
    assert [(t.kind, t.note) for t in res.parts[2:]] == [("skipped", "nicht lesbar (eingebettete Outlook-Nachricht)")]


def test_winmail_tests_leave_sources_untouched(tmp_path):
    root = tmp_path / "ordner"
    tnef, _, _ = _standard_tnef(tmp_path)
    winmail_eml(root / "m.eml", tnef)
    winmail_eml(root / "k.eml", tnef[:len(tnef) // 2])
    before = tree_state(root)
    db, con, ps = index(tmp_path, root)
    for r in parts_of(con, ids_of(con)["m.eml"])[1:]:
        mailmod.find_part((root / "m.eml").read_bytes(), r["mime_path"])
    assert tree_state(root) == before and sorted(os.listdir(root)) == ["k.eml", "m.eml"]


def test_tnef_attr_helper_checksum_matches():
    assert tnef_attr(1, 0x00018004, b"ab")[-2:] == struct.pack("<H", ord("a") + ord("b"))


# --- Nachbesserung nach der Prüfung ------------------------------------------------------------------------------

def test_migration_finds_winmail_that_format_10_read_as_pdf(tmp_path, monkeypatch):
    """Format 10 hielt eine kleine winmail.dat mit einem PDF vorn für ein PDF (Teil 'pdf' mit der sha256 der ganzen
    winmail.dat und den Seiten nur dieses PDFs). Die Umstellung merkt sich solche Teile; der nächste Lauf liest die
    Mail neu – mit allen Anhängen darin. Ein echtes PDF namens winmail.dat wird dabei einmal neu gelesen, nicht mehr."""
    from belegsuche import extract
    root = tmp_path / "ordner"
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-21 Zaunbau Holm"]])
    tnef = tnef_bytes([tnef_attachment(pdf, "Zaun.pdf"), tnef_attachment(photo(("Zaunfeld", "Pfosten 7")), "Z.png")])
    assert extract._pdf_after_preamble(tnef[:1024]) and extract.sniff(tnef) == "tnef"
    winmail_eml(root / "a.eml", tnef)
    make_eml(root / "b.eml", text="Echtes PDF mit falschem Namen",
             attachments=[("winmail.dat", pdf_bytes(tmp_path, [["Mahnung M-5"]], "m.pdf"), "application/pdf")])
    before = tree_state(root)
    db = tmp_path / "i.db"
    with monkeypatch.context() as m:
        _old_behaviour(m)
        con = dbm.connect(db)
        run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
        ids = ids_of(con)
        assert [(r["kind"], r["note"]) for r in parts_of(con, ids["a.eml"])[1:]] == [("pdf", None)]
        con.close()
    con = dbm.connect(db)
    assert con.execute("SELECT count(*) FROM files WHERE status != 'done'").fetchone()[0] == 0
    assert dbm.has_outdated_parts(con, ids["a.eml"]) and dbm.has_outdated_parts(con, ids["b.eml"])
    eng = BytesEngine()
    _, ps = run_index(con, [str(root)], engine=eng, log=quiet)
    assert ps.done == 2 and ps.errors == 0 and eng.calls == 1                 # nur das Foto braucht Texterkennung
    assert [(r["kind"], r["name"], r["mime_path"]) for r in parts_of(con, ids["a.eml"])[1:]] == [
        ("tnef", "winmail.dat", "2"), ("pdf", "Zaun.pdf", "2!1"), ("image", "Z.png", "2!2")]
    assert [(r["kind"], r["note"]) for r in parts_of(con, ids["b.eml"])[1:]] == [("pdf", None)]
    hit = Searcher(db).search("RE-21 Zaunbau Holm").hits[0]
    assert (hit.filename, hit.part_name, hit.part_from) == ("a.eml", "Zaun.pdf", "winmail.dat")
    con.close()
    con = dbm.connect(db)
    _, ps = run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert ps.done == 0                                                      # einmalig, nicht bei jedem Lauf
    assert tree_state(root) == before


def test_switch_alone_changes_nothing_visible_and_index_rereads_only_its_folders(tmp_path, monkeypatch, capsys):
    """Die Umstellung (schon durch „suche“, „start“, „bericht“) setzt keine Mail auf „wird neu eingelesen“: bis
    zum Einlesen gilt der bisherige Stand als fertig, der Bericht nennt die noch nicht geöffneten winmail.dat. Ein
    Lauf über einen Ordner liest nur dessen Mails neu."""
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-22 Kaminkehrer Ost"]])
    roots = [tmp_path / "eins", tmp_path / "zwei"]
    for r in roots:
        winmail_eml(r / "a.eml", tnef_bytes([tnef_attachment(logo(), "image001.png", variant="B"),
                                            tnef_attachment(pdf, "Kamin.pdf")]))
    db = tmp_path / "i.db"
    with monkeypatch.context() as m:
        _old_behaviour(m)
        con = dbm.connect(db)
        for r in roots:
            run_index(con, [str(r)], engine=BytesEngine(), log=quiet)
        con.close()
    monkeypatch.setattr(sys, "argv", ["belegsuche", "--db", str(db), "suche", "Anbei Unterlagen"])
    cli.main()                                                     # stellt um
    out = capsys.readouterr().out
    assert "a.eml" in out and "geändert" not in out
    con = dbm.connect(db)
    assert dbm.get_meta(con, "index_format") == "11"
    assert con.execute("SELECT count(*) FROM files WHERE status != 'done'").fetchone()[0] == 0
    text = format_report(build_report(con))
    assert "noch offen:              0" in text
    assert "winmail.dat (Outlook): 2 noch nicht geöffnet – der nächste Lauf von „belegsuche index“ liest sie" in text
    log = []
    _, ps = run_index(con, [str(roots[0])], engine=BytesEngine(), log=log.append)
    assert ps.done == 1 and any("1 E-Mail(s) mit winmail.dat werden neu gelesen" in x for x in log)
    status = {r[0]: r[1] for r in con.execute("SELECT r.path, f.status FROM files f JOIN roots r ON r.id = f.root_id")}
    assert status == {str(roots[0]): "done", str(roots[1]): "done"}
    text = format_report(build_report(con))
    assert "winmail.dat (Outlook): 1 geöffnet, darin 1 Anhang (oben mitgezählt)" in text
    assert "winmail.dat (Outlook): 1 noch nicht geöffnet" in text
    _, ps = run_index(con, [], engine=BytesEngine(), log=quiet)
    assert ps.done == 1 and "noch nicht geöffnet" not in format_report(build_report(con))


def test_report_tells_empty_winmail_apart(tmp_path):
    root = tmp_path / "ordner"
    winmail_eml(root / "a.eml", tnef_bytes([tnef_attachment(logo(), "image001.png", variant="B")]))
    winmail_eml(root / "b.eml", tnef_bytes([tnef_attachment(logo("red"), "image001.png", variant="B")]))
    winmail_eml(root / "c.eml", tnef_bytes([]))
    winmail_eml(root / "d.eml", tnef_bytes([tnef_attachment(photo(), "f.png", variant="C"),
                                            tnef_attachment(pdf_bytes(tmp_path, [["Rechnung RE-23"]]), "r.pdf")]))
    db, con, ps = index(tmp_path, root)
    assert ("winmail.dat (Outlook): 4 geöffnet (davon 2 nur Logos, 1 ohne Anhänge), darin 2 Anhänge "
            "(oben mitgezählt)") in format_report(build_report(con))


def test_also_under_names_the_winmail_origin(tmp_path):
    """„auch unter“: steht derselbe Inhalt in einer winmail.dat, heißt es „(Anhang 2 aus winmail.dat)“."""
    root = tmp_path / "ordner"
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-24 Dachdecker Meisel"]])
    (root / "V").mkdir(parents=True)
    (root / "V" / "Rechnung.pdf").write_bytes(pdf)
    winmail_eml(root / "V" / "Emails" / "m.eml", tnef_bytes([tnef_attachment(pdf, "Rechnung.pdf")]))
    make_eml(root / "V" / "Emails" / "n.eml", text="Rechnung anbei", attachments=[("Rechnung.pdf", pdf,
                                                                                   "application/pdf")])
    db, con, ps = index(tmp_path, root)
    hit = Searcher(db).search("RE-24 Dachdecker Meisel").hits[0]
    assert hit.filename == "Rechnung.pdf" and hit.kind != "mail"
    assert sorted(os.path.basename(d) for d in hit.duplicates) == ["m.eml (Anhang 2 aus winmail.dat)",
                                                                   "n.eml (Anhang 1)"]


def test_long_names_and_mime_tags_from_winmail_are_capped(tmp_path):
    """Präparierte Namen: Millionen Zeichen landen nicht in Teil, Zeile „Anhänge:“ und Suchindex; die Endung bleibt.
    Gilt auch für Namen von Mail-Anhängen."""
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-25 Elektro Funke"]])
    tnef = tnef_bytes([tnef_attachment(pdf, "Ä" * 2_000_000 + ".pdf", variant="B", mime="x/" + "y" * 100_000)])
    p = winmail_eml(tmp_path / "m.eml", tnef, extra=[("B" * 600 + ".docx", docx_bytes(["Brief"]),
                                                      "application/octet-stream")])
    t0 = time.time()
    res = extract_full(p, BytesEngine())
    assert time.time() - t0 < 5
    names = {t.mime_path: t for t in res.parts}
    inner, docx = names["2!1"], names["3"]
    assert len(inner.name) == mailmod.MAX_NAME_CHARS and inner.name.endswith("….pdf") and inner.name[0] == "Ä"
    assert len(inner.mime) <= mailmod.MAX_NAME_CHARS and inner.n_pages == 1
    assert len(docx.name) == mailmod.MAX_NAME_CHARS and docx.name.endswith("….docx") and docx.kind == "word"
    assert len(res.pages[0].text) < 2000
    root = tmp_path / "ordner"
    root.mkdir()
    os.replace(p, root / "m.eml")
    db, con, ps = index(tmp_path, root)
    assert con.execute("SELECT max(length(path)) FROM fts").fetchone()[0] < 2000
    assert Searcher(db).search("RE-25 Funke").hits[0].part_name == inner.name


def test_many_small_crafted_winmail_in_one_mail_share_one_budget(tmp_path):
    """30 winmail.dat in einer winmail.dat, jede mit 114.000 Eigenschaften (je unter dem Budget eines Stroms): alle
    winmail.dat einer Mail teilen sich ein Budget – Vorabblick im Hauptprozess (attachment_shas, ohne Zeitlimit) und
    Einlesen bleiben schnell."""
    lst = struct.pack("<I", 19_000) + struct.pack("<HHI", 0x0003, 0x0E21, 7) * 19_000
    inner = b"\x78\x9f\x3e\x22\x00\x00" + tnef_attr(1, 0x00069003, lst) * 6
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-26 Maler Grün"]])
    tnef = tnef_bytes([tnef_attachment(pdf, "r.pdf")] + [tnef_attachment(inner, f"w{i}.dat", variant="C")
                                                          for i in range(30)])
    assert len(tnef) > 25_000_000
    p = winmail_eml(tmp_path / "m.eml", tnef)
    raw = p.read_bytes()
    t0 = time.time()
    shas = mailmod.attachment_shas(raw)
    t_shas = time.time() - t0
    t0 = time.time()
    res = extract_full(p, BytesEngine())
    t_read = time.time() - t0
    assert t_shas < 2.0 and t_read < 2.0, (t_shas, t_read)          # je winmail.dat ein Budget: über 3 s
    assert shas == [hashlib.sha256(pdf).hexdigest()]
    assert res.parts[2].kind == "pdf" and res.parts[2].n_pages == 1
    from belegsuche import tnef as tnef_mod
    assert any(tnef_mod.BUDGET_NOTE in (t.note or "") for t in res.parts if t.kind == "tnef")


def test_error_in_mail_text_fallback_does_not_cost_the_mail(tmp_path, monkeypatch):
    """Der Mailtext aus der winmail.dat ist nur ein Rückfall: ein Fehler darin wird ein Hinweis, die Mail (bei der
    Umstellung eine bisher durchsuchbare) bleibt fertig, mit ihren Anhängen."""
    from belegsuche import tnef as tnef_mod

    def broken(self):
        raise ValueError("kaputt")
    monkeypatch.setattr(tnef_mod.Message, "text", broken)
    pdf = pdf_bytes(tmp_path, [["Rechnung RE-27 Fliesen Kurz"]])
    root = tmp_path / "ordner"
    winmail_eml(root / "m.eml", tnef_bytes([tnef_attachment(pdf, "f.pdf")]), text="")
    db, con, ps = index(tmp_path, root)
    assert ps.done == 1 and ps.errors == 0
    rows = parts_of(con, ids_of(con)["m.eml"])
    assert rows[1]["note"] == "ausgewertet: 1 Anhang; Mailtext darin nicht lesbar (ValueError)"
    assert rows[2]["kind"] == "pdf" and rows[2]["n_pages"] == 1
