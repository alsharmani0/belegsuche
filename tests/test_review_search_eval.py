"""Unabhängige Regressionen mit ausschließlich künstlichen Dokumenten im temporären Ordner."""
import csv

import pytest

from belegsuche import db as dbm
from belegsuche.evaluate import EVAL_LIMIT, diagnose, evaluate, load_queries
from belegsuche.normalize import amounts, extra_tokens
from belegsuche.search import Searcher, parse_query


def _index(tmp_path, documents):
    root = tmp_path / "Belege"
    root.mkdir()
    db_path = tmp_path / "index.db"
    con = dbm.connect(db_path)
    try:
        root_id = con.execute("INSERT INTO roots(path) VALUES (?)", (str(root),)).lastrowid
        for i, (relpath, pages) in enumerate(documents.items()):
            original = root / relpath
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_text("künstlicher Beleg", encoding="utf-8")
            file_id = con.execute(
                "INSERT INTO files(root_id, relpath, status, sha256, text_chars, pages) "
                "VALUES (?, ?, 'done', ?, ?, ?)",
                (root_id, relpath, f"synthetisch-{i}", sum(map(len, pages)), len(pages)),
            ).lastrowid
            for page_no, text in enumerate(pages, 1):
                con.execute("INSERT INTO pages(file_id, page_no, text) VALUES (?, ?, ?)",
                            (file_id, page_no, text))
            dbm.index_file_text(con, file_id, relpath)
        con.commit()
    finally:
        con.close()
    return Searcher(db_path)


def test_csv_preserves_newlines_in_quoted_fields(tmp_path):
    path = tmp_path / "fragen.csv"
    path.write_bytes('suche;datei;notiz\r\n"Wartung\r\nHeizung";ziel.pdf;"Zeile 1\r\nZeile 2"\r\n'.encode())
    [row] = load_queries(path)
    assert row["suche"].split() == ["Wartung", "Heizung"]
    assert row["notiz"].splitlines() == ["Zeile 1", "Zeile 2"]
    assert [g.label for g in parse_query(row["suche"])] == ["wartung", "heizung"]


@pytest.mark.parametrize("delimiter", [";", ",", "\t"])
def test_excel_csv_preserves_phrase_quotes_and_file_names(tmp_path, delimiter):
    path = tmp_path / "fragen.csv"
    expected = {"suche": '"roter Hahn" Vertrag', "datei": 'Akte "Mai".pdf'}
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=expected, delimiter=delimiter)
        writer.writeheader()
        writer.writerow(expected)
    assert load_queries(path) == [expected]


def test_unterminated_quote_cannot_absorb_following_queries(tmp_path):
    path = tmp_path / "fragen.csv"
    path.write_text('suche;datei;notiz\nKessel;eins.pdf;"offen\nTherme;zwei.pdf;ok\n')
    with pytest.raises(ValueError, match="CSV"):
        load_queries(path)


def test_distinct_indexed_files_with_equivalent_search_paths(tmp_path):
    searcher = _index(tmp_path, {"Akte/ROT.pdf": ["Dachstuhl"], "Akte/rot.pdf": ["Ventiltausch"]})
    queries = [{"suche": "Ventiltausch", "datei": path}
               for path in ("Akte/ROT.pdf", "Akte/rot.pdf", "AKTE/Rot.pdf", "/Akte/rot.pdf")]
    rows, _ = evaluate(searcher, queries)
    assert [(r.rang, r.kategorie) for r in rows] == [
        (None, "Wortwahl"), (1, "ok"), (None, "Index"), (None, "Index")]


def test_search_counts_candidate_pages_before_limiting_hits(tmp_path):
    searcher = _index(tmp_path, {"a.pdf": ["Kesselwartung", "Kesselwartung"], "b.pdf": ["Kesselwartung"]})
    response = searcher.search("Kesselwartung", limit=1)
    assert len(response.hits) == 1
    assert response.candidates == 3


