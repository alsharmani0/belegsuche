"""Text aus PDFs und Bildern holen – pro Seite, mit OCR wo nötig.

Originale werden nur gelesen, nie geschrieben.
"""
from __future__ import annotations

import ctypes
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .ocr import ImageFormatError, OcrResult, enhance, load_image

PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".bmp", ".gif", ".webp"}
SUPPORTED_EXT = PDF_EXT | IMAGE_EXT

OCR_DPI = 300
MAX_RENDER_PX = 5000          # längste Bildseite beim Rendern von PDF-Seiten
MIN_TEXT_CHARS = 25           # darunter gilt eine PDF-Seite als "ohne Text"
MIN_TEXT_QUALITY = 0.5        # darunter gilt eine Textebene als unbrauchbar (schlechte Scanner-OCR)
MAX_GARBAGE_RATIO = 0.12      # mehr verstümmelte Wörter -> Textebene verwerfen
OCR_COVERAGE = 0.15           # Bilder bedecken mehr -> zusätzlich Texterkennung (Scan, eingebettetes Belegfoto)
MIN_CROP_AREA = 0.02          # kleinere Bilder werden einzeln erkannt: ab diesem Flächenanteil (Logo, Belegfoto) …
MIN_CROP_PX = (250, 120)      # … und mindestens so groß bei OCR_DPI (lange × kurze Seite); Icons/Linien nicht
MAX_CROPS = 6                 # höchstens die größten Bilder je Seite
CROP_BORDER = 16              # weißer Rand um Bildausschnitte (Text am Bildrand wird sonst schlechter erkannt)
MAX_IMAGE_PAGES = 1000        # mehrseitige Bilder (Fax-TIFF): höchstens so viele Seiten
RETRY_BELOW_CHARS = 40        # OCR-Ergebnis so kurz -> zweiter Versuch mit Kontrastverstärkung
RETRY_BELOW_CONF = 0.45
NOTE_PREFIX = "Hinweis:"      # Hinweise zu fertig eingelesenen Dateien (stehen in files.error, Status 'done')


class ExtractError(Exception):
    """Datei lässt sich nicht verarbeiten; Meldung ist für den Bericht gedacht."""


@dataclass
class PageText:
    page_no: int
    text: str
    source: str               # text | ocr | text+ocr
    ocr_conf: float | None = None


_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_VOWELS = set("aeiouyäöüAEIOUYÄÖÜéèàáóíú")


def text_quality(text: str) -> float:
    """Anteil plausibler Wörter (0..1). Zufälliger Zeichensalat liegt deutlich unter 0,5."""
    toks = [t for t in _TOKEN_RE.findall(text) if len(t) >= 3]
    if not toks:
        return 0.0
    good = 0
    for t in toks:
        if not any(c in _VOWELS for c in t):
            continue
        body = t[1:]
        if body and not (body.islower() or body.isupper()):
            continue  # "xKqZa" – wildes Groß/Klein mitten im Wort
        run = longest = 0
        for c in t.lower():
            run = 0 if c in _VOWELS else run + 1
            longest = max(longest, run)
        if longest <= 4:
            good += 1
    return good / len(toks)


_BAD_CHARS = set("@|~^°`¦¬")
_MIXED_RE = re.compile(r"[^\W\d_]\d+[^\W\d_]|[^\W\d_][+!?=][^\W\d_]")


def garbage_ratio(text: str) -> float:
    """Anteil verstümmelter Wörter (typisch für schlechte Scanner-OCR: 'Kro5tebn1ux9', 'qvmlzt@kn')."""
    toks = [t for t in text.split() if len(t) >= 3]
    if not toks:
        return 0.0
    bad = sum(1 for t in toks if any(c in _BAD_CHARS for c in t) or _MIXED_RE.search(t))
    return bad / len(toks)


