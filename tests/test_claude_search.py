"""Suche und Normalisierung: README-Zusagen und Randfälle, die bisher ungetestet waren.

Belege werden direkt in die Datenbank geschrieben (ohne PDF/OCR) – schnell und ohne Vision."""
import os
import shutil
import sqlite3
import unicodedata

import pytest

from belegsuche import db as dbm
from belegsuche.indexer import run_index
from belegsuche.normalize import extra_tokens, fold, index_text, words
from belegsuche.report import build_report
from belegsuche.search import Searcher, make_snippet, parse_query
from belegsuche.synonyms import load_synonyms

from .conftest import digital_pdf

FILLER = ["Sehr geehrte Damen und Herren,", "anbei erhalten Sie die Unterlagen.", "Mit freundlichen Grüßen"]


def build(tmp_path, docs: dict[str, list[str]], root_name="docs"):
    """docs: relpath -> Seitentexte. Direkt in die Datenbank (ohne PDF/OCR)."""
    root = tmp_path / root_name
    root.mkdir()
    con = dbm.connect(tmp_path / "i.db")
    rid = con.execute("INSERT INTO roots(path) VALUES (?)", (str(root),)).lastrowid
    for i, (rel, pages) in enumerate(docs.items()):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        fid = con.execute("INSERT INTO files(root_id, relpath, ext, kind, size, mtime, sha256, status, pages)"
                          " VALUES (?, ?, '.pdf', 'pdf', 1, ?, ?, 'done', ?)",
                          (rid, rel, i, f"sha-{rel}", len(pages))).lastrowid
        for n, text in enumerate(pages, 1):
            con.execute("INSERT INTO pages(file_id, page_no, text) VALUES (?, ?, ?)", (fid, n, text))
        dbm.index_file_text(con, fid, rel)
    con.commit()
    con.close()
    return Searcher(tmp_path / "i.db")


def top(s, q, n=1):
    return [h.relpath for h in s.search(q).hits[:n]]


# --- Normalisierung (README: „Für deutsche Belege gebaut“) --------------------------------------

@pytest.mark.parametrize("text", ["1 234,56 €", "1 234,56 €", "1 234,56 €", "1.234,56€", "EUR 1.234,56"])
def test_betrag_mit_geschuetztem_oder_schmalem_leerzeichen(text):
    assert "0amt1234c56" in extra_tokens(text), text


def test_grosses_eszett_und_vollbreite_zeichen():
    assert fold("MUSTERSTRAẞE") == "musterstrasse"
    assert words("ＲＥＣＨＮＵＮＧ") == ["rechnung"]           # NFKC
    assert fold(unicodedata.normalize("NFD", "Gebühr")) == "gebuehr"


def test_strassen_schreibweisen_auch_mit_bindestrich():
    for t in ("Muster-Straße 12", "Muster Str. 12", "Musterstr. 12", "Musterstraße 12"):
        toks = set(index_text(t).split())
        assert {"musterstrasse", "musterstr"} <= toks, t


@pytest.mark.parametrize("schreibweise", ["Muster Str. 12", "Muster-Straße 12"])
def test_hausnummer_gehoert_zur_strasse_auch_bei_getrennter_schreibweise(tmp_path, schreibweise):
    s = build(tmp_path, {"ziel.pdf": [f"Objekt {schreibweise}, 12345 Musterstadt", *FILLER],
                         "falsch.pdf": ["Objekt Musterstraße 21, Whg. 12, 12345 Musterstadt", *FILLER]})
    # README: Musterstraße = Musterstr. (street_tokens: auch „Muster Str.“ und „Muster-Straße“)
    for q in ("Musterstraße 12", "Musterstr. 12"):
        assert top(s, q) == ["ziel.pdf"], q


def test_hausnummer_gehoert_zur_strasse_bei_zusammengeschriebener_abkuerzung(tmp_path):
    s = build(tmp_path, {"ziel.pdf": ["Objekt Musterstr. 12, 12345 Musterstadt", *FILLER],
                         "falsch.pdf": ["Objekt Musterstraße 21, Whg. 12, 12345 Musterstadt", *FILLER]})
    for q in ("Musterstraße 12", "Musterstr. 12"):
        assert top(s, q) == ["ziel.pdf"], q


def test_datum_zweistellige_jahre_und_monatsnamen():
    assert "0dt20210312" in extra_tokens("12.03.21")
    assert "0dt19990101" in extra_tokens("01.01.99")
    assert "0mo202212" in extra_tokens("Dez. 2022")
    assert "0mo202109" in extra_tokens("Sept 2021")
    assert not any(t.startswith("0dt") for t in extra_tokens("99.99.9999"))


# --- Suche: README-Beispiele in beide Richtungen ------------------------------------------------

