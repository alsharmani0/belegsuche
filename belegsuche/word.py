"""Word-Dateien lesen (auch als Mail-Anhang): Text je Seite, ohne Texterkennung.

- .docx/.dotx/.dotm: direkt aus dem ZIP (zipfile + ElementTree): Haupttext, Kopf- und Fußzeilen, Fuß- und
  Endnoten. Das ist schneller als textutil, und textutil lässt Kopf- und Fußzeilen weg.
- .doc/.dot/.rtf/.odt: über macOS `textutil`. Die Datei geht nur über stdin hinein und der Text über stdout
  hinaus. Nie den Pfad des Originals übergeben: `textutil -convert` schreibt sonst eine Datei daneben.
- Die Art steht an den ersten Bytes, nicht an der Endung (manche .doc sind in Wahrheit .docx).
- Seiten: an Seitenumbrüchen (\\f) geteilt, sonst eine Seite. Bilder in Word-Dateien werden nicht per
  Texterkennung gelesen (fast immer Briefkopf-Logos); hat die Datei fast keinen Text, steht ein Hinweis da.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .extract import MIN_TEXT_CHARS, NOTE_PREFIX, ExtractError, PageText, sniff

TEXTUTIL = "/usr/bin/textutil"
TEXTUTIL_TIMEOUT = 60                  # Sekunden je Datei; deutlich unter dem Zeitlimit des Arbeitsprozesses
MAX_WORD_BYTES = 200 * 1024 * 1024     # größere Word-Dateien werden nicht gelesen
MAX_XML_BYTES = 64 * 1024 * 1024       # je XML-Datei im docx (entpackt) – Schutz vor ZIP-Bomben
PASSWORD_MSG = "Word-Datei ist passwortgeschützt"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_P, _T, _TAB, _BR, _CR = _W + "p", _W + "t", _W + "tab", _W + "br", _W + "cr"
_TC, _TR = _W + "tc", _W + "tr"
_NB_HYPHEN = _W + "noBreakHyphen"
_PART_RE = re.compile(r"word/(header|footer)(\d*)\.xml$")
_ODT_MIME = b"application/vnd.oasis.opendocument.text"


def _xml_text(data: bytes) -> str:
    """Sichtbarer Text eines WordprocessingML-Teils. Absätze werden Zeilen, Tabellenzellen durch Tab getrennt,
    harte Seitenumbrüche werden \\f. Ersatzdarstellungen (mc:Fallback) werden übersprungen – sonst stünde der
    Text von Textfeldern doppelt da."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ExtractError("Word-Datei nicht lesbar (ungewöhnlicher XML-Inhalt)")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ExtractError(f"Word-Datei beschädigt oder nicht lesbar ({e})") from e
    out: list[str] = []
    stack: list[tuple[ET.Element, bool]] = [(root, False)]
    while stack:                      # ohne Rekursion: tief verschachtelte Dateien sprengen den Stapel nicht
        el, done = stack.pop()
        tag = el.tag
        if done:
            if tag == _P:
                out.append("\n")
            elif tag == _TC:                  # Zellen einer Tabellenzeile durch Tab getrennt in einer Zeile
                while out and out[-1] == "\n":
                    out.pop()
                out.append("\t")
            elif tag == _TR:
                if out and out[-1] == "\t":
                    out[-1] = "\n"
                else:
                    out.append("\n")
            continue
        if tag == _FALLBACK:
            continue
        if tag == _T:
            out.append(el.text or "")
        elif tag == _TAB:
            out.append("\t")
        elif tag == _BR or tag == _CR:
            out.append("\f" if el.get(_W + "type") == "page" else "\n")
        elif tag == _NB_HYPHEN:
            out.append("-")
        stack.append((el, True))
        stack.extend((c, False) for c in reversed(el))
    return "".join(out)


def _read_member(z: zipfile.ZipFile, name: str) -> bytes:
    info = z.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ExtractError(f"Word-Datei zu groß zum Lesen ({name}: {info.file_size // (1024 * 1024)} MB)")
    with z.open(info) as f:
        data = f.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise ExtractError(f"Word-Datei zu groß zum Lesen ({name})")
    return data


def _natural(name: str) -> tuple[str, int]:
    m = _PART_RE.search(name)
    return (m.group(1), int(m.group(2) or 0)) if m else (name, 0)


def _docx_text(z: zipfile.ZipFile, notes: list[str]) -> list[str]:
    """Seiten einer docx-Datei: Kopfzeilen vorn auf Seite 1, Fußzeilen hinten auf Seite 1, Fuß- und Endnoten
    auf der letzten Seite. Gleiche Kopf- oder Fußzeilen (erste Seite, gerade Seiten) nur einmal."""
    names = set(z.namelist())
    pages = _xml_text(_read_member(z, "word/document.xml")).split("\f")
    extra: dict[str, list[str]] = {"header": [], "footer": []}
    for name in sorted((n for n in names if _PART_RE.search(n)), key=_natural):
        text = _xml_text(_read_member(z, name)).replace("\f", "\n").strip()
        kind = _PART_RE.search(name).group(1)
        if text and text not in extra[kind]:
            extra[kind].append(text)
    notes_text = [t for n in ("word/footnotes.xml", "word/endnotes.xml") if n in names
                  for t in [_xml_text(_read_member(z, n)).replace("\f", "\n").strip()] if t]
    pages[0] = "\n".join([*extra["header"], pages[0], *extra["footer"]])
    if notes_text:
        pages[-1] = "\n".join([pages[-1], *notes_text])
    chars = sum(1 for p in pages for c in p if not c.isspace())
    if chars < MIN_TEXT_CHARS and any(n.startswith("word/media/") for n in names):
        notes.append(f"{NOTE_PREFIX} Word-Datei enthält fast nur Bilder – Bilder in Word-Dateien werden nicht "
                     "per Texterkennung gelesen")
    return pages