def layer_is_clean(text: str) -> bool:
    return _chars(text) >= MIN_TEXT_CHARS and text_quality(text) >= MIN_TEXT_QUALITY \
        and garbage_ratio(text) <= MAX_GARBAGE_RATIO


_ALNUM3_RE = re.compile(r"[^\W_]{3,}", re.UNICODE)


def layer_is_short_clean(text: str) -> bool:
    """Kurze, aber saubere Textebene (z. B. nur 'RE-2021/00487' in winziger Schrift): behalten, OCR ergänzt.
    Verstümmelte Reste (Zeichensalat, Steuer-/Ersatzzeichen, 'Kro5tebn1ux9') gelten nicht als sauber."""
    n = _chars(text)
    if not 0 < n < MIN_TEXT_CHARS or not _ALNUM3_RE.search(text):
        return False
    odd = sum(1 for c in text if not c.isspace() and (c == "\ufffd" or unicodedata.category(c)[0] == "C"))
    if odd > 0.1 * n or garbage_ratio(text) > MAX_GARBAGE_RATIO:
        return False
    words = [t for t in _TOKEN_RE.findall(text) if len(t) >= 3]
    return not words or text_quality(text) >= MIN_TEXT_QUALITY


def _chars(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


def _best_ocr(engine, img: Image.Image) -> OcrResult:
    res = engine.recognize_image(img)
    if res.chars < RETRY_BELOW_CHARS or res.confidence < RETRY_BELOW_CONF:
        alt = engine.recognize_image(enhance(img))
        if alt.chars * max(alt.confidence, 0.05) > res.chars * max(res.confidence, 0.05):
            return alt
    return res


_NORM = 100_000               # Gerätegröße für normierte Koordinaten (0..1) über FPDF_PageToDevice
_MAX_COORD = 100_000.0        # Seitenkoordinaten begrenzen, damit die Umrechnung nicht überläuft


def _image_boxes(page, pdfium_c) -> list[tuple[float, float, float, float]]:
    """Eingebettete Bilder als Rechtecke (x0, y0, x1, y1) in 0..1 der angezeigten Seite, y nach unten.
    Berücksichtigt Seitendrehung, verschobene CropBox und Bilder in Formularobjekten (XObjects)."""
    boxes = []
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=3):
        rect = obj.get_bounds()
        form = obj.container
        while form is not None:                     # Formular-Koordinaten -> Seitenkoordinaten
            rect = form.get_matrix().on_rect(*rect)
            form = form.container
        xs, ys = [], []
        for x, y in ((rect[0], rect[1]), (rect[2], rect[3])):
            dx, dy = ctypes.c_int(), ctypes.c_int()
            pdfium_c.FPDF_PageToDevice(page, 0, 0, _NORM, _NORM, 0,
                                       max(-_MAX_COORD, min(_MAX_COORD, x)), max(-_MAX_COORD, min(_MAX_COORD, y)),
                                       ctypes.byref(dx), ctypes.byref(dy))
            xs.append(min(1.0, max(0.0, dx.value / _NORM)))
            ys.append(min(1.0, max(0.0, dy.value / _NORM)))
        if xs[0] != xs[1] and ys[0] != ys[1]:
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def _coverage_of(boxes) -> float:
    return min(1.0, sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes))


def _image_coverage(page, pdfium_c) -> float:
    """Anteil der Seitenfläche, den eingebettete Bilder bedecken (grob, ohne Überlappung)."""
    try:
        return _coverage_of(_image_boxes(page, pdfium_c))
    except Exception:
        return 0.0


