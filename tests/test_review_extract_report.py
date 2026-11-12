"""Focused regressions for extraction and report review using temporary inputs only."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from belegsuche import db as dbm
from belegsuche import extract as ex
from belegsuche import ocr
from belegsuche.ocr import OcrResult
from belegsuche.report import build_report, format_report


def _digital_pdf(path: Path) -> None:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path))
    pdf.drawString(60, 780, "Rechnung Hausverwaltung Beispiel Musterstadt Heizung Wartung")
    pdf.drawImage(ImageReader(Image.new("RGB", (400, 200), "white")), 60, 300, 240, 120)
    pdf.save()


class _ImageEngine:
    def __init__(self, *, file_result=None, image_result=None):
        self.file_result = file_result or OcrResult("kurz", 0.2)
        self.image_result = image_result or OcrResult("Bildausschnitt Rechnung 4711 Betrag 100 Euro Musterstadt", 0.95)
        self.image_sizes = []
        self.file_calls = 0

    def recognize_file(self, path):
        self.file_calls += 1
        return self.file_result

    def recognize_image(self, image):
        self.image_sizes.append(image.size)
        return self.image_result


def test_pdf_image_inspection_failure_falls_back_to_page_ocr(tmp_path, monkeypatch):
    path = tmp_path / "mail.pdf"
    _digital_pdf(path)
    engine = _ImageEngine()

    def unavailable(*args):
        raise RuntimeError("Bildobjekte nicht auswertbar")

    monkeypatch.setattr(ex, "_image_boxes", unavailable)
    pages = ex.extract(path, engine)
    assert len(engine.image_sizes) == 1
    assert pages[0].source == "text+ocr"
    assert "Hausverwaltung" in pages[0].text
    assert "4711" in pages[0].text


def test_short_file_ocr_retries_only_enhanced_image(tmp_path):
    path = tmp_path / "beleg.png"
    Image.new("RGB", (400, 200), "white").save(path)
    # The second OCR result stays short: retrying that same image again would
    # add a third OCR pass even though the direct file was already recognized.
    engine = _ImageEngine(image_result=OcrResult("Rechnung 4711", 0.9))
    pages = ex.extract(path, engine)
    assert engine.file_calls == 1
    assert engine.image_sizes == [(640, 320)]
    assert pages[0].text == "Rechnung 4711"


@pytest.mark.parametrize("bad", ["\ufffd", "\x00", "\u200b"])
def test_long_layer_with_mostly_replacement_or_control_characters_needs_ocr(bad):
    assert not ex.layer_is_clean("Rechnung Hausverwaltung Musterstadt " + bad * 200)


def test_direct_file_failure_keeps_original_then_enhanced_image_fallback(tmp_path):
    path = tmp_path / "beleg.png"
    Image.new("RGB", (400, 200), "white").save(path)

    class FailedFileEngine(_ImageEngine):
        def recognize_file(self, path):
            raise RuntimeError("Direkter Dateizugriff nicht verfügbar")

    engine = FailedFileEngine(image_result=OcrResult("Rechnung 4711", 0.9))
    assert ex.extract(path, engine)[0].text == "Rechnung 4711"
    assert engine.image_sizes == [(400, 200), (640, 320)]


def test_weaker_enhanced_ocr_keeps_direct_file_text(tmp_path):
    path = tmp_path / "beleg.png"
    Image.new("RGB", (400, 200), "white").save(path)
    engine = _ImageEngine(file_result=OcrResult("Rechnung 4711", 0.8), image_result=OcrResult("", 0.0))
    assert ex.extract(path, engine)[0].text == "Rechnung 4711"


def _transparent_receipt(path):
    image = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
    image.paste((0, 0, 0, 255), (100, 50, 200, 100))
    image.save(path)


def test_load_image_preserves_visible_black_text_on_transparency(tmp_path):
    path = tmp_path / "beleg.png"
    _transparent_receipt(path)
    before = path.read_bytes()
    image = ocr.load_image(path)
    assert image.mode == "RGB"
    assert image.getpixel((0, 0)) == (255, 255, 255)
    assert image.getpixel((150, 75)) == (0, 0, 0)
    assert path.read_bytes() == before


def test_transparent_gif_frame_keeps_foreground_visible_for_ocr(tmp_path):
    path = tmp_path / "beleg.gif"
    _transparent_receipt(path)

    class VisibleImageEngine(_ImageEngine):
        def recognize_image(self, image):
            assert image.getpixel((0, 0)) == (255, 255, 255)
            assert image.getpixel((150, 75)) == (0, 0, 0)
            return self.image_result

    assert "4711" in ex.extract(path, VisibleImageEngine())[0].text


@pytest.mark.parametrize("confidence", [0.0, 0.95])
@pytest.mark.parametrize("blank", ["\n\t\r \n", "\u2009\u200a\xa0\u202f\u3000"])
def test_report_blank_page_ignores_whitespace(tmp_path, blank, confidence):
    con = dbm.connect(tmp_path / "index.db")
    root = tmp_path / "belege"
    root.mkdir()
    try:
        rid = con.execute("INSERT INTO roots(path) VALUES (?)", (str(root),)).lastrowid
        fid = con.execute(
            "INSERT INTO files(root_id, relpath, status, text_chars, ocr_pages) VALUES (?, ?, 'done', 30, 2)",
            (rid, "scan.pdf"),
        ).lastrowid
        con.executemany(
            "INSERT INTO pages(file_id, page_no, text, source, ocr_conf) VALUES (?, ?, ?, 'ocr', ?)",
            [(fid, 1, "Rechnung Hausverwaltung Beispiel", 0.95), (fid, 2, blank, confidence)],
        )
        report = build_report(con)
        assert report["blank_pages"] == 1
        assert report["low_conf_pages"] == 0
        assert "leere Seite 2" in report["problems"][0]["grund"]
    finally:
        con.close()


@pytest.mark.parametrize("retained_status", ["pending", "processing"])
def test_report_counts_name_only_documents_and_retained_text(tmp_path, retained_status):
    con = dbm.connect(tmp_path / "index.db")
    root = tmp_path / "belege"
    root.mkdir()
    try:
        rid = con.execute("INSERT INTO roots(path) VALUES (?)", (str(root),)).lastrowid
        for name, status, text, has_page in [
            ("rueckseite.pdf", "done", "", True),
            ("rechnung.pdf", retained_status, "Rechnung Hausverwaltung Beispiel", True),
            ("neu.pdf", "pending", "", False),
            ("fehler.pdf", "error", "Alter Rechnungstext", True),
        ]:
            fid = con.execute(
                "INSERT INTO files(root_id, relpath, status, text_chars, ocr_pages) VALUES (?, ?, ?, ?, 1)",
                (rid, name, status, len(text)),
            ).lastrowid
            if has_page:
                con.execute(
                    "INSERT INTO pages(file_id, page_no, text, source, ocr_conf) VALUES (?, 1, ?, 'ocr', 0.9)",
                    (fid, text),
                )
        report = build_report(con)
        assert report["searchable"] == 2
        assert report["searchable_with_text"] == 1
        assert report["searchable_name_only"] == 1
        assert report["searchable_with_ocr_text"] == 1
        assert report["searchable_outdated"] == 1
        rendered = format_report(report)
        assert "durchsuchbar:            2" in rendered
        assert "mit erkanntem Text:      1  (davon mit Texterkennung: 1)" in rendered
        assert "nur Dateiname/Pfad:      1" in rendered
        assert "davon mit bisherigem Stand: 1" in rendered
        assert "noch offen:              2" in rendered
        assert "Fehler:                  1" in rendered
        reasons = {Path(p["pfad"]).name: p["grund"] for p in report["problems"]}
        assert reasons["neu.pdf"] == "noch nicht eingelesen"
        if retained_status == "pending":
            assert "noch nicht neu eingelesen; bisheriger Stand weiter durchsuchbar" in reasons["rechnung.pdf"]
    finally:
        con.close()


def test_report_blank_ocr_document_does_not_count_as_recognized_text(tmp_path):
    con = dbm.connect(tmp_path / "index.db")
    try:
        rid = con.execute("INSERT INTO roots(path) VALUES (?)", (str(tmp_path),)).lastrowid
        fid = con.execute(
            "INSERT INTO files(root_id, relpath, status, text_chars, ocr_pages) VALUES (?, 'leer.pdf', 'done', 0, 1)",
            (rid,),
        ).lastrowid
        con.execute("INSERT INTO pages(file_id, page_no, text, source, ocr_conf) VALUES (?, 1, '', 'ocr', 0)", (fid,))
        report = build_report(con)
        assert report["searchable"] == 1
        assert report["searchable_with_text"] == 0
        assert report["searchable_name_only"] == 1
        assert report["searchable_with_ocr_text"] == 0
        rendered = format_report(report)
        assert "durchsuchbar:            1" in rendered
        assert "mit erkanntem Text:      0  (davon mit Texterkennung: 0)" in rendered
        assert "ohne erkennbaren Text:   1" in rendered
    finally:
        con.close()
