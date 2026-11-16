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


# --- E-Mails und Word (nur erfundene Daten, Adressen auf .example) --------------------------------------

def make_eml(path: Path, *, subject: str | None = "Rechnung",
             sender: str | None = "Erika Beispiel <erika@firma.example>",
             to: str | None = "Hausverwaltung Nord <info@hv-nord.example>", cc: str | None = None,
             date: str | None = "Fri, 12 Mar 2021 14:05:00 +0100", text: str | None = None, html: str | None = None,
             attachments=(), inline=(), headers: dict | None = None) -> Path:
    """Synthetische E-Mail schreiben. attachments: [(Name, Bytes, "typ/untertyp")] oder angehängte
    EmailMessage (message/rfc822); inline: [(Name, Bytes, "typ/untertyp")] mit Content-Disposition inline."""
    from email import policy
    from email.message import EmailMessage
    msg = EmailMessage(policy=policy.SMTP)
    for key, value in (("Subject", subject), ("From", sender), ("To", to), ("Cc", cc), ("Date", date)):
        if value is not None:
            msg[key] = value
    for key, value in (headers or {}).items():
        msg[key] = value
    if html is not None and text is not None:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(text if text is not None else "")
    for i, (name, data, mime) in enumerate(inline):
        maintype, subtype = mime.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name, disposition="inline",
                           cid=f"<bild{i}@mail.example>")
    for att in attachments:
        if isinstance(att, EmailMessage):
            msg.add_attachment(att)
            continue
        name, data, mime = att
        maintype, subtype = mime.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(msg.as_bytes())
    return path


