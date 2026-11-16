"""Suche, Kommandozeile und Bericht mit E-Mails und Word-Dateien (nur erfundene Daten)."""
import pytest

from belegsuche import cli
from belegsuche import db as dbm
from belegsuche.indexer import run_index
from belegsuche.ocr import OcrResult
from belegsuche.report import build_report, format_report
from belegsuche.search import Searcher

from .conftest import BytesEngine, digital_pdf, docx_bytes, make_eml

FILL = ["Sehr geehrte Damen und Herren, anbei erhalten Sie unsere Unterlagen zur weiteren Bearbeitung.",
        "Bitte prüfen Sie die Angaben und melden Sie sich bei Rückfragen gern telefonisch bei uns."]


@pytest.fixture(autouse=True)
def _nie_echter_index(tmp_path, monkeypatch):
    monkeypatch.setenv("BELEGSUCHE_DB", str(tmp_path / "env-index.db"))


def quiet(_):
    pass


def pdf(tmp_path, pages, name="x.pdf") -> bytes:
    p = tmp_path / name
    digital_pdf(p, [list(pg) + FILL for pg in pages])
    return p.read_bytes()


def index(tmp_path, root, engine=None):
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=engine or BytesEngine(), log=quiet)
    return db, con


def test_hit_names_mail_attachment_and_page_inside_it(tmp_path):
    root = tmp_path / "ordner"
    one = pdf(tmp_path, [["Lieferschein Holzhandel Birke"]], "a.pdf")
    five = pdf(tmp_path, [[f"Sammelrechnung Blatt {i} Gartenbau Eiche"] for i in range(1, 3)]
               + [["Rechnungsnummer RE-2024/0815 Rasenpflege Lindenweg"]]
               + [[f"Sammelrechnung Blatt {i} Gartenbau Eiche"] for i in range(4, 6)], "b.pdf")
    make_eml(root / "Emails" / "m.eml", subject="Rechnungen Juli", text="Anbei zwei Dateien",
             attachments=[("Lieferschein.pdf", one, "application/pdf"), ("Sammelrechnung.pdf", five, "application/pdf")])
    db, _ = index(tmp_path, root)
    hits = Searcher(db).search("RE-2024/0815").hits
    assert len(hits) == 1
    h = hits[0]
    assert (h.kind, h.part_no, h.part_name, h.part_kind, h.part_page, h.part_pages) == (
        "mail", 2, "Sammelrechnung.pdf", "pdf", 3, 5)
    assert h.page_no == 5 and h.pages == 7                         # page_no bleibt die Seite der ganzen Mail
    assert (h.mail_subject, h.mail_from, h.mail_date) == ("Rechnungen Juli", "Erika Beispiel <erika@firma.example>",
                                                          "12.03.2021 14:05")
    body = Searcher(db).search("Anbei zwei Dateien").hits[0]
    assert (body.part_no, body.part_kind, body.part_page, body.part_pages) == (0, "body", 1, 1)


def test_sender_and_amount_in_attachment_find_the_mail(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "Emails" / "k.eml", subject="Ihre Rechnung", sender="Dachdecker Kowalski <info@kowalski.example>",
             text="Anbei", attachments=[("RG.pdf", pdf(tmp_path, [["Rechnung Dachreparatur", "Summe 2.906,49 EUR"]]),
                                         "application/pdf")])
    make_eml(root / "Emails" / "k2.eml", subject="Termin", sender="Dachdecker Kowalski <info@kowalski.example>",
             text="Wir kommen am Montag")
    digital_pdf(root / "andere.pdf", [["Rechnung Malerbetrieb Schulz", "Summe 2.906,49 EUR", *FILL]])
    db, _ = index(tmp_path, root)
    hits = Searcher(db).search("Kowalski 2.906,49").hits
    assert hits[0].filename == "k.eml" and hits[0].missing == [] and hits[0].part_no == 1


