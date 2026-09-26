"""Rangfolge-Regeln, geprüft an künstlichen Belegen."""
from belegsuche import db as dbm
from belegsuche.normalize import extra_tokens
from belegsuche.search import Searcher, parse_query

FILLER = ["Sehr geehrte Damen und Herren,", "anbei erhalten Sie die Unterlagen.", "Mit freundlichen Grüßen"]


def build(tmp_path, docs: dict[str, list[str]]):
    """docs: relpath -> Seitentexte. Direkt in die Datenbank (ohne PDF/OCR)."""
    root = tmp_path / "docs"
    root.mkdir()
    con = dbm.connect(tmp_path / "i.db")
    rid = con.execute("INSERT INTO roots(path) VALUES (?)", (str(root),)).lastrowid
    for i, (rel, pages) in enumerate(docs.items()):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        fid = con.execute("INSERT INTO files(root_id, relpath, ext, kind, size, mtime, sha256, status, pages)"
                          " VALUES (?, ?, '.pdf', 'pdf', 1, ?, ?, 'done', ?)",
                          (rid, rel, i, f"sha{i}", len(pages))).lastrowid
        for n, text in enumerate(pages, 1):
            con.execute("INSERT INTO pages(file_id, page_no, text) VALUES (?, ?, ?)", (fid, n, text))
        dbm.index_file_text(con, fid, rel)
    con.commit()
    return Searcher(tmp_path / "i.db")


def top(s, q, n=1):
    return [h.relpath for h in s.search(q).hits[:n]]


def test_house_number_belongs_to_street(tmp_path):
    s = build(tmp_path, {
        "a/haus9.pdf": ["Bescheid Niederschlagswasser Musterstädter Weg 9, 12345 Musterstadt", "7 Monate", *FILLER],
        "a/haus7.pdf": ["Bescheid Niederschlagswasser Musterstädter Weg 7, 12345 Musterstadt", *FILLER],
    })
    assert top(s, "Musterstädter Weg 7") == ["a/haus7.pdf"]
    assert top(s, "Niederschlagswasser Musterstädter Weg 9") == ["a/haus9.pdf"]


def test_document_text_counts_more_than_folder_name(tmp_path):
    s = build(tmp_path, {
        "Musterallee 8/rechnung_a.pdf": ["Rauchmelder Wartung Beispielstraße 7", *FILLER],
        "diverses/rechnung_b.pdf": ["Rauchmelder Wartung Musterallee 8", *FILLER],
    })
    assert top(s, "Rauchmelder Musterallee") == ["diverses/rechnung_b.pdf"]


def test_terms_far_apart_in_collective_pdf_do_not_count_fully(tmp_path):
    pages = [f"Buchung {i} Miete Lastschrift" for i in range(30)]
    pages[2] = "Buchung Frau Musterhöfer Miete Lastschrift"
    pages[25] = "Mieterkonto Übersicht Saldo"
    s = build(tmp_path, {
        "bank/Kontoauszug_komplett.pdf": pages,
        "mieter/Musterhoefer.pdf": ["Mieterkonto Frau Musterhöfer Saldovortrag", *FILLER],
    })
    hits = s.search("Musterhöfer Mieterkonto").hits
    assert hits[0].relpath == "mieter/Musterhoefer.pdf"
    assert hits[1].score < hits[0].score - 0.05


def test_shown_page_contains_the_terms_itself(tmp_path):
    s = build(tmp_path, {"a/bescheid.pdf": [
        "Gebührenbescheid Abfallentsorgung Musterallee 8 Restmüll 2025",
        "Allgemeine Hinweise zum Bescheid",
        "Zahlungshinweise Bescheid Musterallee",
    ]})
    assert s.search("Bescheid Abfallentsorgung Musterallee 2025").hits[0].page_no == 1


def test_compound_found_as_nearby_parts_but_not_as_far_apart_words(tmp_path):
    s = build(tmp_path, {
        "a/liste.pdf": ["Objekt Therme Firma Termin", "Musterweg 11 Gastherme Wartung laut Auftrag für Mieter"],
        "a/weit.pdf": ["Therme im Keller. " + "Text " * 60 + " Wartung des Aufzugs", *FILLER],
    })
    hits = s.search("Thermenwartung").hits
    assert hits[0].relpath == "a/liste.pdf"
    assert len(hits) == 1 or hits[1].score < hits[0].score


def test_compound_part_does_not_match_other_compounds_starting_with_it(tmp_path):
    s = build(tmp_path, {
        "bank/auszug.pdf": ["Kontoauszug Mieternummer 4711 Konto 12345 Buchung", *FILLER],
        "mieter/konto.pdf": ["Mieter Konto Saldo offen", *FILLER],
    })
    assert top(s, "Mieterkonto") == ["mieter/konto.pdf"]


def test_singular_plural_and_umlaut_plural(tmp_path):
    s = build(tmp_path, {
        "a/therme.pdf": ["Wartung der Therme im Haus", *FILLER],
        "a/konto.pdf": ["Konto Jahresübersicht 2025", *FILLER],
        "a/anderes.pdf": ["Malerarbeiten Treppenhaus", *FILLER],
    })
    assert top(s, "Thermen") == ["a/therme.pdf"]
    assert top(s, "Konten") == ["a/konto.pdf"]


