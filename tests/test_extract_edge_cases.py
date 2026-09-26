"""Regressionstests für Texterkennung und PDF-Auswertung:
A) HEIC mit Tesseract, B) kleine eingebettete Bilder auf digitalen Seiten, C) kurze saubere Textebene."""
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from belegsuche import extract as ex
from belegsuche import ocr
from belegsuche.extract import ExtractError, extract, extract_with_notes
from belegsuche.ocr import OcrResult

from .conftest import FONT, CountingEngine, text_image, tree_state

SIPS = Path("/usr/bin/sips")
needs_sips = pytest.mark.skipif(not SIPS.exists(), reason="sips (macOS) fehlt")
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract nicht installiert")

MAIL = ["Von Buchhaltung Hausverwaltung Nord", "Betreff Rechnung fuer Reparatur", "Anbei erhalten Sie die Rechnung",
        "Bitte den Gesamtbetrag zahlen", "Mit freundlichen Gruessen Erika Beispiel"]


class FakeEngine:
    """Ersatz-OCR ohne Vision: fester Text, merkt sich die Größe jedes erkannten Bildes."""

    def __init__(self, text="Dachdecker Kowalski", conf=0.9):
        self.text, self.conf, self.sizes = text, conf, []

    def recognize_image(self, img):
        self.sizes.append(img.size)
        return OcrResult(self.text, self.conf)

    def recognize_file(self, path):
        return OcrResult(self.text, self.conf)


