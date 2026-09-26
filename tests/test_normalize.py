from belegsuche.normalize import extra_tokens, fold, index_text, path_index_text, words
from belegsuche.search import parse_query


def test_fold_umlauts_and_nfd():
    assert fold("Müller Straße") == "mueller strasse"
    assert fold("Müller") == "mueller"          # macOS-Dateinamen (NFD)
    assert fold("MÜLLER") == "mueller"
    assert fold("Café") == "cafe"


def test_amount_formats_share_token():
    for text in ("Gesamt 1.234,56 €", "EUR 1234,56", "Total 1234.56", "1,234.56 USD"):
        assert "0amt1234c56" in extra_tokens(text), text
    assert "0amt1234c00" in extra_tokens("Betrag 1.234,- €")
    assert "0amt250" in extra_tokens("Pauschale 250 EUR")
    # Datum ist kein Betrag
    assert not any(t.startswith("0amt12c03") for t in extra_tokens("am 12.03.2021"))


def test_invoice_numbers_share_token():
    a = extra_tokens("Rechnung RE-2021/00487")
    b = extra_tokens("Rechnung RE 2021 00487")
    c = extra_tokens("Re.-Nr. 2021-00487")
    assert "0id202100487" in a and "0id202100487" in b and "0id202100487" in c
    assert "0idre202100487" in a and "0idre202100487" in b
    assert "0idinv88213" in extra_tokens("INV-88213")
    assert "0id471122" in extra_tokens("Nr. 4711/22")


def test_dates():
    assert {"0dt20210312", "0mo202103", "0yr2021"} <= set(extra_tokens("vom 12.03.2021"))
    assert {"0dt20210312", "0mo202103"} <= set(extra_tokens("12. März 2021"))
    assert "0mo202103" in extra_tokens("Abrechnung 03/2021")
    assert "0dt20210312" in extra_tokens("2021-03-12")
    assert "0dt20200705" in extra_tokens("05.07.20")


def test_street_variants():
    t = set(index_text("Objekt Musterstr. 12").split())
    assert {"musterstr", "musterstrasse"} <= t
    t = set(index_text("Objekt Musterstraße 12").split())
    assert {"musterstr", "musterstrasse"} <= t
    t = set(index_text("Muster Str. 12").split())
    assert "musterstrasse" in t


def test_path_text_has_folders_and_dates():
    t = path_index_text("2021/Objekte/Musterstraße 12/Scan_20190312_00487.pdf").split()
    assert "objekte" in t and "musterstrasse" in t and "0dt20190312" in t and "pdf" not in t


def test_parse_query_kinds():
    kinds = {g.kind for g in parse_query('Rechnung RE-2021/00487 1.234,56 € 12.03.2021 2021 "Brennwert Kessel"')}
    assert kinds == {"word", "id", "amount", "date", "number", "phrase"}
    g = [g for g in parse_query("Heizung für die Musterstraße") if g.kind == "word"]
    assert [x.label for x in g] == ["heizung", "musterstrasse"]   # Füllwörter entfallen


def test_words_order():
    assert words("Wartung der Heizung") == ["wartung", "der", "heizung"]