@pytest.fixture()
def belege(tmp_path):
    return build(tmp_path, {
        "heizung.pdf": ["Wärmetechnik Sommer GmbH", "Rechnung Nr. RE-2021/00487 Datum: 12.03.2021",
                        "Objekt: Musterstr. 12, 12345 Musterstadt",
                        "Leistung: Heizungswartung Brennwertkessel", "Gesamtbetrag: 1.234,56 €", *FILLER],
        "hauswart.pdf": ["Hauswart Service Petersen", "Rechnung 2022-118", "Lindenweg 3, Musterstadt",
                         "Summe EUR 389,90", *FILLER],
        "mueller.pdf": ["Elektro MUELLER KG", "Rechnung INV-88213 vom 05.07.2020", "Betrag: 642,00 €", *FILLER],
        "strasse.pdf": ["Objekt Birkenallee 21", "Hausmeisterdienst Musterstraße 7", *FILLER],
    })


@pytest.mark.parametrize("query,erwartet", [
    ("Müller", "mueller.pdf"),                 # Müller = Mueller (Beleg ohne Umlaut)
    ("MÜLLER Elektro", "mueller.pdf"),
    ("Musterstr. 7", "strasse.pdf"),           # Abkürzung in der Anfrage, ausgeschrieben im Beleg
    ("Musterstraße 12", "heizung.pdf"),
    ("re 2021 00487", "heizung.pdf"),
    ("1234,56", "heizung.pdf"),
    ("1.234,56", "heizung.pdf"),
    ("1234.56", "heizung.pdf"),                # englische Schreibweise
    ("389,90 €", "hauswart.pdf"),
    ("389,9", "hauswart.pdf"),
    ("2021-03-12", "heizung.pdf"),             # ISO-Datum findet 12.03.2021
    ("12.03.21", "heizung.pdf"),
    ("Juli 2020", "mueller.pdf"),
    ("Hausmeister", "hauswart.pdf"),           # Synonym Hauswart (Hausmeisterdienst ist nur Wortanfang)
    ("Wartung", "heizung.pdf"),                # Wortende: Heizungswartung
    ("ＩＮＶ－８８２１３", "mueller.pdf"),        # Vollbreite Zeichen (NFKC)
])
def test_readme_gleichwertigkeiten(belege, query, erwartet):
    assert erwartet in top(belege, query, 2 if query == "Hausmeister" else 1), query


def test_hausmeister_findet_hauswart_auf_platz_eins_oder_zwei_mit_hausmeisterdienst(belege):
    got = top(belege, "Hausmeister", 2)
    assert set(got) == {"hauswart.pdf", "strasse.pdf"}


def test_betrag_mit_eur_in_der_anfrage_gleich_wie_mit_eurozeichen(belege):
    resp = belege.search("1.234,56 EUR")
    assert resp.hits[0].relpath == "heizung.pdf"
    assert resp.hits[0].missing == []
    assert [h.relpath for h in resp.hits] == ["heizung.pdf"]      # hauswart.pdf hat nur 'EUR', anderen Betrag


# --- Anfragen, die nicht abstürzen dürfen ------------------------------------------------------

@pytest.mark.parametrize("q", ['"', '""', "*", "NEAR(a b)", "a:b", "^heiz", "\x00", "💥", "ß" * 300, "1" * 400,
                               "-", "/", "RE-", "AND", "OR NOT", "(Heizung", "Heizung)", "Heizung'", "Heizung -Wartung",
                               "١٢٣", "²", "Ⅻ", "1.234.567,89", "31.02.2021", "2021-13-45", "퟿", "a" * 5000,
                               " ".join(f"wort{i}" for i in range(60)), "%", "_", "\\", "'; DROP TABLE files; --"])
def test_ungewoehnliche_anfragen_stuerzen_nicht_ab(belege, q):
    resp = belege.search(q)
    assert isinstance(resp.hits, list) and isinstance(resp.unknown, list)


def test_leerer_index_liefert_nichts(tmp_path):
    dbm.connect(tmp_path / "i.db").close()
    resp = Searcher(tmp_path / "i.db").search("Heizung Musterstraße 1.234,56")
    assert resp.hits == []


def test_nur_fuellwoerter_werden_trotzdem_gesucht(belege):
    assert parse_query("der die das")                   # nicht leer: dann wird wörtlich gesucht
    belege.search("der die das")                          # kein Absturz


# --- Textausschnitt ----------------------------------------------------------------------------

def test_snippet_escaped_html_aus_dem_beleg(tmp_path):
    s = build(tmp_path, {"x.pdf": ["<img src=x onerror=alert(1)> Zauberwort & Co <b>fett</b> \"zitat\""]})
    h = s.search("Zauberwort").hits[0]
    assert "<mark>Zauberwort</mark>" in h.snippet_html
    assert "<img" not in h.snippet_html and "<b>" not in h.snippet_html
    assert "&lt;img" in h.snippet_html and "&amp; Co" in h.snippet_html
    assert "»Zauberwort«" in h.snippet_text and "<img" in h.snippet_text   # Klartext für die Kommandozeile


