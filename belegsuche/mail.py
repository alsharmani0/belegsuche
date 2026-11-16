"""E-Mails (.eml) lesen: Kopf, Mailtext und Anhänge – nur im Speicher, nichts wird geschrieben.

Seitenmodell (Tabelle pages, fortlaufend nummeriert):
- Seite 1: Mailkopf (Betreff, Von, An, Cc, Datum) und Mailtext. HTML-Mails werden mit html.parser aus der
  Standardbibliothek in Text umgewandelt (kein Netzzugriff, keine Bilder, kein AppKit).
- Danach die Seiten der Anhänge: PDF, Bilder (Texterkennung), Word, Text und weitergeleitete Mails (.eml,
  bis Tiefe 2). Jeder Anhang ist ein Teil (Tabelle parts) mit seinem Seitenbereich.

Anhänge werden an den ersten Bytes erkannt: die Mehrzahl heißt nur application/octet-stream. DOMUS-Platzhalter
(„Die Datei '…' wurde in die Dokumentenverwaltung von DOMUS … verschoben“) werden am Inhalt erkannt: kein eigener
Text, sondern ein Verweis (Teil 'link') auf die gleichnamige Datei im Vorgangsordner.

Übliche Auslassungen (Signatur-Logos, Excel, ZIP, zu große Anhänge, passwortgeschützte PDFs) sind kein Fehler
der Mail: sie stehen als Hinweis am Teil (parts.note).

winmail.dat aus Outlook (TNEF, erkannt an den ersten Bytes, nicht am Namen) wird geöffnet (belegsuche.tnef): die
winmail.dat bleibt ein Teil ohne Seiten (kind 'tnef', Hinweis „ausgewertet: 3 Anhänge“ oder der Grund), jeder
Anhang darin wird ein eigener Teil und genauso gelesen wie ein Mail-Anhang. Eingebettete Outlook-Nachrichten
darin sind wie angehängte Mails ein Teil 'mail' mit eigener Seite. Hat die Mail selbst keinen Text, aber die
winmail.dat (PR_BODY, HTML oder RTF), ist das der Mailtext.

mime_path: Lage eines Teils im MIME-Baum, wie IMAP-Abschnitte: „1“, „2.1“ … (1-basiert; eine Mail ohne
multipart hat nur „1“). „/“ trennt die Ebene einer angehängten Mail: „3/1.2“ = in Teil 3 (eine Mail) deren
Teil 1.2. „!k“ = Anhang k (1-basiert, in der Reihenfolge im Strom, übersprungene Logos mitgezählt) der
winmail.dat davor: „2!3“ = Anhang 3 der winmail.dat in Teil 2, „2!3!1“ = Anhang 1 einer eingebetteten
Outlook-Nachricht (oder winmail.dat) dort, „3/2!1“ = winmail.dat in einer angehängten Mail, „2!3/1“ = Teil 1
einer Mail (.eml) in der winmail.dat. find_part() findet den Teil darüber wieder.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from . import tnef
from .extract import (IMAGE_KINDS, MAX_IMAGE_PAGES, MIN_CROP_PX, NOTE_PREFIX, Extracted, ExtractError, MailInfo,
                      PageText, PartInfo, _page_done, extract_image_bytes, extract_pdf, image_size, sniff)

MAX_MAIL_BYTES = 200 * 1024 * 1024        # größere .eml: nur der Kopf
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024   # größere Anhänge werden nicht ausgewertet
MAX_TNEF_BYTES = 100 * 1024 * 1024        # größere winmail.dat werden nicht geöffnet (Anhänge darin: wie oben)
MAX_MAIL_PARTS = 60                       # höchstens so viele Anhänge je Mail
MAX_MAIL_PAGES = MAX_IMAGE_PAGES          # Seiten je Mail insgesamt (wie bei mehrseitigen Bildern)
MIN_INLINE_IMAGE_BYTES = 10 * 1024        # kleinere Bilder im Mailtext (Signatur-Logos) nicht erkennen
MAX_DEPTH = 2                             # angehängte Mails in angehängten Mails: so tief
MAX_TNEF_DEPTH = 2                        # winmail.dat bzw. eingebettete Outlook-Nachricht in einer winmail.dat
PLACEHOLDER_MAX_BYTES = 1024
MAX_TEXT_ATTACHMENT_BYTES = 5 * 1024 * 1024
PLACEHOLDER_RE = re.compile(r"Die Datei '([^\n]+?)' wurde in die Dokumentenverwaltung von DOMUS (CRM|NAVI) verschoben")
REUSED_NOTE = "Text übernommen (gleicher Inhalt schon eingelesen)"
ENCRYPTED_NOTE = "E-Mail ist verschlüsselt (S/MIME) – nur der Kopf ist durchsuchbar"
SAFE_NOTE = "nicht gelesen (nur Mailtext nach Zeitlimit oder Absturz)"
SAFE_MAIL_NOTE = (f"{NOTE_PREFIX} nur Mailtext eingelesen – PDF- und Bild-Anhänge nach Zeitlimit oder Absturz "
                  "übersprungen")
NOT_EVALUATED = "nicht ausgewertet"
# Zeilen im Kopf von Seite 1, die angezeigt, aber nicht als Mailtext durchsucht werden (index_body_text): Namen
# der Anhänge (stehen abgewertet in der path-Spalte, sonst stünde die Mail bei der Suche nach einem Dateinamen vor
# der Datei selbst) und das Ersatzdatum einer Mail ohne Date-Kopf
ATTACH_LINE = "Anhänge: "
LINK_LINE = "Anhang in DOMUS abgelegt: "
NO_DATE_SUFFIX = "(Änderungsdatum der Datei – Mail ohne Datum)"
DUPLICATE_NOTE = "gleicher Inhalt wie Anhang"
TNEF_NAME = "winmail.dat"
TNEF_READ = "ausgewertet"                 # Hinweis einer gelesenen winmail.dat: „ausgewertet: 3 Anhänge“
EVALUATED_KINDS = ("pdf", "image", "word", "text", "mail")
REUSABLE_KINDS = ("pdf", "image", "word")   # Seiten 1:1 übertragbar (gleicher Inhalt = gleiche Seiten)

_SIGNATURE_TYPES = {"application/pkcs7-signature", "application/x-pkcs7-signature", "application/pgp-signature"}
_ENCRYPTED_TYPES = {"application/pkcs7-mime", "application/x-pkcs7-mime", "multipart/encrypted"}
_TEXT_TYPES = {"text/plain", "text/csv", "text/html", "text/tab-separated-values"}
_TEXT_EXT = (".txt", ".csv", ".tsv", ".htm", ".html")
_WORD_EXT = (".doc", ".docx", ".dot", ".dotx", ".dotm", ".rtf", ".odt")
_MAIL_HEAD_RE = re.compile(rb"^(?:[\w-]{2,40}:[^\r\n]*\r?\n){2,}", re.ASCII)
_MAIL_HEADERS = (b"from:", b"to:", b"subject:", b"date:", b"received:", b"message-id:", b"mime-version:",
                 b"return-path:")


# --- Text säubern ---------------------------------------------------------------------------------------

def clean(s: str | None) -> str:
    """Kopfzeile/Name: Steuer- und Formatzeichen (auch Richtungswechsel U+202E) entfernen, Leerraum
    zusammenfassen, Unicode NFC (Namen vom Mac kommen oft zerlegt: u + ¨)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFC", str(s))
    s = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c for c in s
                if unicodedata.category(c) not in ("Cf", "Cs", "Co", "Cn"))
    return re.sub(r"\s+", " ", s).strip()