def docx_bytes(paragraphs: list[str], *, header: str | None = None, footer: str | None = None,
               footnote: str | None = None, page_break_before: int | None = None, table: list[list[str]] | None = None,
               fallback_dup: str | None = None, images: int = 0) -> bytes:
    """Kleine, gültige docx-Datei im Speicher (zipfile), ohne Word."""
    import io
    import zipfile
    from xml.sax.saxutils import escape
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    mc = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    body = []
    for i, p in enumerate(paragraphs):
        br = '<w:r><w:br w:type="page"/></w:r>' if page_break_before == i else ""
        body.append(f'<w:p>{br}<w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r></w:p>')
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{escape(c)}</w:t></w:r></w:p></w:tc>" for c in row)
                       + "</w:tr>" for row in table)
        body.append(f"<w:tbl>{rows}</w:tbl>")
    if fallback_dup:
        body.append(f'<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:t>{escape(fallback_dup)}</w:t>'
                    f'</mc:Choice><mc:Fallback><w:t>{escape(fallback_dup)}</w:t></mc:Fallback>'
                    '</mc:AlternateContent></w:r></w:p>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types '
                               'xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document {w} {mc}><w:body>'
                             + "".join(body) + "</w:body></w:document>",
    }
    for name, content in (("word/header1.xml", header), ("word/footer1.xml", footer)):
        if content is not None:
            tag = "hdr" if "header" in name else "ftr"
            files[name] = f'<w:{tag} {w}><w:p><w:r><w:t>{escape(content)}</w:t></w:r></w:p></w:{tag}>'
    if footnote is not None:
        files["word/footnotes.xml"] = (f'<w:footnotes {w}><w:footnote><w:p><w:r><w:t>{escape(footnote)}</w:t></w:r>'
                                       "</w:p></w:footnote></w:footnotes>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
        for i in range(images):
            z.writestr(f"word/media/image{i + 1}.png", b"\x89PNG\r\n\x1a\n" + bytes(64))
    return buf.getvalue()


def textutil_word(tmp: Path, name: str, text: str) -> Path:
    """Word-Testdatei (.doc/.docx/.rtf/.odt) mit macOS textutil aus einer Textdatei in tmp erzeugen."""
    import subprocess
    src = tmp / (name + ".txt")
    src.write_text(text, encoding="utf-8")
    out = tmp / name
    fmt = out.suffix.lstrip(".")
    subprocess.run(["/usr/bin/textutil", "-convert", fmt, str(src), "-output", str(out)], check=True,
                   capture_output=True, timeout=60)
    src.unlink()
    return out


class BytesEngine:
    """Ersatz-OCR ohne Vision: liefert für jedes Bild einen festen Text und zählt die Aufrufe
    (recognize_bytes = Mail-Anhang, recognize_image = gerenderte PDF-Seite)."""

    def __init__(self, text="Belegfoto Baumarkt Nord Kassenbon Summe 11,48 EUR Musterstadt", conf=0.9):
        from belegsuche.ocr import OcrResult
        self._result = OcrResult(text, conf)
        self.calls = 0

    def recognize_image(self, img):
        self.calls += 1
        return self._result

    def recognize_file(self, path):
        self.calls += 1
        return self._result

    def recognize_bytes(self, data):
        self.calls += 1
        return self._result


# --- winmail.dat (TNEF) bauen – nur erfundene Daten --------------------------------------------------------
# Aufbau wie im Bestand vermessen (MS-OXTNEF): Signatur, Schlüssel, Attribute (Ebene, Kennung, Länge, Daten,
# Prüfsumme); Anhänge in den drei vorkommenden Bauformen der Eigenschaften (A „Datei“, B „inline“, C „minimal“).

TNEF_SIG = b"\x78\x9f\x3e\x22"
PSETID_COMMON = bytes.fromhex("0820060000000000c000000000000046")
PS_PUBLIC_STRINGS = bytes.fromhex("2903020000000000c000000000000046")
IID_IMESSAGE = bytes.fromhex("0703020000000000c000000000000046")
IID_ISTORAGE = bytes.fromhex("0b00000000000000c000000000000046")
PT_SHORT, PT_LONG, PT_BOOLEAN, PT_SYSTIME, PT_CLSID = 0x0002, 0x0003, 0x000B, 0x0040, 0x0048
PT_STRING8, PT_UNICODE, PT_BINARY, PT_OBJECT, MV = 0x001E, 0x001F, 0x0102, 0x000D, 0x1000


def tnef_attr(level: int, att_id: int, data: bytes, checksum: int | None = None) -> bytes:
    import struct
    cks = sum(data) & 0xFFFF if checksum is None else checksum
    return struct.pack("<BII", level, att_id, len(data)) + data + struct.pack("<H", cks)


def _pad4(b: bytes) -> bytes:
    return b + bytes(-len(b) % 4)


def _mapi_one(base: int, v) -> bytes:
    """Ein Wert: feste Typen auf 4 Byte aufgefüllt, variable mit Länge (ohne Anzahl)."""
    import struct
    if base in (PT_SHORT, PT_BOOLEAN):
        return struct.pack("<H", int(v) & 0xFFFF) + b"\x00\x00"
    if base == PT_LONG:
        return struct.pack("<I", int(v) & 0xFFFFFFFF)
    if base in (PT_SYSTIME, 0x0005, 0x0006, 0x0007, 0x0014):
        return struct.pack("<Q", int(v))
    if base == PT_CLSID:
        return bytes(v)
    if base == PT_UNICODE:
        raw = v.encode("utf-16-le") + b"\x00\x00"
    elif base == PT_STRING8:
        raw = (v if isinstance(v, bytes) else v.encode("cp1252")) + b"\x00"
    else:
        raw = bytes(v)
    return struct.pack("<I", len(raw)) + _pad4(raw)


def mapi_props(props) -> bytes:
    """MAPI-Eigenschaftsliste: [(Typ, Nummer, Wert)] oder (Typ, Nummer, Wert, (GUID, Nummer oder Name)) für
    benannte Eigenschaften (Nummer ab 0x8000). Typ | MV: Wert ist eine Liste."""
    import struct
    out = struct.pack("<I", len(props))
    for p in props:
        ptype, pid, value = p[:3]
        out += struct.pack("<HH", ptype, pid)
        if len(p) > 3:
            guid, ident = p[3]
            out += guid
            if isinstance(ident, int):
                out += struct.pack("<II", 0, ident)
            else:
                name = ident.encode("utf-16-le") + b"\x00\x00"
                out += struct.pack("<II", 1, len(name)) + _pad4(name)
        base = ptype & ~MV
        if ptype & MV:
            out += struct.pack("<I", len(value)) + b"".join(_mapi_one(base, x) for x in value)
        elif base in (PT_UNICODE, PT_STRING8, PT_BINARY, PT_OBJECT):
            out += struct.pack("<I", 1) + _mapi_one(base, value)
        else:
            out += _mapi_one(base, value)
    return out


def tnef_attachment(data: bytes | None, name: str, *, variant: str = "A", title: str | None = None,
                    mime: str | None = None, cid: str | None = None, method: int = 1, metafile: bool = False,
                    dates: bool = False, rend: bool = True, data_in_props: bool = False, embedded: bytes | None = None,
                    ole: bytes | None = None, name8: bool = False, extra=()) -> bytes:
    """Ein Anhang (Ebene 2). variant A: Datei (Name in 0x3707/0x3001/0x3704), B: inline (MIME-Tag, Content-ID,
    Kennzeichen 4, versteckt), C: minimal (Name nur in 0x3704). attAttachTitle hat „?“ statt Umlauten."""
    import struct
    out = b""
    if rend:
        out += tnef_attr(2, 0x00069002, struct.pack("<HIHHI", 1, 0xFFFFFFFF, 0, 0, 0))
    if data is not None and not data_in_props:
        out += tnef_attr(2, 0x0006800F, data)
    t = title if title is not None else "".join(c if ord(c) < 128 else "?" for c in name)
    out += tnef_attr(2, 0x00018010, t.encode("ascii", "replace") + b"\x00")
    if metafile:
        out += tnef_attr(2, 0x00068011, b"\x01\x00\x09\x00\x00\x03" + bytes(200))
    if dates:
        out += tnef_attr(2, 0x00038012, bytes(14)) + tnef_attr(2, 0x00038013, bytes(14))
    s = PT_STRING8 if name8 else PT_UNICODE
    ext = ("." + name.rsplit(".", 1)[-1]) if "." in name else ""
    props = [(PT_LONG, 0x0E21, 1), (PT_BINARY, 0x0FF9, bytes(range(16))), (PT_LONG, 0x0FFE, 7)]
    if variant in ("A", "B"):
        props += [(s, 0x3001, name), (PT_LONG, 0x340D, 0x40000), (s, 0x3703, ext), (s, 0x3704, "ANHANG~1" + ext[:4]),
                  (PT_LONG, 0x3705, method), (s, 0x3707, name)]
        if variant == "A":
            props += [(PT_BINARY, 0x3709, b"\x01\x00\x09\x00"), (PT_LONG, 0x370B, -1), (PT_LONG, 0x3714, 0),
                      (PT_BOOLEAN, 0x7FFE, False)]
        else:
            props += [(PT_LONG, 0x370B, -1), (s, 0x370E, mime or "image/png"),
                      (s, 0x3712, cid or "image001.png@01DA0000.00000000"), (PT_LONG, 0x3714, 4),
                      (PT_BOOLEAN, 0x7FFE, True)]
    else:
        props += [(PT_LONG, 0x340D, 0x40000), (s, 0x3704, name), (PT_LONG, 0x3705, method), (PT_LONG, 0x370B, -1)]
        if cid:
            props.append((s, 0x3712, cid))
    if data_in_props and data is not None:
        props.append((PT_BINARY, 0x3701, data))
    if embedded is not None:
        props.append((PT_OBJECT, 0x3701, IID_IMESSAGE + embedded))
    if ole is not None:
        props.append((PT_OBJECT, 0x3701, IID_ISTORAGE + ole))
    props += list(extra)
    out += tnef_attr(2, 0x00069005, mapi_props(props))
    return out


def tnef_bytes(attachments=(), *, codepage: int = 28591, subject: str = "Rechnung Juli",
               message_class: str = "IPM.Microsoft Mail.Note", body: str | None = None, html: bytes | None = None,
               rtf: bytes | None = None, named: bool = True, recipients: bool = True, extra_props=(),
               key: int = 0) -> bytes:
    """winmail.dat: die neun Nachrichten-Attribute wie im Bestand, attMsgProps (mit benannten Eigenschaften, eine
    davon mit ungerader Zeichenzahl), Empfängertabelle, danach die Anhänge (fertige Bytes aus tnef_attachment)."""
    import struct
    codec = {28591: "latin-1", 65001: "utf-8"}.get(codepage, "cp1252")
    out = TNEF_SIG + struct.pack("<H", key)
    out += tnef_attr(1, 0x00089006, b"\x00\x00\x01\x00")
    out += tnef_attr(1, 0x00069007, struct.pack("<II", codepage, 0))
    out += tnef_attr(1, 0x00078008, message_class.encode("ascii") + b"\x00")
    out += tnef_attr(1, 0x0004800D, b"\x02\x00")
    out += tnef_attr(1, 0x00018004, subject.encode(codec, "replace") + b"\x00")
    out += tnef_attr(1, 0x00038020, bytes(14))
    out += tnef_attr(1, 0x00018009, b"0123456789ABCDEF0123456789ABCDEF\x00")
    prop_class = "IPM.Note" if message_class == "IPM.Microsoft Mail.Note" else message_class
    props = [(PT_BOOLEAN, 0x0E1F, True), (PT_LONG, 0x1016, 3), (PT_LONG, 0x3FDE, codepage),
             (PT_UNICODE, 0x0037, subject), (PT_UNICODE, 0x001A, prop_class), (PT_SYSTIME, 0x0039, 0)]
    if named:
        props += [(PT_LONG, 0x8000, 1, (PSETID_COMMON, 0x8510)),
                  (PT_UNICODE, 0x8001, "Mustertext", (PS_PUBLIC_STRINGS, "Objektnr")),   # 8 Zeichen + NUL
                  (PT_BOOLEAN, 0x8002, False, (PS_PUBLIC_STRINGS, "Abc")),
                  (PT_UNICODE | MV, 0x8003, ["eins", "zwei"], (PS_PUBLIC_STRINGS, "Liste")),
                  (PT_CLSID, 0x8004, bytes(16), (PSETID_COMMON, 0x8582))]
    if body is not None:
        props.append((PT_UNICODE, 0x1000, body))
    if html is not None:
        props.append((PT_BINARY, 0x1013, html))
    if rtf is not None:
        props.append((PT_BINARY, 0x1009, rtf))
    props += list(extra_props)
    out += tnef_attr(1, 0x00069003, mapi_props(props))
    if recipients:
        row = mapi_props([(PT_UNICODE, 0x3001, "Hausverwaltung Nord"), (PT_UNICODE, 0x39FE, "info@hv-nord.example"),
                          (PT_LONG, 0x0C15, 1)])
        out += tnef_attr(1, 0x00069004, struct.pack("<I", 1) + row)
    return out + b"".join(attachments)


def lzfu(rtf: bytes) -> bytes:
    """RTF als PR_RTF_COMPRESSED (LZFu nur mit Literalen und Endmarke – gültig, nur nicht kleiner)."""
    import struct
    import zlib
    tokens = [bytes([b]) for b in rtf]
    end = ((207 + len(rtf)) & 0xFFF) << 4
    tokens.append(struct.pack(">H", end))
    body = bytearray()
    for i in range(0, len(tokens), 8):
        run = tokens[i:i + 8]
        body.append(sum(1 << k for k, t in enumerate(run) if len(t) == 2))
        body += b"".join(run)
    crc = zlib.crc32(bytes(body), 0xFFFFFFFF) ^ 0xFFFFFFFF
    return struct.pack("<IIII", len(body) + 12, len(rtf), 0x75465A4C, crc) + bytes(body)


def mela(rtf: bytes) -> bytes:
    """RTF unkomprimiert („MELA“)."""
    import struct
    return struct.pack("<IIII", len(rtf) + 12, len(rtf), 0x414C454D, 0) + rtf


def html_rtf(paragraphs: list[str]) -> bytes:
    """Aus HTML gekapseltes RTF (\\fromhtml1, MS-OXRTFEX), wie es Outlook schreibt."""
    def esc(s: str) -> str:
        out = []
        for c in s:
            if c in "\\{}":
                out.append("\\" + c)
            elif ord(c) < 128:
                out.append(c)
            elif c.encode("cp1252", "ignore"):
                out.append("\\'%02x" % c.encode("cp1252")[0])
            else:
                out.append("\\u%d?" % ord(c))
        return "".join(out)
    body = "".join("{\\*\\htmltag64 <p class=MsoNormal>}\\htmlrtf {\\htmlrtf0 " + esc(p)
                   + "{\\*\\htmltag244 <o:p>}{\\*\\htmltag252 </o:p>}\\htmlrtf }\\htmlrtf0 \\htmlrtf\\par}"
                   "\\htmlrtf0{\\*\\htmltag72 </p>}\r\n" for p in paragraphs)
    return ("{\\rtf1\\ansi\\ansicpg1252\\fromhtml1 \\fbidis \\deff0{\\fonttbl{\\f0\\fswiss Arial;}"
            "{\\f1\\fmodern Courier New;}}{\\colortbl\\red0\\green0\\blue0;}\r\n{\\*\\htmltag19 <html>}"
            "{\\*\\htmltag34 <head>}{\\*\\htmltag177 <style>p \\{color:red\\}</style>}{\\*\\htmltag41 </head>}"
            "{\\*\\htmltag50 <body lang=DE>}\\htmlrtf \\lang1031 \\htmlrtf0 \r\n" + body
            + "{\\*\\htmltag58 </body>}{\\*\\htmltag27 </html>}}").encode("ascii")