def test_snippet_langer_text_zeigt_die_fundstelle():
    text = " ".join(["Füllwort"] * 400) + " Zauberwort " + " ".join(["Füllwort"] * 400)
    h, t = make_snippet(text, {"zauberwort"})
    assert "<mark>Zauberwort</mark>" in h and h.startswith("… ") and h.endswith(" …")
    assert len(t) < 400


# --- Eigene Synonyme (README: synonyme.txt neben der Index-Datenbank) --------------------------

def test_eigene_synonyme_mit_gleichheitszeichen_und_kommentar(tmp_path):
    (tmp_path / "synonyme.txt").write_text(
        "﻿# eigene Gruppen\nobjektbetreuer = gebäudeservice   # Kommentar am Zeilenende\n\n"
        "# rasen, treppenhaus\n", encoding="utf-8")
    s = build(tmp_path, {"g.pdf": ["Gebäudeservice Nord Rechnung", *FILLER],
                         "t.pdf": ["Treppenhaus gereinigt", *FILLER]})
    assert top(s, "Objektbetreuer") == ["g.pdf"]
    assert s.search("Rasen").hits == []                  # auskommentierte Zeile gilt nicht
    table = load_synonyms(tmp_path / "synonyme.txt")
    assert "gebaeudeservice" in table["objektbetreuer"]


def test_fehlende_synonymdatei_ist_kein_fehler(tmp_path):
    table = load_synonyms(tmp_path / "gibtsnicht.txt")
    assert "hauswart" in table["hausmeister"]


@pytest.mark.parametrize("encoding", ["cp1252", "utf-16"])
def test_synonymdatei_in_anderer_kodierung_bricht_suche_nicht_ab(tmp_path, encoding):
    (tmp_path / "synonyme.txt").write_bytes("objektbetreuer, gebäudeservice\n".encode(encoding))
    s = build(tmp_path, {"g.pdf": ["Gebäudeservice Nord Rechnung", *FILLER]})
    assert top(s, "Gebäudeservice") == ["g.pdf"]


# --- abgezogene SSD, Kopien -----------------------------------------------------------------

class _NoOCR:
    def recognize_image(self, img):
        raise AssertionError("keine Texterkennung erwartet")

    recognize_file = recognize_image


def test_erreichbare_kopie_hat_vorrang_und_offline_bleibt_suchbar(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(), b.mkdir()
    digital_pdf(a / "r.pdf", [["Dachdecker Kowalski Rechnung D-9001", *FILLER]])
    shutil.copy(a / "r.pdf", b / "kopie.pdf")
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(a)], engine=_NoOCR(), log=lambda s: None)
    run_index(con, [str(b)], engine=_NoOCR(), log=lambda s: None)
    con.close()

    for off, on_name in ((a, "kopie.pdf"), (b, "r.pdf")):
        off.rename(tmp_path / "weg")
        hits = Searcher(tmp_path / "i.db").search("Kowalski").hits
        assert len(hits) == 1                             # inhaltsgleich: ein Treffer
        assert hits[0].filename == on_name and hits[0].available
        assert len(hits[0].duplicates) == 1
        (tmp_path / "weg").rename(off)

    a.rename(tmp_path / "A_weg")
    b.rename(tmp_path / "B_weg")
    hits = Searcher(tmp_path / "i.db").search("Kowalski").hits
    assert len(hits) == 1 and hits[0].available is False   # README: Suche funktioniert weiter


# --- Bericht bei sehr großem Bestand -----------------------------------------------------------

def test_bericht_mit_mehr_offenen_dateien_als_sqlite_variablen(tmp_path):
    con = dbm.connect(tmp_path / "i.db")
    limit = con.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    rid = con.execute("INSERT INTO roots(path) VALUES (?)", (str(tmp_path / "ssd"),)).lastrowid
    # z. B. erster Lauf über einen großen Bestand mit Ctrl+C abgebrochen: alles noch 'pending'
    con.executemany("INSERT INTO files(root_id, relpath, ext, kind, status) VALUES (?, ?, '.pdf', 'pdf', 'pending')",
                    ((rid, f"scans/{i:07d}.pdf") for i in range(limit + 1)))
    con.commit()
    try:
        r = build_report(con)
    finally:
        con.close()
    assert r["status"]["pending"] == limit + 1 and len(r["problems"]) == limit + 1


def test_bericht_listet_nur_problemfaelle(tmp_path):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "gut.pdf", [["Dachdecker Kowalski Rechnung D-9001", *FILLER]])
    (root / "leer.pdf").write_bytes(b"")
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=_NoOCR(), log=lambda s: None)
    os.remove(root / "gut.pdf")
    run_index(con, [], engine=_NoOCR(), log=lambda s: None, confirm_missing=True)
    r = build_report(con)
    con.close()
    probs = {os.path.basename(p["pfad"]): (p["status"], p["grund"]) for p in r["problems"]}
    assert probs["gut.pdf"] == ("missing", "Datei fehlt (gelöscht oder verschoben)")
    assert probs["leer.pdf"][0] == "error"