@pytest.mark.parametrize("batch", [False, True])
def test_index_update_during_snippet_read_uses_consistent_pages(tmp_path, monkeypatch, batch):
    searcher = _index(tmp_path, {"ziel.pdf": ["Solarwartung Dachstuhl"]})
    writer = dbm.connect(searcher.db_path)
    connect_reader = searcher._connect
    deleted = []

    def delete_before_snippet(sql):
        if not deleted and sql.startswith("SELECT text FROM pages WHERE id ="):
            dbm.delete_search_rows(writer, 1)
            writer.execute("DELETE FROM files WHERE id=1")
            writer.commit()
            deleted.append(True)

    def traced_reader():
        con = connect_reader()
        con.set_trace_callback(delete_before_snippet)
        return con

    monkeypatch.setattr(searcher, "_connect", traced_reader)
    try:
        if batch:
            rows, _ = evaluate(searcher, [{"suche": q, "datei": "ziel.pdf"} for q in ("Dachstuhl", "Solarwartung")])
            assert [r.rang for r in rows] == [1, 1]
        else:
            assert searcher.search("Dachstuhl").hits[0].filename == "ziel.pdf"
        assert deleted and searcher.search("Dachstuhl").hits == []
    finally:
        writer.close()


@pytest.mark.parametrize("bad_row,missing", [(";fehler.pdf;1", "suche"),
                                             ("Wartung;;1", "datei"), ("Wartung", "datei")])
def test_csv_rejects_nonempty_incomplete_rows_instead_of_dropping_them(tmp_path, bad_row, missing):
    path = tmp_path / "fragen.csv"
    path.write_text("suche;datei;seite\nWartung;ziel.pdf;1\n" + bad_row + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"fragen.csv, Zeile 3:.*fehlt: {missing}"):
        load_queries(path)


def test_csv_error_line_accounts_for_leading_blanks_and_multiline_rows(tmp_path):
    path = tmp_path / "fragen.csv"
    path.write_text('\n\nsuche;datei;seite\n"Solar\nwartung";ziel.pdf;1\n;fehler.pdf;1\n', encoding="utf-8")
    with pytest.raises(ValueError, match="fragen.csv, Zeile 6:"):
        load_queries(path)


@pytest.mark.parametrize("page", ["0", "-1", "abc", "1.5", "1_0", "1 0", "9" * 5000])
def test_csv_rejects_invalid_page_with_line_context(tmp_path, page):
    path = tmp_path / "fragen.csv"
    path.write_text(f"suche;datei;seite\nWartung;ziel.pdf;1\nWartung;fehler.pdf;{page}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fragen.csv, Zeile 3: seite muss eine positive ganze Zahl ab 1"):
        load_queries(path)


@pytest.mark.parametrize("header", ["suche; SUCHE ;datei", "suche;datei; DATEI ", "suche;datei;seite; SEITE "])
def test_csv_rejects_duplicate_normalized_headers(tmp_path, header):
    path = tmp_path / "fragen.csv"
    path.write_text(header + "\nWartung;ziel.pdf;1;1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fragen.csv, Zeile 1: Doppelte Spaltennamen:"):
        load_queries(path)


def test_csv_allows_fully_blank_rows_and_optional_page(tmp_path):
    path = tmp_path / "fragen.csv"
    path.write_text("suche;datei;seite;notiz\n\n ; ; ; \nWartung;ziel.pdf;;\n\n", encoding="utf-8")
    [row] = load_queries(path)
    assert row == {"suche": "Wartung", "datei": "ziel.pdf", "seite": "", "notiz": ""}
    path.write_text("suche;datei\nWartung;ziel.pdf\n", encoding="utf-8")
    assert load_queries(path) == [{"suche": "Wartung", "datei": "ziel.pdf"}]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "cp1252", "mac_roman"])
