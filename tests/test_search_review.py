"""Regressionstests für die Suche: Rangfolge, Beträge, Tippfehler, Komposita, Adressen."""

from belegsuche import db as dbm
from belegsuche import search as srch
from belegsuche.indexer import run_index
from belegsuche.search import Searcher, parse_query

from .conftest import digital_pdf


def build(tmp_path, docs: dict[str, list[list[str]]]):
    root = tmp_path / "k"
    for rel, pages in docs.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        digital_pdf(p, pages)
    db = tmp_path / "i.db"
    run_index(dbm.connect(db), [str(root)], log=lambda s: None)
    return Searcher(db)


def names(s, q, limit=30):
    return [h.filename for h in s.search(q, limit=limit).hits]


FILLER = ["Sehr geehrte Damen und Herren,", "für die ausgeführten Arbeiten erlauben wir uns zu berechnen:"]


def test_terms_on_different_pages_count_for_the_document(tmp_path):
    docs = {f"andere/R{i}.pdf": [["Wärmetechnik Brandl GmbH", f"Objekt Lindenweg 3, Rechnung {i}", *FILLER]]
            for i in range(40)}
    docs["ziel/Scan_0815.pdf"] = [["Wärmetechnik Brandl GmbH", "Objekt Lindenweg 3", *FILLER],
                                  ["Pos 3 Dachrinne gereinigt", "Pos 4 Anfahrt"]]
    s = build(tmp_path, docs)
    hit = s.search("Brandl Lindenweg Dachrinne").hits[0]
    assert hit.filename == "Scan_0815.pdf" and hit.missing == []


def test_big_pdf_does_not_crowd_out_other_documents(tmp_path):
    docs = {"ordner/Scan_Ordner_komplett.pdf": [[f"Hausgeld Abrechnung Seite {i}", *FILLER] for i in range(250)]}
    for i in range(5):
        docs[f"einzeln/b{i}.pdf"] = [[f"Hausgeld Birkenallee 21 Nachzahlung {i}", *FILLER]]
    s = build(tmp_path, docs)
    got = names(s, "Hausgeld Birkenallee")
    assert {f"b{i}.pdf" for i in range(5)} <= set(got[:5])


def test_amount_variants(tmp_path):
    s = build(tmp_path, {
        "a/leer.pdf": [["Rechnungsbetrag 2 500,00 €", *FILLER]],
        "a/ocr.pdf": [["Gesamt 1.151.44", *FILLER]],
        "a/ohne_punkt.pdf": [["Gesamtbetrag 3700,00 €", *FILLER]],
        "a/zwoelf.pdf": [["Summe 12,50 €", *FILLER]],
        "a/rauschen.pdf": [["Kundennummer 3700", "12,5 % Rabatt entfällt", *FILLER]],
    })
    assert names(s, "2.500,00")[0] == "leer.pdf"
    assert names(s, "1151,44")[0] == "ocr.pdf"
    assert names(s, "3.700")[0] == "ohne_punkt.pdf"
    assert "zwoelf.pdf" in names(s, "12,50")[:1]


def test_transposed_letters(tmp_path):
    s = build(tmp_path, {"x/a.pdf": [["Wärmetechnik Brandl GmbH", *FILLER]], "x/b.pdf": [["Elektro Petersen", *FILLER]]})
    assert names(s, "Brnadl")[0] == "a.pdf"
    assert names(s, "Petresen")[0] == "b.pdf"


def test_unknown_compound_is_split(tmp_path):
    s = build(tmp_path, {"x/schluessel.pdf": [["Schlüsseldienst Nord", "Öffnung der Wohnungstür, 2. OG", *FILLER]],
                         "x/haustuer.pdf": [["Tischlerei West", "Haustür neu eingestellt", *FILLER]],
                         "x/anderes.pdf": [["Malerbetrieb Süd", "Anstrich Treppenhaus", *FILLER]]})
    assert names(s, "Türöffnung")[0] == "schluessel.pdf"


def test_rare_compound_beyond_40_variants(tmp_path):
    many = [f"Heizung{x}" for x in ("anlage", "raum", "rohr", "keller", "pumpe", "ventil", "kessel", "technik",
                                     "wartung", "bau", "firma", "kosten", "abrechnung", "notdienst", "zentrale",
                                     "regler", "leitung", "steuerung", "tausch", "check", "service", "monteur",
                                     "plan", "druck", "wasser", "filter", "anschluss", "schaden", "leck",
                                     "ausfall", "modul", "platte", "gitter", "deckel", "sensor", "fuehler",
                                     "uhr", "schalter", "kabel", "gehaeuse", "halter", "mischer", "dichtung")]
    docs = {f"v/{i}.pdf": [[w, *FILLER]] for i, w in enumerate(many)}
    docs["ziel/thermo.pdf"] = [["Heizungsthermostatkopf getauscht, Birkenallee 21", *FILLER]]
    s = build(tmp_path, docs)
    assert "thermo.pdf" in names(s, "Heizung", limit=100)