def test_attachments_do_not_strengthen_each_other(tmp_path):
    root = tmp_path / "ordner"
    a = pdf(tmp_path, [["Gartenpflege Grünwerk Rasenmähen"]], "a.pdf")
    b = pdf(tmp_path, [["Aufzug Nord Wartungsvertrag"]], "b.pdf")
    make_eml(root / "Emails" / "m.eml", subject="Unterlagen", text="Anbei", attachments=[
        ("a.pdf", a, "application/pdf"), ("b.pdf", b, "application/pdf")])
    both = root / "zusammen.pdf"
    digital_pdf(both, [["Gartenpflege Grünwerk Rasenmähen", *FILL], ["Aufzug Nord Wartungsvertrag", *FILL]])
    db, _ = index(tmp_path, root)
    hits = {h.filename: h for h in Searcher(db).search("Grünwerk Aufzug").hits}
    assert hits["zusammen.pdf"].missing == []                      # Nachbarseite im selben PDF zählt mit
    assert hits["m.eml"].missing != []                             # Nachbarseite in einem anderen Anhang nicht
    assert list(hits)[0] == "zusammen.pdf"


def test_same_attachment_as_standalone_file_is_one_hit(tmp_path):
    root = tmp_path / "ordner"
    data = pdf(tmp_path, [["Wärmedienst Nord Servicerechnung 77100001", "Liegenschaft 42/0001"]])
    (root / "090001").mkdir(parents=True)
    (root / "090001" / "Rechnung.pdf").write_bytes(data)
    make_eml(root / "090001" / "Emails" / "m.eml", subject="Rechnung", text="Anbei",
             attachments=[("Rechnung.pdf", data, "application/octet-stream")])
    db, _ = index(tmp_path, root)
    hits = Searcher(db).search("77100001").hits
    assert len(hits) == 1 and hits[0].filename == "Rechnung.pdf" and hits[0].kind == "pdf"
    assert hits[0].duplicates == [str(root / "090001" / "Emails" / "m.eml") + " (Anhang 1)"]


def test_search_without_parts_table_still_works(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", subject="Alt", text="Kellerschlüssel Hausmeister Petersen",
             attachments=[("n.txt", "Notiz Heizöl".encode(), "text/plain")])
    db, con = index(tmp_path, root)
    con.execute("DROP TABLE parts")
    con.execute("DROP TABLE mails")
    con.commit()
    con.close()
    hits = Searcher(db).search("Kellerschlüssel").hits                # nur lesend: Tabellen fehlen
    assert [h.filename for h in hits] == ["m.eml"] and hits[0].part_no is None and hits[0].mail_subject is None