@pytest.mark.parametrize("delimiter", [";", ",", "\t"])
def test_csv_validation_preserves_supported_encodings_dialects_and_multiline_fields(tmp_path, encoding, delimiter):
    path = tmp_path / "fragen.csv"
    text = (delimiter.join((" SUCHE ", " DATEI ", " SEITE ", "notiz")) + "\n" +
            delimiter.join(('"Müller\nHeizung"', "ziel.pdf", "02", "Prüfung")) + "\n")
    path.write_bytes(text.encode(encoding))
    [row] = load_queries(path)
    assert row == {"suche": "Müller\nHeizung", "datei": "ziel.pdf", "seite": "02", "notiz": "Prüfung"}


@pytest.mark.parametrize("page", [0, -1, "-1", "abc", 1.5, True])
def test_direct_evaluation_does_not_silently_disable_invalid_page_checks(tmp_path, page):
    searcher = _index(tmp_path, {"ziel.pdf": ["Solarwartung Rechnung"]})
    with pytest.raises(ValueError, match="Suchfrage 1: seite muss eine positive ganze Zahl ab 1"):
        evaluate(searcher, [{"suche": "Solarwartung", "datei": "ziel.pdf", "seite": page}])


@pytest.mark.parametrize("page", ["1", "01", "+1", "١", 1])
def test_direct_evaluation_accepts_positive_integer_page_formats(tmp_path, page):
    searcher = _index(tmp_path, {"ziel.pdf": ["Solarwartung Rechnung"]})
    [row], summary = evaluate(searcher, [{"suche": "Solarwartung", "datei": "ziel.pdf", "seite": page}])
    assert row.seite_erwartet == 1 and row.seite_gefunden == 1
    assert summary["seite_falsch"] == 0


def test_diagnosis_recognizes_phrase_in_folder(tmp_path):
    searcher = _index(tmp_path, {"Akte Gruen/ziel.pdf": ["Handwerker Rechnung ohne Ortsangabe"]})
    query = '"Akte Gruen"'
    assert searcher.search(query).hits[0].relpath == "Akte Gruen/ziel.pdf"
    con = searcher._connect()
    try:
        assert diagnose(con, searcher, query, "Akte Gruen/ziel.pdf") == (
            "Alle Begriffe im Dokument, aber zu weit unten sortiert", "Ranking")
    finally:
        con.close()


def test_diagnosis_uses_the_same_compound_matching_as_search(tmp_path):
    searcher = _index(tmp_path, {"Therme/ziel.pdf": ["Wartung Handwerker Rechnung"],
                                 "kontext.pdf": ["Therme Wartung Vertrag"]})
    query = "Thermenwartung"
    assert [h.relpath for h in searcher.search(query).hits] == ["kontext.pdf"]
    con = searcher._connect()
    try:
        # Die Suche verbindet keine Kompositum-Teile aus Ordnername und Belegtext.
        assert diagnose(con, searcher, query, "Therme/ziel.pdf") == (
            "Im Dokument nicht gefunden: thermenwartung", "Wortwahl")
    finally:
        con.close()


@pytest.mark.parametrize("k", [0, -1, EVAL_LIMIT + 1, 1.5, "5"])
def test_evaluation_rejects_k_outside_measured_range(tmp_path, k):
    # Muss vor dem Öffnen einer Datenbank scheitern.
    with pytest.raises(ValueError, match=f"1 und {EVAL_LIMIT}"):
        evaluate(Searcher(tmp_path / "nicht_vorhanden.db"), [], k=k)


@pytest.mark.parametrize("k", [1, EVAL_LIMIT])
def test_evaluation_accepts_measured_range_endpoints(tmp_path, k):
    searcher = _index(tmp_path, {"ziel.pdf": ["Solarwartung Rechnung"]})
    rows, summary = evaluate(searcher, [{"suche": "Solarwartung", "datei": "ziel.pdf"}], k=k)
    assert rows[0].rang == 1
    assert summary[f"top_{k}"] == 1


