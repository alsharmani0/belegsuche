"""Mail-Fundstellen und Dubletten werden ohne Parsen ihrer Anzeigetexte ausgewertet.

Nur künstliche E-Mails/PDFs/TNEF-Ströme und ein eigener temporärer Index.
"""
from dataclasses import asdict

import pytest

from belegsuche import db as dbm
from belegsuche.evaluate import evaluate, format_eval, load_queries
from belegsuche.indexer import run_index
from belegsuche.search import Searcher

from .conftest import BytesEngine, digital_pdf, make_eml, tnef_attachment, tnef_bytes, tree_state


FILL = ["Sehr geehrte Damen und Herren, bitte pruefen Sie die beigefuegten Angaben.",
        "Die dokumentierten Arbeiten werden nach der Pruefung zur weiteren Bearbeitung abgelegt."]


@pytest.fixture
def mail_index(tmp_path):
    root = tmp_path / "belege"
    root.mkdir()
    ordinary_source = tmp_path / "a.pdf"
    nested_source = tmp_path / "b.pdf"
    digital_pdf(ordinary_source, [["Deckblatt Ausfuehrungsnachweis", *FILL],
                                  ["Silberfarn Rohrkupplung abgeschlossen", *FILL]])
    digital_pdf(nested_source, [["Deckblatt Technischer Nachweis", *FILL],
                                ["Nordquarz Druckprobe bestanden", *FILL], ["Abschlussvermerk", *FILL]])
    ordinary, nested = ordinary_source.read_bytes(), nested_source.read_bytes()
    tnef = tnef_bytes([tnef_attachment(nested, "Anlage B.pdf", metafile=True, dates=True)])
    main = make_eml(root / "haupt.eml", subject="Unterlagen Vorgang 62", text="Kontrollvermerk Mailablage. Anbei.",
                    attachments=[("Anlage A.pdf", ordinary, "application/pdf"),
                                 ("winmail.dat", tnef, "application/ms-tnef")])
    (root / "kopie.eml").write_bytes(main.read_bytes())
    make_eml(root / "anderer_versatz.eml", subject="Andere Ablage", text="Anbei die Unterlagen.",
             attachments=[("winmail.dat", tnef, "application/ms-tnef")])
    (root / "einzel.pdf").write_bytes(nested)
    digital_pdf(root / "ablenker.pdf", [["Nordquarz nur Terminplanung", *FILL]])
    before = tree_state(root)
    db_path = tmp_path / "eigener-index.db"
    engine = BytesEngine()
    con = dbm.connect(db_path)
    try:
        _, processed = run_index(con, [str(root)], engine=engine, log=lambda _: None)
        assert processed.errors == 0 and engine.calls == 0
        parts = {r["relpath"]: {p["name"]: dict(p) for p in con.execute(
            "SELECT * FROM parts WHERE file_id = ?", (r["id"],))}
            for r in con.execute("SELECT id, relpath FROM files WHERE kind = 'mail'")}
    finally:
        con.close()
    yield Searcher(db_path), root, parts
    assert tree_state(root) == before


def test_locations_describe_mail_copies_and_their_own_pages(mail_index):
    searcher, root, _ = mail_index
    [hit] = searcher.search("Silberfarn Rohrkupplung").hits
    locations = {location.fullpath: location for location in hit.locations}
    assert set(locations) == {str(root / "haupt.eml"), str(root / "kopie.eml")}
    for location in locations.values():
        assert (location.page_no, location.pages, location.part_no, location.part_page, location.part_pages) == (
            3, 6, 1, 2, 2)
    assert len(hit.duplicates) == 1 and hit.duplicates[0].endswith(" (Anhang 1)")
    assert asdict(hit)["locations"][0]["fullpath"] == hit.fullpath


def test_copied_eml_counts_as_found_regardless_of_primary_copy(mail_index):
    searcher, _, _ = mail_index
    rows, summary = evaluate(searcher, [
        {"suche": "Silberfarn Rohrkupplung", "datei": name, "seite": "3"}
        for name in ("haupt.eml", "kopie.eml")])
    assert [(r.rang, r.seite_gefunden, r.kategorie) for r in rows] == [(1, 3, "ok")] * 2
    assert summary["nicht_gefunden"] == 0 and summary["ziel_top_5"] == 2


def test_tnef_duplicate_uses_expected_mail_global_page_not_primary_pdf_page(mail_index):
    searcher, root, parts = mail_index
    hit = searcher.search("Nordquarz Druckprobe").hits[0]
    assert hit.relpath == "einzel.pdf" and hit.page_no == 2
    locations = {location.fullpath: location for location in hit.locations}
    assert locations[str(root / "haupt.eml")].page_no == 5
    assert locations[str(root / "anderer_versatz.eml")].page_no == 3
    questions = []
    for name, page in (("haupt.eml", 5), ("kopie.eml", 5), ("anderer_versatz.eml", 3)):
        questions.append({"suche": "Nordquarz Druckprobe", "datei": name, "seite": str(page),
                          "teil": str(parts[name]["Anlage B.pdf"]["part_no"]), "teilseite": "2"})
    questions.append({"suche": "Nordquarz Druckprobe", "datei": "einzel.pdf", "seite": "2"})
    rows, summary = evaluate(searcher, questions)
    assert [(r.rang, r.seite_gefunden) for r in rows] == [(1, 5), (1, 5), (1, 3), (1, 2)]
    assert all(r.ziel_erfuellt and r.kategorie == "ok" for r in rows)
    assert summary["seite_falsch"] == 0 and summary["ziel_top_5"] == 4


