"""Text aus PDFs, Bildern, Word-Dateien und E-Mails holen – pro Seite, mit OCR wo nötig.

Originale werden nur gelesen, nie geschrieben. PDFs und Bilder lassen sich auch aus Bytes lesen
(Mail-Anhänge), ohne Zwischendatei neben dem Original.
"""
from __future__ import annotations

import ctypes
import io
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

from .ocr import ImageFormatError, OcrResult, enhance, load_image, load_image_bytes, to_rgb

PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".bmp", ".gif", ".webp"}
MAIL_EXT = {".eml"}
WORD_EXT = {".doc", ".docx", ".dot", ".dotx", ".dotm", ".rtf", ".odt"}
SUPPORTED_EXT = PDF_EXT | IMAGE_EXT | MAIL_EXT | WORD_EXT

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

# Wird nach jeder fertig verarbeiteten Seite aufgerufen. Der Arbeitsprozess beim Einlesen startet damit
# sein Zeitlimit neu: es gilt je Seite, nicht für die ganze Datei (lange Sammel-PDFs sind kein Hänger).
on_page_done: Callable[[], None] | None = None


def _page_done() -> None:
    if on_page_done is not None:
        on_page_done()


class ExtractError(Exception):
    """Datei lässt sich nicht verarbeiten; Meldung ist für den Bericht gedacht."""


@dataclass
class PageText:
    page_no: int
    text: str
    source: str               # text | ocr | text+ocr
    ocr_conf: float | None = None


@dataclass
class PartInfo:
    """Ein Teil einer E-Mail (Tabelle parts): 0 = Mailkopf + Mailtext, danach die Anhänge."""
    part_no: int
    kind: str                 # body | pdf | image | word | text | mail | link | skipped
    first_page: int | None = None
    n_pages: int = 0
    name: str | None = None
    mime: str | None = None
    size: int | None = None
    sha256: str | None = None
    mime_path: str | None = None
    note: str | None = None


@dataclass
class MailInfo:
    """Kopf einer E-Mail (Tabelle mails)."""
    subject: str | None = None
    sender: str | None = None
    recipients: str | None = None
    sent_at: str | None = None        # ISO 8601
    message_id: str | None = None


@dataclass
class Extracted:
    """Ergebnis des Einlesens einer Datei: Seiten, Hinweise und bei E-Mails Teile und Mailkopf.

    Lässt sich zwischen Prozessen übertragen und wie bisher als (pages, notes) entpacken."""
    pages: list[PageText]
    notes: list[str] = field(default_factory=list)
    parts: list[PartInfo] = field(default_factory=list)
    mail: MailInfo | None = None
    reused_ocr_pages: int = 0         # Seiten mit Texterkennung, die von gleichem Inhalt übernommen wurden

    def __iter__(self):
        return iter((self.pages, self.notes))


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


def _odd_chars(text: str) -> int:
    return sum(1 for c in text if not c.isspace()
               and (c == "\ufffd" or unicodedata.category(c)[0] == "C"))


def layer_is_clean(text: str) -> bool:
    n = _chars(text)
    return n >= MIN_TEXT_CHARS and _odd_chars(text) <= 0.1 * n \
        and text_quality(text) >= MIN_TEXT_QUALITY and garbage_ratio(text) <= MAX_GARBAGE_RATIO


_ALNUM3_RE = re.compile(r"[^\W_]{3,}", re.UNICODE)


def layer_is_short_clean(text: str) -> bool:
    """Kurze, aber saubere Textebene (z. B. nur 'RE-2021/00487' in winziger Schrift): behalten, OCR ergänzt.
    Verstümmelte Reste (Zeichensalat, Steuer-/Ersatzzeichen, 'Kro5tebn1ux9') gelten nicht als sauber."""
    n = _chars(text)
    if not 0 < n < MIN_TEXT_CHARS or not _ALNUM3_RE.search(text):
        return False
    if _odd_chars(text) > 0.1 * n or garbage_ratio(text) > MAX_GARBAGE_RATIO:
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