def _decode_bytes(raw: bytes, charset: str | None) -> str:
    """Text eines Mailteils. Ohne Angabe (oder us-ascii mit 8-Bit-Inhalt): UTF-8, sonst Windows-1252.
    iso-8859-1 wird als Windows-1252 gelesen (Obermenge, enthält z. B. das €-Zeichen). Als UTF-8 angegeben, aber
    überwiegend kein UTF-8 (falsch deklarierte alte Mails): Windows-1252."""
    cs = (charset or "").strip().strip('"').lower()
    if cs in ("", "us-ascii", "ascii", "unknown-8bit", "x-unknown", "default"):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", errors="replace")
    if cs in ("iso-8859-1", "latin-1", "latin1", "iso8859-1", "iso_8859-1", "windows-1252", "cp1252"):
        return raw.decode("cp1252", errors="replace")
    if cs in ("utf-8", "utf8"):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            high = sum(1 for b in raw if b >= 0x80)
            # echtes UTF-8 mit einzelnen kaputten Bytes: ersetzen; fast jedes 8-Bit-Byte ungültig: Windows-1252
            return raw.decode("cp1252", errors="replace") if text.count("�") * 2 > high else text
    try:
        return raw.decode(cs, errors="replace")
    except LookupError:                        # unbekannter Zeichensatz
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", errors="replace")


def _payload(part: Message) -> bytes:
    """Dekodierter Inhalt eines Blatt-Teils (base64/quoted-printable); kaputte Kodierung so weit wie möglich."""
    try:
        data = part.get_payload(decode=True)
    except Exception:
        data = None
    if data is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            data = raw.encode("utf-8", errors="surrogateescape")
        else:
            data = b""
    return data