def test_mieter_is_not_reduced_to_miete(tmp_path):
    s = build(tmp_path, {
        "a/miete.pdf": ["Miete Oktober Lastschrift", *FILLER],
        "a/mieter.pdf": ["Mieter Frau Beispiel Minderung", *FILLER],
    })
    hits = s.search("Mieter").hits
    assert hits[0].relpath == "a/mieter.pdf"
    # "Miete" darf höchstens als Tippfehler-Kandidat weit hinten auftauchen
    assert all(h.score <= 0.6 for h in hits if h.relpath == "a/miete.pdf")


def test_glued_number_and_name_are_split():
    toks = extra_tokens("125 98765432Beispiel Erika")
    assert "beispiel" in toks and "98765432" in toks


def test_scope_words_count_little():
    groups = {g.label: g for g in parse_query("Übersicht Thermenwartung alle Häuser")}
    assert "alle" not in groups and groups["haeuser"].factor < 1 and groups["uebersicht"].factor == 1


def test_address_query_parts():
    kinds = [(g.kind, g.label) for g in parse_query("Musterstädter Weg 7")]
    assert ("number", "weg 7") in kinds and ("word", "musterstaedter") in kinds and ("word", "weg") in kinds


# --- Zeiträume, Etagen, Nummern mit Kürzel, Hausnummernbereiche -----------------------------

def test_year_range_is_period_not_number():
    g = [g for g in parse_query("Wasserrechnung Musterallee 2024/2025") if g.kind == "date"]
    assert g and g[0].label == "2024/2025"
    assert [g.kind for g in parse_query("Abrechnung 2024/25")][0] == "date"


def test_year_range_finds_period_document(tmp_path):
    s = build(tmp_path, {
        "a/wasser.pdf": ["Stadtwerke Musterstadt Wasser Abrechnung Zeitraum 01.10.2024 - 30.09.2025 Musterallee 8", *FILLER],
        "a/wasser_alt.pdf": ["Stadtwerke Musterstadt Wasser Abrechnung Zeitraum 01.10.2023 - 30.09.2024 Musterallee 8", *FILLER],
    })
    assert top(s, "Wasser Musterallee 2024/2025") == ["a/wasser.pdf"]


def test_floor_is_not_house_number(tmp_path):
    kinds = {g.label: g.kind for g in parse_query("Musterstraße 1. Etage")}
    assert "1. etage" in kinds and not any(l.startswith("musterstrasse 1") for l in kinds)
    s = build(tmp_path, {
        "a/og3.pdf": ["Rechnung Musterstraße 1, 3. OG links", *FILLER],
        "a/og1.pdf": ["Rechnung Musterstraße 5, 1. OG rechts", *FILLER],
    })
    assert top(s, "Musterstraße 1. Etage") == ["a/og1.pdf"]


def test_prefixed_number_with_space(tmp_path):
    s = build(tmp_path, {
        "a/re.pdf": ["Rechnung RE9142 vom 12.03.2024", *FILLER],
        "a/andere.pdf": ["Kunde 9142 Rechnung RE0777", *FILLER],
    })
    assert top(s, "RE 9142") == ["a/re.pdf"]
    assert [g.kind for g in parse_query("Nebenkosten bis 2025")] == ["word", "number"]   # kein Kürzel+Jahr


def test_document_type_compound_matches_topic_compound(tmp_path):
    s = build(tmp_path, {
        "a/hm.pdf": ["Rechnung Hausmeisterdienstleistung Januar 2024 Musterstädter Weg", *FILLER],
        "a/anders.pdf": ["Rechnung Gartenpflege Januar 2024 Musterstädter Weg", *FILLER],
        "a/vertrag.pdf": ["Hausmeister Vertrag Petersen 2019", *FILLER],
        "a/protokoll.pdf": ["Protokoll Begehung mit dem Hausmeister", *FILLER],
    })
    assert top(s, "Hausmeisterrechnung 2024 Musterstädter Weg") == ["a/hm.pdf"]


def test_neighbour_page_of_other_document_does_not_count(tmp_path):
    s = build(tmp_path, {
        "sammel/scan.pdf": ["Seite 1 von 1 Rechnung Hausmeister Januar", "Seite 1 von 1 Rechnung Maler Musterstädter Weg"],
        "einzeln/hm.pdf": ["Rechnung Hausmeister Musterstädter Weg", *FILLER],
    })
    assert top(s, "Hausmeister Musterstädter Weg") == ["einzeln/hm.pdf"]


def test_ocr_split_word_is_repaired():
    assert "bescheid" in extra_tokens("Gebühren Besche id vom 10.01.2025")


def test_house_number_ranges_and_lists():
    toks = extra_tokens("Standort: Musterstädter Weg 9, 11 | Objekt Musterstädter Weg 7-11 | Musterstädter Weg 9, 12345 Musterstadt")
    assert {"0hnweg7", "0hnweg8", "0hnweg11"} <= set(toks)


def test_house_number_inside_range_is_found(tmp_path):
    s = build(tmp_path, {
        "a/bereich.pdf": ["Müllabfuhr 2024 Objekt Musterstädter Weg 7-11", *FILLER],
        "a/andere.pdf": ["Müllabfuhr 2024 Objekt Musterstädter Weg 13", *FILLER],
    })
    assert top(s, "Müllabfuhr Musterstädter Weg 11") == ["a/bereich.pdf"]