def _crop_boxes(boxes, page_size) -> list[tuple[float, float, float, float]]:
    """Bilder, die einzeln erkannt werden: nicht winzig, höchstens MAX_CROPS, in Lesereihenfolge.
    Aneinanderstoßende Teile (manche Programme zerlegen ein Bild in Streifen) zählen als ein Bild."""
    w_pt, h_pt = page_size
    tx, ty = 1.0 / max(w_pt, 1.0), 1.0 / max(h_pt, 1.0)   # 1 pt Toleranz
    boxes = sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)[:300]
    merged: list[tuple[float, float, float, float]] = []
    for b in boxes:
        while True:
            for k, o in enumerate(merged):
                if b[0] <= o[2] + tx and o[0] <= b[2] + tx and b[1] <= o[3] + ty and o[1] <= b[3] + ty:
                    b = (min(b[0], o[0]), min(b[1], o[1]), max(b[2], o[2]), max(b[3], o[3]))
                    del merged[k]
                    break
            else:
                break
        merged.append(b)
    px = OCR_DPI / 72
    keep = []
    for x0, y0, x1, y1 in merged:
        w, h = (x1 - x0) * w_pt * px, (y1 - y0) * h_pt * px
        if (x1 - x0) * (y1 - y0) >= MIN_CROP_AREA and max(w, h) >= MIN_CROP_PX[0] and min(w, h) >= MIN_CROP_PX[1]:
            keep.append((x0, y0, x1, y1))
    keep = sorted(keep, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)[:MAX_CROPS]
    return sorted(keep, key=lambda b: (b[1], b[0]))


def _render(page) -> Image.Image:
    w, h = page.get_size()
    scale = OCR_DPI / 72
    if max(w, h) * scale > MAX_RENDER_PX:
        scale = MAX_RENDER_PX / max(w, h)
    return page.render(scale=scale).to_pil().convert("RGB")


def _ocr_crops(engine, img: Image.Image, boxes) -> tuple[str, float | None]:
    """Bildausschnitte aus der einmal gerenderten Seite einzeln erkennen: (Text, Konfidenz).
    Die Konfidenz zählt nur längere Bildtexte (Belegfoto) – ein unsicher gelesenes Logo soll eine
    saubere Digitalseite im Bericht nicht als unsicher markieren."""
    texts, total, weight = [], 0.0, 0
    W, H = img.size
    for x0, y0, x1, y1 in boxes:
        crop = img.crop((math.floor(x0 * W), math.floor(y0 * H), math.ceil(x1 * W), math.ceil(y1 * H)))
        crop = ImageOps.expand(crop, border=CROP_BORDER, fill="white")
        res = engine.recognize_image(crop)
        if res.chars and res.confidence < RETRY_BELOW_CONF:     # etwas gefunden, aber unsicher
            alt = engine.recognize_image(enhance(crop))
            if alt.chars * max(alt.confidence, 0.05) > res.chars * max(res.confidence, 0.05):
                res = alt
        if res.chars:
            texts.append(res.text)
        if res.chars >= RETRY_BELOW_CHARS:
            total += res.confidence * res.chars
            weight += res.chars
    return "\n".join(texts), (total / weight if weight else None)


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p.strip())


def extract_pdf(path: Path, engine) -> list[PageText]:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as e:
        msg = str(e).lower()
        if "password" in msg:
            raise ExtractError("PDF ist passwortgeschützt") from e
        raise ExtractError(f"PDF beschädigt oder nicht lesbar ({e})") from e
    pages: list[PageText] = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                try:
                    layer = tp.get_text_range() or ""
                finally:
                    tp.close()
                layer = layer.replace("\r\n", "\n").replace("\r", "\n")
                clean = layer_is_clean(layer)
                # Kurze, aber saubere Textebene (nur eine Nummer in winziger Schrift) behalten – die OCR
                # übersieht solche Schrift leicht. Verstümmelte Textebenen werden verworfen.
                keep = clean or layer_is_short_clean(layer)
                try:
                    boxes = _image_boxes(page, pdfium_c)
                except Exception:
                    boxes = []
                # Textebene allein nur, wenn höchstens kleine Bilder auf der Seite liegen. Eingescannte
                # Seiten (vorhandene Scanner-OCR ist oft schlecht) und große eingebettete Belegfotos werden
                # ganz erkannt, kleinere Bilder (Belegfoto in ausgedruckter E-Mail, Firmenlogo) einzeln.
                if clean and _coverage_of(boxes) <= OCR_COVERAGE:
                    crops = _crop_boxes(boxes, page.get_size()) if engine is not None else []
                    extra, conf = _ocr_crops(engine, _render(page), crops) if crops else ("", None)
                    if extra:
                        pages.append(PageText(i + 1, _join(layer, extra), "text+ocr", conf))
                    else:
                        pages.append(PageText(i + 1, layer, "text"))
                    continue
                if engine is None:
                    pages.append(PageText(i + 1, layer if keep else "", "text"))
                    continue
                res = _best_ocr(engine, _render(page))
                if keep:
                    pages.append(PageText(i + 1, _join(layer, res.text), "text+ocr", res.confidence))
                else:
                    pages.append(PageText(i + 1, res.text, "ocr", res.confidence))
            finally:
                page.close()
    finally:
        pdf.close()
    return pages