def _textutil(data: bytes, fmt: str) -> str:
    """Text über macOS textutil – nur über stdin/stdout, nie mit dem Pfad des Originals."""
    try:
        proc = subprocess.run([TEXTUTIL, "-stdin", "-stdout", "-convert", "txt", "-format", fmt,
                               "-encoding", "UTF-8"],
                              input=data, capture_output=True, timeout=TEXTUTIL_TIMEOUT,
                              cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired as e:
        raise ExtractError(f"Word-Datei: Zeitlimit beim Lesen überschritten ({TEXTUTIL_TIMEOUT} s)") from e
    except OSError as e:
        raise ExtractError(f"Word-Datei nicht lesbar (textutil nicht verfügbar: {e.strerror or e})") from e
    text = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    # textutil meldet Lesefehler nur auf stderr und endet trotzdem mit 0
    if proc.returncode != 0 or (not text.strip() and err):
        raise ExtractError("Word-Datei beschädigt oder nicht lesbar" + (f" (textutil: {err[:200]})" if err else ""))
    return text


def _is_encrypted_ole(data: bytes) -> bool:
    """Passwortgeschützte docx-Dateien sind OLE-Container mit dem Strom „EncryptedPackage“."""
    return "EncryptedPackage".encode("utf-16-le") in data


def is_word_ole(data: bytes) -> bool:
    """OLE-Datei mit Word-Inhalt (nicht Excel, Outlook-.msg, PowerPoint)."""
    return "WordDocument".encode("utf-16-le") in data


def is_word_zip(data: bytes) -> bool:
    """ZIP-Datei mit Word-Inhalt (docx/dotx/dotm) oder OpenDocument-Text."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if "word/document.xml" in names:
                return True
            return "mimetype" in names and _read_member(z, "mimetype").strip() == _ODT_MIME
    except (zipfile.BadZipFile, ExtractError, KeyError, OSError, RuntimeError):
        return False


def word_text(data: bytes, notes: list[str]) -> list[str]:
    """Texte der Seiten einer Word-Datei (als Bytes)."""
    kind = sniff(data)
    if kind == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                if "word/document.xml" in names:
                    return _docx_text(z, notes)
                if "mimetype" in names and _read_member(z, "mimetype").strip() == _ODT_MIME:
                    return _textutil(data, "odt").split("\f")
        except zipfile.BadZipFile as e:
            raise ExtractError(f"Word-Datei beschädigt oder nicht lesbar ({e})") from e
        except (KeyError, RuntimeError, NotImplementedError, OSError) as e:   # fehlender Teil, verschlüsselt …
            raise ExtractError(f"Word-Datei beschädigt oder nicht lesbar ({e})") from e
        raise ExtractError("Word-Datei nicht lesbar (ZIP-Datei ohne Word-Inhalt)")
    if kind == "ole":
        if _is_encrypted_ole(data):
            raise ExtractError(PASSWORD_MSG)
        return _textutil(data, "doc").split("\f")
    if kind == "rtf":
        return _textutil(data, "rtf").split("\f")
    raise ExtractError("Word-Datei nicht lesbar (unbekanntes Format)")


def word_pages(data: bytes, notes: list[str]) -> list[PageText]:
    """Seiten einer Word-Datei (als Bytes). Leere Datei: eine leere Seite mit Hinweis (über den Namen findbar)."""
    if not data:
        notes.append(f"{NOTE_PREFIX} leere Datei (0 Byte)")
        return [PageText(1, "", "text")]
    texts = [t.replace("\r\n", "\n").replace("\r", "\n") for t in word_text(data, notes)]
    while len(texts) > 1 and not texts[-1].strip():    # Umbruch am Dokumentende ergibt keine eigene Seite
        texts.pop()
    return [PageText(i + 1, t.strip("\n"), "text") for i, t in enumerate(texts)]


def extract_word(src: Path | bytes, notes: list[str]) -> list[PageText]:
    """Word-Datei von der Platte oder aus Bytes (Mail-Anhang) lesen. Originale werden nur gelesen."""
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = bytes(src)
    else:
        size = os.stat(src).st_size
        if size > MAX_WORD_BYTES:
            raise ExtractError(f"Word-Datei zu groß ({size // (1024 * 1024)} MB)")
        with open(src, "rb") as f:
            data = f.read()
    return word_pages(data, notes)
