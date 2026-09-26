#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Synthetischer deutscher Testkorpus für "belegsuche".

Aufruf (aus dem Projektverzeichnis):

    .venv/bin/python tools/make_testdata.py

Erzeugt deterministisch (fester Seed, reportlab mit invariant=1):

    testdata/korpus/              Belegordner (wird bei jedem Lauf gelöscht und neu erzeugt)
    testdata/manifest.json        ein Eintrag pro Datei inkl. Fakten pro Seite
    testdata/queries_dev.csv      Entwicklungs-Suchanfragen
    testdata/queries_holdout.csv  zurückgehaltene Suchanfragen (unabhängige Evaluation)
    testdata/README.md            Beschreibung des Korpus

Alle Firmen, Personen, Adressen, Telefonnummern und Bankverbindungen sind frei
erfunden. IBANs tragen die Prüfziffer 00 und sind damit ungültig; Web/E-Mail
nutzen die reservierte TLD ".example".

Benötigt: reportlab, pillow, pypdfium2 (im .venv) und das macOS-Tool `sips`
(nur für HEIC). Schriften: /System/Library/Fonts/Supplemental/*.ttf
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import NormalDist

import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

# --------------------------------------------------------------------------------------
# Grundeinstellungen
# --------------------------------------------------------------------------------------

SEED = 20260924
ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "testdata"
KORPUS = TESTDATA / "korpus"
MANIFEST = TESTDATA / "manifest.json"
DEV_CSV = TESTDATA / "queries_dev.csv"
HOLDOUT_CSV = TESTDATA / "queries_holdout.csv"
README = TESTDATA / "README.md"

FONT_DIR = Path("/System/Library/Fonts/Supplemental")
FONT_FILES = {
    "Arial": "Arial.ttf",
    "Arial-B": "Arial Bold.ttf",
    "Times": "Times New Roman.ttf",
    "Times-B": "Times New Roman Bold.ttf",
    "Cour": "Courier New.ttf",
    "Cour-B": "Courier New Bold.ttf",
    "Narrow": "Arial Narrow.ttf",
    "Narrow-B": "Arial Narrow Bold.ttf",
}
FAMILIES = {
    "arial": ("TT-Arial", "TT-Arial-B"),
    "times": ("TT-Times", "TT-Times-B"),
    "courier": ("TT-Cour", "TT-Cour-B"),
    "narrow": ("TT-Narrow", "TT-Narrow-B"),
}
PIL_ARIAL = str(FONT_DIR / "Arial.ttf")
PIL_ARIAL_BLACK = str(FONT_DIR / "Arial Black.ttf")
PIL_ARIAL_BOLD = str(FONT_DIR / "Arial Bold.ttf")

W_A4, H_A4 = A4
D = Decimal
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]


def rng_for(*parts) -> "random.Random":
    import random
    return random.Random(f"{SEED}:" + ":".join(str(p) for p in parts))


def register_fonts() -> None:
    for name, fn in FONT_FILES.items():
        pdfmetrics.registerFont(TTFont("TT-" + name, str(FONT_DIR / fn)))


# --------------------------------------------------------------------------------------
# Formatierung (deutsch)
# --------------------------------------------------------------------------------------

def q2(x: Decimal) -> Decimal:
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def de_number(x, decimals: int = 2, thousands: bool = True) -> str:
    s = format(D(x), f",.{decimals}f")
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    if not thousands:
        s = s.replace(".", "")
    return s


def de_qty(x) -> str:
    x = D(str(x))
    if x == x.to_integral_value():
        return de_number(x, 0)
    s = de_number(x, 3)
    return s.rstrip("0").rstrip(",")


def de_price(x) -> str:
    x = D(str(x))
    if x != x.quantize(D("0.01")):
        return de_number(x, 4)
    return de_number(x, 2)


def fmt_eur(x, style: str = "suffix") -> str:
    if style == "suffix":
        return f"{de_number(x)} €"
    if style == "prefix":
        return f"EUR {de_number(x)}"
    if style == "suffix_eur":
        return f"{de_number(x)} EUR"
    if style == "sym":
        return f"€ {de_number(x)}"
    if style == "plain":
        return de_number(x, thousands=False)
    raise ValueError(style)


def fmt_date(d: date, style: str = "num") -> str:
    if style == "num":
        return d.strftime("%d.%m.%Y")
    if style == "long":
        return f"{d.day}. {MONATE[d.month - 1]} {d.year}"
    if style == "short":
        return d.strftime("%d.%m.%y")
    raise ValueError(style)


ASCII_MAP = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
    "€": "EUR", "–": "-", "·": "|", "×": "x", "²": "2", "³": "3", "§": "Par.",
})


def asciify(s: str) -> str:
    return s.translate(ASCII_MAP)


# --------------------------------------------------------------------------------------
# Stammdaten (alles fiktiv)
# --------------------------------------------------------------------------------------

@dataclass
class Company:
    key: str
    name: str
    tagline: str
    street: str
    city: str
    phone: str
    email: str
    web: str
    iban: str
    bic: str
    bank: str
    taxid: str
    register: str
    owner: str
    color: tuple = (0.1, 0.2, 0.45)
    font: str = "arial"
    header: str = "classic"
    labels: str = "a"
    handwerk: bool = True
    signer: str = ""
    creator: str = "Faktura 4.2"


C = {}


def _co(**kw):
    c = Company(**kw)
    C[c.key] = c


_co(key="brandl", name="Wärmetechnik Brandl GmbH", tagline="Heizung · Sanitär · Solar · Kundendienst",
    street="Gewerbering 14", city="12345 Musterstadt", phone="0123 45 67-80",
    email="info@brandl-waermetechnik.example", web="www.brandl-waermetechnik.example",
    iban="DE00 1234 5000 0081 7730 12", bic="MUSTDEXX101", bank="Sparkasse Musterstadt",
    taxid="USt-IdNr. DE 000 118 245", register="Amtsgericht Musterstadt HRB 2231",
    owner="Geschäftsführer: Anton Brandl", color=(0.62, 0.10, 0.10), header="classic", labels="a",
    signer="Anton Brandl", creator="HandwerkPro 11")
_co(key="mueller", name="Haustechnik Müller GmbH", tagline="Sanitär · Heizung · Kundendienst · Notdienst 24 h",
    street="Kanalstraße 3", city="12346 Musterstadt", phone="0123 77 01 20",
    email="buero@haustechnik-mueller.example", web="www.haustechnik-mueller.example",
    iban="DE00 1234 5000 0040 2211 90", bic="MUSTDEXX101", bank="Sparkasse Musterstadt",
    taxid="USt-IdNr. DE 000 553 170", register="Amtsgericht Musterstadt HRB 1907",
    owner="Geschäftsführer: Bernd Müller, Lena Müller", color=(0.05, 0.33, 0.60), header="bar", labels="b",
    signer="Bernd Müller", creator="SHK-Office 2019")
_co(key="krueger", name="Hauswartservice Krüger", tagline="Hauswart · Objektbetreuung · Winterdienst",
    street="Am Bahndamm 9", city="12349 Beispielhausen", phone="0123 000 42 17",
    email="hauswart.krueger@post.example", web="",
    iban="DE00 5555 0000 0012 9087 65", bic="VOLKDEXX555", bank="Volksbank Beispielhausen",
    taxid="St.-Nr. 000/123/45678", register="", owner="Inhaber: Dennis Krüger",
    color=(0.15, 0.15, 0.15), font="times", header="centered", labels="c", signer="Dennis Krüger",
    creator="Textverarbeitung")
_co(key="petersen", name="Hausmeisterdienst Petersen GmbH",
    tagline="Hausmeisterservice · Kleinreparaturen · Objektbetreuung",
    street="Hafenstraße 27", city="12345 Musterstadt", phone="0123 55 09 10",
    email="auftrag@hmd-petersen.example", web="www.hmd-petersen.example",
    iban="DE00 1234 5000 0077 1200 33", bic="MUSTDEXX101", bank="Sparkasse Musterstadt",
    taxid="USt-IdNr. DE 000 902 614", register="Amtsgericht Musterstadt HRB 3390",
    owner="Geschäftsführerin: Inga Petersen", color=(0.10, 0.40, 0.20), font="narrow", header="classic",
    labels="a", signer="Inga Petersen", creator="Objektmanager 3")
_co(key="vogt", name="Elektro Vogt GmbH & Co. KG", tagline="Elektroinstallation · E-Check · Sprechanlagen",
    street="Schulweg 11", city="12347 Musterstadt", phone="0123 61 61 0",
    email="service@elektro-vogt.example", web="www.elektro-vogt.example",
    iban="DE00 7000 1000 0003 3021 44", bic="HANDDEXX700", bank="Handelsbank Musterstadt",
    taxid="USt-IdNr. DE 000 377 802", register="Amtsgericht Musterstadt HRA 880",
    owner="Komplementärin: Vogt Verwaltungs GmbH, GF Markus Vogt", color=(0.85, 0.45, 0.02),
    header="bar", labels="b", signer="Markus Vogt", creator="ElektroFaktura")
_co(key="stadtwerke", name="Stadtwerke Musterstadt GmbH", tagline="Strom · Gas · Wasser · Wärme",
    street="Werkstraße 1", city="12345 Musterstadt", phone="0123 900-0",
    email="kundenservice@stadtwerke-musterstadt.example", web="www.stadtwerke-musterstadt.example",
    iban="DE00 1234 5000 0000 0100 01", bic="MUSTDEXX101", bank="Sparkasse Musterstadt",
    taxid="USt-IdNr. DE 000 100 001", register="Amtsgericht Musterstadt HRB 100",
    owner="Geschäftsführung: Dr. Petra Hainbach", color=(0.0, 0.30, 0.55), header="classic",
    labels="stadt", handwerk=False, creator="Abrechnungssystem Versorger")
_co(key="nordlicht", name="Nordlicht Versicherung AG", tagline="Wohngebäude · Haftpflicht · Hausrat",
    street="Hafenkante 50", city="12340 Musterstadt", phone="0800 000 12 34",
    email="service@nordlicht-versicherung.example", web="www.nordlicht-versicherung.example",
    iban="DE00 2000 3000 0099 8877 00", bic="NORDDEXX200", bank="Nordbank",
    taxid="VersSt-Nr. 000/000/00017", register="Amtsgericht Musterstadt HRB 17",
    owner="Vorstand: Dr. Karin Ostholt (Vors.), Tim Laakmann", color=(0.05, 0.12, 0.35), font="times",
    header="classic", labels="vers", handwerk=False, creator="Bestandsführung VS")
_co(key="albrecht", name="Schornsteinfegermeister Jens Albrecht",
    tagline="Bevollmächtigter Bezirksschornsteinfeger · Kehrbezirk Musterstadt III",
    street="Rußweg 5", city="12349 Beispielhausen", phone="0123 48 22 91",
    email="j.albrecht@kehrbezirk3.example", web="",
    iban="DE00 5555 0000 0033 4455 66", bic="VOLKDEXX555", bank="Volksbank Beispielhausen",
    taxid="St.-Nr. 000/222/33344", register="", owner="", color=(0, 0, 0), font="courier",
    header="typewriter", labels="a", signer="J. Albrecht", creator="Kehrbuch 2.0")
_co(key="gruenwerk", name="Grünwerk Gartenpflege Schulte", tagline="Garten- und Landschaftspflege · Baumpflege",
    street="Feldweg 2", city="12349 Beispielhausen", phone="0123 39 00 77",
    email="info@gruenwerk-schulte.example", web="www.gruenwerk-schulte.example",
    iban="DE00 5555 0000 0061 7070 81", bic="VOLKDEXX555", bank="Volksbank Beispielhausen",
    taxid="St.-Nr. 000/444/55512", register="", owner="Inh. Mareike Schulte",
    color=(0.15, 0.45, 0.15), header="classic", labels="c", signer="M. Schulte", creator="GaLaBau-Rechnung")
_co(key="kranich", name="Kranich Aufzugtechnik GmbH", tagline="Aufzüge · Wartung · Modernisierung · Notruf 24h",
    street="Industriepark 5", city="12348 Musterstadt", phone="0123 80 80 800",
    email="service@kranich-aufzug.example", web="www.kranich-aufzug.example",
    iban="DE00 7000 1000 0008 8100 20", bic="HANDDEXX700", bank="Handelsbank Musterstadt",
    taxid="USt-IdNr. DE 000 640 118", register="Amtsgericht Musterstadt HRB 5120",
    owner="Geschäftsführer: Olaf Kranich", color=(0.20, 0.22, 0.28), header="bar", labels="inv",
    handwerk=True, creator="SAP-like ERP")
_co(key="glanzwerk", name="Glanzwerk Gebäudereinigung GmbH",
    tagline="Unterhaltsreinigung · Glasreinigung · Treppenhausreinigung",
    street="Parkstraße 19", city="12345 Musterstadt", phone="0123 22 33 44",
    email="kontakt@glanzwerk-reinigung.example", web="www.glanzwerk-reinigung.example",
    iban="DE00 1234 5000 0055 6677 88", bic="MUSTDEXX101", bank="Sparkasse Musterstadt",
    taxid="USt-IdNr. DE 000 789 456", register="Amtsgericht Musterstadt HRB 6012",
    owner="Geschäftsführerin: Sabine Kowalczyk", color=(0.0, 0.45, 0.45), header="centered", labels="b",
    signer="S. Kowalczyk", creator="CleanOffice")
_co(key="hoffmannbeck", name="Dachdeckerei Hoffmann & Beck GbR", tagline="Dach · Fassade · Bauklempnerei",
    street="Ziegeleiweg 4", city="12349 Beispielhausen", phone="0123 47 10 10",
    email="dach@hoffmann-beck.example", web="www.hoffmann-beck.example",
    iban="DE00 5555 0000 0021 2121 21", bic="VOLKDEXX555", bank="Volksbank Beispielhausen",
    taxid="USt-IdNr. DE 000 246 813", register="", owner="Gesellschafter: Uwe Hoffmann, Jörg Beck",
    color=(0.45, 0.22, 0.05), font="times", header="classic", labels="a", signer="Uwe Hoffmann",
    creator="Dachdecker-Office")
_co(key="lindqvist", name="Malerbetrieb Lindqvist", tagline="Meisterbetrieb · Malerarbeiten · Tapezieren · Fassade",
    street="Farbgasse 6", city="12345 Musterstadt", phone="0123 62 62 62",
    email="maler@lindqvist.example", web="www.maler-lindqvist.example",
    iban="DE00 1234 5000 0019 1919 19", bic="MUSTDEXX101", bank="Sparkasse Musterstadt",
    taxid="St.-Nr. 000/555/66677", register="", owner="Inh. Sven Lindqvist, Malermeister",
    color=(0.30, 0.10, 0.40), font="times", header="centered", labels="c", signer="Sven Lindqvist",
    creator="Malerwerk")
_co(key="yilmaz", name="Schlüsseldienst Yilmaz", tagline="Türöffnung · Schließanlagen · Einbruchschutz · 24h-Notdienst",
    street="Marktplatz 3", city="12345 Musterstadt", phone="0123 11 22 333",
    email="info@schluessel-yilmaz.example", web="",
    iban="DE00 7000 1000 0006 5432 10", bic="HANDDEXX700", bank="Handelsbank Musterstadt",
    taxid="St.-Nr. 000/666/77788", register="", owner="Inh. Murat Yilmaz",
    color=(0.55, 0.05, 0.05), header="bar", labels="c", signer="M. Yilmaz", creator="Quittungsblock")
_co(key="weissenfels", name="Weißenfels & Partner Rechtsanwälte mbB",
    tagline="Miet- und Wohnungseigentumsrecht · Immobilienrecht",
    street="Justizplatz 2", city="12345 Musterstadt", phone="0123 40 40 40",
    email="kanzlei@weissenfels-partner.example", web="www.weissenfels-partner.example",
    iban="DE00 2000 3000 0044 4000 10", bic="NORDDEXX200", bank="Nordbank",
    taxid="USt-IdNr. DE 000 313 131", register="Partnerschaftsregister Musterstadt PR 42",
    owner="Dr. Helene Weißenfels, Konstantin Rahe", color=(0.10, 0.10, 0.10), font="times",
    header="centered", labels="ra", handwerk=False, signer="Dr. Helene Weißenfels, Rechtsanwältin",
    creator="Kanzleisoftware")
_co(key="bauwelt", name="BAUWELT Baumarkt Musterstadt", tagline="", street="Industriestraße 30",
    city="12345 Musterstadt", phone="0123 98 76 50", email="", web="", iban="", bic="", bank="",
    taxid="USt-IdNr. DE 000 808 080", register="", owner="", handwerk=False)
_co(key="brueckner", name="Freie Tankstelle Brückner", tagline="", street="Landstraße 120",
    city="12349 Beispielhausen", phone="0123 43 21 00", email="", web="", iban="", bic="", bank="",
    taxid="St.-Nr. 000/777/88899", register="", owner="Inh. Sabine Brückner", handwerk=False)
_co(key="orion", name="ORION Rohr- und Kanalservice GmbH", tagline="", street="Kanalweg 12",
    city="12345 Musterstadt", phone="0123 70 70 70", email="auftrag@orion-rohrservice.example",
    web="www.orion-rohrservice.example", iban="DE00 1234 5000 0070 7070 70", bic="MUSTDEXX101",
    bank="Sparkasse Musterstadt", taxid="USt-IdNr. DE 000 717 171",
    register="Amtsgericht Musterstadt HRB 7117", owner="GF: Nils Orthmann",
    header="logo", labels="b", creator="Rechnung")
_co(key="kaminski", name="Fensterbau Kaminski GmbH", tagline="", street="Am Sägewerk 5",
    city="12349 Beispielhausen", phone="0123 45 00 45", email="info@fenster-kaminski.example",
    web="www.fenster-kaminski.example", iban="DE00 5555 0000 0045 4545 45", bic="VOLKDEXX555",
    bank="Volksbank Beispielhausen", taxid="USt-IdNr. DE 000 454 545",
    register="Amtsgericht Musterstadt HRB 4545", owner="GF: Piotr Kaminski",
    header="logo", labels="a", creator="Rechnung")
_co(key="sonnleitner", name="Glaserei Sonnleitner", tagline="Glas · Spiegel · Reparaturverglasung · Notdienst",
    street="Kirchgasse 10", city="12345 Musterstadt", phone="0123 31 31 00",
    email="glaserei@sonnleitner.example", web="www.glaserei-sonnleitner.example",
    iban="DE00 1234 5000 0031 3131 31", bic="MUSTDEXX101", bank="Sparkasse Musterstadt",
    taxid="St.-Nr. 000/888/99900", register="", owner="Inh. Georg Sonnleitner, Glasermeister",
    color=(0.05, 0.35, 0.50), header="centered", labels="a", signer="G. Sonnleitner", creator="GlasCalc")
_co(key="rieger", name="Schädlingsbekämpfung Rieger", tagline="Kammerjäger · Schädlingsbekämpfung · Taubenabwehr",
    street="Mühlweg 4", city="12346 Musterstadt", phone="0123 66 60 66",
    email="rieger@kammerjaeger-rieger.example", web="www.kammerjaeger-rieger.example",
    iban="DE00 7000 1000 0001 6660 66", bic="HANDDEXX700", bank="Handelsbank Musterstadt",
    taxid="St.-Nr. 000/999/11122", register="", owner="Inh. Frank Rieger, geprüfter Schädlingsbekämpfer",
    color=(0.35, 0.30, 0.05), header="classic", labels="c", handwerk=True, signer="Frank Rieger",
    creator="Rechnungsblock")
_co(key="thermometrik", name="Thermometrik Messdienst GmbH", tagline="Messdienstleistungen für die Wohnungswirtschaft",
    street="Zählerring 20", city="12340 Musterstadt", phone="0123 900 44 00",
    email="abrechnung@thermometrik.example", web="www.thermometrik.example",
    iban="DE00 2000 3000 0077 1900 00", bic="NORDDEXX200", bank="Nordbank",
    taxid="USt-IdNr. DE 000 771 900", register="Amtsgericht Musterstadt HRB 7719",
    owner="Geschäftsführer: Dr. Felix Brandauer", color=(0.30, 0.10, 0.45), header="bar", labels="b",
    handwerk=False, creator="Messdienst-Abrechnung")

LABELSETS = {
    "a": dict(nr="Rechnungs-Nr.", date="Rechnungsdatum", kd="Kunden-Nr.", net="Nettobetrag",
              vat="zzgl. {r} % MwSt.", gross="Gesamtbetrag", pos="Pos.", desc="Leistung / Material",
              qty="Menge", unit="Einh.", ep="EP €", tot="Gesamt €"),
    "b": dict(nr="Rechnungsnummer", date="Datum", kd="Kundennummer", net="Summe netto",
              vat="Umsatzsteuer {r} %", gross="Rechnungsbetrag", pos="Nr.", desc="Bezeichnung",
              qty="Anzahl", unit="ME", ep="Einzelpreis", tot="Betrag"),
    "c": dict(nr="Beleg-Nr.", date="Belegdatum", kd="Kd.-Nr.", net="Zwischensumme",
              vat="+ {r} % MwSt", gross="Endbetrag", pos="Pos", desc="Beschreibung",
              qty="Menge", unit="Einheit", ep="Preis/E", tot="Summe"),
    "inv": dict(nr="Rechnungs-Nr.", date="Rechnungsdatum", kd="Debitor", net="Nettowert",
                vat="MwSt. {r} %", gross="Gesamtbetrag", pos="Pos", desc="Material / Leistung",
                qty="Menge", unit="ME", ep="Preis", tot="Wert"),
    "stadt": dict(nr="Rechnungsnummer", date="Rechnungsdatum", kd="Vertragskonto", net="Summe netto",
                  vat="Umsatzsteuer {r} %", gross="Rechnungsbetrag brutto", pos="Pos.", desc="Leistung",
                  qty="Menge", unit="Einheit", ep="Preis €", tot="Betrag €"),
    "vers": dict(nr="Beitragsrechnung Nr.", date="Datum", kd="Versicherungsschein-Nr.", net="Beitrag netto",
                 vat="Versicherungsteuer {r} %", gross="Zu zahlender Beitrag", pos="Nr.", desc="Versicherungsleistung",
                 qty="", unit="Zeitraum", ep="", tot="Beitrag €"),
    "ra": dict(nr="Kostennote Nr.", date="Datum", kd="Unser Zeichen", net="Zwischensumme netto",
               vat="{r} % Umsatzsteuer Nr. 7008 VV RVG", gross="Gesamtbetrag", pos="Nr.", desc="Gebührentatbestand",
               qty="", unit="", ep="", tot="Betrag €"),
}

OBJ = {
    "muster": ("Musterstraße 12", "12345 Musterstadt"),
    "linden": ("Lindenweg 3", "12345 Musterstadt"),
    "hang": ("Am Hang 7", "12349 Beispielhausen"),
    "birken": ("Birkenallee 21", "12345 Musterstadt"),
    "kastanien": ("Kastanienweg 5a", "12349 Beispielhausen"),
    "muehlen": ("Mühlenstraße 40", "12347 Musterstadt"),
}

HV = ["Hausverwaltung Sommer & Partner GmbH", "Rosenstraße 4", "12345 Musterstadt"]
HV_Z = ["Hausverwaltung Sommer & Partner GmbH", "z. Hd. Frau Beispiel", "Rosenstraße 4", "12345 Musterstadt"]
PRIV = ["Herrn", "Thomas Beispiel", "Am Hang 7", "12349 Beispielhausen"]


def WEG(obj_key: str) -> list:
    return [f"WEG {OBJ[obj_key][0]}", "c/o Hausverwaltung Sommer & Partner GmbH", "Rosenstraße 4",
            "12345 Musterstadt"]


# --------------------------------------------------------------------------------------
# Belege
# --------------------------------------------------------------------------------------

@dataclass
class Invoice:
    id: str
    co: str
    date: date
    nr: str
    obj: str | None
    leistung: str
    items: list
    title: str = "Rechnung"
    obj_text: str | None = None
    obj_label: str = "Objekt"
    recipient: list | None = None
    date_style: str = "num"
    amount_style: str = "suffix"
    vat: int = 19
    kleinunternehmer: bool = False
    period: str | None = None
    betreff: str | None = None
    intro: str | None = None
    notes: list = field(default_factory=list)
    meta: list = field(default_factory=list)
    abschlaege: tuple | None = None
    firma_override: str | None = None
    ascii_only: bool = False
    header: str | None = None
    font: str | None = None
    payment: str = "ueberweisung"
    due_days: int = 14
    kdnr: str | None = None

    # berechnet
    rows: list = field(default_factory=list, init=False)
    per_rate: dict = field(default_factory=dict, init=False)
    taxes: dict = field(default_factory=dict, init=False)
    net: Decimal = field(default=D(0), init=False)
    gross: Decimal = field(default=D(0), init=False)

    def __post_init__(self):
        per_rate = {}
        for it in self.items:
            text, qty, unit, price = it[:4]
            rate = it[4] if len(it) > 4 else self.vat
            qty = D(str(qty))
            price = D(str(price))
            tot = q2(qty * price)
            self.rows.append((text, qty, unit, price, tot, rate))
            per_rate[rate] = per_rate.get(rate, D(0)) + tot
        self.per_rate = per_rate
        self.net = sum(per_rate.values(), D(0))
        if self.kleinunternehmer:
            self.taxes = {}
        else:
            self.taxes = {r: q2(v * r / 100) for r, v in per_rate.items() if r > 0}
        self.gross = self.net + sum(self.taxes.values(), D(0))

    @property
    def company(self) -> Company:
        return C[self.co]

    @property
    def firma(self) -> str:
        return self.firma_override or self.company.name

    @property
    def obj_printed(self) -> str | None:
        if self.obj is None:
            return None
        return self.obj_text or ", ".join(OBJ[self.obj])

    @property
    def obj_canonical(self) -> str | None:
        return None if self.obj is None else ", ".join(OBJ[self.obj])

    def facts(self) -> dict:
        s = (lambda x: asciify(x) if self.ascii_only and x else x)
        return {
            "doc_id": self.id,
            "firma": s(self.firma),
            "rechnungsnummer": self.nr,
            "datum": self.date.isoformat(),
            "datum_text": fmt_date(self.date, self.date_style),
            "gesamtbetrag": f"{self.gross:.2f}",
            "gesamtbetrag_text": s(fmt_eur(self.gross, self.amount_style)),
            "leistung": self.leistung,
            "objektadresse": s(self.obj_printed),
            "objektadresse_normiert": self.obj_canonical,
        }


@dataclass
class Receipt:
    id: str
    co: str
    dt: datetime
    bon: str
    items: list          # (text, qty, price_brutto[, detail])
    leistung: str
    payment: str = "EC-Karte"
    kasse: str = "03"

    @property
    def company(self) -> Company:
        return C[self.co]

    @property
    def gross(self) -> Decimal:
        return sum((q2(D(str(q)) * D(str(p))) for _, q, p, *_ in self.items), D(0))

    def facts(self) -> dict:
        return {
            "doc_id": self.id,
            "firma": self.company.name,
            "rechnungsnummer": f"Bon-Nr. {self.bon}",
            "datum": self.dt.date().isoformat(),
            "datum_text": self.dt.strftime("%d.%m.%Y"),
            "gesamtbetrag": f"{self.gross:.2f}",
            "gesamtbetrag_text": de_number(self.gross),
            "leistung": self.leistung,
            "objektadresse": None,
            "objektadresse_normiert": None,
        }


INVOICES: list[Invoice] = [
    # ---------------------------------------------------------------- 2018
    Invoice("lindqvist_2018", "lindqvist", date(2018, 6, 19), "M-2018-031", "linden",
            "Malerarbeiten Treppenhaus (Wände, Decken, Handlauf)",
            [("Abdeck- und Abklebearbeiten Treppenhaus, Böden und Fenster geschützt", 1, "psch", "185.00"),
             ("Wandflächen Treppenhaus EG bis 2. OG gespachtelt, geschliffen und zweimal gestrichen "
              "(Dispersion, weiß)", "142.5", "m²", "9.80"),
             ("Deckenflächen Treppenhaus gestrichen", 38, "m²", "11.50"),
             ("Handlauf und Geländer angeschliffen und lackiert", 24, "lfm", "14.00"),
             ("Materialpauschale (Dispersionsfarbe, Tiefgrund, Lack)", 1, "psch", "236.40")],
            date_style="long", period="04.06.2018 – 15.06.2018",
            betreff="Treppenhaus streichen – Ihr Auftrag vom 02.05.2018 (Angebot A-2018-012)",
            recipient=HV_Z),
    Invoice("stadtwerke_2018", "stadtwerke", date(2018, 2, 14), "3001185539", "hang",
            "Jahresabrechnung Wasser/Abwasser 2017",
            [("Trinkwasser Verbrauch 01.01.–31.12.2017, Zähler 00 000 408", 164, "m³", "1.92", 7),
             ("Grundpreis Wasser, Zähler Qn 2,5", 12, "Monate", "6.40", 7),
             ("Schmutzwasser (Abwasser)", 164, "m³", "2.87", 0),
             ("Niederschlagswasser, versiegelte Fläche", 210, "m²", "0.58", 0)],
            title="Jahresabrechnung Wasser / Abwasser 2017", obj_label="Verbrauchsstelle",
            recipient=PRIV, abschlaege=(11, D("95.00")), payment="lastschrift",
            meta=[("Zähler-Nr.", "71 223 408")], kdnr="4000 2187 33", amount_style="suffix"),
    Invoice("albrecht_2018", "albrecht", date(2018, 11, 5), "SF-2018-0347", "kastanien",
            "Feuerstättenschau, Überprüfung Abgasanlage, Kehrung",
            [("Feuerstättenschau gem. § 14 SchfHwG", 1, "Stk", "38.50"),
             ("Überprüfung Abgasanlage Gas-Brennwertgerät 24 kW", 1, "Stk", "29.60"),
             ("Kehrung Schornstein (Kaminofen Whg. 1. OG)", 1, "Stk", "24.80"),
             ("Wegegeld", 1, "psch", "9.90")],
            date_style="short", amount_style="prefix", recipient=HV),
    # ---------------------------------------------------------------- 2019
    Invoice("mueller_2019", "mueller", date(2019, 3, 12), "2019-00312", "linden",
            "Wartung der Gastherme",
            [("Wartung der Gastherme (Gas-Kombitherme, Kellerraum) nach Herstellervorgabe", 1, "Stk", "129.00"),
             ("Brenner und Wärmetauscher gereinigt, Zündung und Flammenüberwachung geprüft", 1.5, "Std", "62.00"),
             ("Dichtungssatz Brennerkammer", 1, "Stk", "18.40"),
             ("Ausdehnungsgefäß Vordruck geprüft und nachgefüllt", 1, "psch", "15.00"),
             ("Anfahrtspauschale", 1, "psch", "35.00")],
            obj_label="Leistungsort", recipient=HV),
    Invoice("vogt_2019", "vogt", date(2019, 8, 2), "3318/19", "birken",
            "Treppenhausbeleuchtung auf LED umgerüstet, Bewegungsmelder",
            [("LED-Wand-/Deckenleuchte IP44 mit HF-Bewegungsmelder", 9, "Stk", "89.50"),
             ("Montage Leuchten inkl. Demontage und Entsorgung Altleuchten", 9, "Stk", "38.00"),
             ("Treppenlichtzeitschalter ausgebaut, Unterverteilung angepasst", 1, "psch", "145.00"),
             ("Elektromeister / Monteur", "11.5", "Std", "58.00"),
             ("Kleinmaterial (Klemmen, Leitungen, Dosen)", 1, "psch", "64.80")],
            title="Rechnung Nr. {nr}", date_style="long", recipient=WEG("birken")),
    Invoice("stadtwerke_2019", "stadtwerke", date(2019, 1, 23), "3001192276", "muster",
            "Jahresabrechnung Allgemeinstrom 2018",
            [("Arbeitspreis Allgemeinstrom 01.01.–31.12.2018", 4418, "kWh", "0.2790"),
             ("Grundpreis", 12, "Monate", "9.90")],
            title="Jahresabrechnung Strom 2018 – Allgemeinstrom", obj_label="Verbrauchsstelle",
            recipient=HV, abschlaege=(12, D("125.00")), payment="lastschrift", amount_style="prefix",
            meta=[("Zähler-Nr.", "1ESY 0000 0000 01")], kdnr="4000 3312 07"),
    Invoice("nordlicht_2019", "nordlicht", date(2019, 1, 2), "BR-2019-771903", "muster",
            "Beitragsrechnung Wohngebäudeversicherung 2019",
            [("Wohngebäudeversicherung (Feuer, Leitungswasser, Sturm/Hagel)", 1, "01.01.–31.12.2019", "1184.20"),
             ("Zusatzbaustein Elementarschäden", 1, "01.01.–31.12.2019", "146.80")],
            title="Beitragsrechnung 2019", obj_label="Versichertes Gebäude", recipient=HV,
            kdnr="WG 4471-0932", date_style="long", amount_style="prefix", payment="lastschrift"),
    Invoice("petersen_2019", "petersen", date(2019, 4, 2), "HM-9021", "birken",
            "Hausmeisterleistungen 1. Quartal 2019",
            [("Hausmeisterpauschale Januar – März 2019", 3, "Monat", "185.00"),
             ("Leuchtmittel Kellergang ersetzt inkl. Material", 1, "psch", "34.50"),
             ("Mülltonnen bereitgestellt und zurückgestellt", 13, "x", "6.00")],
            period="01.01.2019 – 31.03.2019", recipient=WEG("birken")),
    Invoice("gruenwerk_2019", "gruenwerk", date(2019, 10, 28), "GW 2019/118", "hang",
            "Herbstschnitt Hecken, Laubbeseitigung",
            [("Heckenschnitt Hainbuche/Liguster inkl. Schnittgut", 64, "lfm", "4.20"),
             ("Laubbeseitigung Rasen- und Beetflächen", 6, "Std", "42.00"),
             ("Grüngutentsorgung Container 3 m³", 1, "psch", "95.00"),
             ("Rasen letztmalig gemäht", 1, "psch", "48.00")],
            recipient=PRIV),
    # ---------------------------------------------------------------- 2020
    Invoice("stadtwerke_2020_bk", "stadtwerke", date(2020, 4, 20), "BK-2019-00817", "birken",
            "Betriebskostenabrechnung 2019 (Wasser, Abwasser, Müll, Straßenreinigung)",
            [("Trinkwasser", 486, "m³", "1.95", 7),
             ("Grundgebühr Wasser", 12, "Monate", "14.20", 7),
             ("Schmutzwasser", 486, "m³", "2.91", 0),
             ("Niederschlagswasser", 540, "m²", "0.61", 0),
             ("Abfallentsorgung Restmüll 4 × 1.100 l, 52 Leerungen", 208, "Leerung", "11.40", 0),
             ("Straßenreinigung, 28 m Frontlänge", 28, "m", "3.85", 0)],
            title="Betriebskostenabrechnung 2019", obj_label="Abrechnungsobjekt", recipient=WEG("birken"),
            abschlaege=(12, D("640.00")), period="01.01.2019 – 31.12.2019", payment="ueberweisung",
            kdnr="4000 5120 90"),
    Invoice("krueger_2020", "krueger", date(2020, 12, 1), "R2020 0388", "hang",
            "Hauswartdienste November 2020",
            [("Hauswartpauschale November 2020 (Kontrollgänge, Treppenhaus, Außenanlagen)", 1, "Monat", "160.00"),
             ("Laub gefegt Einfahrt und Gehweg", 3, "Std", "28.00"),
             ("Heizöltank Füllstand kontrolliert, Meldung an Eigentümer", 1, "psch", "15.00")],
            kleinunternehmer=True, recipient=PRIV, period="November 2020"),
    Invoice("sonnleitner_2020", "sonnleitner", date(2020, 9, 17), "20200917-3", "muehlen",
            "Glasbruch Haustür, Verbundsicherheitsglas ersetzt",
            [("Notverglasung Haustür (Glasbruch) mit Holzplatte gesichert", 1, "psch", "89.00"),
             ("Verbundsicherheitsglas VSG 8 mm, Ornament, 48 × 124 cm", "0.6", "m²", "186.00"),
             ("Glaserarbeiten Aus- und Einbau, neue Glasleisten", "2.5", "Std", "54.00"),
             ("Entsorgung Altglas", 1, "psch", "12.00")],
            obj_text="Mühlenstr. 40, 12347 Musterstadt", recipient=HV),
    Invoice("glanzwerk_2020", "glanzwerk", date(2020, 10, 5), "GR-20-0781", "muster",
            "Treppenhausreinigung 3. Quartal 2020",
            [("Treppenhausreinigung wöchentlich Juli – September 2020", 13, "Einsätze", "38.50"),
             ("Reinigung Kellergänge und Waschküche", 3, "Einsätze", "29.00"),
             ("Fensterreinigung Treppenhaus innen/außen", 1, "psch", "96.00")],
            period="3. Quartal 2020", recipient=HV),
    Invoice("kranich_2020", "kranich", date(2020, 7, 15), "INV-88213", "birken",
            "Wartung Personenaufzug 1. Halbjahr 2020",
            [("Wartung Personenaufzug Fabr.-Nr. K-3307, 1. Halbjahr 2020 lt. Wartungsvertrag", 1, "psch", "612.00"),
             ("Notrufsystem 24h, Aufschaltung 01–06/2020", 6, "Monat", "24.90")],
            amount_style="sym", recipient=WEG("birken")),
    Invoice("mueller_2020", "mueller", date(2020, 2, 11), "2020-00107", "muehlen",
            "Austausch Warmwasserspeicher 300 l",
            [("Warmwasserspeicher 300 l, emailliert, inkl. Magnesiumanode", 1, "Stk", "1180.00"),
             ("Demontage und Entsorgung Altspeicher", 1, "psch", "145.00"),
             ("Sicherheitsgruppe und Zirkulationspumpe angeschlossen", 1, "psch", "210.00"),
             ("Monteur und Helfer", 9, "Std", "104.00")],
            obj_label="Leistungsort", recipient=HV, amount_style="plain"),
    # ---------------------------------------------------------------- 2021
    Invoice("brandl_2021", "brandl", date(2021, 3, 12), "RE-2021/00487", "muster",
            "Heizungswartung",
            [("Heizungswartung lt. Wartungsvertrag: Gas-Brennwertgerät 35 kW, Brenner und Wärmetauscher "
              "gereinigt", 1, "psch", "189.00"),
             ("Abgaswerte gemessen, Protokoll erstellt", 1, "psch", "38.00"),
             ("Kundendiensttechniker", 2, "Std", "68.50"),
             ("Kondensatablauf gereinigt, Neutralisationsgranulat erneuert", 1, "Stk", "42.60"),
             ("Anfahrt Stadtgebiet", 1, "psch", "39.00")],
            obj_text="Musterstr. 12, 12345 Musterstadt", date_style="long", recipient=HV_Z),
    Invoice("hoffmannbeck_2021", "hoffmannbeck", date(2021, 2, 22), "RE 21-1093", "birken",
            "Sturmschaden Dach: Dachziegel ersetzt, First neu vermörtelt",
            [("Sturmschaden vom 17.02.2021: Dachfläche Nordseite begangen, Schaden aufgenommen", 1, "psch", "85.00"),
             ("Dachziegel Doppelmuldenfalz rot ersetzt", 46, "Stk", "4.90"),
             ("Firstziegel neu vermörtelt", 7, "lfm", "38.00"),
             ("Dachdecker-Geselle", 6, "Std", "56.00"),
             ("Hubsteiger inkl. An- und Abfahrt", 1, "Tag", "310.00")],
            recipient=WEG("birken")),
    Invoice("petersen_2021", "petersen", date(2021, 5, 31), "HM-10233", "muster",
            "Hausmeisterleistungen Mai 2021",
            [("Hausmeisterpauschale Mai 2021", 1, "Monat", "210.00"),
             ("Rasen gemäht und Kanten geschnitten", 2, "Std", "32.00"),
             ("Tropfenden Wasserhahn Waschküche repariert inkl. Dichtung", 1, "psch", "27.50"),
             ("Sperrgut aus dem Keller zum Wertstoffhof gefahren", 1, "psch", "45.00")],
            recipient=HV, period="Mai 2021"),
    Invoice("gruenwerk_2021", "gruenwerk", date(2021, 11, 30), "GW 2021/203", "birken",
            "Gartenpflege Saison 2021 (Pflegevertrag)",
            [("Pflegevertrag Grünanlagen Saison April – Oktober 2021", 7, "Monat", "135.00"),
             ("Zusatzleistung: Beet am Eingang neu bepflanzt (Bodendecker)", 1, "psch", "186.00"),
             ("Baumschnitt Ahorn, Lichtraumprofil Zufahrt", 3, "Std", "49.00")],
            recipient=WEG("birken"), period="Saison 2021"),
    Invoice("orion_2021", "orion", date(2021, 9, 8), "OR-2021-1187", "muehlen",
            "Rohrreinigung Fallstrang Küche, Kamerabefahrung",
            [("Rohrreinigung Fallstrang Küche (DN 70) mit Spirale, 2. OG bis Keller", 1, "psch", "149.00"),
             ("Kamerabefahrung mit Dokumentation auf USB-Stick", 1, "psch", "119.00"),
             ("Hochdruckspülung Grundleitung", "1.5", "Std", "98.00"),
             ("Anfahrtspauschale", 1, "psch", "49.00")],
            obj_text="Mühlenstr. 40, 12347 Musterstadt", obj_label="Einsatzort", recipient=HV),
    Invoice("weissenfels_2021", "weissenfels", date(2021, 6, 29), "2021/0419", "linden",
            "Beratung und Mieterhöhungsverlangen (Kostennote RVG)",
            [("1,3 Geschäftsgebühr Nr. 2300 VV RVG, Gegenstandswert 1.440,00 €", 1, "", "193.70"),
             ("Post- und Telekommunikationspauschale Nr. 7002 VV RVG", 1, "", "20.00")],
            title="Kostennote", obj_label="Betreff",
            obj_text="Mieterhöhungsverlangen Wohnung 2. OG links, Lindenweg 3, 12345 Musterstadt",
            recipient=HV, kdnr="0419/21 HW", date_style="num"),
    Invoice("yilmaz_2021", "yilmaz", date(2021, 12, 24), "Q-211224-01", "muehlen",
            "Türöffnung Wohnungstür (Notdienst), Schließzylinder ersetzt",
            [("Türöffnung Wohnungstür 1. OG rechts (zugefallen), zerstörungsfrei", 1, "psch", "89.00"),
             ("Notdienstzuschlag Feiertag (24.12.)", 1, "psch", "60.00"),
             ("Profilzylinder 30/35 mit 3 Schlüsseln", 1, "Stk", "42.00"),
             ("Anfahrt", 1, "psch", "35.00")],
            title="Rechnung / Quittung", obj_text="Mühlenstraße 40, 1. OG rechts", obj_label="Einsatzort",
            recipient=HV, payment="bar"),
    # ---------------------------------------------------------------- Sammel-PDF (2021)
    Invoice("nordlicht_2021", "nordlicht", date(2021, 1, 4), "BR-2021-789554", "birken",
            "Beitragsrechnung Wohngebäudeversicherung 2021",
            [("Wohngebäudeversicherung (Feuer, Leitungswasser, Sturm/Hagel)", 1, "01.01.–31.12.2021", "1622.40"),
             ("Zusatzbaustein Elementarschäden", 1, "01.01.–31.12.2021", "198.00")],
            title="Beitragsrechnung 2021", obj_label="Versichertes Gebäude", recipient=WEG("birken"),
            kdnr="WG 4471-1175", date_style="long", amount_style="prefix", payment="lastschrift"),
    Invoice("vogt_2021", "vogt", date(2021, 3, 3), "1502/21", "linden",
            "Störung Türsprechanlage behoben, Wohnungssprechstelle ersetzt",
            [("Störungssuche Türsprechanlage (kein Ton in Whg. 2. OG)", "1.5", "Std", "58.00"),
             ("Wohnungssprechstelle Audio ersetzt", 1, "Stk", "96.00"),
             ("Anfahrt", 1, "psch", "28.00")],
            title="Rechnung Nr. {nr}", recipient=HV),
    Invoice("kranich_2021", "kranich", date(2021, 3, 10), "INV-89977", "birken",
            "Begleitung TÜV-Hauptprüfung Aufzug, Notlicht Kabine erneuert",
            [("Begleitung Hauptprüfung durch ZÜS (TÜV) am 09.03.2021", 1, "psch", "165.00"),
             ("Mängelbeseitigung: Notlicht Kabine erneuert", 1, "Stk", "78.00"),
             ("Techniker", 2, "Std", "92.00")],
            amount_style="sym", recipient=WEG("birken")),
    Invoice("mueller_2021", "mueller", date(2021, 3, 15), "2021-00219", "hang",
            "Thermostatventile getauscht, Heizkörper entlüftet",
            [("Thermostatventile getauscht (inkl. Thermostatköpfe)", 4, "Stk", "46.50"),
             ("Heizkörper entlüftet, Anlagendruck geprüft", 1, "psch", "45.00"),
             ("Monteur", "2.5", "Std", "62.00")],
            obj_label="Leistungsort", recipient=PRIV),
    Invoice("glanzwerk_2021", "glanzwerk", date(2021, 4, 2), "GR-21-0402", "muehlen",
            "Treppenhausreinigung 1. Quartal 2021",
            [("Treppenhausreinigung wöchentlich Januar – März 2021", 13, "Einsätze", "36.00"),
             ("Glasreinigung Hauseingang", 3, "Einsätze", "18.00")],
            period="1. Quartal 2021", recipient=HV),
    Invoice("stadtwerke_2021", "stadtwerke", date(2021, 3, 22), "3001213302", "kastanien",
            "Jahresabrechnung Allgemeinstrom 2020",
            [("Arbeitspreis Allgemeinstrom 01.01.–31.12.2020", 1873, "kWh", "0.3010"),
             ("Grundpreis", 12, "Monate", "10.50")],
            title="Jahresabrechnung Strom 2020 – Allgemeinstrom", obj_label="Verbrauchsstelle",
            recipient=HV, abschlaege=(12, D("55.00")), payment="lastschrift",
            meta=[("Zähler-Nr.", "1ESY 0000 0000 02")], kdnr="4000 6621 18"),
    # ---------------------------------------------------------------- 2022
    Invoice("krueger_2022", "krueger", date(2022, 7, 4), "R2022 0415", "linden",
            "Hauswarttätigkeit 2. Quartal 2022",
            [("Hauswarttätigkeit April – Juni 2022 (Pauschale)", 3, "Monat", "175.00"),
             ("Treppenhausfenster: Scharnier repariert", 1, "psch", "22.00"),
             ("Grünschnitt Vorgarten, Abfuhr zum Wertstoffhof", "2.5", "Std", "28.00")],
            kleinunternehmer=True, date_style="long", recipient=HV, period="2. Quartal 2022"),
    Invoice("vogt_2022", "vogt", date(2022, 5, 17), "4711/22", "hang",
            "E-Check und Austausch Klingeltableau",
            [("E-Check nach DGUV V3 / VDE 0105-100, Wohngebäude", 1, "psch", "165.00"),
             ("Klingeltableau 3-fach Edelstahl mit Namensschildern ausgetauscht", 1, "Stk", "138.00"),
             ("Klingeltrafo erneuert", 1, "Stk", "29.90"),
             ("Monteur", 3, "Std", "61.00")],
            title="Rechnung Nr. {nr}", recipient=PRIV),
    Invoice("albrecht_2022", "albrecht", date(2022, 10, 11), "SF-2022-1162", "muster",
            "Abgasmessung (Emissionsmessung), Feuerstättenschau",
            [("Emissionsmessung / Abgasmessung nach 1. BImSchV, Gas-Brennwertgerät 35 kW", 1, "Stk", "46.30"),
             ("Überprüfung Abgasleitung (Luft-Abgas-System)", 1, "Stk", "31.20"),
             ("Feuerstättenschau gem. § 14 SchfHwG", 1, "Stk", "41.00"),
             ("Wegegeld", 1, "psch", "10.40")],
            date_style="short", amount_style="prefix", recipient=HV),
    Invoice("glanzwerk_2022", "glanzwerk", date(2022, 3, 30), "GR-22-0915", "kastanien",
            "Grundreinigung Wohnung nach Auszug, Fensterreinigung",
            [("Grundreinigung Wohnung EG rechts nach Mieterauszug, 68 m²", 68, "m²", "3.90"),
             ("Fenster- und Rahmenreinigung Wohnung", 1, "psch", "85.00"),
             ("Kalkablagerungen Bad/Küche entfernt", 2, "Std", "34.00")],
            recipient=HV),
    Invoice("thermometrik_2022", "thermometrik", date(2022, 6, 10), "7719-2022-0043", "birken",
            "Heizkosten- und Warmwasserabrechnung 2021, Gerätemiete",
            [("Heizkosten- und Warmwasserabrechnung 2021, 12 Nutzeinheiten", 12, "NE", "24.60"),
             ("Miete Heizkostenverteiler (Funk)", 58, "Stk", "2.10"),
             ("Miete Warmwasserzähler (Funk)", 12, "Stk", "11.40"),
             ("Miete Kaltwasserzähler (Funk)", 12, "Stk", "9.60")],
            title="Rechnung zur Heizkostenabrechnung 2021", obj_label="Liegenschaft", recipient=WEG("birken"),
            period="01.01.2021 – 31.12.2021", kdnr="LG 7719-0206"),
    Invoice("nordlicht_2022", "nordlicht", date(2022, 1, 3), "BR-2022-803317", "linden",
            "Beitragsrechnung Wohngebäudeversicherung 2022",
            [("Wohngebäudeversicherung (Feuer, Leitungswasser, Sturm/Hagel)", 1, "01.01.–31.12.2022", "1296.00"),
             ("Glasversicherung Gemeinschaftsflächen", 1, "01.01.–31.12.2022", "88.50")],
            title="Beitragsrechnung 2022", obj_label="Versichertes Gebäude", recipient=HV,
            kdnr="WG 4471-0987", date_style="long", amount_style="prefix", payment="lastschrift"),
    Invoice("rieger_2022", "rieger", date(2022, 8, 23), "SB-5521", "muster",
            "Rattenbekämpfung Hof und Mülltonnenplatz",
            [("Befallsermittlung Ratten, Hof und Mülltonnenstellplatz", 1, "psch", "69.00"),
             ("Köderboxen aufgestellt inkl. Rodentizid", 6, "Stk", "18.50"),
             ("Nachkontrollen", 3, "x", "39.00"),
             ("Dokumentation gem. Biozidverordnung", 1, "psch", "25.00")],
            title="Rechnung Rattenbekämpfung", recipient=HV),
    # ---------------------------------------------------------------- TIFF (Fax 02/2022)
    Invoice("lindqvist_2022", "lindqvist", date(2022, 1, 24), "M-2022-012", "muehlen",
            "Kellerabgang und Kellerflur gestrichen",
            [("Kellerabgang und Kellerflur: Wände gereinigt, grundiert, zweimal gestrichen", 64, "m²", "10.20"),
             ("Kellertüren (4 Stk) lackiert", 4, "Stk", "68.00"),
             ("Material", 1, "psch", "118.60")],
            date_style="long", recipient=HV),
    Invoice("yilmaz_2022", "yilmaz", date(2022, 2, 3), "Q-220203-01", "birken",
            "Schließanlage: Schlüssel nachgefertigt, Kellertürzylinder",
            [("Schlüssel für Schließanlage nachgefertigt (Sicherungskarte vorgelegt)", 6, "Stk", "24.50"),
             ("Profilzylinder Kellertür, gleichschließend", 1, "Stk", "118.00"),
             ("Montage", 1, "psch", "38.00")],
            recipient=WEG("birken")),
    Invoice("petersen_2022", "petersen", date(2022, 2, 1), "HM-10544", "muster",
            "Winterdienst Januar 2022",
            [("Winterdienst Januar 2022, Gehweg und Zugang, Pauschale", 1, "Monat", "145.00"),
             ("Zusatzeinsätze bei Glätte", 5, "Einsatz", "24.00"),
             ("Streugut abstumpfend", 2, "Sack", "11.90")],
            period="01.01.2022 – 31.01.2022", recipient=HV),
    # ---------------------------------------------------------------- 2023
    Invoice("mueller_2023", "mueller", date(2023, 5, 14), "2023-00588", "linden",
            "Rohrbruch Notdienst Keller, Kupferleitung ersetzt",
            [("Notdiensteinsatz Sonntag: Rohrbruch Kaltwasserleitung Keller, Wasser abgesperrt", 1, "psch", "145.00"),
             ("Kupferrohr 22 mm inkl. Pressfittings ersetzt", "2.4", "m", "38.50"),
             ("Monteur Notdienst (Sonntagszuschlag 50 %)", 3, "Std", "94.50"),
             ("Leckageortung mit Thermografie", 1, "psch", "120.00")],
            firma_override="Haustechnik Mueller GmbH", ascii_only=True, amount_style="suffix_eur",
            obj_label="Leistungsort", recipient=HV),
    Invoice("brandl_2023", "brandl", date(2023, 9, 26), "RE-2023/01122", "birken",
            "Brennwertkessel-Inspektion",
            [("Brennwertkessel-Inspektion gem. Herstellervorgabe (Gas-Brennwertkessel 60 kW)", 1, "psch", "245.00"),
             ("Kundendiensttechniker", "2.5", "Std", "72.00"),
             ("Zünd- und Überwachungselektrode ersetzt", 1, "Satz", "52.40"),
             ("Anfahrt", 1, "psch", "42.00")],
            recipient=WEG("birken")),
    Invoice("kranich_2023", "kranich", date(2023, 2, 8), "INV-91540", "birken",
            "Reparatur Türantrieb Aufzug",
            [("Störungsbeseitigung: Kabinentürantrieb defekt, Aufzug außer Betrieb", 1, "psch", "180.00"),
             ("Türantrieb Kabinentür (Austauschteil)", 1, "Stk", "1240.00"),
             ("Techniker", 4, "Std", "96.00")],
            amount_style="sym", recipient=WEG("birken")),
    Invoice("glanzwerk_2023", "glanzwerk", date(2023, 3, 31), "GR-23-0311", "linden",
            "Treppenhausreinigung März 2023",
            [("Treppenhausreinigung wöchentlich März 2023", 5, "Einsätze", "41.00"),
             ("Reinigung Eingangsbereich und Briefkastenanlage", 5, "Einsätze", "8.50")],
            period="März 2023", recipient=HV),
    Invoice("sonnleitner_2023", "sonnleitner", date(2023, 11, 14), "20231114-1", "birken",
            "Isolierglasscheibe Balkontür ersetzt",
            [("Isolierglasscheibe 2-fach, Ug 1,1, 78 × 186 cm, für Balkontür Whg. 1. OG", "1.45", "m²", "142.00"),
             ("Aufmaß vor Ort", 1, "psch", "35.00"),
             ("Glaserarbeiten Aus- und Einbau", 3, "Std", "56.00")],
            recipient=WEG("birken")),
    Invoice("gruenwerk_2023", "gruenwerk", date(2023, 4, 3), "GW 2023/052", "muster",
            "Rückschnitt Kirschlorbeer, Rasen vertikutiert",
            [("Rückschnitt Kirschlorbeerhecke", 30, "lfm", "5.60"),
             ("Rasen vertikutiert und nachgesät", 220, "m²", "0.85"),
             ("Grüngutentsorgung", 1, "psch", "65.00")],
            recipient=HV),
    Invoice("stadtwerke_2023", "stadtwerke", date(2023, 2, 1), "3001230145", "linden",
            "Jahresabrechnung Allgemeinstrom 2022",
            [("Arbeitspreis Allgemeinstrom 01.01.–31.12.2022", 2960, "kWh", "0.3890"),
             ("Grundpreis", 12, "Monate", "12.50")],
            title="Jahresabrechnung Strom 2022 – Allgemeinstrom", obj_label="Verbrauchsstelle",
            recipient=HV, abschlaege=(12, D("105.00")), payment="lastschrift",
            meta=[("Zähler-Nr.", "1ESY 0000 0000 03")], kdnr="4000 7702 55"),
    Invoice("petersen_2023", "petersen", date(2023, 3, 15), "HM-10871", "muehlen",
            "Winterdienst Saison 2022/23",
            [("Winterdienst Saison 2022/23 (Nov. – März), Gehweg 34 m, Pauschale", 1, "Saison", "540.00"),
             ("Streugut (Granulat), Nachlieferung", 4, "Sack", "11.90"),
             ("Einsätze außerhalb der Pauschale (Sonn-/Feiertag)", 3, "Einsatz", "28.00")],
            recipient=HV),
    # ---------------------------------------------------------------- 2024
    Invoice("weissenfels_2024", "weissenfels", date(2024, 2, 6), "2024/0133", "kastanien",
            "Räumungsklage wegen Mietrückstand (Kostennote RVG)",
            [("1,3 Verfahrensgebühr Nr. 3100 VV RVG, Gegenstandswert 9.600,00 €", 1, "", "865.80"),
             ("1,2 Terminsgebühr Nr. 3104 VV RVG", 1, "", "799.20"),
             ("Post- und Telekommunikationspauschale Nr. 7002 VV RVG", 1, "", "20.00")],
            title="Kostennote", obj_label="Betreff",
            obj_text="Räumungsklage Wohnung EG links, Kastanienweg 5a, 12349 Beispielhausen",
            recipient=HV, kdnr="0133/24 HW"),
    Invoice("brandl_2024", "brandl", date(2024, 1, 9), "RE-2024/00031", "kastanien",
            "Notdienst Heizungsausfall, Umwälzpumpe getauscht",
            [("Notdiensteinsatz Heizungsausfall (Wochenende)", 1, "psch", "95.00"),
             ("Hocheffizienz-Umwälzpumpe 25-60 geliefert und montiert", 1, "Stk", "389.00"),
             ("Kundendiensttechniker", 3, "Std", "72.00"),
             ("Anlage entlüftet und Druck aufgefüllt", 1, "psch", "35.00")],
            recipient=HV),
    Invoice("kaminski_2024", "kaminski", date(2024, 4, 16), "FK/24/0062", "kastanien",
            "Austausch Kellerfenster",
            [("Kunststoff-Kellerfenster 80 × 50 cm, Dreh-Kipp, 2-fach verglast, weiß", 3, "Stk", "214.00"),
             ("Demontage und Entsorgung Altfenster", 3, "Stk", "35.00"),
             ("Montage inkl. Anschlussfuge und Beiputzarbeiten", 3, "Stk", "78.00")],
            obj_label="Bauvorhaben", recipient=HV),
    Invoice("vogt_2024", "vogt", date(2024, 8, 21), "0932/24", "muehlen",
            "Wallbox-Installation Tiefgarage",
            [("Wallbox 11 kW inkl. Anmeldung beim Netzbetreiber", 1, "Stk", "749.00"),
             ("Zuleitung NYY-J 5×6 mm² ab Unterverteilung Tiefgarage", 28, "m", "18.40"),
             ("FI/LS-Schutzschalter Typ A EV, Unterverteilung erweitert", 1, "psch", "286.00"),
             ("Elektromeister / Monteur", 7, "Std", "64.00")],
            title="Rechnung Nr. {nr}", obj_text="Mühlenstraße 40 (Tiefgarage, Stellplatz 7)", recipient=HV,
            amount_style="plain"),
    Invoice("lindqvist_2024", "lindqvist", date(2024, 7, 1), "M-2024-077", "hang",
            "Beseitigung Wasserschaden Decke Küche",
            [("Wasserschaden Küche (Whg. 2. OG): Decke ausgebessert, gespachtelt, grundiert", 1, "psch", "280.00"),
             ("Decke und Wände Küche zweimal gestrichen, Sperrgrund gegen Wasserflecken", 26, "m²", "12.40"),
             ("Abdeckarbeiten, Möbel gerückt", 1, "psch", "60.00")],
            date_style="long", recipient=PRIV),
    Invoice("glanzwerk_2024", "glanzwerk", date(2024, 2, 5), "GR-24-0102", "birken",
            "Unterhaltsreinigung Januar 2024",
            [("Unterhaltsreinigung Treppenhaus und Aufzugskabine Januar 2024", 4, "Einsätze", "52.00"),
             ("Glasreinigung Eingangstür und Briefkastenanlage", 4, "Einsätze", "9.50"),
             ("Sonderreinigung Treppenhaus nach Umzug Whg. 3. OG", "1.5", "Std", "36.00")],
            period="Januar 2024", recipient=WEG("birken")),
    Invoice("hoffmannbeck_2024", "hoffmannbeck", date(2024, 11, 5), "RE 24-2210", "muster",
            "Dachrinnenreinigung, Kontrolle Dachfläche",
            [("Dachrinnen und Fallrohre gereinigt, Laubfanggitter eingesetzt", 42, "lfm", "6.80"),
             ("Sichtkontrolle Dachfläche, 3 Ziegel nachgelegt", 1, "psch", "95.00"),
             ("Hubsteiger", "0.5", "Tag", "310.00")],
            obj_text="Musterstr. 12, 12345 Musterstadt", recipient=HV),
    Invoice("kranich_2024", "kranich", date(2024, 1, 15), "INV-93307", "birken",
            "Wartung Personenaufzug 1. Halbjahr 2024",
            [("Wartung Personenaufzug Fabr.-Nr. K-3307, 1. Halbjahr 2024 lt. Wartungsvertrag", 1, "psch", "688.00"),
             ("Notrufsystem 24h, Aufschaltung 01–06/2024", 6, "Monat", "27.90")],
            amount_style="sym", recipient=WEG("birken")),
    Invoice("thermometrik_2024", "thermometrik", date(2024, 3, 5), "7719-2024-0112", "linden",
            "Rauchwarnmelder Wartung 2024",
            [("Jährliche Inspektion und Wartung Rauchwarnmelder gem. DIN 14676", 21, "Stk", "4.90"),
             ("Austausch Rauchwarnmelder (Batterie erschöpft)", 2, "Stk", "29.00"),
             ("Mietgebühr Rauchwarnmelder 2024", 21, "Stk", "3.60")],
            obj_label="Liegenschaft", recipient=HV, kdnr="LG 7719-0311"),
    # ---------------------------------------------------------------- 2025
    Invoice("stadtwerke_2025_bk", "stadtwerke", date(2025, 3, 18), "BK-2024-01394", "muehlen",
            "Betriebskostenabrechnung 2024 (Wasser, Abwasser, Abfall)",
            [("Trinkwasser", 612, "m³", "2.18", 7),
             ("Grundgebühr Wasser", 12, "Monate", "15.80", 7),
             ("Schmutzwasser", 612, "m³", "3.12", 0),
             ("Niederschlagswasser", 480, "m²", "0.66", 0),
             ("Abfallentsorgung Restmüll 3 × 1.100 l, 52 Leerungen", 156, "Leerung", "12.90", 0)],
            title="Betriebskostenabrechnung 2024", obj_label="Abrechnungsobjekt", recipient=HV,
            abschlaege=(12, D("520.00")), period="01.01.2024 – 31.12.2024", kdnr="4000 8830 41"),
    Invoice("krueger_2025", "krueger", date(2025, 1, 31), "R2025 0027", "kastanien",
            "Hauswart Januar 2025, Sperrmüll entsorgt",
            [("Sperrmüll aus Kellergang entsorgt (Transporter, Wertstoffhof)", 1, "psch", "120.00"),
             ("Hauswartpauschale Januar 2025", 1, "Monat", "180.00"),
             ("Schneeräumen Gehweg", 3, "Einsatz", "25.00")],
            kleinunternehmer=True, recipient=HV),
    Invoice("nordlicht_2025", "nordlicht", date(2025, 1, 2), "BR-2025-846120", "kastanien",
            "Beitragsrechnung Wohngebäudeversicherung 2025",
            [("Wohngebäudeversicherung (Feuer, Leitungswasser, Sturm/Hagel)", 1, "01.01.–31.12.2025", "1418.60"),
             ("Zusatzbaustein Elementarschäden", 1, "01.01.–31.12.2025", "171.40")],
            title="Beitragsrechnung 2025", obj_label="Versichertes Gebäude", recipient=HV,
            kdnr="WG 4471-2210", date_style="long", amount_style="prefix", payment="lastschrift"),
]

RECEIPTS: list[Receipt] = [
    Receipt("bauwelt_2023", "bauwelt", datetime(2023, 1, 19, 14, 37), "4417",
            [("Silikon Sanitär transp. 310ml", 2, "6.99"),
             ("Kartuschenpistole Profi", 1, "12.49"),
             ("Streusalz 25 kg", 3, "8.99"),
             ("Schneeschieber Alu 50cm", 1, "24.99"),
             ("Spanplattenschr. 4x40 200St", 1, "9.49")],
            "Kassenbon Baumarkt: Silikon, Kartuschenpistole, Streusalz, Schneeschieber, Schrauben"),
    Receipt("bauwelt_2021", "bauwelt", datetime(2021, 4, 10, 11, 3), "2093",
            [("Wandfarbe weiss matt 10 l", 2, "39.99"),
             ("Abdeckfolie 4x5m", 3, "2.49"),
             ("Malerkrepp 50m", 3, "3.29"),
             ("Farbrollen-Set 25cm", 1, "11.99"),
             ("Abstreifgitter", 1, "2.79")],
            "Kassenbon Baumarkt: Wandfarbe, Abdeckfolie, Malerkrepp, Farbrollen-Set", kasse="01",
            payment="BAR"),
    Receipt("brueckner_2024", "brueckner", datetime(2024, 6, 8, 10, 12), "0082741",
            [("Super E10  Säule 3", "10.02", "1.869", "10,02 l x 1,869 EUR/l"),
             ("Kanister 10 l Kunststoff", 1, "14.99"),
             ("2-Takt-Öl 1 l", 1, "9.99")],
            "Tankstellenbeleg: Super E10 für Rasenmäher, Kanister, 2-Takt-Öl", kasse="1"),
]

DOCS = {d.id: d for d in INVOICES + RECEIPTS}


# --------------------------------------------------------------------------------------
# Zeichnen (reportlab)
# --------------------------------------------------------------------------------------

class Pen:
    """Wrapper um den reportlab-Canvas, der jeden Textschnipsel samt Position mitschreibt."""

    def __init__(self, c, fam: str = "arial", ascii_only: bool = False):
        self.c = c
        self.fam = fam
        self.ascii_only = ascii_only
        self.lines: list[tuple] = []

    def fontname(self, bold=False, font=None):
        return font or FAMILIES[self.fam][1 if bold else 0]

    def t(self, s):
        return asciify(s) if self.ascii_only else s

    def text(self, x, y, s, size=9.0, bold=False, align="left", color=None, font=None):
        s = self.t(s)
        f = self.fontname(bold, font)
        self.c.setFont(f, size)
        if color is not None:
            self.c.setFillColorRGB(*color)
        if align == "left":
            self.c.drawString(x, y, s)
        elif align == "right":
            self.c.drawRightString(x, y, s)
        else:
            self.c.drawCentredString(x, y, s)
        if color is not None:
            self.c.setFillColorRGB(0, 0, 0)
        w = pdfmetrics.stringWidth(s, f, size)
        x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
        self.lines.append((x0, y, size, w, s))
        return w

    def wrap(self, s, width, size, bold=False):
        s = self.t(s)
        f = self.fontname(bold)
        out, cur = [], ""
        for word in s.split():
            cand = (cur + " " + word).strip()
            if pdfmetrics.stringWidth(cand, f, size) <= width:
                cur = cand
            else:
                if cur:
                    out.append(cur)
                cur = word
        if cur:
            out.append(cur)
        return out


_LOGO_CACHE: dict[str, Image.Image] = {}


def make_logo(key: str) -> Image.Image:
    """Firmenlogo als Rastergrafik – der Firmenname steht damit NICHT im Textlayer."""
    if key in _LOGO_CACHE:
        return _LOGO_CACHE[key]
    if key == "orion":
        img = Image.new("RGB", (1300, 360), "white")
        d = ImageDraw.Draw(img)
        blue = (0, 70, 140)
        d.ellipse([20, 20, 330, 330], fill=blue)
        for i in range(3):
            y = 110 + i * 60
            d.arc([70, y - 45, 280, y + 45], 200, 340, fill="white", width=20)
        d.text((370, 10), "ORION", font=ImageFont.truetype(PIL_ARIAL_BLACK, 200), fill=blue)
        d.text((376, 262), "Rohr- und Kanalservice", font=ImageFont.truetype(PIL_ARIAL, 66), fill=(90, 90, 90))
    elif key == "kaminski":
        img = Image.new("RGB", (1400, 360), "white")
        d = ImageDraw.Draw(img)
        green = (20, 110, 60)
        d.rectangle([30, 30, 320, 330], outline=green, width=26)
        d.line([175, 30, 175, 330], fill=green, width=20)
        d.line([30, 180, 320, 180], fill=green, width=20)
        d.text((370, 30), "KAMINSKI", font=ImageFont.truetype(PIL_ARIAL_BLACK, 170), fill=green)
        d.text((378, 250), "Fenster · Türen · Rollläden", font=ImageFont.truetype(PIL_ARIAL, 66),
               fill=(70, 70, 70))
    else:
        raise KeyError(key)
    _LOGO_CACHE[key] = img
    return img


def draw_invoice(pen: Pen, inv: Invoice) -> None:
    co = inv.company
    c = pen.c
    W, H = W_A4, H_A4
    L, R = 20 * mm, W - 20 * mm
    pen.fam = inv.font or co.font
    pen.ascii_only = inv.ascii_only
    header = inv.header or co.header
    lab = LABELSETS[co.labels]
    name = inv.firma
    grey = (0.35, 0.35, 0.35)

    # ---------------- Briefkopf
    if header == "classic":
        pen.text(L, H - 24 * mm, name, 17, bold=True, color=co.color)
        if co.tagline:
            pen.text(L, H - 30 * mm, co.tagline, 8.5, color=grey)
        y = H - 15 * mm
        for s in [co.street, co.city, f"Tel. {co.phone}", co.email, co.web]:
            if s:
                pen.text(R, y, s, 7.5, align="right")
                y -= 3.5 * mm
        c.setStrokeColorRGB(*co.color)
        c.setLineWidth(1.2)
        c.line(L, H - 35 * mm, R, H - 35 * mm)
        c.setStrokeColorRGB(0, 0, 0)
    elif header == "bar":
        c.setFillColorRGB(*co.color)
        c.rect(0, H - 33 * mm, W, 33 * mm, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        pen.text(L, H - 17 * mm, name, 19, bold=True, color=(1, 1, 1))
        pen.text(L, H - 24 * mm, co.tagline, 8.5, color=(1, 1, 1))
        y = H - 12 * mm
        for s in [co.street, co.city, f"Tel. {co.phone}", co.email, co.web]:
            if s:
                pen.text(R, y, s, 7.5, align="right", color=(1, 1, 1))
                y -= 3.6 * mm
    elif header == "centered":
        pen.text(W / 2, H - 20 * mm, name, 18, bold=True, align="center", color=co.color)
        pen.text(W / 2, H - 26 * mm, co.tagline, 8.5, align="center", color=grey)
        contact = f"{co.street} · {co.city} · Tel. {co.phone}" + (f" · {co.email}" if co.email else "")
        pen.text(W / 2, H - 31 * mm, contact, 7.5, align="center")
        c.setLineWidth(0.6)
        c.line(L, H - 34 * mm, R, H - 34 * mm)
    elif header == "typewriter":
        pen.text(L, H - 20 * mm, name, 12, bold=True)
        pen.text(L, H - 25 * mm, co.tagline, 8.5)
        pen.text(L, H - 30 * mm, f"{co.street}, {co.city}   Tel. {co.phone}   {co.email}", 8.5)
        pen.text(L, H - 34 * mm, "-" * 86, 8.5)
    elif header == "logo":
        logo = make_logo(co.key)
        lw = 72 * mm
        lh = lw * logo.height / logo.width
        c.drawImage(ImageReader(logo), L, H - 14 * mm - lh, width=lw, height=lh)
        y = H - 15 * mm
        for s in [co.street, co.city, f"Tel. {co.phone}"]:
            pen.text(R, y, s, 8, align="right")
            y -= 3.8 * mm
        c.setLineWidth(0.5)
        c.line(L, H - 38 * mm, R, H - 38 * mm)
    else:
        raise ValueError(header)

    # Falz- und Lochmarken
    c.setLineWidth(0.3)
    for yy in (H - 105 * mm, H - 210 * mm):
        c.line(4 * mm, yy, 8 * mm, yy)
    c.line(3 * mm, H / 2, 9 * mm, H / 2)

    # ---------------- Anschrift
    if header == "logo":
        sender = f"{co.street} · {co.city}"
    else:
        sender = f"{name} · {co.street} · {co.city}"
    w = pen.text(L, H - 49 * mm, sender, 6.5)
    c.setLineWidth(0.3)
    c.line(L, H - 49.8 * mm, L + w, H - 49.8 * mm)
    y = H - 55 * mm
    for s in inv.recipient or HV:
        pen.text(L, y, s, 10)
        y -= 4.6 * mm

    # ---------------- Info-Block
    rng = rng_for("kdnr", inv.id)
    kdnr = inv.kdnr or f"{rng.randint(20, 89)}{rng.randint(100, 999)}"
    info = [(lab["nr"], inv.nr), (lab["date"], fmt_date(inv.date, inv.date_style)), (lab["kd"], kdnr)]
    info += inv.meta
    if co.labels not in ("stadt", "vers", "ra"):
        info.append(("Bearbeiter", co.signer or "Büro"))
    y = H - 55 * mm
    for k, v in info:
        pen.text(118 * mm, y, k + ":", 8.5)
        pen.text(R, y, v, 8.5, align="right", bold=(k == lab["nr"]))
        y -= 4.3 * mm

    # ---------------- Betreff / Titel
    y = H - 98 * mm
    pen.text(L, y, inv.title.format(nr=inv.nr), 13, bold=True)
    y -= 6.5 * mm
    if inv.obj_printed:
        for ln in pen.wrap(f"{inv.obj_label}: {inv.obj_printed}", R - L, 9.5, bold=True):
            pen.text(L, y, ln, 9.5, bold=True)
            y -= 4.8 * mm
    if inv.betreff:
        pen.text(L, y, f"Betreff: {inv.betreff}", 9.5)
        y -= 4.8 * mm
    if inv.period:
        lbl = "Abrechnungszeitraum" if co.labels in ("stadt",) else "Leistungszeitraum"
        pen.text(L, y, f"{lbl}: {inv.period}", 9.5)
        y -= 4.8 * mm
    y -= 2 * mm
    pen.text(L, y, "Sehr geehrte Damen und Herren,", 9.5)
    y -= 4.8 * mm
    intro = inv.intro or {
        "stadt": "nachfolgend erhalten Sie die Abrechnung für den genannten Zeitraum:",
        "vers": "für den Versicherungsvertrag berechnen wir Ihnen folgenden Beitrag:",
        "ra": "in obiger Angelegenheit erlaube ich mir, meine Tätigkeit wie folgt abzurechnen:",
    }.get(co.labels, "für die ausgeführten Arbeiten erlauben wir uns, Ihnen Folgendes zu berechnen:")
    for ln in pen.wrap(intro, R - L, 9.5):
        pen.text(L, y, ln, 9.5)
        y -= 4.8 * mm
    y -= 3 * mm

    # ---------------- Positionstabelle
    x_pos, x_desc = L, L + 10 * mm
    x_qty_r, x_unit, x_ep_r, x_tot_r = 136 * mm, 138 * mm, 166 * mm, R
    desc_w = {"vers": 95 * mm, "ra": 130 * mm}.get(co.labels, 88 * mm)
    if co.labels == "vers":
        x_unit = 128 * mm
    typewriter = header == "typewriter"
    if not typewriter and header in ("bar", "centered"):
        c.setFillColorRGB(0.92, 0.92, 0.92)
        c.rect(L - 1 * mm, y - 1.6 * mm, R - L + 2 * mm, 5.6 * mm, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
    hs = 8.5
    pen.text(x_pos, y, lab["pos"], hs, bold=True)
    pen.text(x_desc, y, lab["desc"], hs, bold=True)
    if lab["qty"]:
        pen.text(x_qty_r, y, lab["qty"], hs, bold=True, align="right")
    if lab["unit"]:
        pen.text(x_unit, y, lab["unit"], hs, bold=True)
    if lab["ep"]:
        pen.text(x_ep_r, y, lab["ep"], hs, bold=True, align="right")
    pen.text(x_tot_r, y, lab["tot"], hs, bold=True, align="right")
    y -= 2.2 * mm
    if typewriter:
        y -= 1.5 * mm
        pen.text(L, y, "-" * 86, 8.5)
    else:
        c.setLineWidth(0.6)
        c.line(L, y, R, y)
    y -= 4.6 * mm
    ts = 9
    for i, (text, qty, unit, price, tot, rate) in enumerate(inv.rows, 1):
        lines = pen.wrap(text, desc_w, ts)
        pen.text(x_pos, y, f"{i}", ts)
        if co.labels in ("vers", "ra"):
            pass
        else:
            pen.text(x_qty_r, y, de_qty(qty), ts, align="right")
            pen.text(x_unit, y, unit, ts)
            pen.text(x_ep_r, y, de_price(price), ts, align="right")
        if co.labels == "vers":
            pen.text(x_unit, y, unit, ts)
        pen.text(x_tot_r, y, de_number(tot), ts, align="right")
        for ln in lines:
            pen.text(x_desc, y, ln, ts)
            y -= 4.3 * mm
        y -= 1.2 * mm
    if typewriter:
        pen.text(L, y + 1.5 * mm, "-" * 86, 8.5)
    else:
        c.setLineWidth(0.4)
        c.line(L, y + 2.2 * mm, R, y + 2.2 * mm)
    y -= 3 * mm

    # ---------------- Summen
    xl = 112 * mm
    pen.text(xl, y, lab["net"], 9.5)
    pen.text(R, y, fmt_eur(inv.net, "suffix") if inv.amount_style != "suffix_eur" else fmt_eur(inv.net, "suffix_eur"),
             9.5, align="right")
    y -= 4.8 * mm
    if inv.kleinunternehmer:
        pass
    else:
        for rate in sorted(inv.per_rate, reverse=True):
            base = inv.per_rate[rate]
            if rate == 0:
                pen.text(xl, y, "nicht steuerbar (0 %)", 9.5)
                pen.text(R, y, de_number(D(0)) + (" €" if inv.amount_style != "suffix_eur" else " EUR"),
                         9.5, align="right")
            else:
                lbl = lab["vat"].format(r=rate)
                if len(inv.per_rate) > 1:
                    lbl += f" auf {de_number(base)}"
                pen.text(xl, y, lbl, 9.5)
                pen.text(R, y, de_number(inv.taxes[rate]) + (" €" if inv.amount_style != "suffix_eur" else " EUR"),
                         9.5, align="right")
            y -= 4.8 * mm
    c.setLineWidth(0.8)
    c.line(xl, y + 3.2 * mm, R, y + 3.2 * mm)
    y -= 1 * mm
    pen.text(xl, y, lab["gross"] + (" EUR" if inv.amount_style == "plain" else ""), 10.5, bold=True)
    pen.text(R, y, fmt_eur(inv.gross, inv.amount_style), 10.5, bold=True, align="right")
    y -= 3 * mm
    c.line(xl, y + 1 * mm, R, y + 1 * mm)
    c.line(xl, y + 0.4 * mm, R, y + 0.4 * mm)
    y -= 5 * mm

    due = inv.date + timedelta(days=inv.due_days)
    rest = None
    if inv.abschlaege:
        n, amt = inv.abschlaege
        paid = n * amt
        rest = inv.gross - paid
        pen.text(xl - 32 * mm, y, f"abzüglich geleistete Abschläge ({n} × {de_number(amt)} €)", 9.5)
        pen.text(R, y, "-" + de_number(paid) + " €", 9.5, align="right")
        y -= 5 * mm
        lbl = "Nachzahlung" if rest >= 0 else "Guthaben"
        pen.text(xl, y, lbl, 10.5, bold=True)
        pen.text(R, y, fmt_eur(abs(rest), "suffix"), 10.5, bold=True, align="right")
        y -= 7 * mm

    # ---------------- Hinweise / Zahlung
    notes = list(inv.notes)
    if inv.kleinunternehmer:
        notes.append("Gemäß § 19 UStG wird keine Umsatzsteuer berechnet (Kleinunternehmerregelung).")
    if co.handwerk and not inv.kleinunternehmer:
        lohn = sum((r[4] for r in inv.rows if r[2] in ("Std",)), D(0))
        if lohn > 0:
            notes.append(f"Im Rechnungsbetrag enthaltene Arbeitskosten i. S. d. § 35a EStG: "
                         f"{de_number(q2(lohn * D('1.19')))} € (brutto).")
    if inv.payment == "ueberweisung":
        if rest is not None:
            if rest >= 0:
                notes.append(f"Bitte überweisen Sie den Nachzahlungsbetrag bis zum {fmt_date(due)} unter Angabe "
                             f"der Rechnungsnummer auf das unten genannte Konto.")
            else:
                notes.append("Das Guthaben wird Ihnen in den nächsten Tagen erstattet.")
        else:
            notes.append(f"Bitte überweisen Sie den Rechnungsbetrag bis zum {fmt_date(due)} ohne Abzug auf das "
                         f"unten genannte Konto. Verwendungszweck: {inv.nr}")
    elif inv.payment == "lastschrift":
        if rest is not None and rest < 0:
            notes.append("Das Guthaben wird mit dem nächsten Abschlag verrechnet bzw. Ihrem Konto gutgeschrieben.")
        else:
            notes.append(f"Der fällige Betrag wird am {fmt_date(due)} per SEPA-Lastschrift von Ihrem Konto "
                         f"eingezogen.")
    elif inv.payment == "bar":
        notes.append("Betrag dankend in bar erhalten.")
    for n_ in notes:
        for ln in pen.wrap(n_, R - L, 8.5):
            pen.text(L, y, ln, 8.5)
            y -= 4.2 * mm
        y -= 1 * mm
    y -= 2 * mm
    pen.text(L, y, "Mit freundlichen Grüßen", 9.5)
    if co.signer:
        y -= 9 * mm
        pen.text(L, y, co.signer, 9.5, font=FAMILIES["times"][0] if pen.fam != "courier" else None)

    # ---------------- Fußzeile
    if header == "logo":
        # Firmenname erscheint im Textlayer NUR hier, in winziger Schrift.
        fl = (f"{name} · {co.street} · {co.city} · {co.email} · {co.register} · {co.owner}")
        pen.text(W / 2, 13 * mm, fl, 5.5, align="center", color=grey)
        pen.text(W / 2, 10.5 * mm, f"{co.bank} · IBAN {co.iban} · BIC {co.bic} · {co.taxid}", 5.5,
                 align="center", color=grey)
        return
    c.setLineWidth(0.4)
    c.setStrokeColorRGB(0.5, 0.5, 0.5)
    c.line(L, 25 * mm, R, 25 * mm)
    c.setStrokeColorRGB(0, 0, 0)
    col1 = [name, co.street, co.city, f"Tel. {co.phone}"]
    col2 = [co.bank, f"IBAN {co.iban}" if co.iban else "", f"BIC {co.bic}" if co.bic else ""]
    col3 = [co.taxid, co.register, co.owner]
    fs = 6.5
    for x, col in ((L, col1), (80 * mm, col2), (135 * mm, col3)):
        yy = 21 * mm
        for s in col:
            if s:
                for ln in pen.wrap(s, 55 * mm, fs):
                    pen.text(x, yy, ln, fs, color=grey)
                    yy -= 3 * mm


def receipt_lines(rc: Receipt) -> list[tuple[str, str, bool]]:
    """Zeilen eines Kassenbons: (text, align, bold)."""
    co = rc.company
    L = []
    width = 34
    if rc.co == "bauwelt":
        L += [("BAUWELT", "c", True), ("Baumarkt Musterstadt", "c", False)]
    else:
        L += [("Freie Tankstelle", "c", False), ("BRÜCKNER", "c", True)]
    L += [(co.street, "c", False), (co.city, "c", False), (f"Tel. {co.phone}", "c", False),
          (co.taxid, "c", False), ("", "l", False), ("-" * width, "l", False)]
    for it in rc.items:
        text, qty, price = it[0], D(str(it[1])), D(str(it[2]))
        tot = q2(qty * price)
        if len(it) > 3:
            L.append((text, "l", False))
            L.append((_lr(f"  {it[3]}", de_number(tot) + " A", width), "l", False))
        elif qty != 1:
            L.append((text, "l", False))
            L.append((_lr(f"  {de_qty(qty)} x {de_number(price)}", de_number(tot) + " A", width), "l", False))
        else:
            L.append((_lr(text, de_number(tot) + " A", width), "l", False))
    gross = rc.gross
    net = q2(gross / D("1.19"))
    tax = gross - net
    L += [("-" * width, "l", False), (_lr("SUMME EUR", de_number(gross), width), "l", True),
          ("=" * width, "l", False), (_lr(f"Gegeben {rc.payment}", de_number(gross), width), "l", False)]
    if rc.payment != "BAR":
        L += [("", "l", False), ("Kartenzahlung girocard", "l", False), ("Terminal-ID 0000 4721", "l", False),
              ("Beleg 1187  Trace 3342", "l", False), ("Zahlung erfolgt", "l", False)]
    L += [("", "l", False), (_lr("MwSt  Netto  MwSt", "Brutto", width), "l", False),
          (_lr(f"A 19% {de_number(net)} {de_number(tax)}", de_number(gross), width), "l", False),
          ("", "l", False),
          (_lr(rc.dt.strftime("%d.%m.%Y %H:%M"), f"Bon-Nr. {rc.bon}", width), "l", False),
          (_lr(f"Kasse {rc.kasse}", "Bed. 12", width), "l", False),
          ("TSE-Seriennr.: 7c3e1f09a2b4", "l", False),
          (f"TSE-Start: {rc.dt.strftime('%Y-%m-%dT%H:%M')}:04", "l", False),
          ("TSE-Signatur: MEUCIQD4kz1+Lx0", "l", False),
          ("", "l", False), ("Vielen Dank für Ihren Einkauf!", "c", False)]
    if rc.co == "bauwelt":
        L.append(("Umtausch nur mit Kassenbon", "c", False))
    return L


def _lr(left: str, right: str, width: int) -> str:
    pad = max(1, width - len(left) - len(right))
    return left + " " * pad + right


def receipt_pagesize(rc: Receipt) -> tuple[float, float]:
    n = len(receipt_lines(rc))
    return (72 * mm, n * 3.9 * mm + 22 * mm)


def draw_receipt(pen: Pen, rc: Receipt) -> None:
    W, H = receipt_pagesize(rc)
    pen.fam = "courier"
    y = H - 12 * mm
    for text, align, bold in receipt_lines(rc):
        size = 10.5 if (bold and align == "c") else 8.6
        if align == "c":
            pen.text(W / 2, y, text, size, bold=bold, align="center")
        else:
            pen.text(4 * mm, y, text, size, bold=bold)
        y -= 3.9 * mm if size < 10 else 5 * mm


# --------------------------------------------------------------------------------------
# PDF-Erzeugung
# --------------------------------------------------------------------------------------

def new_canvas(buf, pagesize=A4, encrypt=None):
    return rl_canvas.Canvas(buf, pagesize=pagesize, invariant=1, pageCompression=1, encrypt=encrypt)


def set_meta(c, doc, digital=True):
    if isinstance(doc, Invoice):
        co = doc.company
        c.setCreator(co.creator)
        if co.header != "logo":
            c.setAuthor(doc.firma if not doc.ascii_only else asciify(doc.firma))
        if digital and co.header != "logo":
            c.setTitle(asciify(f"{doc.title.format(nr=doc.nr)} {doc.nr}") if doc.ascii_only
                       else f"{doc.title.format(nr=doc.nr)} {doc.nr}")
    else:
        c.setCreator("Scan")


def draw_doc(pen: Pen, doc) -> None:
    if isinstance(doc, Invoice):
        draw_invoice(pen, doc)
    else:
        draw_receipt(pen, doc)


def pagesize_for(doc):
    return receipt_pagesize(doc) if isinstance(doc, Receipt) else A4


def digital_pdf(doc, encrypt=None) -> tuple[bytes, list]:
    buf = io.BytesIO()
    c = new_canvas(buf, pagesize_for(doc), encrypt=encrypt)
    set_meta(c, doc)
    pen = Pen(c)
    draw_doc(pen, doc)
    c.showPage()
    c.save()
    return buf.getvalue(), pen.lines


def rasterize(pdf_bytes: bytes, dpi: int) -> Image.Image:
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        page = pdf[0]
        img = page.render(scale=dpi / 72).to_pil().convert("RGB")
        page.close()
    finally:
        pdf.close()
    return img


def render_doc(doc, dpi: int) -> tuple[Image.Image, list]:
    pdf, lines = digital_pdf(doc)
    return rasterize(pdf, dpi), lines


# --------------------------------------------------------------------------------------
# Bild-Degradierung (nur Pillow, deterministisch über eigenen RNG)
# --------------------------------------------------------------------------------------

_ND = NormalDist()


def noise_image(size, sigma: float, rng) -> Image.Image:
    lut = [max(0, min(255, round(128 + sigma * _ND.inv_cdf((i + 0.5) / 256)))) for i in range(256)]
    raw = rng.randbytes(size[0] * size[1])
    return Image.frombytes("L", size, raw).point(lut)


def lowfreq(size, rng, lo: int, hi: int, grid=(4, 6)) -> Image.Image:
    g = Image.new("L", grid)
    g.putdata([rng.randint(lo, hi) for _ in range(grid[0] * grid[1])])
    return g.resize(size, Image.BICUBIC)


def add_noise(img: Image.Image, sigma: float, rng) -> Image.Image:
    n = noise_image(img.size, sigma, rng)
    if img.mode == "RGB":
        n = Image.merge("RGB", (n, n, n))
    return ImageChops.add(img, n, 1.0, -128)


def jpeg_roundtrip(img: Image.Image, quality: int) -> Image.Image:
    b = io.BytesIO()
    img.save(b, "JPEG", quality=quality)
    b.seek(0)
    out = Image.open(b)
    out.load()
    return out


def degrade_scan(img: Image.Image, rng, *, dpi: int, skew: float, blur: float, noise: float,
                 paper: int, ink: int, holes: bool, edge: bool, jpeg_q: int, fade: int = 0) -> Image.Image:
    g = img.convert("L")
    # Kontrast reduzieren / graues Papier
    lut = [int(ink + (paper - ink) * (v / 255.0)) for v in range(256)]
    g = g.point(lut)
    # ungleichmäßige Ausleuchtung / Vergilbung
    g = ImageChops.subtract(g, lowfreq(g.size, rng, 0, 18))
    if fade:
        # verblasste Bereiche (Thermopapier)
        mask = lowfreq(g.size, rng, 0, fade, grid=(3, 7))
        g = Image.composite(Image.new("L", g.size, paper), g, mask)
    g = g.filter(ImageFilter.GaussianBlur(blur))
    # Lochung (Locher) links
    if holes:
        d = ImageDraw.Draw(g)
        r = int(2.6 * dpi / 25.4)
        cx = int(12 * dpi / 25.4)
        for yy_mm in (148.5 - 40, 148.5 + 40):
            cy = int(yy_mm * dpi / 25.4)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=30)
    # Schieflage
    fill = 25 if edge else paper
    g = g.rotate(skew, resample=Image.BICUBIC, expand=False, fillcolor=fill)
    if edge:
        # Scanner-Deckel sichtbar: dunkler Rand oben/rechts
        d = ImageDraw.Draw(g)
        e = int(rng.uniform(2.5, 5) * dpi / 25.4)
        d.rectangle([g.width - e, 0, g.width, g.height], fill=28)
    # Staub/Punkte
    d = ImageDraw.Draw(g)
    for _ in range(rng.randint(40, 120)):
        x, y = rng.randrange(g.width), rng.randrange(g.height)
        s = rng.choice([1, 1, 1, 2, 2, 3])
        d.ellipse([x, y, x + s, y + s], fill=rng.randint(40, 120))
    g = add_noise(g, noise, rng)
    return jpeg_roundtrip(g, jpeg_q)


def perspective_coeffs(dst, src):
    """Koeffizienten für Image.transform(PERSPECTIVE): Ausgabepunkt dst -> Eingabepunkt src."""
    A, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b.append(v)
    n = 8
    M = [row[:] + [bv] for row, bv in zip(A, b)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        for r in range(n):
            if r != col:
                f = M[r][col] / M[col][col]
                for k in range(col, n + 1):
                    M[r][k] -= f * M[col][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def make_background(size, rng, kind: str) -> Image.Image:
    W, H = size
    if kind == "wood":
        base = (126, 88, 56)
        img = Image.new("RGB", (W, H), base)
        d = ImageDraw.Draw(img)
        x = 0
        while x < W:
            w = rng.randint(2, 9)
            s = rng.randint(-28, 22)
            d.rectangle([x, 0, x + w, H], fill=tuple(max(0, min(255, v + s)) for v in base))
            x += w
        img = img.filter(ImageFilter.GaussianBlur(2.0))
    elif kind == "desk":
        img = Image.new("RGB", (W, H), (74, 76, 82))
    else:  # heller Tisch
        img = Image.new("RGB", (W, H), (206, 200, 188))
    light = lowfreq((W, H), rng, 150, 255, grid=(3, 4))
    return ImageChops.multiply(img, Image.merge("RGB", (light, light, light)))


def photograph(page: Image.Image, rng, *, out=(1512, 2016), bg="wood", fill=0.80, tilt=4.0,
               taper=0.05, blur=1.0, noise=5.0, quality=85) -> Image.Image:
    """Simuliert ein Handyfoto: Perspektive, Hintergrund, Schatten, Licht, Unschärfe, Rauschen."""
    W, H = out
    page = page.convert("RGB")
    page = ImageChops.multiply(page, Image.new("RGB", page.size, (255, 250, 238)))  # warmes Licht
    pw, ph = page.size
    s = min(fill * W / pw, fill * H / ph)
    w2, h2 = pw * s / 2, ph * s / 2
    cx, cy = W / 2 + rng.uniform(-30, 30), H / 2 + rng.uniform(-30, 30)
    k = taper
    quad = [(-w2 * (1 - k), -h2), (w2 * (1 - k), -h2), (w2, h2), (-w2, h2)]
    import math
    a = math.radians(rng.uniform(-tilt, tilt))
    dst = []
    for x, y in quad:
        xr = x * math.cos(a) - y * math.sin(a) + cx + rng.uniform(-14, 14)
        yr = x * math.sin(a) + y * math.cos(a) + cy + rng.uniform(-14, 14)
        dst.append((xr, yr))
    src = [(0, 0), (pw, 0), (pw, ph), (0, ph)]
    coeffs = perspective_coeffs(dst, src)
    warped = page.transform((W, H), Image.PERSPECTIVE, coeffs, Image.BICUBIC, fillcolor=(0, 0, 0))
    mask = Image.new("L", page.size, 255).transform((W, H), Image.PERSPECTIVE, coeffs, Image.BILINEAR,
                                                    fillcolor=0)
    bgimg = make_background((W, H), rng, bg)
    shadow = Image.new("L", (W, H), 0)
    shadow.paste(mask, (rng.randint(8, 18), rng.randint(10, 22)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14)).point(lambda v: int(v * 0.55))
    bgimg = Image.composite(Image.new("RGB", (W, H), (10, 10, 10)), bgimg, shadow)
    img = Image.composite(warped, bgimg, mask)
    light = lowfreq((W, H), rng, 175, 255, grid=(3, 4))
    img = ImageChops.multiply(img, Image.merge("RGB", (light, light, light)))
    img = img.filter(ImageFilter.GaussianBlur(blur))
    img = add_noise(img, noise, rng)
    return img


def jpeg_bytes(img: Image.Image, quality: int, dpi=None, exif=None) -> bytes:
    b = io.BytesIO()
    kw = {"quality": quality}
    if dpi:
        kw["dpi"] = (dpi, dpi)
    if exif is not None:
        kw["exif"] = exif
    img.save(b, "JPEG", **kw)
    return b.getvalue()


def exif_bytes(dt: datetime, orientation: int | None = None) -> bytes:
    ex = Image.Exif()
    ex[0x0132] = dt.strftime("%Y:%m:%d %H:%M:%S")  # DateTime
    ex[0x0131] = "Kamera"                           # Software
    if orientation:
        ex[0x0112] = orientation
    return ex.tobytes()


# --------------------------------------------------------------------------------------
# Scan-/Sonder-PDFs
# --------------------------------------------------------------------------------------

def image_pdf(pages: list[bytes], sizes: list[tuple[float, float]], creator="Scan") -> bytes:
    """Reine Bild-PDF (kein Textlayer). pages = JPEG-Bytes je Seite."""
    buf = io.BytesIO()
    c = new_canvas(buf, sizes[0])
    c.setCreator(creator)
    for jpg, sz in zip(pages, sizes):
        c.setPageSize(sz)
        c.drawImage(ImageReader(io.BytesIO(jpg)), 0, 0, width=sz[0], height=sz[1])
        c.showPage()
    c.save()
    return buf.getvalue()


CONFUSE = {"a": "ao@", "b": "h6", "c": "eo(", "d": "cl", "e": "ec", "f": "tr", "g": "q9", "h": "bn",
           "i": "l1!|", "k": "lc", "l": "1I|", "m": "rn", "n": "rih", "o": "0c", "p": "q", "r": "tn",
           "s": "5z", "t": "f+", "u": "vn", "w": "vv", "z": "2", "ä": "a", "ö": "o", "ü": "u", "ß": "B"}
DIGIT_JUNK = "lIOoSsBZ|!?°"


def mangle_word(w: str, rng) -> str:
    out, changed = [], 0
    for ch in w:
        lo = ch.lower()
        if ch.isdigit():
            out.append(rng.choice(DIGIT_JUNK))
            changed += 1
        elif lo in CONFUSE and rng.random() < 0.5:
            out.append(rng.choice(CONFUSE[lo]))
            changed += 1
        elif ch.isalpha() and rng.random() < 0.08:
            changed += 1
        elif ch in ".,:/-€" and rng.random() < 0.6:
            out.append(rng.choice("·'`,;:~_"))
        else:
            out.append(ch)
    s = "".join(out)
    if len(w) >= 3 and changed < 2:
        pos = rng.randrange(len(s) + 1)
        s = s[:pos] + rng.choice("il|'^~") + s[pos:]
        s = rng.choice("Il!") + s[1:] if s else s
    return s


def garbage_line(s: str, rng) -> str:
    words = [mangle_word(w, rng) for w in s.split()]
    out = []
    for w in words:
        out.append(w)
        if rng.random() < 0.15:
            out.append(rng.choice(["~", "¦", "‚", "°", "''", ",,", "|"]))
    return " ".join(out)


def garbage_textlayer_pdf(jpg: bytes, lines: list, rng) -> bytes:
    """Scan-Bild + unsichtbare (Rendermodus 3) Müll-Textebene wie von schlechtem Scanner-OCR."""
    buf = io.BytesIO()
    c = new_canvas(buf, A4)
    c.setCreator("Scan2PDF OCR")
    c.drawImage(ImageReader(io.BytesIO(jpg)), 0, 0, width=W_A4, height=H_A4)
    t = c.beginText()
    t.setTextRenderMode(3)
    for (x, y, size, w, s) in lines:
        if not s.strip():
            continue
        if rng.random() < 0.12:
            continue  # Zeile "übersehen"
        g = garbage_line(s, rng)
        t.setFont(FAMILIES["arial"][0], max(5, size + rng.uniform(-1.5, 1.5)))
        t.setTextOrigin(x + rng.uniform(-8, 8), y + rng.uniform(-6, 6))
        t.textOut(g)
    for _ in range(rng.randint(6, 12)):
        t.setFont(FAMILIES["arial"][0], rng.uniform(6, 14))
        t.setTextOrigin(rng.uniform(20, 500), rng.uniform(20, 800))
        t.textOut("".join(rng.choice(".,:;'`~^°|¦Il!") for _ in range(rng.randint(3, 25))))
    c.drawText(t)
    c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# Unsupported / Junk-Dateien
# --------------------------------------------------------------------------------------

def zip_bytes(files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            zi = zipfile.ZipInfo(name, date_time=(2023, 3, 1, 12, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
    return b.getvalue()


def fake_xlsx() -> bytes:
    return zip_bytes({
        "[Content_Types].xml": b'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "xl/workbook.xml": b'<?xml version="1.0" encoding="UTF-8"?><workbook/>',
        "xl/worksheets/sheet1.xml": "<worksheet><!-- Nebenkosten 2023 Entwurf --></worksheet>".encode(),
    })


def fake_docx() -> bytes:
    return zip_bytes({
        "[Content_Types].xml": b'<?xml version="1.0" encoding="UTF-8"?><Types/>',
        "word/document.xml": "<w:document><!-- Mietvertrag Entwurf --></w:document>".encode(),
    })


def fake_ole(tag: bytes, size: int = 4096) -> bytes:
    rng = rng_for("ole", tag)
    head = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 16 + b"\x3e\x00\x03\x00\xfe\xff\x09\x00"
    body = tag + rng.randbytes(size - len(head) - len(tag))
    return head + body


def ds_store() -> bytes:
    return b"\x00\x00\x00\x01Bud1" + b"\x00" * 2036 + b"DSDB" + b"\x00" * 2048


def office_lockfile() -> bytes:
    name = "Frau Beispiel".encode("latin-1")
    return bytes([len(name)]) + name + b"\x00" * (54 - len(name)) + "Frau Beispiel".encode("utf-16-le") + b"\x00" * 80


# --------------------------------------------------------------------------------------
# Dateiplan
# --------------------------------------------------------------------------------------

# kind: digital_pdf | scan_pdf | photo_jpg | png | heic | tiff_multipage | sammel_pdf |
#       garbage_textlayer_pdf | encrypted_pdf | corrupt_pdf | unsupported | duplicate_of
PLAN: list[dict] = [
    # ---- digitale PDFs
    dict(kind="digital_pdf", path="2018/Belege/Unbenannt.pdf", doc="lindqvist_2018"),
    dict(kind="digital_pdf", path="2018/Dokument (3).pdf", doc="albrecht_2018", tags=["schreibmaschinenschrift"]),
    dict(kind="digital_pdf", path="2019/Handwerker/20190312_1422.pdf", doc="mueller_2019"),
    dict(kind="digital_pdf", path="2019/Handwerker/Rechnung.pdf", doc="vogt_2019"),
    dict(kind="digital_pdf", path="2019/Stadtwerke & Versorger/Abrechnung_2018.pdf", doc="stadtwerke_2019"),
    dict(kind="digital_pdf", path="Versicherung & Recht/Nordlicht/Beitrag 2019.pdf", doc="nordlicht_2019"),
    dict(kind="digital_pdf", path="2020/Rechnungen 2020/Stadtwerke Abrechnung 2019.pdf", doc="stadtwerke_2020_bk",
         tags=["synonym_betriebskosten_nebenkosten"]),
    dict(kind="digital_pdf", path="2020/Rechnungen 2020/Dokument (1).pdf", doc="krueger_2020",
         tags=["hauswart_statt_hausmeister"]),
    dict(kind="digital_pdf", path="2020/Wartungsverträge/INV-88213.pdf", doc="kranich_2020"),
    dict(kind="digital_pdf", path="2021/Objekte/Musterstraße 12/Rechnungen/Rechnung (1).pdf", doc="brandl_2021",
         tags=["abgekuerzte_strasse", "kompositum_heizungswartung"]),
    dict(kind="digital_pdf", path="2021/Objekte/Musterstraße 12/Rechnungen/Unbenannt.pdf", doc="petersen_2021"),
    dict(kind="digital_pdf", path="2021/Objekte/Birkenallee 21/Garten 2021.pdf", doc="gruenwerk_2021"),
    dict(kind="digital_pdf", path="2021/Dokument.pdf", doc="orion_2021", tags=["logo_only_company"]),
    dict(kind="digital_pdf", path="Versicherung & Recht/Anwalt/Kostennote.pdf", doc="weissenfels_2021",
         tags=["eszett_weissenfels"]),
    dict(kind="digital_pdf", path="2022/Reinigung + Pflege/Rechnung Juli.pdf", doc="krueger_2022",
         tags=["hauswart_statt_hausmeister"]),
    dict(kind="digital_pdf", path="2022/Handwerker/Rechnung_Vogt.pdf", doc="vogt_2022"),
    dict(kind="digital_pdf", path="2022/Objekte/Birkenallee 21/Heizkosten 2021.pdf", doc="thermometrik_2022"),
    dict(kind="digital_pdf", path="Versicherung & Recht/Nordlicht/Dokument (2).pdf", doc="nordlicht_2022"),
    dict(kind="digital_pdf", path="Mails/Anhänge/Mai 2023/RE_2023-00588.pdf", doc="mueller_2023",
         tags=["ascii_umlaute_mueller"]),
    dict(kind="digital_pdf", path="2023/Q1/Rechnung (3).pdf", doc="glanzwerk_2023"),
    dict(kind="digital_pdf", path="2023/Dokument (7).pdf", doc="sonnleitner_2023"),
    dict(kind="digital_pdf", path="2024/Belege Jan-Apr/20240109_Brandl.pdf", doc="brandl_2024"),
    dict(kind="digital_pdf", path="2024/Belege Jan-Apr/Dokument (4).pdf", doc="kaminski_2024",
         tags=["logo_only_company"]),
    dict(kind="digital_pdf", path="2024/Handwerker/Rechnung (2).pdf", doc="vogt_2024"),
    dict(kind="digital_pdf", path="Objekte/Musterstr. 12/Dach/Unbenannt 2.pdf", doc="hoffmannbeck_2024",
         tags=["abgekuerzte_strasse"]),
    dict(kind="digital_pdf", path="Verschiedenes/alt/Unbenannt.pdf", doc="thermometrik_2024",
         tags=["kompositum_rauchwarnmelder"]),
    dict(kind="digital_pdf", path="2025/Offene Posten/Abrechnung.pdf", doc="stadtwerke_2025_bk"),
    dict(kind="digital_pdf", path="2025/Versicherung/Beitragsrechnung 2025.pdf", doc="nordlicht_2025",
         tags=["kompositum_wohngebaeudeversicherung"]),
    # ---- Scans (reine Bild-PDFs)
    dict(kind="scan_pdf", path="2018/Belege/scan0001.pdf", doc="stadtwerke_2018", scan="old"),
    dict(kind="scan_pdf", path="2019/Handwerker/scan0005.pdf", doc="petersen_2019", scan="office"),
    dict(kind="scan_pdf", path="Verschiedenes/alt/neu/Dokument (3).pdf", doc="mueller_2020", scan="old"),
    dict(kind="scan_pdf", path="Scans unsortiert/Scan_00487.pdf", doc="sonnleitner_2020", scan="office",
         tags=["dateiname_irrefuehrend"]),
    dict(kind="scan_pdf", path="Buchhaltung/Eingang/Scans/scan0003.pdf", doc="glanzwerk_2020", scan="office"),
    dict(kind="scan_pdf", path="Buchhaltung/Eingang/Scans/scan0001.pdf", doc="hoffmannbeck_2021", scan="office",
         rotate=90, tags=["rotated_90"]),
    dict(kind="scan_pdf", path="Buchhaltung/Eingang/Scans/scan0009.pdf", doc="bauwelt_2021", scan="thermal",
         tags=["thermal_receipt", "kassenbon"]),
    dict(kind="scan_pdf", path="Scans unsortiert/20221011_0915.pdf", doc="albrecht_2022", scan="office",
         tags=["schreibmaschinenschrift"]),
    dict(kind="scan_pdf", path="2023/Wartungsverträge/scan0002.pdf", doc="brandl_2023", scan="office",
         tags=["kompositum_brennwertkessel_inspektion"]),
    dict(kind="scan_pdf", path="Scans unsortiert/scan0012.pdf", doc="stadtwerke_2023", scan="old", rotate=180,
         tags=["rotated_180"]),
    dict(kind="scan_pdf", path="Kassenbons/scan0004.pdf", doc="brueckner_2024", scan="thermal",
         tags=["thermal_receipt", "kassenbon"]),
    dict(kind="scan_pdf", path="2025/Offene Posten/scan0001.pdf", doc="krueger_2025", scan="office",
         tags=["hauswart_statt_hausmeister"]),
    # ---- Fotos / Bilder
    dict(kind="photo_jpg", path="Handy Fotos/IMG_2231.JPG", doc="gruenwerk_2019", photo=dict(bg="wood")),
    dict(kind="photo_jpg", path="Handy Fotos/IMG_4402.jpg", doc="yilmaz_2021", photo=dict(bg="desk"),
         exif_orientation=6, tags=["exif_orientation_6"]),
    dict(kind="photo_jpg", path="Kassenbons/IMG_6093.jpg", doc="bauwelt_2023", photo=dict(bg="desk"),
         thermal=True, tags=["thermal_receipt", "kassenbon"]),
    dict(kind="photo_jpg", path="Handy Fotos/WhatsApp Image 2024-07-02 at 18.41.07.jpeg", doc="lindqvist_2024",
         photo=dict(bg="light", out=(1200, 1600), quality=72), tags=["whatsapp_ohne_exif"]),
    dict(kind="png", path="Mails/Anhänge/image001.png", doc="rieger_2022", png="scan"),
    dict(kind="png", path="Verschiedenes/Bildschirmfoto 2023-03-20 um 09.14.52.png", doc="petersen_2023",
         png="screenshot"),
    dict(kind="heic", path="Handy Fotos/IMG_5120.HEIC", doc="kranich_2023", photo=dict(bg="wood")),
    dict(kind="heic", path="Handy Fotos/IMG_7731.HEIC", doc="glanzwerk_2024", photo=dict(bg="light")),
    # ---- Mehrseitige Dateien
    dict(kind="tiff_multipage", path="Buchhaltung/Eingang/Fax/FAX_20220207_0832.tif",
         docs=["lindqvist_2022", "yilmaz_2022", "petersen_2022"], tags=["bilevel_g4", "fax"]),
    dict(kind="sammel_pdf", path="Buchhaltung/Eingang/Scan_Stapel_März.pdf",
         docs=["nordlicht_2021", "vogt_2021", "kranich_2021", "mueller_2021", "glanzwerk_2021", "stadtwerke_2021"],
         scanned_pages=[2, 4, 6], tags=["gemischt_digital_und_scan"]),
    # ---- Müll-Textebene
    dict(kind="garbage_textlayer_pdf", path="Buchhaltung/Eingang/Scans/scan0007.pdf", doc="glanzwerk_2022",
         scan="office"),
    dict(kind="garbage_textlayer_pdf", path="Scans unsortiert/Scan_2023-04-11.pdf", doc="gruenwerk_2023",
         scan="office"),
    # ---- Fehlerfälle
    dict(kind="encrypted_pdf", path="Mails/Anhänge/Kostennote_2024-0133_verschlüsselt.pdf", doc="weissenfels_2024",
         password="Sommer2024!"),
    dict(kind="corrupt_pdf", path="Mails/Anhänge/INV-93307.pdf", doc="kranich_2024"),
    # ---- nicht unterstützt / Junk
    dict(kind="unsupported", path="Buchhaltung/Nebenkosten 2023.xlsx", maker="xlsx"),
    dict(kind="unsupported", path="Buchhaltung/~$Nebenkosten 2023.xlsx", maker="lock"),
    dict(kind="unsupported", path="Mails/Anhänge/AW Rechnung Heizung.msg", maker="msg"),
    dict(kind="unsupported", path="Verschiedenes/Mietvertrag Entwurf.docx", maker="docx"),
    dict(kind="unsupported", path="Mails/Anhänge/Rechnungen_2022.zip", maker="zip"),
    dict(kind="unsupported", path=".DS_Store", maker="dsstore"),
    dict(kind="unsupported", path="Buchhaltung/Eingang/Scans/Thumbs.db", maker="thumbs"),
    # ---- Dubletten (byte-identische Kopien)
    dict(kind="duplicate_of", path="Buchhaltung/Erledigt/2022/4711_22.pdf", of="2022/Handwerker/Rechnung_Vogt.pdf"),
    dict(kind="duplicate_of", path="Mails/Anhänge/Rechnung 4711-22.pdf", of="2022/Handwerker/Rechnung_Vogt.pdf"),
    dict(kind="duplicate_of", path="Mails/Anhänge/Beitragsrechnung.pdf",
         of="Versicherung & Recht/Nordlicht/Beitrag 2019.pdf"),
]

SCAN_PRESETS = {
    "office": dict(dpi=(200, 200), skew=(1.0, 2.6), blur=(0.55, 0.9), noise=(6, 9), paper=(226, 240),
                   ink=(35, 60), jpeg=(32, 45)),
    "old": dict(dpi=(150, 150), skew=(1.2, 3.0), blur=(0.5, 0.75), noise=(8, 11), paper=(205, 220),
                ink=(50, 70), jpeg=(30, 40)),
    "thermal": dict(dpi=(200, 200), skew=(1.0, 2.5), blur=(0.6, 0.9), noise=(5, 7), paper=(232, 242),
                    ink=(148, 162), jpeg=(35, 45), fade=(85, 115)),
}


def scan_params(preset: str, rng) -> dict:
    p = SCAN_PRESETS[preset]
    out = dict(
        dpi=p["dpi"][0],
        skew=round(rng.uniform(*p["skew"]) * rng.choice([-1, 1]), 2),
        blur=round(rng.uniform(*p["blur"]), 2),
        noise=round(rng.uniform(*p["noise"]), 1),
        paper=rng.randint(*p["paper"]),
        ink=rng.randint(*p["ink"]),
        jpeg_q=rng.randint(*p["jpeg"]),
        holes=(preset != "thermal") and rng.random() < 0.5,
        edge=(preset != "thermal") and rng.random() < 0.35,
    )
    if "fade" in p:
        out["fade"] = rng.randint(*p["fade"])
    return out


def scanned_page(doc, preset: str, rng, rotate: int = 0) -> tuple[bytes, tuple, dict, list]:
    """Rendert Beleg, degradiert ihn wie einen Scan; liefert JPEG, Seitengröße (pt), Parameter, Textzeilen."""
    params = scan_params(preset, rng)
    img, lines = render_doc(doc, params["dpi"])
    g = degrade_scan(img, rng, **params)
    if isinstance(doc, Receipt):
        # Kassenbon liegt auf dem Flachbettscanner: A4-Seite, Bon irgendwo darauf
        dpi = params["dpi"]
        page = Image.new("L", (int(210 / 25.4 * dpi), int(297 / 25.4 * dpi)), 244)
        page = add_noise(page, 3, rng)
        x = int(rng.uniform(25, 95) / 25.4 * dpi)
        y = int(rng.uniform(15, 40) / 25.4 * dpi)
        page.paste(g, (x, y))
        g = jpeg_roundtrip(page, params["jpeg_q"])
    if rotate:
        g = g.rotate(rotate, expand=True)
    dpi = params["dpi"]
    size_pt = (g.width / dpi * 72, g.height / dpi * 72)
    params["rotate"] = rotate
    return jpeg_bytes(g, params["jpeg_q"], dpi=dpi), size_pt, params, lines


def thermal_image(doc: Receipt, rng, dpi=220) -> Image.Image:
    img, _ = render_doc(doc, dpi)
    params = dict(dpi=dpi, skew=0.0, blur=0.7, noise=4, paper=240, ink=rng.randint(145, 158), holes=False,
                  edge=False, jpeg_q=92, fade=rng.randint(85, 110))
    return degrade_scan(img, rng, **params).convert("RGB")


# --------------------------------------------------------------------------------------
# Hauptprogramm
# --------------------------------------------------------------------------------------

def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def write(rel: str, data: bytes) -> Path:
    p = KORPUS / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def heic_from_jpeg(jpg: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.jpg"
        dst = Path(td) / "out.heic"
        src.write_bytes(jpg)
        subprocess.run(["sips", "-s", "format", "heic", str(src), "--out", str(dst)], check=True,
                       capture_output=True)
        return dst.read_bytes()


def build() -> dict:
    register_fonts()
    if KORPUS.exists():
        shutil.rmtree(KORPUS)
    KORPUS.mkdir(parents=True)
    entries = []
    by_path = {}

    for spec in PLAN:
        kind, rel = spec["kind"], spec["path"]
        rng = rng_for("file", rel)
        entry = dict(path=rel, kind=kind, pages=1, tags=list(spec.get("tags", [])), readable=True,
                     expected_error=None, duplicate_of=None, facts=[], params=None)
        doc = DOCS.get(spec.get("doc", ""), None)

        if kind == "digital_pdf":
            data, _ = digital_pdf(doc)
            entry["facts"] = [dict(seite=1, quelle="textlayer", **doc.facts())]

        elif kind == "scan_pdf":
            jpg, size, params, _ = scanned_page(doc, spec["scan"], rng, rotate=spec.get("rotate", 0))
            data = image_pdf([jpg], [size])
            entry["params"] = params
            entry["facts"] = [dict(seite=1, quelle="nur_bild", **doc.facts())]

        elif kind == "garbage_textlayer_pdf":
            jpg, size, params, lines = scanned_page(doc, spec["scan"], rng)
            data = garbage_textlayer_pdf(jpg, lines, rng)
            entry["params"] = params
            entry["tags"].append("unsichtbarer_muell_textlayer")
            entry["facts"] = [dict(seite=1, quelle="nur_bild", **doc.facts())]

        elif kind in ("photo_jpg", "heic"):
            p = dict(spec.get("photo", {}))
            if spec.get("thermal"):
                page = thermal_image(doc, rng)
                p.setdefault("fill", 0.86)
                p.setdefault("blur", 0.9)
            else:
                page, _ = render_doc(doc, 150)
            out = p.pop("out", (1512, 2016))
            quality = p.pop("quality", 85)
            img = photograph(page, rng, out=out, **p)
            dt = datetime.combine(doc.date if isinstance(doc, Invoice) else doc.dt.date(),
                                  datetime.min.time()) + timedelta(hours=18, minutes=rng.randint(0, 59))
            orient = spec.get("exif_orientation")
            if orient == 6:
                img = img.transpose(Image.Transpose.ROTATE_90)  # Pixel quer, Anzeige via EXIF aufrecht
            if "WhatsApp" in rel:
                jpg = jpeg_bytes(img, quality)
            else:
                jpg = jpeg_bytes(img, quality, exif=exif_bytes(dt, orient))
            data = jpg if kind == "photo_jpg" else heic_from_jpeg(jpg)
            entry["params"] = dict(out=list(out), quality=quality, exif_orientation=orient,
                                   **{k: v for k, v in p.items()})
            entry["facts"] = [dict(seite=1, quelle="nur_bild", **doc.facts())]

        elif kind == "png":
            if spec["png"] == "scan":
                params = scan_params("office", rng)
                params["dpi"] = 150
                img, _ = render_doc(doc, 150)
                g = degrade_scan(img, rng, **params)
                g = g.resize((int(g.width * 0.8), int(g.height * 0.8)), Image.LANCZOS)
                b = io.BytesIO()
                g.save(b, "PNG", optimize=False)
                entry["params"] = params
            else:  # Bildschirmfoto eines PDF-Viewers
                img, _ = render_doc(doc, 110)
                Wd, Hd = img.width + 120, img.height + 140
                win = Image.new("RGB", (Wd, Hd), (236, 236, 236))
                d = ImageDraw.Draw(win)
                d.rectangle([0, 0, Wd, 52], fill=(222, 222, 222))
                for i, col in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
                    d.ellipse([18 + i * 30, 17, 36 + i * 30, 35], fill=col)
                d.text((Wd // 2 - 120, 16), "Rechnung Winterdienst.pdf – Seite 1 von 1",
                       font=ImageFont.truetype(PIL_ARIAL, 17), fill=(60, 60, 60))
                win.paste(img, (60, 90))
                crop_h = int(Hd * 0.82)  # unteres Stück abgeschnitten, wie beim Screenshot
                win = win.crop((0, 0, Wd, crop_h))
                b = io.BytesIO()
                win.save(b, "PNG", optimize=False)
                entry["tags"].append("bildschirmfoto")
            data = b.getvalue()
            entry["facts"] = [dict(seite=1, quelle="nur_bild", **doc.facts())]

        elif kind == "tiff_multipage":
            pages = []
            for i, did in enumerate(spec["docs"]):
                d_ = DOCS[did]
                prng = rng_for("tiffpage", rel, i)
                img, _ = render_doc(d_, 200)
                g = degrade_scan(img, prng, dpi=200, skew=round(prng.uniform(-1.2, 1.2), 2), blur=0.5, noise=5,
                                 paper=235, ink=30, holes=False, edge=False, jpeg_q=90)
                g = g.point(lambda v: 255 if v > 150 else 0).convert("1")
                dd = ImageDraw.Draw(g)
                for _ in range(prng.randint(250, 500)):
                    x, y = prng.randrange(g.width), prng.randrange(g.height)
                    dd.point((x, y), fill=0)
                # Faxkopfzeile
                dd.text((40, 12), f"07.02.2022 08:3{i}   FAX +49 123 0000{i}   S. {i + 1}/3",
                        font=ImageFont.truetype(PIL_ARIAL, 26), fill=0)
                pages.append(g)
            b = io.BytesIO()
            pages[0].save(b, "TIFF", compression="group4", save_all=True, append_images=pages[1:], dpi=(200, 200))
            data = b.getvalue()
            entry["pages"] = len(pages)
            entry["facts"] = [dict(seite=i + 1, quelle="nur_bild", **DOCS[d].facts())
                              for i, d in enumerate(spec["docs"])]

        elif kind == "sammel_pdf":
            buf = io.BytesIO()
            c = new_canvas(buf, A4)
            c.setCreator("Scan2PDF Stapel")
            facts = []
            for i, did in enumerate(spec["docs"], 1):
                d_ = DOCS[did]
                if i in spec["scanned_pages"]:
                    jpg, size, params, _ = scanned_page(d_, "office", rng_for("sammel", rel, i))
                    c.setPageSize(size)
                    c.drawImage(ImageReader(io.BytesIO(jpg)), 0, 0, width=size[0], height=size[1])
                    facts.append(dict(seite=i, quelle="nur_bild", **d_.facts()))
                else:
                    c.setPageSize(A4)
                    draw_invoice(Pen(c), d_)
                    facts.append(dict(seite=i, quelle="textlayer", **d_.facts()))
                c.showPage()
            c.save()
            data = buf.getvalue()
            entry["pages"] = len(spec["docs"])
            entry["facts"] = facts

        elif kind == "encrypted_pdf":
            enc = StandardEncryption(spec["password"], ownerPassword="Kanzlei-Owner-77", canPrint=1, canModify=0,
                                     canCopy=0, canAnnotate=0, strength=128)
            data, _ = digital_pdf(doc, encrypt=enc)
            entry.update(readable=False, expected_error="encrypted", password=spec["password"])
            entry["facts"] = [dict(seite=1, quelle="verschluesselt", **doc.facts())]

        elif kind == "corrupt_pdf":
            full, _ = digital_pdf(doc)
            data = full[: int(len(full) * 0.4)]
            entry.update(readable=False, expected_error="corrupt")
            entry["tags"].append("abgeschnitten_40_prozent")
            entry["facts"] = [dict(seite=1, quelle="defekt", **doc.facts())]

        elif kind == "unsupported":
            m = spec["maker"]
            data = {
                "xlsx": fake_xlsx,
                "lock": office_lockfile,
                "msg": lambda: fake_ole(b"__substg1.0_0037001F AW: Rechnung Heizung"),
                "docx": fake_docx,
                "zip": lambda: zip_bytes({"Rechnungen_2022/Liesmich.txt":
                                          "Rechnungen 2022 – siehe Ordner Buchhaltung.\n".encode()}),
                "dsstore": ds_store,
                "thumbs": lambda: fake_ole(b"Thumbs", 2048),
            }[m]()
            entry.update(pages=0, readable=False, expected_error="unsupported" if m not in ("dsstore", "thumbs", "lock")
                         else "junk")

        elif kind == "duplicate_of":
            orig = by_path[spec["of"]]
            data = (KORPUS / spec["of"]).read_bytes()
            entry.update(pages=orig["pages"], duplicate_of=spec["of"], facts=[dict(f) for f in orig["facts"]],
                         tags=["byte_identisch"])
        else:
            raise ValueError(kind)

        write(rel, data)
        entry["sha256"] = sha256(data)
        entry["size_bytes"] = len(data)
        entries.append(entry)
        by_path[rel] = entry

    return {"entries": entries, "by_path": by_path}


# --------------------------------------------------------------------------------------
# Suchanfragen
# --------------------------------------------------------------------------------------

def locate(entries) -> dict[str, tuple[str, int]]:
    loc = {}
    for e in entries:
        if e["kind"] == "duplicate_of":
            continue
        for f in e["facts"]:
            loc[f["doc_id"]] = (e["path"], f["seite"])
    return loc


def g_text(did):
    return DOCS[did].facts()["gesamtbetrag_text"]


def g_plain(did):
    return de_number(DOCS[did].gross if isinstance(DOCS[did], Invoice) else DOCS[did].gross, thousands=False)


def dev_queries() -> list[tuple[str, str, str]]:
    return [
        # exakt
        ("RE-2024-00031", "brandl_2024", "exakt: Rechnungsnummer mit - statt / (Dokument: RE-2024/00031)"),
        (g_plain("vogt_2019"), "vogt_2019",
         f"exakt: Gesamtbetrag ohne Tausenderpunkt und ohne € (Dokument: {g_text('vogt_2019')})"),
        ("INV88213", "kranich_2020", "exakt: Rechnungsnummer ohne Bindestrich (Dokument: INV-88213)"),
        ("4711-22", "vogt_2022",
         "exakt+dublette: Nr. 4711/22 mit - statt /. Byte-identische Kopien Buchhaltung/Erledigt/2022/4711_22.pdf "
         "und Mails/Anhänge/Rechnung 4711-22.pdf gelten ebenfalls als Treffer"),
        ("HM 10233", "petersen_2021", "exakt: Rechnungsnummer mit Leerzeichen statt Bindestrich (Dokument: HM-10233)"),
        (g_plain("stadtwerke_2019").replace(",", "."), "stadtwerke_2019",
         f"exakt: Betrag mit Dezimalpunkt statt Komma (Dokument: {g_text('stadtwerke_2019')})"),
        # name
        ("Schornsteinfger Kastanienweg", "albrecht_2018", "name: Tippfehler Schornsteinfeger + Objekt"),
        ("Mueller Gastherme", "mueller_2019",
         "name: ue statt ü (Dokument: Haustechnik Müller GmbH, nur die E-Mail-Domain schreibt 'mueller')"),
        ("Weissenfels Mieterhöhung", "weissenfels_2021",
         "name: ss statt ß (Dokument: Weißenfels & Partner, nur die E-Mail-Domain schreibt 'weissenfels')"),
        ("Sonleitner Balkontür", "sonnleitner_2023", "name: Tippfehler im Firmennamen (Sonnleitner)"),
        ("Kammerjäger Rieger", "rieger_2022",
         "name: Firmenname + Branchenwort aus dem Briefkopf, Beleg ist PNG (Mail-Inlinebild, ohne Text)"),
        ("Thermometrik Heizkosten", "thermometrik_2022",
         "name: Firma + Dokumentart (zwei Thermometrik-Belege, nur einer betrifft Heizkosten)"),
        # beschreibend
        ("Wartung Heizung Musterstraße", "brandl_2021",
         "beschreibend: Dokument sagt 'Heizungswartung' und 'Musterstr. 12' (Kompositum + Abkürzung, "
         "Ordnername enthält aber 'Musterstraße 12')"),
        ("Hausmeister Lindenweg 2022", "krueger_2022", "beschreibend: Dokument sagt 'Hauswart', nicht 'Hausmeister'"),
        ("Treppenhaus streichen Lindenweg", "lindqvist_2018", "beschreibend: Malerarbeiten 2018, Betreffzeile"),
        ("Nebenkosten Birkenallee 2019", "stadtwerke_2020_bk",
         "beschreibend: Dokument heißt 'Betriebskostenabrechnung 2019' (Synonym Nebenkosten)"),
        ("Rauchmelder Lindenweg", "thermometrik_2024",
         "beschreibend: Dokument sagt 'Rauchwarnmelder' (Kompositum-Variante)"),
        ("Wallbox Tiefgarage", "vogt_2024", "beschreibend: seltene Leistung, nichtssagender Dateiname"),
        # schwer
        ("Sturmschaden Dach Birkenallee", "hoffmannbeck_2021", "schwer: Scan-PDF ohne Textlayer, Seite um 90° gedreht"),
        ("Grundreinigung Kastanienweg", "glanzwerk_2022",
         "schwer: Scan mit unsichtbarer Müll-Textebene (kaputtes Scanner-OCR), Inhalt nur im Bild"),
        ("Thermostatventile Am Hang", "mueller_2021", "schwer: Seite 4 im Sammel-PDF (gescannte Seite)"),
        ("Orion Rohrreinigung", "orion_2021", "schwer: Firmenname nur im Logo-Bild und in winziger Fußzeile"),
        ("Baumarkt Streusalz", "bauwelt_2023", "schwer: verblasster Thermo-Kassenbon als Handyfoto (JPG)"),
        ("Winterdienst Musterstraße", "petersen_2022", "schwer: Seite 3 im mehrseitigen Fax-TIFF (1-bit, G4)"),
        ("Aufzug Türantrieb", "kranich_2023", "schwer: HEIC-Handyfoto"),
        ("Schlüsseldienst Mühlenstraße", "yilmaz_2021",
         "schwer: Handyfoto-JPG mit EXIF-Orientierung 6 (Pixel liegen quer)"),
    ]


def holdout_queries() -> list[tuple[str, str, str]]:
    return [
        # exakt
        ("GW-2019-118", "gruenwerk_2019", "exakt: Rechnungsnummer anders getrennt (Dokument: GW 2019/118), Handyfoto"),
        ("EUR " + g_plain("stadtwerke_2025_bk"), "stadtwerke_2025_bk",
         f"exakt: Betrag mit vorangestelltem EUR, ohne Tausenderpunkt (Dokument: {g_text('stadtwerke_2025_bk')})"),
        ("GR 24-0102", "glanzwerk_2024", "exakt: Rechnungsnummer mit Leerzeichen (Dokument: GR-24-0102), HEIC-Foto"),
        ("M 2022 012", "lindqvist_2022", "exakt: Rechnungsnummer ohne Bindestriche, Seite 1 im Fax-TIFF"),
        # name
        ("Müller Rohrbruch", "mueller_2023", "name: Suche mit ü, Dokument schreibt durchgehend 'Mueller' (ASCII)"),
        ("Kaminsky Fensterbau", "kaminski_2024", "name: Tippfehler y statt i, Firmenname nur im Logo + Fußzeile"),
        ("Petersen Winterdienst Mühlenstraße", "petersen_2023", "name: Firma + Leistung + Objekt, PNG-Bildschirmfoto"),
        # beschreibend
        ("Gebäudeversicherung Kastanienweg", "nordlicht_2025",
         "beschreibend: Dokument sagt 'Wohngebäudeversicherung' (Teilwort eines Kompositums)"),
        ("Glasbruch Haustür", "sonnleitner_2020", "beschreibend: Scan-PDF, Dateiname Scan_00487 irreführend"),
        ("Abgasmessung 2022", "albrecht_2022", "beschreibend: Scan-PDF, Leistung + Jahr"),
        ("Wasserschaden Decke Am Hang", "lindqvist_2024", "beschreibend: WhatsApp-Foto ohne EXIF"),
        ("Dachrinne Musterstraße", "hoffmannbeck_2024",
         "beschreibend: Dokument und Ordner schreiben nur 'Musterstr. 12', Dokument sagt 'Dachrinnen'"),
        # schwer
        ("Strom Lindenweg 2022", "stadtwerke_2023",
         "schwer: Scan-PDF auf dem Kopf (180°), Dokument sagt 'Allgemeinstrom' / 'Jahresabrechnung Strom 2022'"),
        ("Rückschnitt Kirschlorbeer", "gruenwerk_2023", "schwer: Scan mit unsichtbarer Müll-Textebene"),
        ("Türsprechanlage Lindenweg", "vogt_2021", "schwer: Seite 2 im Sammel-PDF (gescannte Seite)"),
        ("Tankstelle Kanister", "brueckner_2024", "schwer: verblasster Thermo-Kassenbon als Scan-PDF"),
    ]


def write_queries(path: Path, queries, loc) -> list[dict]:
    rows = []
    for suche, did, notiz in queries:
        datei, seite = loc[did]
        assert ";" not in suche and ";" not in notiz and ";" not in datei, (suche, notiz, datei)
        rows.append(dict(suche=suche, datei=datei, seite=seite, notiz=notiz))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        w.writerow(["suche", "datei", "seite", "notiz"])
        for r in rows:
            w.writerow([r["suche"], r["datei"], r["seite"], r["notiz"]])
    return rows


def category_counts(rows) -> dict:
    out = {}
    for r in rows:
        cat = r["notiz"].split(":", 1)[0].split("+")[0].strip()
        out[cat] = out.get(cat, 0) + 1
    return out


# --------------------------------------------------------------------------------------
# README
# --------------------------------------------------------------------------------------

def write_readme(entries, dev_rows, hold_rows) -> None:
    kinds = {}
    for e in entries:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    total = sum(e["size_bytes"] for e in entries)
    n_pages = sum(len(e["facts"]) for e in entries if e["kind"] != "duplicate_of")
    firmen = sorted({DOCS[f["doc_id"]].co for e in entries for f in e["facts"]})

    def paths(pred):
        return [e["path"] for e in entries if pred(e)]

    hard = [
        ("Scan-PDF um 90° gedreht", paths(lambda e: "rotated_90" in e["tags"])),
        ("Scan-PDF um 180° gedreht", paths(lambda e: "rotated_180" in e["tags"])),
        ("Thermo-Kassenbon (verblasst)", paths(lambda e: "thermal_receipt" in e["tags"])),
        ("Handyfoto mit EXIF-Orientierung 6", paths(lambda e: "exif_orientation_6" in e["tags"])),
        ("HEIC", paths(lambda e: e["kind"] == "heic")),
        ("PNG", paths(lambda e: e["kind"] == "png")),
        ("Mehrseitiges TIFF (3 Seiten, 1-bit G4, Fax)", paths(lambda e: e["kind"] == "tiff_multipage")),
        ("Sammel-PDF (6 Belege, Seiten 2/4/6 gescannt)", paths(lambda e: e["kind"] == "sammel_pdf")),
        ("Scan + unsichtbare Müll-Textebene", paths(lambda e: e["kind"] == "garbage_textlayer_pdf")),
        ("Firmenname nur im Logo + Fußzeile", paths(lambda e: "logo_only_company" in e["tags"])),
        ("ASCII-Umlaute (Mueller, Strasse)", paths(lambda e: "ascii_umlaute_mueller" in e["tags"])),
        ("Abgekürzte Straße (Musterstr.)", paths(lambda e: "abgekuerzte_strasse" in e["tags"])),
        ("Hauswart statt Hausmeister", paths(lambda e: "hauswart_statt_hausmeister" in e["tags"])),
        ("Verschlüsselt (Benutzerpasswort)", paths(lambda e: e["kind"] == "encrypted_pdf")),
        ("Defekt (auf 40 % abgeschnitten)", paths(lambda e: e["kind"] == "corrupt_pdf")),
        ("Nicht unterstützt / Junk", paths(lambda e: e["kind"] == "unsupported")),
        ("Dubletten (byte-identisch)", [f"{e['path']}  (= {e['duplicate_of']})" for e in entries
                                        if e["kind"] == "duplicate_of"]),
    ]
    lines = [
        "# Testkorpus belegsuche",
        "",
        "Synthetischer, vollständig **fiktiver** Korpus deutscher Hausverwaltungs- und Haushaltsbelege",
        "(Handwerker, Hauswart/Hausmeister, Stadtwerke, Versicherung, Schornsteinfeger, Anwalt,",
        "Kassenbons …) zum Testen von Indexierung, OCR und Suche.",
        "",
        "Erzeugt mit `.venv/bin/python tools/make_testdata.py` (deterministisch, Seed "
        f"{SEED}; `testdata/korpus/` wird bei jedem Lauf gelöscht und neu erzeugt).",
        "Prüfen mit `.venv/bin/python tools/check_testdata.py` (Optionen: `--determinism`, `--ocr`, `--render DIR`).",
        "",
        "Alle Firmen, Personen, Adressen und Bankdaten sind erfunden. IBANs haben die Prüfziffer `00`",
        "(ungültig), Web/E-Mail nutzen die reservierte TLD `.example`.",
        "",
        "## Umfang",
        "",
        f"- {len(entries)} Dateien, {total / 1e6:.1f} MB, {n_pages} inhaltliche Belegseiten, {len(firmen)} Firmen",
        "- Zeitraum 2018–2025, Objekte: Musterstraße 12, Lindenweg 3, Am Hang 7, Birkenallee 21,",
        "  Kastanienweg 5a, Mühlenstraße 40",
        "- Verschachtelte, unordentliche Ordner (Umlaute, Leerzeichen, `&`, `+`), überwiegend",
        "  nichtssagende Dateinamen (`scan0001.pdf` kommt dreimal vor, `Unbenannt.pdf` zweimal).",
        "",
        "| kind | Anzahl | Bedeutung |",
        "|---|---:|---|",
    ]
    desc = {
        "digital_pdf": "PDF mit sauberem Textlayer",
        "scan_pdf": "reine Bild-PDF (Scan, verrauscht, schief, JPEG-Artefakte), kein Textlayer",
        "photo_jpg": "Handyfoto (Perspektive, Hintergrund, Licht)",
        "png": "PNG (Mail-Inlinebild bzw. Bildschirmfoto)",
        "heic": "HEIC-Handyfoto (via `sips`)",
        "tiff_multipage": "mehrseitiges TIFF, pro Seite ein anderer Beleg",
        "sammel_pdf": "mehrere Belege in einer PDF, digitale und gescannte Seiten gemischt",
        "garbage_textlayer_pdf": "Scan + unsichtbare, falsche OCR-Textebene",
        "encrypted_pdf": "passwortgeschützt – soll im Fehlerbericht landen",
        "corrupt_pdf": "abgeschnittene PDF – soll im Fehlerbericht landen",
        "unsupported": "xlsx/docx/msg/zip und Junk (.DS_Store, Thumbs.db, Office-Lockdatei)",
        "duplicate_of": "byte-identische Kopie einer anderen Datei",
    }
    for k in desc:
        if k in kinds:
            lines.append(f"| `{k}` | {kinds[k]} | {desc[k]} |")
    lines += ["", "## Schwierige Fälle", ""]
    for title, ps in hard:
        lines.append(f"- **{title}**")
        for p in ps:
            lines.append(f"  - `{p}`")
    lines += [
        "",
        "## manifest.json",
        "",
        "Ein Eintrag pro Datei: `path` (relativ zu `korpus/`), `kind`, `pages`, `tags`, `readable`,",
        "`expected_error` (`encrypted`, `corrupt`, `unsupported`, `junk` oder `null`), `duplicate_of`,",
        "`sha256`, `size_bytes`, `params` (Degradierungsparameter), `password` (nur `encrypted_pdf`)",
        "und `facts` – pro Seite: `seite`,",
        "`quelle` (`textlayer`, `nur_bild`, `verschluesselt`, `defekt`), `doc_id`, `firma` (wie gedruckt),",
        "`rechnungsnummer`, `datum` (ISO), `datum_text` (wie gedruckt), `gesamtbetrag` (Punkt-Dezimal),",
        "`gesamtbetrag_text` (wie gedruckt), `leistung`, `objektadresse` (wie gedruckt, ggf. abgekürzt),",
        "`objektadresse_normiert`.",
        "",
        "Hinweis: Bei Stadtwerke-Jahresabrechnungen ist `gesamtbetrag` der Rechnungsbetrag brutto (vor",
        "Abzug der Abschläge); die Nachzahlung steht zusätzlich im Dokument.",
        "",
        "## Suchanfragen",
        "",
        "`queries_dev.csv` und `queries_holdout.csv`: UTF-8, Semikolon, Kopfzeile `suche;datei;seite;notiz`.",
        "`datei` ist das EINE erwartete Dokument (relativ zu `korpus/`), `seite` 1-basiert (bei",
        "Einzelseiten immer 1). `notiz` beginnt mit der Kategorie: `exakt`, `name`, `beschreibend`, `schwer`.",
        "",
        f"- `queries_dev.csv`: {len(dev_rows)} Anfragen – " +
        ", ".join(f"{k} {v}" for k, v in category_counts(dev_rows).items()),
        f"- `queries_holdout.csv`: {len(hold_rows)} Anfragen – " +
        ", ".join(f"{k} {v}" for k, v in category_counts(hold_rows).items()),
        "",
        "**Die Holdout-Datei ist für eine unabhängige Evaluation reserviert:** Suche und Ranking werden",
        "nicht anhand dieser Anfragen abgestimmt. Sie zielt – bis auf TIFF und Sammel-PDF (andere Seiten) –",
        "auf andere Dokumente als die Dev-Datei.",
        "",
        "Dubletten: Eine Dev-Anfrage zielt auf eine Datei mit byte-identischen Kopien; laut `notiz`",
        "zählt ein Treffer auf eine der Kopien ebenfalls als korrekt. Verschlüsselte, defekte und nicht",
        "unterstützte Dateien sind nie Ziel einer Anfrage.",
        "",
    ]
    README.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    res = build()
    entries = res["entries"]
    loc = locate(entries)
    dev_rows = write_queries(DEV_CSV, dev_queries(), loc)
    hold_rows = write_queries(HOLDOUT_CSV, holdout_queries(), loc)
    manifest = {
        "generator": "tools/make_testdata.py",
        "seed": SEED,
        "korpus_root": "testdata/korpus",
        "hinweis": "Alle Daten fiktiv. Pfade relativ zu korpus_root.",
        "anzahl_dateien": len(entries),
        "documents": entries,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    write_readme(entries, dev_rows, hold_rows)
    kinds = {}
    for e in entries:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    total = sum(e["size_bytes"] for e in entries)
    print(f"{len(entries)} Dateien, {total / 1e6:.1f} MB in {KORPUS.relative_to(ROOT)}")
    for k, v in kinds.items():
        print(f"  {k:24s} {v}")
    print(f"Dev-Anfragen: {len(dev_rows)}  Holdout-Anfragen: {len(hold_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