@pytest.mark.parametrize("query", ["9" * 5000, "9" * 5000 + ",00", "1" + ".000" * 1700,
                                   "Euro " + "9" * 5000])
def test_long_numeric_queries_do_not_use_bounded_integer_conversion(query):
    groups = parse_query(query)
    assert len(groups) == 1
    assert any(a.startswith("0amt") for a in groups[0].alts)


@pytest.mark.parametrize("text", ["9" * 5000 + ",00", "9" * 5000 + " EUR", "000" + "9" * 5000 + ",00"])
def test_long_amounts_keep_exact_magnitude(text):
    assert "0amt" + "9" * 5000 in {token for _, _, tokens in amounts(text) for token in tokens}


def test_long_number_does_not_discard_document_search_text(tmp_path):
    query = "9" * 5000
    searcher = _index(tmp_path, {"ziel.pdf": ["Solarwartung " + query + ",00 EUR"]})
    assert searcher.search("Solarwartung").hits[0].relpath == "ziel.pdf"
    assert searcher.search(query).hits[0].relpath == "ziel.pdf"


def test_number_canonicalization_keeps_existing_tokens():
    assert "0amt123c45" in extra_tokens("000123,45 EUR")
    assert "0amt123c45" in extra_tokens("١٢٣,45 EUR")
    assert "0amt0c00" in extra_tokens("000,00 EUR")
    address = next(g for g in parse_query("Baumarkt ١٢٠") if g.kind == "number")
    assert "0amt120" in address.alts


@pytest.mark.parametrize("address,numbers", [
    ("Musterweg 7, 11", {7, 11}),
    ("Musterweg 12, 14, 16, 18", {12, 14, 16, 18}),
    ("Musterweg 18, 12, 18", {12, 18}),
    ("Musterweg 7-11", set(range(7, 12))),
    ("Musterweg 7–11", set(range(7, 12))),
    ("Musterweg 7/11", set(range(7, 12))),
    ("Musterweg 7-47", set(range(7, 48))),
    ("Musterweg 7-48", {7}),
    ("Musterweg 11-7", {11}),
    ("Musterweg 7-9, 12, 14-16", {7, 8, 9, 12, 14, 15, 16}),
    ("Musterweg 7, 12345 Musterstadt", {7}),
    ("Musterweg 12, 14, 12345 Musterstadt", {12, 14}),
    ("Musterweg 12, 14, 1234", {12, 14}),
])
def test_house_number_lists_ranges_and_postal_code_boundary(address, numbers):
    tokens = {t for t in extra_tokens(address) if t.startswith("0hn")}
    assert tokens == {f"0hnmusterweg{n}" for n in numbers}


def test_house_number_list_keeps_separate_street_variants():
    tokens = {t for t in extra_tokens("Muster Str. 12, 14, 16, 18") if t.startswith("0hnmuster")}
    assert tokens == {f"0hn{street}{n}" for street in ("musterstr", "musterstrasse")
                      for n in (12, 14, 16, 18)}


def test_house_number_list_does_not_count_unlisted_house_as_exact_match(tmp_path):
    searcher = _index(tmp_path, {"liste.pdf": ["Wartung Musterweg 7, 11"],
                                 "haus9.pdf": ["Wartung Musterweg 9"]})
    hits = searcher.search("Musterweg 9").hits
    assert hits[0].relpath == "haus9.pdf"
    listed = next(hit for hit in hits if hit.relpath == "liste.pdf")
    assert "musterweg 9" in listed.missing
    assert hits[0].score > listed.score + 0.1


@pytest.mark.parametrize("number", [12, 14, 16, 18])
def test_every_explicit_house_in_a_long_list_matches_at_full_weight(tmp_path, number):
    searcher = _index(tmp_path, {"liste.pdf": ["Wartung Musterweg 12, 14, 16, 18"]})
    [hit] = searcher.search(f"Musterweg {number}").hits
    assert hit.score >= 1.0