def extract_pdf(src: Path | bytes, engine, max_pages: int | None = None,
                notes: list[str] | None = None) -> list[PageText]:
    """PDF aus einer Datei oder aus Bytes (Mail-Anhang). max_pages: höchstens so viele Seiten (Hinweis in notes)."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    try:
        pdf = pdfium.PdfDocument(bytes(src) if isinstance(src, (bytes, bytearray, memoryview)) else str(src))
    except pdfium.PdfiumError as e:
        msg = str(e).lower()
        if "password" in msg:
            raise ExtractError("PDF ist passwortgeschützt") from e
        raise ExtractError(f"PDF beschädigt oder nicht lesbar ({e})") from e
    pages: list[PageText] = []
    try:
        total = len(pdf)
        if max_pages is not None and total > max_pages:
            if notes is not None:
                notes.append(f"{NOTE_PREFIX} nur die ersten {max_pages} von {total} Seiten eingelesen")
            total = max_pages
        for i in range(total):
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
                    # Nicht auswertbare Bildobjekte sind kein Nachweis einer reinen
                    # Textseite. Ganze Seite erkennen, damit eingebettete Belege nicht fehlen.
                    boxes = None
                # Textebene allein nur, wenn höchstens kleine Bilder auf der Seite liegen. Eingescannte
                # Seiten (vorhandene Scanner-OCR ist oft schlecht) und große eingebettete Belegfotos werden
                # ganz erkannt, kleinere Bilder (Belegfoto in ausgedruckter E-Mail, Firmenlogo) einzeln.
                if clean and boxes is not None and _coverage_of(boxes) <= OCR_COVERAGE:
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
                _page_done()
    finally:
        pdf.close()
    return pages


def _extract_frames(path: Path | io.BytesIO, engine, notes: list[str],
                    max_pages: int = MAX_IMAGE_PAGES) -> list[PageText]:
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
        max_pages = min(max_pages, MAX_IMAGE_PAGES)
        for i in range(min(total, max_pages)):
            try:
                im.seek(i)
                frame = to_rgb(im)
            except Exception as e:
                raise ExtractError(f"Bild nicht lesbar (Seite {i + 1}: {e})") from e
            res = _best_ocr(engine, frame)
            out.append(PageText(i + 1, res.text, "ocr", res.confidence))
            _page_done()
    if total > max_pages:
        notes.append(f"{NOTE_PREFIX} nur die ersten {max_pages} von {total} Seiten eingelesen")
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
            image = load_image(path)     # HEIC bei Bedarf über sips
            # Die Datei wurde bereits erkannt: gleich die verbesserte Fassung
            # versuchen, statt dieselbe Vorlage ein zweites Mal zu erkennen.
            better = (_best_ocr(engine, image) if res is None
                      else engine.recognize_image(enhance(image)))
            if res is None or better.chars * max(better.confidence, 0.05) > res.chars * max(res.confidence, 0.05):
                res = better
        except ImageFormatError as e:
            if res is None:
                raise ExtractError(str(e)) from e
        except Exception:
            if res is None:
                raise ExtractError(f"Bild nicht lesbar ({err})") from err
    return [PageText(1, res.text, "ocr", res.confidence)]


# --- Bilder aus Bytes (Mail-Anhänge) --------------------------------------------------------------

_PDF_RE = re.compile(rb"%PDF-\d")
_BMP_HEADER_SIZES = {12, 40, 52, 56, 64, 108, 124}


def _pdf_after_preamble(head: bytes) -> bool:
    """„%PDF-“ nicht am Anfang, aber in den ersten 1024 Bytes (das lassen PDF-Leser zu): nur nach einem binären
    Vorspann (z. B. MacBinary) oder Leerraum – ein Textanhang, der „%PDF-1.4“ irgendwo erwähnt, ist kein PDF."""
    m = _PDF_RE.search(head)
    if m is None:
        return False
    pre = head[:m.start()]
    return not pre.removeprefix(b"\xef\xbb\xbf").strip() or any(b < 0x09 or 0x0E <= b < 0x20 or b == 0x7F
                                                                 for b in pre)


def _plausible_bmp(head: bytes, size: int) -> bool:
    """BMP-Kopf: bekannte Länge des DIB-Kopfs an Stelle 14 – „BMW Niederlassung …“ als Text ist kein Bild."""
    if len(head) < 26 or size <= 26:
        return False
    return int.from_bytes(head[14:18], "little") in _BMP_HEADER_SIZES


def sniff(data: bytes) -> str | None:
    """Dateiart an den ersten Bytes: pdf | png | jpeg | gif | tiff | heic | bmp | webp | zip | ole | rtf |
    appledouble | tnef – oder None. Mail-Anhänge heißen oft application/octet-stream; die Angabe in der Mail
    und die Endung sind kein Beleg für den Inhalt."""
    head = bytes(data[:16])
    if head.startswith(b"%PDF") or _pdf_after_preamble(bytes(data[:1024])):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1",
                                                 b"msf1"):
        return "heic"
    if head.startswith(b"BM") and _plausible_bmp(bytes(data[:30]), len(data)):
        return "bmp"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith((b"\x00\x05\x16\x07", b"\x00\x05\x16\x00")):
        return "appledouble"
    if head.startswith(b"\x78\x9f\x3e\x22"):
        return "tnef"
    return None


IMAGE_KINDS = {"png", "jpeg", "gif", "tiff", "heic", "bmp", "webp"}


def image_size(data: bytes) -> tuple[int, int] | None:
    """Pixelgröße eines Bildes, ohne es zu dekodieren (None, wenn Pillow das Format nicht kennt, z. B. HEIC)."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def extract_image_bytes(data: bytes, engine, notes: list[str] | None = None,
                        max_pages: int = MAX_IMAGE_PAGES) -> list[PageText]:
    """Bild aus Bytes (Mail-Anhang). Vision liest die Bytes direkt (auch HEIC), ohne Temp-Datei."""
    if engine is None:
        return [PageText(1, "", "ocr", 0.0)]
    kind = sniff(data)
    if kind in ("tiff", "gif"):
        return _extract_frames(io.BytesIO(data), engine, notes if notes is not None else [], max_pages)
    res, err = None, None
    rec = getattr(engine, "recognize_bytes", None)
    if rec is not None:
        try:
            res = rec(data)
        except ImageFormatError as e:
            raise ExtractError(str(e)) from e
        except Exception as e:
            err = e
    if res is None or res.chars < RETRY_BELOW_CHARS or res.confidence < RETRY_BELOW_CONF:
        try:
            image = load_image_bytes(data)
            better = (_best_ocr(engine, image) if res is None
                      else engine.recognize_image(enhance(image)))
            if res is None or better.chars * max(better.confidence, 0.05) > res.chars * max(res.confidence, 0.05):
                res = better
        except ImageFormatError as e:
            if res is None:
                raise ExtractError(str(e)) from e
        except Exception as e:
            if res is None:
                raise ExtractError(f"Bild nicht lesbar ({err or e})") from e
    return [PageText(1, res.text, "ocr", res.confidence)]


