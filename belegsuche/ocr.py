"""Texterkennung (OCR).

Standard ist Apples Vision-Framework (lokal, kostenlos, erkennt gedrehte Seiten
selbst). Tesseract ist als Alternative eingebaut, damit man beide auf denselben
Belegen vergleichen kann (`belegsuche index --ocr tesseract`).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

SIPS_TIMEOUT = 120            # Sekunden für die Umwandlung eines Bildes mit macOS `sips`


class ImageFormatError(Exception):
    """Bildformat nicht lesbar (weder Pillow noch macOS kann die Datei öffnen) – kein OCR-Problem."""


def load_image(path: Path) -> Image.Image:
    """Bilddatei als RGB, EXIF-Drehung angewandt. Formate, die Pillow nicht kennt (HEIC), wandelt macOS
    `sips` in eine PNG-Datei im System-Temp-Ordner um – nie neben dem Original; sie wird danach gelöscht."""
    try:
        with Image.open(path) as im:
            return ImageOps.exif_transpose(im).convert("RGB")
    except UnidentifiedImageError:
        pass
    sips = shutil.which("sips") or "/usr/bin/sips"
    with tempfile.TemporaryDirectory(prefix="belegsuche-") as tmp:
        out = Path(tmp) / "bild.png"
        try:
            # --out ist Pflicht: ohne schreibt sips in die Originaldatei
            proc = subprocess.run([sips, "-s", "format", "png", os.path.abspath(path), "--out", str(out)],
                                  capture_output=True, timeout=SIPS_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ImageFormatError(f"Bildformat nicht lesbar ({type(e).__name__})") from e
        if proc.returncode != 0 or not out.is_file():
            errs = [ln.strip() for ln in proc.stderr.decode(errors="replace").splitlines() if ln.startswith("Error")]
            raise ImageFormatError("Bildformat nicht lesbar" + (f" (sips: {errs[0][:200]})" if errs else ""))
        try:
            with Image.open(out) as im:
                return ImageOps.exif_transpose(im).convert("RGB")
        except Exception as e:
            raise ImageFormatError(f"Bildformat nicht lesbar ({e})") from e


@dataclass
class OcrResult:
    text: str
    confidence: float  # 0..1, längengewichteter Mittelwert; 0 wenn nichts erkannt

    @property
    def chars(self) -> int:
        return sum(1 for c in self.text if not c.isspace())


def _join_lines(items: list[tuple[float, float, float, str, float]]) -> OcrResult:
    """items: (y_mitte, hoehe, x_links, text, konfidenz), y wächst nach unten."""
    if not items:
        return OcrResult("", 0.0)
    items.sort(key=lambda t: (t[0], t[2]))
    lines: list[list[tuple[float, float, float, str, float]]] = []
    for it in items:
        if lines and abs(it[0] - lines[-1][0][0]) < 0.5 * max(it[1], lines[-1][0][1]):
            lines[-1].append(it)
        else:
            lines.append([it])
    out = []
    total = weight = 0.0
    for line in lines:
        line.sort(key=lambda t: t[2])
        out.append("  ".join(t[3] for t in line))
        for t in line:
            n = max(1, len(t[3]))
            total += t[4] * n
            weight += n
    return OcrResult("\n".join(out), total / weight if weight else 0.0)


class AppleVisionOCR:
    name = "apple"

    def __init__(self, languages: tuple[str, ...] = ("de-DE", "en-US")):
        import Vision  # noqa: F401  (Fehler hier = kein macOS / pyobjc fehlt)
        self.languages = list(languages)

    def _run(self, handler) -> OcrResult:
        import Vision
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(self.languages)
        req.setUsesLanguageCorrection_(True)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(f"Vision-Fehler: {err}")
        items = []
        for obs in req.results() or []:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            c = cands[0]
            box = obs.boundingBox()  # normiert, Ursprung unten links
            y_mid = 1.0 - (box.origin.y + box.size.height / 2)
            items.append((y_mid, box.size.height, box.origin.x, str(c.string()), float(c.confidence())))
        return _join_lines(items)

    def recognize_image(self, img: Image.Image) -> OcrResult:
        import objc
        import Vision
        from Foundation import NSData
        # PNG statt rohem TIFF/BMP: bei unkomprimierten Formaten gibt Vision den Speicher nicht
        # wieder frei (~60 MB je A4-Seite). compress_level=1 kostet nur einige ms.
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", compress_level=1)
        raw = buf.getvalue()
        del buf
        with objc.autorelease_pool():
            data = NSData.dataWithBytes_length_(raw, len(raw))
            handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
            res = self._run(handler)
            del handler, data   # noch innerhalb des Pools freigeben, sonst wächst der Prozess je Seite
        return res

    def recognize_file(self, path: Path) -> OcrResult:
        """Bilddatei direkt lesen (unterstützt auch HEIC, das Pillow nicht kann)."""
        import objc
        import Vision
        from Foundation import NSURL
        with objc.autorelease_pool():
            url = NSURL.fileURLWithPath_(str(path))
            handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
            res = self._run(handler)
            del handler, url
        return res


class TesseractOCR:
    name = "tesseract"

    def __init__(self, languages: str = "deu+eng"):
        self.binary = shutil.which("tesseract")
        if not self.binary:
            raise RuntimeError("Tesseract ist nicht installiert (brew install tesseract tesseract-lang).")
        self.languages = languages

    def recognize_image(self, img: Image.Image) -> OcrResult:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        proc = subprocess.run(
            [self.binary, "stdin", "stdout", "-l", self.languages, "--psm", "1", "tsv"],
            input=buf.getvalue(), capture_output=True, timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Tesseract-Fehler: {proc.stderr.decode(errors='replace')[:200]}")
        lines: dict[tuple, list[tuple[int, str, float]]] = {}
        for row in proc.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
            parts = row.split("\t")
            if len(parts) < 12 or parts[0] != "5" or not parts[11].strip():
                continue
            key = tuple(int(p) for p in parts[1:5])
            conf = max(0.0, float(parts[10])) / 100.0
            lines.setdefault(key, []).append((int(parts[6]), parts[11], conf))
        out, total, weight = [], 0.0, 0.0
        for key in sorted(lines):
            words = sorted(lines[key])
            out.append(" ".join(w[1] for w in words))
            for w in words:
                total += w[2] * len(w[1])
                weight += len(w[1])
        return OcrResult("\n".join(out), total / weight if weight else 0.0)

    def recognize_file(self, path: Path) -> OcrResult:
        return self.recognize_image(load_image(path))


def enhance(img: Image.Image) -> Image.Image:
    """Zweiter Versuch für blasse oder kleine Vorlagen (Thermopapier, Handyfotos)."""
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g, cutoff=2)
    if max(g.size) < 2500:
        g = g.resize((int(g.width * 1.6), int(g.height * 1.6)), Image.LANCZOS)
    return g.convert("RGB")


def make_engine(name: str):
    if name == "apple":
        return AppleVisionOCR()
    if name == "tesseract":
        return TesseractOCR()
    raise ValueError(f"Unbekannte OCR: {name}")
