"""Regressionstests zu den Suchqualitäts-Funden F1–F3 aus dem Code-Review vom 27.09.2026
(05-belegsuche-code-review.md) und zur Rückrichtung der getrennt geschriebenen Straße.

Belege werden direkt in die Datenbank geschrieben (ohne PDF/OCR), wie in test_claude_search.py."""
from belegsuche import db as dbm
from belegsuche.normalize import index_text
from belegsuche.search import parse_query

from .test_claude_search import FILLER, build, top


def _groups(q):
    return {g.label: g for g in parse_query(q)}


# --- F1: Zeiträume „2024/25“, Nummern „2021/22“, Monat „2020-12“ -------------------------------

def test_zeitraum_kurzform_enthaelt_die_woertliche_schreibweise():
    for q in ("Betriebskostenabrechnung 2024/25", "Betriebskostenabrechnung 2024/2025"):
        g = _groups(q)["2024/2025"]
        assert g.kind == "date"
        assert g.alts["2024 25"] == 1.0 and g.alts["0id202425"] == 1.0, q   # so steht es im Beleg
        assert g.alts["2024 2025"] == 1.0


def test_zeitraum_kurzform_findet_beleg_ohne_datumsangaben(tmp_path):
    s = build(tmp_path, {
        "alt.pdf": ["Betriebskostenabrechnung 2023/24 Objekt Lindenweg 3", *FILLER],
        "ziel.pdf": ["Betriebskostenabrechnung 2024/25 Objekt Lindenweg 3", *FILLER],
        "neu.pdf": ["Betriebskostenabrechnung 2025/26 Objekt Lindenweg 3", *FILLER],
    })
    for q in ("Betriebskostenabrechnung 2024/25", "Betriebskostenabrechnung 2024/2025"):
        resp = s.search(q)
        assert resp.hits[0].relpath == "ziel.pdf", q
        assert resp.hits[0].missing == [], q
        assert all(h.missing for h in resp.hits[1:]), q


def test_nummer_im_jahresformat_wird_gefunden(tmp_path):
    s = build(tmp_path, {
        "ziel.pdf": ["Rechnung 2021/22 Wärmetechnik Brandl", *FILLER],
        "andere.pdf": ["Rechnung 2020/21 Wärmetechnik Brandl", *FILLER],
    })
    resp = s.search("Rechnung 2021/22")
    assert resp.unknown == []                                   # die Nummer selbst kommt im Bestand vor
    assert resp.hits[0].relpath == "ziel.pdf" and resp.hits[0].missing == []
    assert [h.missing for h in resp.hits if h.relpath == "andere.pdf"] == [["2021/2022"]]


def test_jahr_bindestrich_monat_ist_kein_zeitraum():
    g = _groups("Kontoauszug 2020-12")["2020-12"]
    assert g.kind == "date"
    assert g.alts["0mo202012"] == 1.0
    assert not any(a.startswith("0yr") for a in g.alts) and "0id20202012" not in g.alts   # nicht 2020/2012
    assert not any(a.startswith("0yr") for a in _groups("Abrechnung 2023-05")["2023-05"].alts)


def test_jahr_bindestrich_monat_findet_den_monat_und_nicht_das_andere_jahr(tmp_path):
    s = build(tmp_path, {
        "k2012.pdf": ["Kontoauszug Nr. 3 vom 15.03.2012 Girokonto", *FILLER],
        "k2020.pdf": ["Kontoauszug Nr. 12 vom 31.12.2020 Girokonto", *FILLER],
    })
    resp = s.search("Kontoauszug 2020-12")
    assert resp.hits[0].relpath == "k2020.pdf" and resp.hits[0].missing == []
    assert all("2020-12" in h.missing for h in resp.hits if h.relpath == "k2012.pdf")


def test_nicht_aufeinanderfolgende_jahre_mit_zweistelliger_zahl_sind_eine_nummer():
    kinds = {g.label: g.kind for g in parse_query("Angebot 2023-17")}
    assert kinds.get("2023-17") == "id"


# --- F2: kurzes Wort vor einer Zahl, mehrdeutige Straßen-Endungen ------------------------------

def test_kurzes_wort_vor_zahl_bleibt_suchwort_und_zahl_zaehlt_als_betrag():
    for word in ("dach", "gas"):
        groups = _groups(f"{word.title()} 450")
        assert groups[word].kind == "word"
        assert groups["450"].alts["0amt450"] == 1.0


def test_uebliche_kuerzel_und_grossbuchstaben_bleiben_nummer_mit_kuerzel():
    for q, label in (("RE 9142", "re 9142"), ("Kd 12345678", "kd 12345678"), ("nr 4711", "nr 4711"),
                     ("WEG 12345", "weg 12345")):
        g = _groups(q)
        assert label in g and g[label].kind == "id", q