def test_cli_search_shows_mail_and_attachment(tmp_path, capsys):
    root = tmp_path / "ordner"
    three = pdf(tmp_path, [["Deckblatt Angebot"], ["Leistungsverzeichnis"], ["Angebotssumme 7.450,00 EUR Fassade"]])
    make_eml(root / "m.eml", subject="Angebot Fassade", sender="Malerei Weiß <info@weiss.example>", text="Hallo",
             attachments=[("Angebot.pdf", three, "application/pdf")])
    db, con = index(tmp_path, root)
    con.close()
    rc = cli.main(["--db", str(db), "suche", "Angebotssumme", "Fassade"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Mail: Angebot Fassade · Malerei Weiß <info@weiss.example> · 12.03.2021 14:05" in out
    assert "Anhang 1: Angebot.pdf, Seite 3/3" in out
    assert "(Seite 4/4)" not in out


def test_cli_search_escapes_control_characters_in_mail_fields(tmp_path, capsys, monkeypatch):
    from belegsuche.search import Hit, SearchResponse
    hit = Hit(file_id=1, relpath="m.eml", root=str(tmp_path), filename="m.eml", folder="", page_no=2, pages=2,
              score=1.0, snippet_html="", snippet_text="Rechnung", matched=["rechnung"], missing=[], ocr=False,
              available=True, kind="mail", part_no=1, part_name="a\x1b[2J.pdf", part_kind="pdf", part_page=1,
              part_pages=1, mail_subject="Betreff\x1b]0;x\x07", mail_from="a@b.example", mail_date="01.01.2024 10:00")
    monkeypatch.setattr(Searcher, "search", lambda self, q, limit=10: SearchResponse(q, [hit], [], 1, 1.0))
    (tmp_path / "i.db").write_bytes(b"")
    dbm.connect(tmp_path / "i.db").close()
    rc = cli.main(["--db", str(tmp_path / "i.db"), "suche", "Rechnung"])
    out = capsys.readouterr().out
    assert rc == 0 and "\x1b" not in out and "\x07" not in out
    assert "Mail: Betreff\\x1b]0;x\\x07 · a@b.example · 01.01.2024 10:00" in out
    assert "Anhang 1: a\\x1b[2J.pdf, Seite 1/1" in out


PLACEHOLDER = "Die Datei '{}' wurde in die Dokumentenverwaltung von DOMUS NAVI verschoben. 03.02.2023 10:15 EB"


def test_report_counts_mails_word_and_attachments(tmp_path, capsys):
    root = tmp_path / "ordner"
    data = pdf(tmp_path, [["Rechnung Wärmetechnik 1.234,56"]])
    (root / "090001").mkdir(parents=True)
    (root / "090001" / "Rechnung.pdf").write_bytes(data)
    make_eml(root / "090001" / "Emails" / "m.eml", text="Anbei", attachments=[
        ("Kopie.pdf", data, "application/pdf"),                                         # übernommen
        ("Neu.pdf", pdf(tmp_path, [["Angebot Dachrinne 410,00"]], "n.pdf"), "application/pdf"),   # ausgewertet
        ("Rechnung.pdf.txt", PLACEHOLDER.format("Rechnung.pdf").encode(), "application/octet-stream"),
        ("winmail.dat", b"\x78\x9f\x3e\x22" + bytes(100), "application/ms-tnef")])          # nicht lesbar
    (root / "Brief.docx").write_bytes(docx_bytes(["Mietbescheinigung"]))
    (root / "Liste.xlsx").write_bytes(b"PK\x03\x04 kein excel")
    db, con = index(tmp_path, root)
    r = build_report(con)
    assert r["mails"] == 1 and r["words"] == 1 and r["unsupported"] == {".xlsx": 1}
    # die (kaputte) winmail.dat zählt getrennt
    assert r["attachments"] == {"n": 3, "evaluated": 1, "reused": 1, "links": 1, "duplicates": 0, "skipped": 0,
                                "in_tnef": 0, "tnef": {"n": 1, "read": 0, "not_read": {"nicht lesbar": 1}}}
    text = format_report(r)
    assert "E-Mails: 1, Word: 1" in text
    assert ("Mail-Anhänge: 1 ausgewertet, 1 übernommen (gleicher Inhalt), 1 Platzhalter (in DOMUS abgelegt), "
            "0 nicht ausgewertet") in text
    assert "winmail.dat (Outlook): 0 geöffnet, darin 0 Anhänge (oben mitgezählt); 1 nicht ausgewertet " \
           "(nicht lesbar 1)" in text
    assert "nicht unterstützt:       1  (.xlsx 1)" in text
    assert not [p for p in r["problems"] if p["pfad"].endswith((".eml", ".docx"))]   # keine Flut in der Problemliste
    con.close()
    assert cli.main(["--db", str(db), "bericht"]) == 0
    assert "E-Mails: 1, Word: 1" in capsys.readouterr().out


class _UnsichereOCR:
    def recognize_image(self, img):
        return OcrResult("Kaum lesbarer Kassenbon Baumarkt Nord Summe Schrauben Silikon", 0.3)

    def recognize_file(self, path):
        return self.recognize_image(None)

    def recognize_bytes(self, data):
        return self.recognize_image(None)


class _LeereOCR(_UnsichereOCR):
    def recognize_image(self, img):
        return OcrResult("", 0.0)


def _photo() -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (900, 700), "white").save(buf, "PNG")
    return buf.getvalue()


def test_report_names_attachment_pages_and_ignores_blank_photos(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", text="Foto anbei", attachments=[("Bon.png", _photo(), "image/png")])
    db, con = index(tmp_path, root, engine=_UnsichereOCR())
    rows = {p["pfad"]: p["grund"] for p in build_report(con)["problems"]}
    assert rows == {str(root / "m.eml"): "unsichere Texterkennung: Anhang 1 Seite 1 (30%)"}
    con.close()
    root2 = tmp_path / "ordner2"
    make_eml(root2 / "m.eml", text="Foto vom Wasserschaden", attachments=[("Schaden.png", _photo(), "image/png")])
    con2 = dbm.connect(tmp_path / "j.db")
    run_index(con2, [str(root2)], engine=_LeereOCR(), log=quiet)
    assert build_report(con2)["problems"] == []                   # Foto ohne Text ist keine „leere Rückseite“