def test_duplicate_display_strings_do_not_control_evaluation(mail_index, monkeypatch):
    searcher, _, _ = mail_index
    response = searcher.search("Silberfarn Rohrkupplung")
    response.hits[0].duplicates = ["Dies ist absichtlich kein Pfad (Anhang 999 aus winmail.dat)"]
    monkeypatch.setattr(searcher, "search", lambda *args, **kwargs: response)
    rows, summary = evaluate(searcher, [{"suche": "Silberfarn Rohrkupplung", "datei": name, "seite": "3"}
                                         for name in ("haupt.eml", "kopie.eml")])
    assert all(r.rang == 1 and r.ziel_erfuellt for r in rows)
    assert summary["nicht_gefunden"] == 0


def test_incorrect_part_or_local_page_does_not_pass_target_check(mail_index):
    searcher, _, parts = mail_index
    correct_part = parts["haupt.eml"]["Anlage B.pdf"]["part_no"]
    rows, summary = evaluate(searcher, [
        {"suche": "Nordquarz Druckprobe", "datei": "haupt.eml", "teil": "1", "teilseite": "2"},
        {"suche": "Nordquarz Druckprobe", "datei": "haupt.eml", "teil": str(correct_part), "teilseite": "1"},
        {"suche": "Nordquarz Druckprobe", "datei": "haupt.eml", "teil": str(correct_part), "teilseite": "2"},
    ])
    assert all(r.rang == 1 for r in rows)     # Dateirang bleibt als eigene Kennzahl erhalten
    assert [r.ziel_erfuellt for r in rows] == [False, False, True]
    assert [r.kategorie for r in rows] == ["Fundstelle", "Fundstelle", "ok"]
    assert (rows[0].teil_erwartet, rows[0].teil_gefunden) == (1, correct_part)
    assert (rows[1].teilseite_erwartet, rows[1].teilseite_gefunden) == (1, 2)
    assert summary["top_5"] == 3 and summary["ziel_top_5"] == 1
    assert summary["teil_falsch"] == 1 and summary["teilseite_falsch"] == 1
    formatted = format_eval(rows, summary)
    assert "Ziel in Top 5: 1/3" in formatted and "Falscher Teil:  1" in formatted
    assert "Falsche Teilseite: 1" in formatted


def test_mail_body_is_part_zero_and_plain_pdf_is_not_a_mail_part(mail_index):
    searcher, _, _ = mail_index
    rows, summary = evaluate(searcher, [
        {"suche": "Kontrollvermerk Mailablage", "datei": "haupt.eml", "teil": "0", "teilseite": "1"},
        {"suche": "Nordquarz Druckprobe", "datei": "einzel.pdf", "teil": "0", "teilseite": "2"},
    ])
    assert rows[0].teil_gefunden == 0 and rows[0].ziel_erfuellt
    assert rows[1].teil_gefunden is None and not rows[1].ziel_erfuellt
    assert rows[1].kategorie == "Fundstelle" and summary["teil_falsch"] == 1


def test_existing_global_page_semantics_and_file_ranks_remain_compatible(mail_index):
    searcher, _, _ = mail_index
    [row], summary = evaluate(searcher, [{"suche": "Nordquarz Druckprobe", "datei": "haupt.eml", "seite": "2"}])
    assert row.rang == 1 and row.seite_gefunden == 5
    assert row.kategorie == "ok" and row.ursache == "gefunden, aber Seite 5 statt 2"
    assert not row.ziel_erfuellt
    assert summary["platz_1"] == summary["top_5"] == 1
    assert summary["seite_falsch"] == 1 and summary["ziel_top_5"] == 0


@pytest.mark.parametrize("part,local_page", [("-1", ""), ("1.5", ""), ("true", ""),
                                            ("1", "0"), ("1", "-1"), ("1", "x"), ("", "2")])
def test_csv_rejects_invalid_part_targets_with_line_context(tmp_path, part, local_page):
    path = tmp_path / "fragen.csv"
    path.write_text(f"suche;datei;teil;teilseite\nNordquarz;haupt.eml;{part};{local_page}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fragen.csv, Zeile 2:"):
        load_queries(path)


def test_csv_accepts_optional_part_columns_and_body_zero(tmp_path):
    path = tmp_path / "fragen.csv"
    path.write_text("suche;datei;teil;teilseite\nNordquarz;haupt.eml;3;2\nMailablage;haupt.eml;0;1\n",
                    encoding="utf-8")
    rows = load_queries(path)
    assert [(r["teil"], r["teilseite"]) for r in rows] == [("3", "2"), ("0", "1")]


def test_direct_evaluation_rejects_local_page_without_part(mail_index):
    searcher, _, _ = mail_index
    with pytest.raises(ValueError, match="Suchfrage 1: teilseite braucht eine Teilnummer"):
        evaluate(searcher, [{"suche": "Nordquarz", "datei": "haupt.eml", "teilseite": "2"}])