def test_dach_800_findet_dachdecker_mit_betrag(tmp_path):
    s = build(tmp_path, {
        "dach.pdf": ["Dachdeckerei Kowalski Rechnung Reparatur Dachrinne Gesamtbetrag 800,00 €", *FILLER],
        "heizung.pdf": ["Wärmetechnik Brandl Rechnung Wartung Gesamtbetrag 800,00 €", *FILLER],
        "dach_anders.pdf": ["Dachdeckerei Kowalski Rechnung Dachrinne Gesamtbetrag 450,00 €", *FILLER],
    })
    resp = s.search("Dach 800")
    assert resp.hits[0].relpath == "dach.pdf"
    assert resp.hits[0].missing == []


def test_baumarkt_zahl_zaehlt_auch_als_betrag():
    g = _groups("Baumarkt 120")["baumarkt 120"]
    assert g.alts["0amt120"] >= 0.85 and g.alts["120"] >= 0.8     # vorher nur 0.3
    assert g.alts["0hnbaumarkt120"] == 1.0                        # als Adresse weiterhin am stärksten
    assert _groups("Musterstädter Weg 7")["weg 7"].alts["7"] == 0.3   # echte Straßen unverändert


def test_baumarkt_bon_mit_betrag_vor_bon_mit_zahl_nur_im_text(tmp_path):
    s = build(tmp_path, {
        "bon_betrag.pdf": ["Kassenbon Baumarkt Nord Schrauben Silikon Summe 120,00 €", *FILLER],
        "bon_anders.pdf": ["Kassenbon Baumarkt Nord Filiale 120 Schrauben Summe 45,90 €", *FILLER],
    })
    assert top(s, "Baumarkt 120") == ["bon_betrag.pdf"]


# --- F3: führende Nullen --------------------------------------------------------------------

def test_fuehrende_null_ist_nummer_kein_betrag():
    alts = _groups("00487")["00487"].alts
    assert alts["00487"] == 1.0
    assert not any(a.startswith(("0amt", "0yr")) for a in alts)
    assert "0amt487" in _groups("487")["487"].alts                   # ohne Null weiterhin auch Betrag


def test_fuehrende_null_findet_rechnung_vor_betrag(tmp_path):
    s = build(tmp_path, {
        "betrag.pdf": ["Rechnung Wärmetechnik Brandl Summe 487,00 €", *FILLER],
        "ziel.pdf": ["Rechnung Nr. 00487 Hausmeisterservice Petersen", *FILLER],
        "betrag2.pdf": ["Quittung Summe 487,00 € bar erhalten", *FILLER],
    })
    resp = s.search("00487")
    assert [h.relpath for h in resp.hits] == ["ziel.pdf"]


# --- Straße getrennt geschrieben (Rückrichtung zu test_claude_search) ---------------------------

def test_hausnummer_token_mit_vorangehendem_wort():
    for t in ("Muster Str. 12", "Muster-Straße 12", "Muster - Str. 12"):
        toks = set(index_text(t).split())
        assert {"0hnmusterstrasse12", "0hnmusterstr12"} <= toks, t
    assert not any(x.startswith("0hnam") for x in index_text("Am Str. 5").split())   # zu kurzes Wort


def test_anfrage_mit_getrennter_strasse_findet_zusammengeschriebene(tmp_path):
    s = build(tmp_path, {"ziel.pdf": ["Objekt Musterstr. 12, 12345 Musterstadt", *FILLER],
                         "falsch.pdf": ["Objekt Musterstraße 21, Whg. 12, 12345 Musterstadt", *FILLER],
                         "linden.pdf": ["Objekt Linden Str. 12, 12345 Musterstadt", *FILLER]})
    for q in ("Muster Str. 12", "Muster-Straße 12"):
        assert top(s, q) == ["ziel.pdf"], q


def test_indexformat_wurde_fuer_neue_hausnummer_tokens_erhoeht():
    assert int(dbm.INDEX_FORMAT) >= 8


def test_fuellwort_vor_getrennter_strasse_wird_nicht_zum_strassennamen():
    # Prüfer-Korrektur: "der"/"die" vor "Str." ist kein Straßenname – sonst wäre "derstr" Pflichtbegriff
    for q in ("in der Str. 5", "Die Straße 12"):
        labels = {g.label for g in parse_query(q)}
        assert not any(lbl.startswith(("derstr", "diestr")) for lbl in labels), (q, labels)
    assert "musterstr" in {g.label for g in parse_query("Muster Str. 12")}