def extract_full(path: Path, engine, *, known: dict | None = None, safe: bool = False,
                 root: str | None = None) -> Extracted:
    """Datei einlesen. Bei E-Mails mit Teilen und Mailkopf.

    known: sha256 -> Seiten von Anhängen, deren Inhalt schon eingelesen ist (keine erneute OCR).
    safe: E-Mail ohne PDF- und Bild-Anhänge lesen (Rückfall nach Zeitlimit oder Absturz).
    root: eingelesener Ordner (für Verweise auf Dateien im Vorgangsordner)."""
    ext = path.suffix.lower()
    if ext in MAIL_EXT:
        from .mail import extract_mail
        return extract_mail(path, engine, known=known, safe=safe, root=root)
    notes: list[str] = []
    if ext in PDF_EXT:
        return Extracted(extract_pdf(path, engine), notes)
    if ext in IMAGE_EXT:
        return Extracted(extract_image(path, engine, notes), notes)
    if ext in WORD_EXT:
        from .word import extract_word
        return Extracted(extract_word(path, notes), notes)
    raise ExtractError("Dateityp nicht unterstützt")


def extract_with_notes(path: Path, engine) -> tuple[list[PageText], list[str]]:
    """Wie extract(), dazu Hinweise für den Bericht (z. B. nicht alle Seiten eingelesen)."""
    res = extract_full(path, engine)
    return res.pages, res.notes


def extract(path: Path, engine) -> list[PageText]:
    return extract_with_notes(path, engine)[0]
