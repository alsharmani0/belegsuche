"""Kleiner Test-Bestand, der bei jedem Testlauf frisch erzeugt wird."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"


def _font(size: int):
    return ImageFont.truetype(FONT, size)


def text_image(lines: list[str], size=(1654, 2339), font_size=34, degrade=False) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    y = 150
    for line in lines:
        d.text((140, y), line, fill="black", font=_font(font_size))
        y += int(font_size * 1.8)
    if degrade:
        img = img.rotate(1.5, expand=False, fillcolor="white").filter(ImageFilter.GaussianBlur(0.8))
    return img


def digital_pdf(path: Path, pages: list[list[str]], password: str | None = None) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    if "Arial" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Arial", FONT))
    kw = {}
    if password:
        from reportlab.lib import pdfencrypt
        kw["encrypt"] = pdfencrypt.StandardEncryption(password, canPrint=0)
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1, **kw)
    for lines in pages:
        c.setFont("Arial", 11)
        y = 780
        for line in lines:
            c.drawString(60, y, line)
            y -= 18
        c.showPage()
    c.save()


def scan_pdf(path: Path, pages: list[list[str]]) -> None:
    imgs = [text_image(p, degrade=True).convert("L").convert("RGB") for p in pages]
    imgs[0].save(path, "PDF", resolution=200, save_all=True, append_images=imgs[1:])


INVOICE_HEIZUNG = [
    "Wärmetechnik Sommer GmbH · Gewerbering 14 · 12345 Musterstadt",
    "Rechnung Nr. RE-2021/00487",
    "Datum: 12.03.2021",
    "Objekt: Musterstr. 12, 12345 Musterstadt",
    "Leistung: Heizungswartung Brennwertkessel inkl. Abgasmessung",
    "Gesamtbetrag: 1.234,56 €",
]
INVOICE_HAUSWART = [
    "Hauswart Service Petersen",
    "Rechnung 2022-118",
    "Lindenweg 3, Musterstadt",
    "Hauswart-Tätigkeiten Juni 2022, Treppenhausreinigung",
    "Summe EUR 389,90",
]
INVOICE_MUELLER = [
    "Elektro Müller KG",
    "Rechnung INV-88213 vom 05.07.2020",
    "Austausch Sicherungskasten, Am Hang 7",
    "Betrag: 642,00 €",
]
SCAN_SCHORNSTEIN = [
    "Bezirksschornsteinfeger Max Probe",
    "Gebührenbescheid Feuerstättenschau",
    "Birkenallee 21 Musterstadt",
    "Rechnungsnummer 4711/22",
    "Zu zahlen: 97,40 EUR",
]
PHOTO_BON = [
    "BAUMARKT NORD",
    "Schrauben 4x40   3,99",
    "Silikon weiss    7,49",
    "SUMME EUR       11,48",
]
SAMMEL = [
    ["Versicherung Hanse AG", "Beitragsrechnung 2023", "Gebäudeversicherung Musterstraße 12", "Beitrag 812,00 €"],
    ["Gartenpflege Grünwerk", "Rechnung GW-5521", "Rasenmähen Lindenweg 3", "Betrag 145,00 €"],
    ["Aufzug Nord GmbH", "Wartungsrechnung A-77/2023", "Aufzugsanlage Am Hang 7", "Summe 530,00 €"],
]


def build_corpus(root: Path) -> dict[str, Path]:
    files = {}
    (root / "2021" / "Objekte" / "Musterstraße 12").mkdir(parents=True)
    (root / "Scans unsortiert").mkdir(parents=True)
    (root / "Diverses" / "alt").mkdir(parents=True)
    files["heizung"] = root / "2021" / "Objekte" / "Musterstraße 12" / "Dokument (3).pdf"
    digital_pdf(files["heizung"], [INVOICE_HEIZUNG])
    files["hauswart"] = root / "Diverses" / "20220630_1422.pdf"
    digital_pdf(files["hauswart"], [INVOICE_HAUSWART])
    files["mueller"] = root / "Diverses" / "alt" / "Unbenannt.pdf"
    digital_pdf(files["mueller"], [INVOICE_MUELLER])
    files["scan"] = root / "Scans unsortiert" / "Scan_00487.pdf"
    scan_pdf(files["scan"], [SCAN_SCHORNSTEIN])
    files["bon"] = root / "Scans unsortiert" / "IMG_2231.png"
    text_image(PHOTO_BON, size=(900, 700), font_size=36).save(files["bon"])
    files["sammel"] = root / "Scans unsortiert" / "scan0001.pdf"
    scan_pdf(files["sammel"], SAMMEL)
    files["encrypted"] = root / "Diverses" / "geheim.pdf"
    digital_pdf(files["encrypted"], [["Vertraulich"]], password="1234")
    good = files["mueller"].read_bytes()
    files["corrupt"] = root / "Diverses" / "kaputt.pdf"
    files["corrupt"].write_bytes(good[: len(good) // 3])
    files["xlsx"] = root / "Diverses" / "Liste.xlsx"
    files["xlsx"].write_bytes(b"PK\x03\x04 kein echtes excel")
    (root / "Diverses" / ".DS_Store").write_bytes(b"\x00\x00")
    return files


def tree_state(root: Path) -> dict[str, tuple]:
    """Größe, Änderungszeit und Hash aller Dateien – um zu prüfen, dass nichts verändert wurde."""
    out = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = Path(dirpath) / n
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


class CountingEngine:
    """Echte Apple-OCR, zählt aber die Aufrufe (für Tests zu Duplikaten/Verschieben)."""

    def __init__(self, fail_after: int | None = None):
        from belegsuche.ocr import AppleVisionOCR
        self.inner = AppleVisionOCR()
        self.calls = 0
        self.fail_after = fail_after

    def _tick(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise KeyboardInterrupt

    def recognize_image(self, img):
        self._tick()
        return self.inner.recognize_image(img)

    def recognize_file(self, path):
        self._tick()
        return self.inner.recognize_file(path)


_vision_ok: bool | None = None


def _vision_reads_text() -> bool:
    """Liest Vision auf diesem Rechner Text? Direkt über pyobjc, damit ein Fehler in belegsuche.ocr
    nicht als „Vision fehlt“ durchgeht."""
    try:
        import io

        import Vision
        from Foundation import NSData
        buf = io.BytesIO()
        text_image(["Rechnung 4711"], size=(900, 300), font_size=60).save(buf, "PNG")
        raw = buf.getvalue()
        data = NSData.dataWithBytes_length_(raw, len(raw))
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        ok, _ = handler.performRequests_error_([req], None)
        found = " ".join(str(c[0].string()) for o in (req.results() or []) if (c := o.topCandidates_(1)))
        return bool(ok) and "4711" in found
    except Exception:
        return False


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Tests mit @pytest.mark.vision brauchen Apples Texterkennung. Normalerweise laufen sie immer.
    Nur wenn BELEGSUCHE_TEST_VISION_OPTIONAL=1 gesetzt ist (CI) und Vision dort keinen Text liest,
    werden sie übersprungen – mit Begründung in der Ausgabe."""
    global _vision_ok
    if item.get_closest_marker("vision") is None or os.environ.get("BELEGSUCHE_TEST_VISION_OPTIONAL") != "1":
        return
    if _vision_ok is None:
        _vision_ok = _vision_reads_text()
    if not _vision_ok:
        pytest.skip("Apples Vision-Texterkennung liest auf diesem Rechner keinen Text")


@pytest.fixture()
def corpus(tmp_path):
    root = tmp_path / "korpus"
    root.mkdir()
    files = build_corpus(root)
    return root, files


@pytest.fixture()
def indexed(corpus, tmp_path):
    from belegsuche import db as dbm
    from belegsuche.indexer import run_index
    root, files = corpus
    db_path = tmp_path / "index.db"
    con = dbm.connect(db_path)
    engine = CountingEngine()
    run_index(con, [str(root)], engine=engine, log=lambda s: None)
    return root, files, db_path, con, engine