def test_inner_substring_does_not_outrank_real_word(tmp_path):
    docs = {f"n/r{i}.pdf": [[f"Rechnung {i} Birkenallee 21", *FILLER, "Wir erlauben uns, die Leistung zu berechnen",
                             "Position " * 20]] for i in range(20)}
    docs["ziel/laub.pdf"] = [["Laubbeseitigung Birkenallee 21", "Gartenpflege Herbst", *FILLER]]
    s = build(tmp_path, docs)
    assert names(s, "Laub Birkenallee")[0] == "laub.pdf"
    assert names(s, "Laub")[0] == "laub.pdf"


def test_street_with_glued_house_number(tmp_path):
    docs = {f"m/d{i}.pdf": [[f"Objekt Musterstraße 12, Rechnung {i}", *FILLER]] for i in range(3)}
    docs["m/andere.pdf"] = [["Objekt Musterweg 7", *FILLER]]
    s = build(tmp_path, docs)
    for q in ("Musterstr.12", "Musterstraße12", "Musterstr. 12"):
        assert set(names(s, q)[:3]) == {"d0.pdf", "d1.pdf", "d2.pdf"}, q


def test_multiword_synonyms(tmp_path):
    s = build(tmp_path, {"x/check.pdf": [["Elektro Nord", "E-Check nach DGUV V3 durchgeführt", *FILLER]]})
    (tmp_path / "synonyme.txt").write_text("elektroprüfung, e-check\n", encoding="utf-8")
    s = Searcher(tmp_path / "i.db")
    assert names(s, "Elektroprüfung")[0] == "check.pdf"


def test_date_is_highlighted(tmp_path):
    s = build(tmp_path, {"x/a.pdf": [["Malerbetrieb Süd", *FILLER, *(["Position Arbeiten laut Aufmaß"] * 12),
                                      "Rechnungsdatum: 12.03.2021"]]})
    assert "<mark>12.03.2021</mark>" in s.search("12.03.2021").hits[0].snippet_html


def test_word_amt_does_not_hit_amounts(tmp_path):
    docs = {f"r/R{i}.pdf": [[f"Rechnung {i}", f"Gesamtbetrag {100 + i},50 €", *FILLER]] for i in range(10)}
    docs["b/bescheid.pdf"] = [["Amt für Abfallwirtschaft", "Gebührenbescheid", *FILLER]]
    s = build(tmp_path, docs)
    assert names(s, "Amt") == ["bescheid.pdf"]


def test_german_quotes_are_phrases_and_odd_digits_do_not_crash():
    assert [g.kind for g in parse_query("„Wartung der Heizung“")] == ["phrase"]
    assert [g.kind for g in parse_query("«Am Hang 7»")] == ["phrase"]
    parse_query("Rechnung ፩ 𐩀5")          # darf nicht abstürzen


def test_best_partial_matches_are_candidates(tmp_path, monkeypatch):
    docs = {f"t/drei{i}.pdf": [[f"Brandl Haustechnik Lindenweg 2016 Nr {i}", *FILLER]] for i in range(3)}
    docs.update({f"t/zwei{i}.pdf": [[f"Brandl Bau Kastanienweg 2016 Nr {i}", *FILLER]] for i in range(12)})
    s = build(tmp_path, docs)
    monkeypatch.setattr(srch, "GROUP_FETCH_MAX_DF", 1)
    monkeypatch.setattr(srch, "CANDIDATE_LIMIT", 3)
    got = names(s, "Brandl Lindenweg 2016 Musterfrau")
    assert set(got[:3]) == {"drei0.pdf", "drei1.pdf", "drei2.pdf"}


def test_new_words_found_while_vocabulary_is_stale(tmp_path):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "a.pdf", [["Elektro Nord", *FILLER]])
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], log=lambda s: None)
    s = Searcher(db)
    s.search("Elektro")                       # Wortschatz geladen
    digital_pdf(root / "b.pdf", [["Dachdecker Kowalski", *FILLER]])
    run_index(con, [], log=lambda s: None)
    assert names(s, "Kowalski") == ["b.pdf"]  # sofort, nicht erst nach dem Neuladen