def _extract_frames(path: Path, engine, notes: list[str]) -> list[PageText]:
    """Mehrseitige Bilder (Fax-TIFF) Seite für Seite: nie alle Seiten gleichzeitig im Speicher."""
    try:
        im = Image.open(path)
    except Exception as e:
        raise ExtractError(f"Bild nicht lesbar ({e})") from e
    out = []
    with im:
        try:
            total = getattr(im, "n_frames", 1)
        except Exception as e:
            raise ExtractError(f"Bild nicht lesbar ({e})") from e
        for i in range(min(total, MAX_IMAGE_PAGES)):
            try:
                im.seek(i)
                frame = im.convert("RGB")
            except Exception as e:
                raise ExtractError(f"Bild nicht lesbar (Seite {i + 1}: {e})") from e
            res = _best_ocr(engine, frame)
            out.append(PageText(i + 1, res.text, "ocr", res.confidence))
    if total > MAX_IMAGE_PAGES:
        notes.append(f"{NOTE_PREFIX} nur die ersten {MAX_IMAGE_PAGES} von {total} Seiten eingelesen")
    return out


def extract_image(path: Path, engine, notes: list[str] | None = None) -> list[PageText]:
    if engine is None:
        return [PageText(1, "", "ocr", 0.0)]
    ext = path.suffix.lower()
    if ext in (".tif", ".tiff", ".gif"):
        return _extract_frames(path, engine, notes if notes is not None else [])
    try:
        res = engine.recognize_file(path)
    except ImageFormatError as e:
        raise ExtractError(str(e)) from e
    except Exception as e:
        res = None
        err = e
    if res is None or res.chars < RETRY_BELOW_CHARS or res.confidence < RETRY_BELOW_CONF:
        try:
            better = _best_ocr(engine, load_image(path))   # HEIC über sips, bekommt so auch den zweiten Versuch
            if res is None or better.chars * max(better.confidence, 0.05) > res.chars * max(res.confidence, 0.05):
                res = better
        except ImageFormatError as e:
            if res is None:
                raise ExtractError(str(e)) from e
        except Exception:
            if res is None:
                raise ExtractError(f"Bild nicht lesbar ({err})") from err
    return [PageText(1, res.text, "ocr", res.confidence)]


def extract_with_notes(path: Path, engine) -> tuple[list[PageText], list[str]]:
    """Wie extract(), dazu Hinweise für den Bericht (z. B. nicht alle Seiten eingelesen)."""
    notes: list[str] = []
    ext = path.suffix.lower()
    if ext in PDF_EXT:
        return extract_pdf(path, engine), notes
    if ext in IMAGE_EXT:
        return extract_image(path, engine, notes), notes
    raise ExtractError("Dateityp nicht unterstützt")


def extract(path: Path, engine) -> list[PageText]:
    return extract_with_notes(path, engine)[0]
