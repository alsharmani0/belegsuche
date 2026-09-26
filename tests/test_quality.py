"""Deutsche Sonderfälle: kaputte Scanner-Textebenen, Komposita mit eingeschobenem Wortteil,
Tippfehler innerhalb langer Komposita."""
from belegsuche import db as dbm
from belegsuche.extract import garbage_ratio, layer_is_clean
from belegsuche.indexer import run_index
from belegsuche.search import Searcher

from .conftest import digital_pdf

GOOD = """Wärmetechnik Sommer GmbH · Gewerbering 14 · 12345 Musterstadt
Rechnung Nr. RE-2021/00487 vom 12.03.2021, Kunden-Nr. 4711, E-Mail info@sommer-waerme.example
Für die ausgeführten Arbeiten erlauben wir uns, Ihnen Folgendes zu berechnen:
Heizungswartung Brennwertkessel inkl. Abgasmessung, Anfahrt pauschal, Kleinmaterial
Gesamtbetrag: 1.234,56 € zahlbar innerhalb von 14 Tagen ohne Abzug."""

BAD = """Gl@n2verl Gehäucetciilgnnq 9n6H vrtcnhat5rc1nigur9 · Kro5tebn1ux9 · +reqpehh@nztinignn
qvmlzt@kn O| · ~ O||Zs Mnstrz+@cf · ITe. °l°Z oZ ‚ !? O! · kcntal+@9l@n2vctk_te1hlqvnq
Rechnvnq O6jek+' costi1enwcg !a~, °?|°| Bi5qlclhouscn Bezeiohnnn9 An2obl ME"""


def test_garbage_layer_detected():
    assert garbage_ratio(GOOD) < 0.05 and layer_is_clean(GOOD)
    assert garbage_ratio(BAD) > 0.3 and not layer_is_clean(BAD)


def test_bridged_compound_and_fuzzy_inside(tmp_path):
    root = tmp_path / "k"
    root.mkdir()
    digital_pdf(root / "a.pdf", [["Brandschutz Nord GmbH", "Prüfung Rauchwarnmelder Lindenweg 3",
                                  "Wir berechnen für die jährliche Sichtprüfung"]])
    digital_pdf(root / "b.pdf", [["Bezirksschornsteinfegermeister Probe", "Feuerstättenschau Kastanienweg 5a",
                                  "Gebührenbescheid für die Überprüfung der Anlage"]])
    digital_pdf(root / "c.pdf", [["Hausverwaltung Nord", "Lindenweg 3 Treppenhaus", "Allgemeine Mitteilung an Mieter"]])
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], log=lambda s: None)
    s = Searcher(tmp_path / "i.db")
    assert s.search("Rauchmelder Lindenweg").hits[0].filename == "a.pdf"
    assert s.search("Schornsteinfger Kastanienweg").hits[0].filename == "b.pdf"