def pdf_page(path: Path, lines: list[str], images=(), font_size=11, rotate=0, form=None) -> None:
    """Digitale A4-Seite mit Textebene; images: [(Bild, (x, y, b, h))]; form: (Bild, x, y, Faktor) als XObject."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    if "Arial" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Arial", FONT))
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    if form:
        img, x, y, k = form
        c.beginForm("logo")
        c.drawImage(ImageReader(img), 0, 0, 100, 40)
        c.endForm()
        c.saveState()
        c.translate(x, y)
        c.scale(k, k)
        c.doForm("logo")
        c.restoreState()
    c.setFont("Arial", font_size)
    for n, line in enumerate(lines):
        c.drawString(60, 780 - n * 18, line)
    for img, box in images:
        c.drawImage(ImageReader(img), *box)
    c.showPage()
    c.save()
    if rotate:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        pdf[0].set_rotation(rotate)
        tmp = path.with_suffix(".tmp.pdf")
        pdf.save(str(tmp))
        pdf.close()
        tmp.replace(path)


def receipt(size=(1400, 900), lines=("Rechnung KV-778899", "Betrag 1845 EUR"), font_size=65) -> Image.Image:
    return text_image(list(lines), size=size, font_size=font_size)


def logo(text: str) -> Image.Image:
    return text_image([text], size=(1000, 300), font_size=70)


# --- A) HEIC mit Tesseract: Formatproblem, kein OCR-Problem ---------------------------------

def make_heic(folder: Path) -> Path:
    png = folder / "beleg.png"
    receipt().save(png)
    heic = folder / "beleg.heic"
    subprocess.run([str(SIPS), "-s", "format", "heic", "--out", str(heic), str(png)], check=True, capture_output=True)
    return heic


@needs_sips
@needs_tesseract
def test_tesseract_reads_heic_via_temp_png(tmp_path, monkeypatch):
    src = tmp_path / "belege"
    src.mkdir()
    heic = make_heic(src)
    before = tree_state(src)
    systemp = tmp_path / "systemp"
    systemp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systemp))
    pages, _ = extract_with_notes(heic, ocr.TesseractOCR())
    assert "KV-778899" in pages[0].text and pages[0].source == "ocr"
    assert tree_state(src) == before                # Original unverändert, nichts daneben angelegt
    assert list(systemp.iterdir()) == []            # Temp-Datei im System-Temp-Ordner wieder gelöscht


@needs_sips
@needs_tesseract
def test_unreadable_image_is_format_error_with_tesseract(tmp_path):
    bad = tmp_path / "kaputt.heic"
    bad.write_bytes(b"\x00\x01kein Bild" * 500)
    with pytest.raises(ExtractError, match="^Bildformat nicht lesbar"):
        extract(bad, ocr.TesseractOCR())


@needs_sips
def test_unreadable_image_is_format_error_with_apple(tmp_path):
    bad = tmp_path / "kaputt.heic"
    bad.write_bytes(b"\x00\x01kein Bild" * 500)
    with pytest.raises(ExtractError, match="^Bildformat nicht lesbar"):
        extract(bad, CountingEngine())


# --- B) Kleine eingebettete Bilder auf digitalen Seiten -------------------------------------

@pytest.mark.vision
def test_embedded_receipt_below_coverage_threshold_is_read(tmp_path, monkeypatch):
    path = tmp_path / "mail.pdf"
    pdf_page(path, MAIL, [(receipt(), (70, 330, 235, 190))])          # 8,9 % der Seite
    before = tree_state(tmp_path)
    renders = []
    orig_render = ex._render
    monkeypatch.setattr(ex, "_render", lambda page: renders.append(1) or orig_render(page))
    engine = CountingEngine()
    pages = extract(path, engine)
    assert pages[0].source == "text+ocr"
    assert "KV-778899" in pages[0].text and "Erika Beispiel" in pages[0].text
    assert engine.calls == 1 and len(renders) == 1                    # nur der Bildausschnitt, keine Voll-OCR
    assert tree_state(tmp_path) == before


@pytest.mark.vision
def test_logo_with_company_name_is_read(tmp_path):
    path = tmp_path / "rechnung.pdf"
    pdf_page(path, MAIL, [(logo("Dachdecker Kowalski"), (330, 760, 220, 66))])   # ca. 2,9 %
    pages = extract(path, CountingEngine())
    assert pages[0].source == "text+ocr" and "Kowalski" in pages[0].text


@pytest.mark.vision
def test_page_rendered_once_for_several_images(tmp_path, monkeypatch):
    path = tmp_path / "zwei.pdf"
    pdf_page(path, MAIL, [(logo("Dachdecker Kowalski"), (330, 760, 220, 66)),
                          (receipt(), (70, 330, 235, 190))])
    renders = []
    orig_render = ex._render
    monkeypatch.setattr(ex, "_render", lambda page: renders.append(1) or orig_render(page))
    engine = CountingEngine()
    pages = extract(path, engine)
    assert "Kowalski" in pages[0].text and "KV-778899" in pages[0].text
    assert len(renders) == 1 and engine.calls == 2
    assert pages[0].text.index("Kowalski") < pages[0].text.index("KV-778899")   # oben vor unten


@pytest.mark.vision
def test_image_split_into_strips_counts_as_one(tmp_path):
    img = receipt()
    strips = []
    for k in range(6):                         # manche Programme zerlegen Bilder in Streifen (je < 2 %)
        part = img.crop((0, k * 150, 1400, (k + 1) * 150))
        strips.append((part, (70, 330 + (5 - k) * 190 / 6, 235, 190 / 6)))
    path = tmp_path / "streifen.pdf"
    pdf_page(path, MAIL, strips)
    engine = CountingEngine()
    pages = extract(path, engine)
    assert "KV-778899" in pages[0].text and engine.calls == 1


def test_tiny_icons_and_lines_are_skipped(tmp_path):
    path = tmp_path / "icons.pdf"
    icon = text_image(["@"], size=(120, 120), font_size=60)
    line = Image.new("RGB", (2000, 10), "black")
    pdf_page(path, MAIL, [(icon, (60, 60, 40, 40)), (line, (60, 500, 480, 3)), (icon, (500, 60, 80, 80))])
    engine = FakeEngine()
    pages = extract(path, engine)
    assert pages[0].source == "text" and engine.sizes == []


def test_at_most_six_largest_images(tmp_path):
    path = tmp_path / "sieben.pdf"
    boxes = [(x, y, 150, 70) for y in (100, 200, 300) for x in (60, 320)] + [(60, 400, 150, 68)]
    pdf_page(path, MAIL, [(logo(f"Bild {k}"), b) for k, b in enumerate(boxes)])   # je gut 2 %, zusammen < 15 %
    engine = FakeEngine()
    pages = extract(path, engine)
    assert pages[0].source == "text+ocr" and len(engine.sizes) == 6
    assert min(h for _, h in engine.sizes) > 68 * 300 / 72 + 2 * ex.CROP_BORDER + 3   # kleinstes Bild fehlt


@pytest.mark.vision
def test_rotated_page_crops_the_right_area(tmp_path):
    path = tmp_path / "quer.pdf"
    pdf_page(path, MAIL, [(logo("Dachdecker Kowalski"), (330, 760, 220, 66))], rotate=90)
    pages = extract(path, CountingEngine())
    assert "Kowalski" in pages[0].text


@pytest.mark.vision
def test_image_inside_form_xobject_is_located(tmp_path):
    path = tmp_path / "form.pdf"
    pdf_page(path, MAIL, form=(logo("Dachdecker Kowalski"), 300, 400, 2.4))   # 240 x 96 pt, ca. 4,6 %
    pages = extract(path, CountingEngine())
    assert "Kowalski" in pages[0].text


def test_uncertain_logo_does_not_mark_page_as_uncertain(tmp_path):
    path = tmp_path / "logo.pdf"
    pdf_page(path, MAIL, [(logo("Kowalski"), (330, 760, 220, 66))])
    short = extract(path, FakeEngine("Kowa1ski", conf=0.3))[0]
    assert short.source == "text+ocr" and short.ocr_conf is None      # kurzer Bildtext: nicht im Bericht
    long_text = "Rechnung Dachdecker Kowalski Betrag 1845 EUR Lindenweg 3"
    photo = extract(path, FakeEngine(long_text, conf=0.3))[0]
    assert photo.ocr_conf == pytest.approx(0.3)                        # längerer Bildtext: schon


def test_without_engine_no_render(tmp_path, monkeypatch):
    path = tmp_path / "mail.pdf"
    pdf_page(path, MAIL, [(receipt(), (70, 330, 235, 190))])
    monkeypatch.setattr(ex, "_render", lambda page: pytest.fail("ohne OCR nicht rendern"))
    pages = extract(path, None)
    assert pages[0].source == "text" and "Erika Beispiel" in pages[0].text


# --- C) Kurze, aber saubere Textebene -------------------------------------------------------

@pytest.mark.vision
def test_short_clean_layer_is_kept(tmp_path):
    path = tmp_path / "klein.pdf"
    pdf_page(path, ["RE-2021/00487"], font_size=6)
    before = tree_state(tmp_path)
    pages = extract(path, CountingEngine())
    assert pages[0].source == "text+ocr" and "RE-2021/00487" in pages[0].text
    assert tree_state(tmp_path) == before


def test_short_clean_layer_kept_without_engine(tmp_path):
    path = tmp_path / "klein.pdf"
    pdf_page(path, ["RE-2021/00487"], font_size=6)
    pages = extract(path, None)
    assert pages[0].source == "text" and pages[0].text.strip() == "RE-2021/00487"


def test_short_garbage_layer_is_dropped(tmp_path):
    path = tmp_path / "salat.pdf"
    pdf_page(path, ["Kro5tebn1ux9 q@vmlz"])
    pages = extract(path, FakeEngine("Quittung Baumarkt Nord Musterstadt Schrauben Silikon Summe 11,48 EUR"))
    assert pages[0].source == "ocr" and "Kro5tebn1ux9" not in pages[0].text


@pytest.mark.parametrize("text,ok", [
    ("RE-2021/00487", True), ("Seite 1 von 2", True), ("Rechnung 4711/22", True),
    ("Kro5tebn1ux9", False), ("xKqZa Wqrtz", False), ("l l1 ,", False), ("", False),
    ("\x01\x02\x03\x04 abc", False), ("��� 12345", False), ("#$%&'()*", False),
    ("Wärmetechnik Sommer GmbH Musterstadt", False),  # nicht kurz: gilt über layer_is_clean
])
def test_layer_is_short_clean(text, ok):
    assert ex.layer_is_short_clean(text) is ok