def part_text(part: Message) -> str:
    raw = _payload(part)
    text = _decode_bytes(raw, part.get_content_charset())
    if part.get_content_type() == "text/html":
        return html_to_text(text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "head", "title", "noscript", "template", "xml", "svg", "object", "iframe"}
    BLOCK = {"p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
             "pre", "hr", "section", "article", "header", "footer", "dd", "dt", "dl", "address", "center", "form"}
    CELL = {"td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0

    def handle_starttag(self, tag, attrs):
        if tag == "body":                  # nicht geschlossener <head>: der Text beginnt trotzdem hier
            self.skip = 0
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag == "pre":
                self.pre += 1
        elif tag in self.CELL:
            self.out.append("\t")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag == "pre":
                self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    """HTML-Mailtext als Text: Skripte, Stile und Kopf weg, Absätze als Zeilen, Entities aufgelöst.
    Nur html.parser der Standardbibliothek: lädt nichts nach, führt nichts aus."""
    p = _HTMLText()
    try:
        p.feed(html)
        p.close()
    except Exception:                     # html.parser ist nachsichtig; zur Sicherheit trotzdem abfangen
        pass
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in "".join(p.out).replace("\r", "").split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- Kopf ----------------------------------------------------------------------------------------------

_EW_RE = re.compile(r"=\?[^?\s]+\?[bBqQ]\?[^?\s]*\?=")


def _decode_words(s: str) -> str:
    """=?charset?Q?…?=-Wörter (RFC 2047) in einem schon dekodierten Text auflösen; Kaputtes bleibt stehen."""
    def one(m):
        try:
            return str(make_header(decode_header(m.group(0))))
        except Exception:
            return m.group(0)
    s = re.sub(r"(\?=)[ \t\r\n]+(=\?)", r"\1\2", s)      # Leerraum zwischen zwei kodierten Wörtern zählt nicht
    return _EW_RE.sub(one, s)


def _has_8bit(v: str) -> bool:
    """Rohe 8-Bit-Zeichen in einer Kopfzeile (der Parser hält sie als Surrogate U+DC80..U+DCFF fest)."""
    return any("\udc80" <= c <= "\udcff" for c in v)


def _from_8bit(v: str) -> str:
    """Kopfzeile mit rohen 8-Bit-Zeichen (ohne =?…?=): UTF-8, sonst Windows-1252 (alte Programme, Latin-1).
    policy.default nimmt immer UTF-8 an und macht aus „Müller“ in Latin-1 „M�ller“."""
    raw = v.encode("utf-8", errors="surrogateescape")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return _decode_words(text)


def _raw_value(msg: Message, name: str) -> str | None:
    for k, v in msg.raw_items():
        if k.lower() == name.lower():
            return v if isinstance(v, str) else str(v)
    return None


def _raw_header(msg: Message, name: str) -> str:
    """Kopfzeile ohne die Prüfungen von policy.default (die bei kaputten Adressen Ausnahmen werfen können)."""
    v = _raw_value(msg, name)
    if v is None:
        return ""
    if _has_8bit(v):
        return _from_8bit(v)
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return v


def header(msg: Message, name: str) -> str:
    raw = _raw_value(msg, name)
    if raw is not None and _has_8bit(raw):
        return clean(_from_8bit(raw))
    try:
        v = msg.get(name)
        v = "" if v is None else str(v)
    except Exception:
        v = _raw_header(msg, name)
    return clean(v)


def _mail_date(msg: Message) -> datetime | None:
    try:
        h = msg.get("Date")
        dt = getattr(h, "datetime", None)
        if dt is None and h is not None:
            dt = parsedate_to_datetime(str(h))
        return dt
    except Exception:
        raw = _raw_header(msg, "Date")
        try:
            return parsedate_to_datetime(raw) if raw else None
        except Exception:
            return None


@dataclass
class _Head:
    info: MailInfo
    lines: list[str]


def read_head(msg: Message, mtime: float | None) -> _Head:
    subject, sender = header(msg, "Subject"), header(msg, "From")
    to, cc = header(msg, "To"), header(msg, "Cc")
    dt = _mail_date(msg)
    lines = [f"Betreff: {subject}" if subject else "Betreff: (ohne Betreff)"]
    if sender:
        lines.append(f"Von: {sender}")
    if to:
        lines.append(f"An: {to}")
    if cc:
        lines.append(f"Cc: {cc}")
    if dt is not None:
        lines.append(f"Datum: {dt.strftime('%d.%m.%Y %H:%M')}")
    elif mtime is not None:
        # nur zur Anzeige: nicht durchsuchbar (index_body_text) und nicht in mails.sent_at – sonst träfe die Suche
        # nach einem Exportdatum alle Mails ohne Date-Kopf
        fdt = datetime.fromtimestamp(mtime).astimezone()
        lines.append(f"Datum: {fdt.strftime('%d.%m.%Y %H:%M')} {NO_DATE_SUFFIX}")
    if header(msg, "X-Unsent") == "1":
        lines.append("Entwurf (nicht gesendet)")
    sent_at = None
    if dt is not None:
        try:
            sent_at = dt.isoformat(timespec="seconds")
        except (ValueError, OverflowError):
            sent_at = None
    recipients = ", ".join(x for x in (to, cc) if x) or None
    info = MailInfo(subject=subject or None, sender=sender or None, recipients=recipients, sent_at=sent_at,
                    message_id=header(msg, "Message-ID") or None)
    return _Head(info, lines)


# --- MIME-Baum -----------------------------------------------------------------------------------------

@dataclass
class _Leaf:
    part: Message
    path: str                              # mime_path innerhalb der aktuellen Mail
    alt: int | None = None                 # id() des nächsten multipart/alternative darüber
    text: str | None = None                # kaputtes multipart (Grenze fehlt): roher Inhalt


_B64_LINE_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_MIME_LINE_RE = re.compile(r"(?i)(?:content-[\w-]+|mime-version)\s*:|--[^\s]{1,200}$")


def _raw_mime_text(raw: str) -> str:
    """Rohinhalt eines multipart-Teils, dessen Grenze im Inhalt fehlt: lesbarer Text ohne base64-Blöcke (kodierte
    Anhänge), MIME-Kopfzeilen und Grenzzeilen – sonst landeten Megabytes Zeichensalat im Suchindex."""
    out: list[str] = []
    in_head = False                    # Fortsetzungszeilen einer übersprungenen MIME-Kopfzeile
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if in_head and s and line[:1] in (" ", "\t"):
            continue
        in_head = bool(_MIME_LINE_RE.match(s))
        if in_head or _B64_LINE_RE.fullmatch(s):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _leaves(msg: Message) -> list[_Leaf]:
    out: list[_Leaf] = []
    stack: list[tuple[Message, str, int | None]] = [(msg, "", None)]
    while stack:
        part, path, alt = stack.pop()
        ctype = part.get_content_type()
        if part.is_multipart() and ctype != "message/rfc822":
            payload = part.get_payload()
            kid_alt = id(part) if ctype == "multipart/alternative" else alt
            kids = [(k, f"{path}.{i}" if path else str(i), kid_alt) for i, k in enumerate(payload, 1)
                    if isinstance(k, Message)]
            stack.extend(reversed(kids))
            continue
        leaf_path = path or "1"
        if part.get_content_maintype() == "multipart":       # Grenze fehlt: Inhalt ist ein String
            raw = part.get_payload()
            if isinstance(raw, str) and (text := _raw_mime_text(raw)).strip():
                out.append(_Leaf(part, leaf_path, alt, text=text))
            continue
        out.append(_Leaf(part, leaf_path, alt))
    return out


def _filename_8bit(part: Message) -> str | None:
    """Dateiname aus Kopfzeilen mit rohen 8-Bit-Zeichen (Latin-1/UTF-8 ohne =?…?=), siehe _from_8bit."""
    m = Message()                                    # compat32: Parameter ohne die UTF-8-Annahme
    for k, v in part.raw_items():
        if k.lower() in ("content-disposition", "content-type"):
            m[k] = _from_8bit(v) if _has_8bit(v) else v
    try:
        name = m.get_filename()
    except Exception:
        return None
    return _decode_words(name) if name else None


def _filename(part: Message) -> str:
    try:
        name = part.get_filename()
    except Exception:
        name = None
    if name and "�" in name:
        name = _filename_8bit(part) or name
    if not name:
        try:
            name = part.get_param("name")
            if isinstance(name, tuple):           # RFC 2231
                name = name[2]
        except Exception:
            name = None
    return _plain_name(name)


def _plain_name(name: str | None) -> str:
    """Nur der Dateiname, kein Pfad; ohne Steuerzeichen, Unicode NFC."""
    return clean(name).replace("\\", "/").rsplit("/", 1)[-1] if name else ""


def _disposition(part: Message) -> str | None:
    try:
        return part.get_content_disposition()
    except Exception:
        return None


def _is_body_text(leaf: _Leaf) -> bool:
    if leaf.text is not None:
        return True
    p = leaf.part
    return (p.get_content_type() in ("text/plain", "text/html") and _disposition(p) != "attachment"
            and not _filename(p))


def _body_text(msg: Message, leaves: list[_Leaf]) -> tuple[str, set[int]]:
    """Mailtext: get_body() (bevorzugt text/plain), dazu weitere Textteile (z. B. Text zwischen Inline-Bildern).
    Von jeder multipart/alternative-Gruppe nur eine Fassung. get_body() bricht bei kaputten Mails mit leerem
    multipart/related ab – dann zählt der eigene Durchgang. Rückgabe: (Text, ids der verwendeten Teile)."""
    try:
        primary = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        primary = None
    cands = [lf for lf in leaves if _is_body_text(lf)]
    groups: dict[int, list[_Leaf]] = {}
    order: list[int] = []
    for lf in cands:
        key = lf.alt if lf.alt is not None else id(lf.part)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(lf)
    texts, used = [], set()
    for key in order:
        grp = groups[key]
        # bevorzugt: was get_body() gewählt hat, sonst text/plain; ist diese Fassung leer, die nächste
        prefs = sorted(grp, key=lambda lf: (lf.part is not primary,
                                           lf.text is not None or lf.part.get_content_type() != "text/plain"))
        for pick in prefs:
            try:
                t = pick.text if pick.text is not None else part_text(pick.part)
            except Exception:
                t = ""
            if t.strip():
                texts.append(t.strip("\n"))
                break
        used.update(id(lf.part) for lf in grp)
    return "\n\n".join(texts), used


# --- Anhänge -------------------------------------------------------------------------------------------

def _looks_like_mail(data: bytes) -> bool:
    head = data[:4096].lstrip()
    if not _MAIL_HEAD_RE.match(head):
        return False
    low = head.lower()
    return sum(1 for h in _MAIL_HEADERS if low.startswith(h) or b"\n" + h in low) >= 2


def _placeholder_name(data: bytes) -> str | None:
    data = data.removeprefix(b"\xef\xbb\xbf")
    if len(data) >= PLACEHOLDER_MAX_BYTES or not data.startswith(b"Die "):
        return None
    m = PLACEHOLDER_RE.match(_decode_bytes(data, None).lstrip("﻿"))
    return clean(m.group(1)) if m else None


@dataclass
class _Att:
    part: Message | None              # None: Anhang in einer winmail.dat
    path: str
    name: str
    mime: str
    inline: bool
    data: bytes
    kind: str                         # pdf | image | word | text | mail | link | tnef | skipped | ignore
    note: str | None = None
    link: str | None = None           # DOMUS-Platzhalter: genannter Dateiname
    _sha: str | None = None
    charset: str | None = None        # Zeichensatz laut Mail (Textanhänge)
    hint: str | None = None           # zusätzlicher Hinweis am Teil (z. B. Daten abgeschnitten)
    embedded: bool = False            # eingebettete Outlook-Nachricht (data ist ein TNEF-Strom)
    tdepth: int = 1                   # bei kind 'tnef' und eingebetteten Nachrichten: Ebene des TNEF-Stroms
    tnef_msg: tnef.Message | None = None  # gelesene winmail.dat (ohne Anhänge): Mailtext, falls die Mail keinen hat
    inner: list[str] = field(default_factory=list)   # Namen der Anhänge in der winmail.dat (Anzeige)

    @property
    def sha(self) -> str:
        if self._sha is None:
            self._sha = hashlib.sha256(self.data).hexdigest()
        return self._sha


def _encoded_rfc822(part: Message) -> str | None:
    """Angehängte Mail mit base64/quoted-printable (eigentlich nicht erlaubt, kommt aber vor): Rohtext der Kodierung.
    Der Parser zerlegt so einen Teil trotzdem als Mail – dann steht die ganze Kodierung als Text in deren Inhalt."""
    try:
        cte = str(part.get("content-transfer-encoding", "")).strip().lower()
    except Exception:
        return None
    if cte not in ("base64", "quoted-printable"):
        return None
    try:
        inner = part.get_payload(0)
        raw = inner.get_payload() if not inner.is_multipart() else None
        if not isinstance(raw, str):
            return None
        keys = [k for k, _ in inner.raw_items()]
    except Exception:
        return None
    # kodierter Text hat keine Kopfzeilen; gingen welche durch, steht der Anfang dort – dann aufgeben
    return raw if not keys else None


def _rfc822_bytes(part: Message) -> bytes:
    """Bytes der in einem message/rfc822-Teil angehängten Mail."""
    enc = _encoded_rfc822(part)
    if enc is not None:
        try:
            cte = str(part.get("content-transfer-encoding", "")).strip().lower()
            if cte == "base64":
                b64 = re.sub(r"[^A-Za-z0-9+/]", "", enc)
                return base64.b64decode(b64 + "=" * (-len(b64) % 4))
            import quopri
            return quopri.decodestring(enc.encode("ascii", errors="surrogateescape"))
        except (binascii.Error, ValueError):
            return b""
    try:
        return part.get_payload(0).as_bytes(policy=policy.default)
    except Exception:
        return b""


def _classify(lf: _Leaf, depth: int) -> _Att:
    """Art eines Anhangs an seinem Inhalt (nicht an der Angabe in der Mail)."""
    part = lf.part
    mime = part.get_content_type()
    name = _filename(part)
    inline = _disposition(part) != "attachment"
    if mime == "message/rfc822":
        data = _rfc822_bytes(part)
        att = _Att(part, lf.path, name or "Weitergeleitete Mail.eml", mime, inline, data, "mail")
        if depth >= MAX_DEPTH:
            att.kind, att.note = "skipped", f"{NOT_EVALUATED} (zu tief verschachtelte Mail)"
        return att
    data = _payload(part)
    try:
        charset = part.get_content_charset()
    except Exception:
        charset = None
    att = _Att(part, lf.path, name, mime, inline, data, "skipped", charset=charset)
    low = name.lower()
    if mime in _SIGNATURE_TYPES or low in ("smime.p7s", "signature.asc"):
        att.kind = "ignore"
        return att
    if mime == "application/applefile":
        att.kind = "ignore"
        return att
    return _classify_content(att, depth)


def _classify_content(att: _Att, depth: int) -> _Att:
    """Art eines Anhangs an seinen ersten Bytes (gilt für Mail-Anhänge und Anhänge in einer winmail.dat)."""
    data, mime, inline = att.data, att.mime, att.inline
    low = att.name.lower()
    if not data:
        att.note = "leer (0 Byte)"
        return att
    magic = sniff(data)
    if magic is None and data[:7] == b"JVBERi0":           # PDF doppelt base64-kodiert
        try:
            inner_pdf = base64.b64decode(data, validate=False)
            if sniff(inner_pdf) == "pdf":
                att.data, magic = inner_pdf, "pdf"
        except (binascii.Error, ValueError):
            pass
    if magic == "appledouble":
        att.kind = "ignore"
        return att
    if (link := _placeholder_name(data)) is not None:
        att.kind, att.link = "link", link
        return att
    if magic == "pdf":
        att.kind = "pdf"
    elif magic in IMAGE_KINDS:
        att.kind = "image"
    elif magic == "rtf":
        att.kind = "word"
    elif magic == "zip":
        from .word import is_word_zip
        if is_word_zip(data):
            att.kind = "word"
        else:
            att.note = f"{NOT_EVALUATED} (ZIP- oder Office-Datei)"
    elif magic == "ole":
        from .word import is_word_ole
        if is_word_ole(data):
            att.kind = "word"
        else:
            att.note = f"{NOT_EVALUATED} (Office- oder Outlook-Datei)"
    elif magic == "tnef":
        att.kind = "tnef"
        att.name = att.name or TNEF_NAME
        if len(att.data) > MAX_TNEF_BYTES:
            att.note = f"{NOT_EVALUATED} (zu groß: {len(att.data) / (1024 * 1024):.0f} MB)"
    elif low.endswith(".eml") or (mime in ("application/octet-stream", "text/plain") and _looks_like_mail(data)
                                  and not low.endswith(_TEXT_EXT)):
        att.kind = "mail"
    elif mime in _TEXT_TYPES or low.endswith(_TEXT_EXT):
        att.kind = "text"
    else:
        ext = os.path.splitext(low)[1]
        att.note = f"{NOT_EVALUATED} ({ext or mime})"
    if att.kind == "mail" and depth >= MAX_DEPTH:
        att.kind, att.note = "skipped", f"{NOT_EVALUATED} (zu tief verschachtelte Mail)"
    if att.kind in EVALUATED_KINDS and len(att.data) > MAX_ATTACHMENT_BYTES:
        att.note = f"{NOT_EVALUATED} (zu groß: {len(att.data) / (1024 * 1024):.0f} MB)"
        att.kind = "skipped"
    if att.kind == "text" and len(att.data) > MAX_TEXT_ATTACHMENT_BYTES:
        att.note = f"{NOT_EVALUATED} (Textdatei zu groß)"
        att.kind = "skipped"
    if att.kind == "image" and (size := image_size(att.data)) is not None:
        if max(size) < MIN_CROP_PX[0] or min(size) < MIN_CROP_PX[1]:
            att.kind = "ignore" if inline else "skipped"
            att.note = "Bild zu klein für die Texterkennung"
    if att.kind == "image" and inline and len(att.data) < MIN_INLINE_IMAGE_BYTES:
        att.kind = "ignore"               # Signatur-Logo im Mailtext
    return att


def _classify_safe(lf: _Leaf, depth: int) -> _Att:
    try:
        return _classify(lf, depth)
    except MemoryError:
        raise
    except Exception as e:            # kaputter Teil: nur dieser Anhang fehlt, nicht die ganze Mail
        try:
            mime = lf.part.get_content_type()
        except Exception:
            mime = "application/octet-stream"
        return _Att(lf.part, lf.path, "", mime, False, b"", "skipped", note=f"nicht lesbar ({type(e).__name__})")


# --- winmail.dat (TNEF) ------------------------------------------------------------------------------------

def _tnef_child(parent: _Att, ta: tnef.Attachment, k: int, depth: int) -> _Att:
    """Anhang k einer winmail.dat (bzw. eingebetteten Outlook-Nachricht) als _Att, an den ersten Bytes erkannt wie
    ein Mail-Anhang. Inline (Content-ID, Kennzeichen „MHTML“, versteckt) wie ein Bild im Mailtext."""
    name = _plain_name(ta.name)
    mime = (clean(ta.mime).lower() if ta.mime else "") or "application/octet-stream"
    a = _Att(None, f"{parent.path}!{k}", name, mime, ta.inline, ta.data or b"", "skipped", tdepth=parent.tdepth + 1)
    if ta.truncated:
        a.hint = "unvollständig (winmail.dat abgeschnitten)"
    if ta.embedded:
        a.name, a.embedded = name or "Outlook-Nachricht", True
        if depth >= MAX_DEPTH or parent.tdepth >= MAX_TNEF_DEPTH:
            a.note = f"{NOT_EVALUATED} (zu tief verschachtelte Nachricht)"
        elif not a.data.startswith(tnef.SIGNATURE):
            a.note = "nicht lesbar (eingebettete Outlook-Nachricht)"
        elif len(a.data) > MAX_ATTACHMENT_BYTES:
            a.note = f"{NOT_EVALUATED} (zu groß: {len(a.data) / (1024 * 1024):.0f} MB)"
        else:
            a.kind = "mail"
        return a
    if ta.data is None:
        by_ref = ta.method not in (None, tnef.ATTACH_BY_VALUE, tnef.ATTACH_EMBEDDED_MSG, tnef.ATTACH_OLE)
        a.note = f"{NOT_EVALUATED} (Verweis auf eine Datei, ohne Inhalt)" if by_ref else "leer (0 Byte)"
        return a
    if mime in _SIGNATURE_TYPES or name.lower() in ("smime.p7s", "signature.asc"):
        a.kind = "ignore"
        return a
    _classify_content(a, depth)
    if a.kind == "tnef" and not a.note and parent.tdepth >= MAX_TNEF_DEPTH:
        a.note = f"{NOT_EVALUATED} (zu tief verschachtelte winmail.dat)"
    return a


def _tnef_child_safe(parent: _Att, ta: tnef.Attachment, k: int, depth: int) -> _Att:
    try:
        return _tnef_child(parent, ta, k, depth)
    except MemoryError:
        raise
    except Exception as e:            # kaputter Anhang: nur dieser fehlt
        return _Att(None, f"{parent.path}!{k}", _plain_name(ta.name), "application/octet-stream", False, b"",
                    "skipped", note=f"nicht lesbar ({type(e).__name__})")


def _tnef_children(att: _Att, depth: int) -> tuple[tnef.Message, list[_Att]]:
    """Gelesene winmail.dat (bzw. eingebettete Nachricht) und ihre Anhänge in der Reihenfolge im Strom – auch die,
    die nicht gelesen werden (Logos): Anhang k hat die Adresse „<Teil>!k“."""
    msg = tnef.parse(att.data)
    return msg, [_tnef_child_safe(att, ta, k, depth) for k, ta in enumerate(msg.attachments, 1)]


def _tnef_note(msg: tnef.Message, n: int, ignored: int) -> str:
    if n:
        note = f"{TNEF_READ}: {n} Anhang" if n == 1 else f"{TNEF_READ}: {n} Anhänge"
    elif ignored:
        note = f"{TNEF_READ}: nur Bilder im Mailtext (Logos), keine Anhänge"
    else:
        note = f"{TNEF_READ}: keine Anhänge (nur Formatierung)"
    if (msg.message_class or "").upper().startswith("IPM.NOTE.SMIME"):
        note += "; verschlüsselte Outlook-Nachricht (S/MIME)"
    if msg.notes:
        note += "; " + "; ".join(dict.fromkeys(msg.notes))
    return note


def _expand_tnef(att: _Att, depth: int) -> list[_Att]:
    """winmail.dat bzw. eingebettete Outlook-Nachricht öffnen: [sie selbst, ihre Anhänge …]. Eine winmail.dat in der
    winmail.dat wird (bis MAX_TNEF_DEPTH) gleich mit geöffnet, Logos fallen weg. Hinweis am Container: wie viele
    Anhänge, was am Strom nicht stimmte."""
    if att.note:                          # zu groß, zu tief: nicht geöffnet
        return [att]
    msg, kids = _tnef_children(att, depth)
    out: list[_Att] = []
    ignored = 0
    for kid in kids:
        if kid.kind == "tnef":
            out.extend(_expand_tnef_safe(kid, depth))
        elif kid.kind == "ignore":
            ignored += 1
        else:
            out.append(kid)
    msg.attachments = []                  # die Inhalte gehören jetzt den Teilen (Speicher früh freigeben)
    att.tnef_msg = msg
    att.inner = [a.name for a in out if a.name and a.kind != "link"]
    att.note = _tnef_note(msg, len(kids) - ignored, ignored)
    if not msg.signature_ok or not msg.n_attributes:      # gar nichts lesbar
        att.note = "nicht lesbar (" + ("; ".join(msg.notes) or "leer") + ")"
    return [att, *out]


def _expand_tnef_safe(att: _Att, depth: int) -> list[_Att]:
    try:
        return _expand_tnef(att, depth)
    except MemoryError:
        raise
    except Exception as e:
        att.note = f"nicht lesbar ({type(e).__name__})"
        return [att]


def _tnef_shas(att: _Att, depth: int) -> list[str]:
    """sha256 der Anhänge in einer winmail.dat, deren Seiten sich übernehmen lassen (wie _shas_of)."""
    if att.note:
        return []
    out: list[str] = []
    for kid in _tnef_children(att, depth)[1]:
        if kid.kind in REUSABLE_KINDS:
            out.append(kid.sha)
        elif kid.kind == "tnef":
            out.extend(_tnef_shas(kid, depth))
        elif kid.kind == "mail" and kid.embedded:
            out.extend(_tnef_shas(kid, depth + 1))
        elif kid.kind == "mail":
            out.extend(_shas_of(_parse_fast(kid.data), depth + 1))
    return out


_ALNUM_RE = re.compile(r"[^\W_]")


def _usable(text: str) -> bool:
    """Brauchbarer Mailtext: mindestens drei Buchstaben oder Ziffern (nicht leer, nicht nur Leerzeichen/Zeichen)."""
    return sum(1 for _ in zip(range(3), _ALNUM_RE.finditer(text))) >= 3


def _tnef_body(att: _Att) -> str:
    """Mailtext aus einer gelesenen winmail.dat (PR_BODY, HTML oder RTF) – leer, wenn sie keinen hat."""
    msg = att.tnef_msg
    if msg is None:
        return ""
    before = len(msg.notes)
    res = msg.text()
    if len(msg.notes) > before and att.note:
        att.note = "; ".join([att.note, *msg.notes[before:]])
    if res is None:
        return ""
    kind, content = res
    text = html_to_text(content) if kind == "html" else content.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _attachments(msg: Message, leaves: list[_Leaf], used: set[int], depth: int) -> list[_Att]:
    out: list[_Att] = []
    for lf in leaves:
        if id(lf.part) in used or lf.text is not None:
            continue
        att = _classify_safe(lf, depth)
        if att.kind == "tnef":
            out.extend(_expand_tnef_safe(att, depth))
        elif att.kind != "ignore":
            out.append(att)
    return out


def parse(data: bytes) -> Message:
    return BytesParser(policy=policy.default).parsebytes(data)


def _encrypted(msg: Message) -> bool:
    return msg.get_content_type() in _ENCRYPTED_TYPES


_MAYBE_ATTACHMENT_RE = re.compile(
    rb"(?im)^content-(?:type:\s*(?:application|image|message|audio|video|model)/|disposition:\s*attachment)")


def _parse_fast(data: bytes) -> Message:
    """Zerlegen ohne die Kopfzeilen-Objekte von policy.default: um ein Vielfaches schneller, und kein quadratischer
    Aufwand bei präparierten Kopfzeilen (z. B. 20.000 RFC-2231-Teile im Dateinamen). Die Inhalte der Teile (und
    damit ihre sha256) sind dieselben wie beim Einlesen."""
    return BytesParser(policy=policy.compat32).parsebytes(data)


def _shas_of(msg: Message, depth: int) -> list[str]:
    if _encrypted(msg):
        return []
    out: list[str] = []
    for lf in _leaves(msg):
        if _is_body_text(lf):          # Mailtext (wie _body_text): kein Anhang
            continue
        part = lf.part
        if part.get_content_type() == "message/rfc822":
            if depth + 1 > MAX_DEPTH:
                continue
            if _encoded_rfc822(part) is None and isinstance(inner := _first_payload(part), Message):
                out.extend(_shas_of(inner, depth + 1))
            else:
                out.extend(_shas_of(_parse_fast(_rfc822_bytes(part)), depth + 1))
            continue
        att = _classify_safe(lf, depth)
        if att.kind in REUSABLE_KINDS:
            out.append(att.sha)
        elif att.kind == "mail":
            out.extend(_shas_of(_parse_fast(att.data), depth + 1))
        elif att.kind == "tnef":
            out.extend(_tnef_shas(att, depth))
    return out


def _first_payload(part: Message):
    try:
        return part.get_payload(0)
    except Exception:
        return None


def attachment_shas(data: bytes, depth: int = 0) -> list[str]:
    """SHA-256 der Anhänge, deren Seiten sich von gleichem Inhalt übernehmen lassen (PDF, Bild, Word) –
    auch in angehängten Mails. Billig (nur Zerlegen, keine Texterkennung): der Hauptprozess sucht damit
    vor dem Einlesen schon bekannte Seiten. Läuft ohne Zeitlimit im Hauptprozess, daher _parse_fast."""
    if not _MAYBE_ATTACHMENT_RE.search(data):       # reine Text-/HTML-Mail: nichts zu zerlegen
        return []
    try:
        return _shas_of(_parse_fast(data), depth)
    except Exception:                  # auch RecursionError (tief verschachtelt): nur ein Vorabblick
        return []


def _resolve(msg: Message, seg: str) -> Message | None:
    if not seg:
        return msg
    if not msg.is_multipart():
        return msg if seg == "1" else None
    part = msg
    for idx in seg.split("."):
        if not idx.isdigit() or not part.is_multipart() or part.get_content_type() == "message/rfc822":
            return None
        kids = part.get_payload()
        i = int(idx) - 1
        if not 0 <= i < len(kids):
            return None
        part = kids[i]
    return part


def _tnef_step(att: _Att, k: str) -> _Att | None:
    """Anhang k der winmail.dat bzw. eingebetteten Nachricht `att` (Adresse „…!k“)."""
    if not k.isdigit() or not att.data.startswith(tnef.SIGNATURE):
        return None
    kids = _tnef_children(att, 0)[1]
    i = int(k) - 1
    return kids[i] if 0 <= i < len(kids) else None


def find_part(data: bytes, mime_path: str) -> bytes | None:
    """Inhalt (dekodiert) des Teils an mime_path (parts.mime_path) – zum Öffnen eines Anhangs, auch eines Anhangs in
    einer winmail.dat („2!3“). None, wenn es ihn nicht gibt. Vor dem Öffnen die sha256 mit parts.sha256 vergleichen."""
    try:
        msg = parse(data)
        segs = mime_path.split("/")
        for n, seg in enumerate(segs):
            seg, *steps = seg.split("!")
            part = _resolve(msg, seg)
            if part is None:
                return None
            if steps:
                att = _classify(_Leaf(part, seg), 0)
                for k in steps:
                    att = _tnef_step(att, k)
                    if att is None:
                        return None
                if n == len(segs) - 1:
                    return att.data
                if att.kind != "mail" or att.embedded:   # nur eine Mail (.eml) hat Teile hinter „/“
                    return None
                msg = parse(att.data)
                continue
            if n == len(segs) - 1:
                return _classify(_Leaf(part, seg), 0).data
            if part.get_content_type() == "message/rfc822" and _encoded_rfc822(part) is None:
                msg = part.get_payload(0)
            elif part.get_content_type() == "message/rfc822":
                msg = parse(_rfc822_bytes(part))
            else:
                msg = parse(_payload(part))
    except Exception:
        return None
    return None


# --- Einlesen ------------------------------------------------------------------------------------------

@dataclass
class _Ctx:
    engine: object
    known: dict
    safe: bool
    notes: list[str]
    pages: list[PageText] = field(default_factory=list)
    parts: list[PartInfo] = field(default_factory=list)
    seen: dict[str, int] = field(default_factory=dict)     # sha256 -> part_no innerhalb dieser Mail
    overflow: int = 0
    page_limit_hit: bool = False
    reused_ocr: int = 0
    skipped_safe: int = 0

    def add_part(self, att: _Att, kind: str, pages: list[PageText], note: str | None, prefix: str,
                 name: str | None = None) -> PartInfo:
        if att.hint:
            note = f"{note}; {att.hint}" if note else att.hint
        first = len(self.pages) + 1 if pages else None
        for p in pages:
            self.pages.append(PageText(len(self.pages) + 1, p.text, p.source, p.ocr_conf))
        info = PartInfo(part_no=len(self.parts), kind=kind, first_page=first, n_pages=len(pages),
                        name=name if name is not None else (att.name or None), mime=att.mime,
                        size=len(att.data), sha256=att.sha if att.data else None,
                        mime_path=prefix + att.path, note=note)
        self.parts.append(info)
        return info


def index_body_text(text: str) -> str:
    """Suchtext einer Seite mit Mailkopf (Seite 1, angehängte Mail): ohne die Zeilen mit den Anhangnamen und ohne
    das Ersatzdatum (Änderungszeit der Datei). Angezeigt wird die Seite unverändert (siehe db.index_file_text)."""
    head, sep, rest = text.partition("\n\n")
    keep = [ln for ln in head.split("\n")
            if not ln.startswith((ATTACH_LINE, LINK_LINE)) and not ln.endswith(NO_DATE_SUFFIX)]
    return "\n".join(keep) + sep + rest


def link_target(mail_path: Path, root: str | None, name: str) -> str | None:
    """Pfad (relativ zum eingelesenen Ordner, Schreibweise wie auf der Platte) der Datei `name` im Vorgangsordner
    der Mail – das Ziel eines DOMUS-Platzhalters. None, wenn sie dort nicht liegt."""
    return _Folder(Path(mail_path), root).find(name)


class _Folder:
    """Dateien im Vorgangsordner (Elternordner von Emails/) für die DOMUS-Platzhalter – nur gelesen."""

    def __init__(self, mail_path: Path, root: str | None):
        d = mail_path.parent
        self.dir = d.parent if d.name.casefold() == "emails" else d
        self.root = root
        self._names: dict[str, str] | None = None

    def find(self, name: str) -> str | None:
        if self._names is None:
            self._names = {}
            try:
                with os.scandir(self.dir) as it:
                    for e in it:
                        if e.name.startswith(".") or not e.is_file(follow_symlinks=False):
                            continue
                        self._names.setdefault(unicodedata.normalize("NFC", e.name).casefold(), e.name)
            except OSError:
                pass
        hit = self._names.get(unicodedata.normalize("NFC", name).casefold())
        if hit is None:
            return None
        full = os.path.join(self.dir, hit)
        base = self.root if self.root else str(self.dir)
        try:
            return os.path.relpath(full, base)
        except ValueError:
            return full


def _read_attachment(att: _Att, ctx: _Ctx, depth: int, prefix: str, folder: _Folder) -> None:
    if len(ctx.parts) - 1 >= MAX_MAIL_PARTS:
        ctx.overflow += 1
        return
    if att.kind == "link":
        ctx.add_part(att, "link", [], folder.find(att.link), prefix, name=att.link)
        return
    if att.kind == "tnef":                # winmail.dat: Teil ohne Seiten, ihre Anhänge folgen als eigene Teile
        ctx.add_part(att, "tnef", [], att.note, prefix)
        return
    if att.kind not in EVALUATED_KINDS:
        ctx.add_part(att, "skipped", [], att.note or NOT_EVALUATED, prefix)
        return
    sha = att.sha
    if sha in ctx.seen:
        if not (att.inline and att.kind == "image"):
            ctx.add_part(att, att.kind, [], f"{DUPLICATE_NOTE} {ctx.seen[sha]}", prefix)
        return
    room = MAX_MAIL_PAGES - len(ctx.pages)
    if room <= 0:
        ctx.page_limit_hit = True
        ctx.add_part(att, att.kind, [], f"{NOT_EVALUATED} (Seitenlimit der Mail erreicht)", prefix)
        return
    if att.kind == "mail":
        if att.embedded:
            info = _read_embedded(att, ctx, depth + 1, prefix, folder)
        else:
            info = _read_nested(att, ctx, depth + 1, prefix, folder)
        ctx.seen.setdefault(sha, info.part_no)
        return
    known = ctx.known.get(sha) if att.kind in REUSABLE_KINDS else None
    note = None
    pages: list[PageText] = []
    if known:
        pages = known[:room]
        note = REUSED_NOTE
        if len(known) > room:
            # gekürzt: kein „übernommen“ ohne Einschränkung, sonst übernähme eine spätere Kopie nur diese Seiten
            note = f"{REUSED_NOTE}; nur die ersten {room} von {len(known)} Seiten eingelesen"
            ctx.page_limit_hit = True
        ctx.reused_ocr += sum(1 for p in pages if p.source != "text")
    elif ctx.safe and att.kind in ("pdf", "image"):
        ctx.skipped_safe += 1
        note = SAFE_NOTE
    else:
        sub: list[str] = []
        try:
            if att.kind == "pdf":
                pages = extract_pdf(att.data, ctx.engine, max_pages=room, notes=sub)
            elif att.kind == "image":
                pages = extract_image_bytes(att.data, ctx.engine, sub, max_pages=room)
            elif att.kind == "word":
                from .word import word_pages
                pages = word_pages(att.data, sub)
                if len(pages) > room:
                    sub.append(f"{NOTE_PREFIX} nur die ersten {room} von {len(pages)} Seiten eingelesen")
                    ctx.page_limit_hit = True
                    pages = pages[:room]
            else:   # text
                t = _decode_bytes(att.data, att.charset)
                if att.mime == "text/html" or att.name.lower().endswith((".htm", ".html")):
                    t = html_to_text(t)
                pages = [PageText(1, t.replace("\r\n", "\n").replace("\r", "\n").strip(), "text")]
        except ExtractError as e:
            note = str(e)
            pages = []
        except MemoryError:
            raise
        except Exception as e:      # z. B. Pillow DecompressionBombError: nur dieser Anhang fehlt
            note = f"nicht lesbar ({type(e).__name__})"
            pages = []
        if sub:
            extra = "; ".join(n[len(NOTE_PREFIX):].strip() if n.startswith(NOTE_PREFIX) else n for n in sub)
            note = f"{note}; {extra}" if note else extra
    info = ctx.add_part(att, att.kind, pages, note, prefix)
    ctx.seen[sha] = info.part_no


def _read_message(msg: Message, ctx: _Ctx, depth: int, prefix: str, folder: _Folder,
                  mtime: float | None) -> tuple[_Head, str, list[_Att]]:
    head = read_head(msg, mtime)
    if _encrypted(msg):
        if depth == 0:     # bei einer angehängten Mail steht der Hinweis an deren Teil (_read_nested)
            ctx.notes.append(f"{NOTE_PREFIX} {ENCRYPTED_NOTE}")
        return head, "\n".join(head.lines), []
    leaves = _leaves(msg)
    body, used = _body_text(msg, leaves)
    atts = _attachments(msg, leaves, used, depth)
    if not _usable(body):
        # Mail ohne eigenen Text (Outlook mit TNEF): der Mailtext steht nur in der winmail.dat
        for a in atts:
            if a.kind == "tnef" and (t := _tnef_body(a)):
                body = t
                break
    lines = list(head.lines)
    names = _attach_names(atts)
    if names:
        lines.append(ATTACH_LINE + ", ".join(dict.fromkeys(names)))
    for a in atts:
        if a.kind == "link":
            lines.append(LINK_LINE + a.link)
    text = "\n".join(lines) + ("\n\n" + body if body else "")
    return head, text, atts


def _attach_names(atts: list[_Att]) -> list[str]:
    """Namen der Anhänge für die Zeile „Anhänge: …“; Anhänge in einer winmail.dat in Klammern dahinter."""
    inside = tuple(a.path + "!" for a in atts if a.kind == "tnef")
    return [a.name + (f" ({', '.join(dict.fromkeys(a.inner))})" if a.inner else "")
            for a in atts if a.kind != "link" and a.name and not (inside and a.path.startswith(inside))]


def _read_embedded(att: _Att, ctx: _Ctx, depth: int, prefix: str, folder: _Folder) -> PartInfo:
    """Eingebettete Outlook-Nachricht in einer winmail.dat: eigene Seite (Betreff, Absender, Text) wie eine
    angehängte Mail, danach ihre Anhänge als weitere Teile („…!k!j“)."""
    atts = _expand_tnef_safe(att, depth)[1:]
    msg, why = att.tnef_msg, att.note
    att.note = None
    if msg is None:                       # nicht lesbar
        return ctx.add_part(att, "mail", [], why, prefix)
    lines = [f"Betreff: {clean(msg.subject)}" if clean(msg.subject) else "Betreff: (ohne Betreff)"]
    if clean(msg.sender):
        lines.append(f"Von: {clean(msg.sender)}")
    names = _attach_names(atts)
    if names:
        lines.append(ATTACH_LINE + ", ".join(dict.fromkeys(names)))
    body = _tnef_body(att)
    text = "\n".join(lines) + ("\n\n" + body if body else "")
    note = "; ".join(dict.fromkeys(msg.notes)) or None      # Mängel am Strom (Prüfsumme, abgeschnitten …)
    info = ctx.add_part(att, "mail", [PageText(1, "Weitergeleitete Mail\n" + text, "text")], note, prefix)
    _page_done()
    for a in atts:
        _read_attachment(a, ctx, depth, prefix, folder)
        a.data = b""
        _page_done()
    return info


def _read_nested(att: _Att, ctx: _Ctx, depth: int, prefix: str, folder: _Folder) -> PartInfo:
    """Angehängte Mail: eigene Seite (Kopf + Text), danach ihre Anhänge als weitere Teile."""
    try:
        inner = parse(att.data)
        sub_prefix = f"{prefix}{att.path}/"
        _, text, atts = _read_message(inner, ctx, depth, sub_prefix, folder, None)
    except Exception as e:
        return ctx.add_part(att, "mail", [], f"nicht lesbar ({type(e).__name__})", prefix)
    note = ENCRYPTED_NOTE if _encrypted(inner) else None
    info = ctx.add_part(att, "mail", [PageText(1, "Weitergeleitete Mail\n" + text, "text")], note, prefix)
    _page_done()
    for a in atts:
        _read_attachment(a, ctx, depth, sub_prefix, folder)
        a.data = b""
        _page_done()
    return info


def _head_only(raw: bytes, st: os.stat_result, ctx: _Ctx, why: str) -> Extracted:
    """Nur der Mailkopf als Seite 1 (Mail zu groß oder nicht zerlegbar)."""
    msg = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    head = read_head(msg, st.st_mtime)
    ctx.notes.append(f"{NOTE_PREFIX} {why}")
    ctx.pages[:] = [PageText(1, "\n".join(head.lines), "text")]
    ctx.parts[:] = [PartInfo(0, "body", 1, 1, mime="message/rfc822", size=st.st_size)]
    return Extracted(ctx.pages, ctx.notes, ctx.parts, head.info)


def extract_mail(path: Path, engine, *, known: dict | None = None, safe: bool = False,
                 root: str | None = None) -> Extracted:
    """E-Mail einlesen (siehe Modulbeschreibung). Originale werden nur gelesen."""
    path = Path(path)
    st = os.stat(path)
    notes: list[str] = []
    ctx = _Ctx(engine, known or {}, safe, notes)
    folder = _Folder(path, root)
    if st.st_size == 0:
        notes.append(f"{NOTE_PREFIX} leere Datei (0 Byte)")
        ctx.pages.append(PageText(1, "", "text"))
        ctx.parts.append(PartInfo(0, "body", 1, 1, mime="message/rfc822", size=0))
        return Extracted(ctx.pages, notes, ctx.parts, MailInfo())
    if st.st_size > MAX_MAIL_BYTES:
        with open(path, "rb") as f:
            raw = f.read(1 << 20)
        return _head_only(raw, st, ctx, f"E-Mail zu groß ({st.st_size // (1024 * 1024)} MB) – nur der Kopf eingelesen")
    with open(path, "rb") as f:
        data = f.read()
    try:
        msg = parse(data)
        head, text, atts = _read_message(msg, ctx, 0, "", folder, st.st_mtime)
    except RecursionError:
        # tausendfach verschachtelte Teile (präpariert oder kaputt): nicht die ganze Mail verlieren
        return _head_only(data[:1 << 20], st, ctx, "Aufbau der E-Mail zu tief verschachtelt – nur der Kopf eingelesen")
    ctx.pages.append(PageText(1, text, "text"))
    ctx.parts.append(PartInfo(0, "body", 1, 1, mime="message/rfc822", size=len(data), mime_path=""))
    del data
    _page_done()
    for att in atts:
        _read_attachment(att, ctx, 0, "", folder)
        att.data = b""                    # Speicher sofort freigeben (große Anhänge)
        _page_done()
    if ctx.overflow:
        notes.append(f"{NOTE_PREFIX} nur die ersten {MAX_MAIL_PARTS} Anhänge ausgewertet "
                     f"({ctx.overflow} weitere nicht)")
    if ctx.page_limit_hit:
        notes.append(f"{NOTE_PREFIX} Seitenlimit der Mail ({MAX_MAIL_PAGES} Seiten) erreicht – "
                     "weitere Anhänge nicht eingelesen")
    if safe and ctx.skipped_safe:
        notes.append(SAFE_MAIL_NOTE)
    return Extracted(ctx.pages, notes, ctx.parts, head.info, reused_ocr_pages=ctx.reused_ocr)
