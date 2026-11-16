"""winmail.dat aus Outlook (TNEF, MS-OXTNEF) lesen – nur im Speicher, nur Standardbibliothek.

Aufbau: Signatur 0x223E9F78, Schlüssel (u16), dann Attribute: Ebene (1 = Nachricht, 2 = Anhang), Kennung (u32),
Länge (u32), Daten, Prüfsumme (u16 = Summe der Datenbytes). Ein Anhang beginnt mit attAttachRendData; seine Daten
stehen in attAttachData (oder in der Eigenschaft PR_ATTACH_DATA_BIN/_OBJ), Name und Art in den MAPI-Eigenschaften
von attAttachment. Der Mailtext steht in attMsgProps (PR_BODY, PR_HTML oder PR_RTF_COMPRESSED – RTF, mit LZFu
komprimiert, MS-OXRTFCP) oder in attBody.

Der Leser wirft nie: jede Längenangabe wird gegen den Rest geprüft (keine Allokation aus einem Längenfeld), die
Zahl der Attribute, Eigenschaften und Anhänge, die Arbeit für Eigenschaftslisten (Budget, je Strom oder für alle
winmail.dat einer Mail), die Länge von Namen und die Größe des entpackten RTF sind begrenzt. Kaputte oder
abgeschnittene Dateien liefern, was lesbar ist, dazu einen Hinweis (Message.notes). Falsche Prüfsumme: weiterlesen,
mit Hinweis.

Eingebettete Outlook-Nachrichten (PR_ATTACH_METHOD 5) sind selbst ein TNEF-Strom (PR_ATTACH_DATA_OBJ nach der
Kennung IID_IMessage): er steht in Attachment.data, Attachment.embedded ist gesetzt. Die Tiefe begrenzt der
Aufrufer (mail.py).
"""
from __future__ import annotations

import codecs
import re
import struct
import zlib
from dataclasses import dataclass, field

SIGNATURE = b"\x78\x9f\x3e\x22"            # 0x223E9F78, little endian
MAX_ATTRIBUTES = 100_000                   # Attribute je Strom
MAX_ATTACHMENTS = 1_000                    # Anhänge je Strom
MAX_PROPS = 20_000                         # Eigenschaften je Liste
MAX_VALUES = 100_000                       # Werte je Liste (Mehrfachwerte zählen einzeln)
MAX_PROP_WORK = 200_000                    # Eigenschaften und einzeln gelesene Werte aller Listen zusammen (Budget)
MAX_NAME_CHARS = 2048                      # Namen, MIME-Art, Betreff: höchstens so viele Zeichen (Anfang + Ende)
BUDGET_NOTE = "zu viele Eigenschaften – Rest übersprungen"
MAX_RTF_BYTES = 4 * 1024 * 1024            # entpacktes RTF (Schutz vor Kompressionsbomben)

# Attribute: nur die Nummer (untere 16 Bit) zählt, der Typ in den oberen Bits wird nicht geprüft
_ATT_SUBJECT = 0x8004
_ATT_MESSAGE_CLASS = 0x8008
_ATT_BODY = 0x800C
_ATT_ATTACH_DATA = 0x800F
_ATT_ATTACH_TITLE = 0x8010
_ATT_ATTACH_TRANSPORT_FILENAME = 0x9001
_ATT_ATTACH_REND_DATA = 0x9002
_ATT_MSG_PROPS = 0x9003
_ATT_ATTACHMENT = 0x9005
_ATT_OEM_CODEPAGE = 0x9007
_CONTENT_ATTRS = {_ATT_ATTACH_DATA, _ATT_ATTACH_TITLE, _ATT_ATTACH_TRANSPORT_FILENAME, _ATT_ATTACHMENT}

# MAPI-Eigenschaften (Nummer)
PR_MESSAGE_CLASS = 0x001A
PR_SUBJECT = 0x0037
PR_SENT_REPRESENTING_NAME = 0x0042
PR_SENDER_NAME = 0x0C1A
PR_BODY = 0x1000
PR_RTF_COMPRESSED = 0x1009
PR_HTML = 0x1013                           # PT_BINARY (PR_HTML) oder Text (PR_BODY_HTML)
PR_DISPLAY_NAME = 0x3001
PR_ATTACH_DATA = 0x3701                    # PT_BINARY (PR_ATTACH_DATA_BIN) oder PT_OBJECT (PR_ATTACH_DATA_OBJ)
PR_ATTACH_EXTENSION = 0x3703
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_METHOD = 0x3705
PR_ATTACH_LONG_FILENAME = 0x3707
PR_ATTACH_MIME_TAG = 0x370E
PR_ATTACH_CONTENT_ID = 0x3712
PR_ATTACH_FLAGS = 0x3714
PR_INTERNET_CPID = 0x3FDE
PR_MESSAGE_CODEPAGE = 0x3FFD
PR_ATTACHMENT_HIDDEN = 0x7FFE

ATTACH_BY_VALUE = 1
ATTACH_EMBEDDED_MSG = 5
ATTACH_OLE = 6
ATT_MHTML_REF = 4                          # PR_ATTACH_FLAGS: Bild im Mailtext

IID_IMESSAGE = bytes.fromhex("0703020000000000c000000000000046")
IID_ISTORAGE = bytes.fromhex("0b00000000000000c000000000000046")

PT_STRING8, PT_UNICODE, PT_BINARY, PT_OBJECT = 0x001E, 0x001F, 0x0102, 0x000D
_MV = 0x1000
_FIXED = {0x0002: 2, 0x0003: 4, 0x0004: 4, 0x0005: 8, 0x0006: 8, 0x0007: 8, 0x000A: 4, 0x000B: 2, 0x0014: 8,
          0x0040: 8, 0x0048: 16}
_VARIABLE = {PT_STRING8, PT_UNICODE, PT_BINARY, PT_OBJECT, 0x00FB, 0x00FD, 0x00FE}

# Codepages, die Python nicht als „cpNNNNN“ kennt. Latin-1 als Windows-1252 (Obermenge, z. B. mit €)
_CODEPAGES = {28591: "cp1252", 1252: "cp1252", 65001: "utf-8", 20127: "ascii", 1200: "utf-16-le", 1201: "utf-16-be",
              28592: "iso8859-2", 28593: "iso8859-3", 28594: "iso8859-4", 28595: "iso8859-5", 28597: "iso8859-7",
              28599: "iso8859-9", 28605: "iso8859-15", 20866: "koi8-r", 21866: "koi8-u", 10000: "mac-roman"}


class _Short(Exception):
    """Länge reicht über das Ende der Daten."""


def codec_name(cp: int | None) -> str:
    """Python-Name einer Windows-Codepage; unbekannt oder ungültig: Windows-1252."""
    if not cp:
        return "cp1252"
    name = _CODEPAGES.get(cp, f"cp{cp}")
    try:
        codecs.lookup(name)
    except LookupError:
        return "cp1252"
    return name


_WINDOWS_8BIT = re.compile(r"cp125\d")


def decode_cp(raw: bytes, codec: str) -> str:
    """Bytes einer Codepage dekodieren. In den Windows-Codepages 125x nicht belegte Bytes (in 1252: 0x81, 0x8D, 0x8F,
    0x90, 0x9D – meist Zeichen einer Symbolschrift) fallen weg, statt „\ufffd“ zu werden."""
    s = bytes(raw).decode(codec, errors="replace")
    return s.replace("\ufffd", "") if _WINDOWS_8BIT.fullmatch(codec) else s


def _decode8(raw: bytes, cp: int | None) -> str:
    return decode_cp(bytes(raw).split(b"\x00", 1)[0], codec_name(cp))


def _decode16(raw: bytes) -> str:
    raw = bytes(raw)
    if len(raw) % 2:
        raw = raw[:-1]
    return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


@dataclass
class _Str:
    """Text aus dem Strom, noch nicht dekodiert (die Codepage kann erst später im Strom stehen)."""
    raw: bytes
    unicode: bool = False

    def text(self, cp: int | None, limit: int | None = None) -> str:
        """Dekodiert; mit limit höchstens limit Zeichen: Anfang und Ende (dort steht die Endung). Ein präparierter
        Name mit Millionen Zeichen landete sonst in Teil, Anzeige und Suchindex."""
        s = _decode16(self.raw) if self.unicode else _decode8(self.raw, cp)
        if limit is not None and len(s) > limit:
            s = s[:limit // 2] + s[-(limit // 2):]
        return s


@dataclass
class Attachment:
    data: bytes | None = None
    title: str | None = None            # attAttachTitle (OEM-Codepage; Umlaute oft nur als „?“)
    long_name: str | None = None        # PR_ATTACH_LONG_FILENAME
    display_name: str | None = None     # PR_DISPLAY_NAME (bei eingebetteten Nachrichten: der Betreff)
    filename: str | None = None         # PR_ATTACH_FILENAME (kurzer Name, oft aber auch der lange)
    transport_name: str | None = None   # attAttachTransportFilename
    mime: str | None = None             # PR_ATTACH_MIME_TAG
    content_id: str | None = None       # PR_ATTACH_CONTENT_ID: Bild im Mailtext
    method: int | None = None           # PR_ATTACH_METHOD
    flags: int = 0                      # PR_ATTACH_FLAGS
    hidden: bool = False                # PR_ATTACHMENT_HIDDEN
    embedded: bool = False              # eingebettete Outlook-Nachricht: data ist ein TNEF-Strom
    ole: bool = False                   # OLE-Objekt: data ist ein Compound File
    truncated: bool = False             # Daten am Ende des Stroms abgeschnitten
    _seen: set = field(default_factory=set, repr=False)
    _raw: dict = field(default_factory=dict, repr=False)

    @property
    def name(self) -> str:
        """Bester Name: lange Form zuerst; der Titel (attAttachTitle) ersetzt Umlaute oft durch „?“."""
        for n in (self.long_name, self.display_name, self.filename, self.title, self.transport_name):
            if n and n.strip():
                return n.strip()
        return ""

    @property
    def inline(self) -> bool:
        """Bild im Mailtext (Signatur-Logo o. Ä.): Content-ID, Kennzeichen „MHTML“ oder versteckt."""
        return bool(self.content_id) or bool(self.flags & ATT_MHTML_REF) or self.hidden


@dataclass
class Message:
    message_class: str | None = None
    subject: str | None = None
    sender: str | None = None
    oem_codepage: int | None = None
    codepage: int | None = None         # PR_MESSAGE_CODEPAGE bzw. PR_INTERNET_CPID
    body: str | None = None             # PR_BODY oder attBody
    html: str | None = None             # PR_HTML / PR_BODY_HTML
    rtf_compressed: bytes | None = None
    attachments: list[Attachment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    signature_ok: bool = False
    n_attributes: int = 0               # gelesene Attribute (0: nichts lesbar)

    def text(self) -> tuple[str, str] | None:
        """Mailtext aus der TNEF: ("text", Text) oder ("html", HTML) – None, wenn keiner drinsteht.
        Reihenfolge: PR_BODY/attBody, PR_HTML, PR_RTF_COMPRESSED (RTF; aus HTML gekapseltes RTF wird wieder HTML).
        Hinweise (z. B. falsche RTF-Prüfsumme) landen in notes."""
        if self.body and self.body.strip():
            return "text", self.body
        if self.html and self.html.strip():
            return "html", self.html
        if self.rtf_compressed:
            rtf, note = decompress_rtf(self.rtf_compressed)
            if note:
                self.notes.append(note)
            if rtf:
                kind, text = rtf_to_text(rtf)
                if text.strip():
                    return kind, text
        return None


class Budget:
    """Arbeit für Eigenschaftslisten: jede Eigenschaft und jeder einzeln gelesene Wert kostet eins. Gilt für alle
    Listen eines Stroms; der Aufrufer kann ein Budget an mehrere Ströme geben (alle winmail.dat einer Mail). Schutz vor
    präparierten Listen, deren Zerlegen sonst viele Sekunden kostet; echte Ströme brauchen wenige Hundert."""
    __slots__ = ("left",)

    def __init__(self, n: int | None = None):
        self.left = MAX_PROP_WORK if n is None else n


class _Reader:
    __slots__ = ("buf", "pos", "end")

    def __init__(self, buf, pos: int = 0, end: int | None = None):
        self.buf, self.pos = buf, pos
        self.end = len(buf) if end is None else end

    @property
    def left(self) -> int:
        return self.end - self.pos

    def take(self, n: int) -> bytes:
        if n < 0 or n > self.left:
            raise _Short
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return bytes(b)

    def skip(self, n: int) -> None:
        if n < 0 or n > self.left:
            raise _Short
        self.pos += n

    def u16(self) -> int:
        if self.left < 2:
            raise _Short
        v = struct.unpack_from("<H", self.buf, self.pos)[0]
        self.pos += 2
        return v

    def u32(self) -> int:
        if self.left < 4:
            raise _Short
        v = struct.unpack_from("<I", self.buf, self.pos)[0]
        self.pos += 4
        return v


def _pad(n: int) -> int:
    return -n % 4


def parse_props(data: bytes, wanted: frozenset[int] | None = None,
                budget: Budget | None = None) -> tuple[list[tuple[int, int, list[bytes]]], str | None]:
    """MAPI-Eigenschaftsliste (attMsgProps, attAttachment): [(Typ, Nummer, [Werte als Bytes])], Hinweis.
    Benannte Eigenschaften (Nummer ab 0x8000) und Mehrfachwerte werden übersprungen bzw. mitgelesen; bei einem
    unbekannten Typ oder einer zu großen Länge endet die Liste dort (das Gelesene bleibt).
    wanted: nur diese Nummern kommen mit ihren Werten zurück; die übrigen werden ohne Kopie übersprungen (Werte fester
    Größe in einem Schritt). budget: ist es aufgebraucht, endet die Liste mit dem Hinweis BUDGET_NOTE."""
    budget = Budget() if budget is None else budget
    if budget.left <= 0:
        return [], BUDGET_NOTE
    r = _Reader(data)
    out: list[tuple[int, int, list[bytes]]] = []
    values = 0
    try:
        count = r.u32()
        if count > MAX_PROPS:
            note = f"zu viele Eigenschaften ({count})"
            count = MAX_PROPS
        else:
            note = None
        for _ in range(count):
            budget.left -= 1
            if budget.left < 0:
                return out, BUDGET_NOTE
            ptype, pid = r.u16(), r.u16()
            if pid >= 0x8000:                       # benannte Eigenschaft: GUID, Art, Nummer oder Name
                r.skip(16)
                kind = r.u32()
                if kind == 0:
                    r.skip(4)
                elif kind == 1:
                    n = r.u32()
                    r.skip(n + _pad(n) if n + _pad(n) <= r.left else n)
                else:
                    return out, "Eigenschaftsliste beschädigt (unbekannte Art einer benannten Eigenschaft)"
            base, multi = ptype & ~_MV, bool(ptype & _MV)
            if base not in _FIXED and base not in _VARIABLE:
                return out, f"Eigenschaftsliste: unbekannter Typ 0x{ptype:04X}"
            if base in _VARIABLE or multi:
                n_vals = r.u32()
                if n_vals * 4 > r.left:             # jeder Wert braucht mindestens 4 Bytes
                    raise _Short
            else:
                n_vals = 1
            values += n_vals
            if values > MAX_VALUES:
                return out, "zu viele Eigenschaftswerte"
            keep = wanted is None or pid in wanted
            if base in _FIXED and not keep:         # feste Größe, nicht gebraucht: in einem Schritt weiter
                size = _FIXED[base]
                step = size + _pad(size)
                if n_vals * step - _pad(size) > r.left:
                    raise _Short
                r.skip(min(n_vals * step, r.left))
                continue
            budget.left -= n_vals
            if budget.left < 0:
                return out, BUDGET_NOTE
            vals = []
            for _ in range(n_vals):
                if base in _VARIABLE:
                    n = r.u32()
                    if keep:
                        vals.append(r.take(n))
                    else:
                        r.skip(n)
                    r.skip(min(_pad(n), r.left))
                else:
                    size = _FIXED[base]
                    vals.append(r.take(size))
                    r.skip(min(_pad(size), r.left))
            if keep:
                out.append((ptype, pid, vals))
    except _Short:
        return out, "Eigenschaftsliste abgeschnitten"
    return out, note


def _int(vals: list[bytes]) -> int | None:
    if not vals or len(vals[0]) not in (2, 4, 8):
        return None
    v = vals[0]
    return int.from_bytes(v[:4] if len(v) >= 4 else v, "little")


def _str_of(ptype: int, vals: list[bytes]) -> _Str | None:
    if not vals:
        return None
    base = ptype & ~_MV
    if base == PT_UNICODE:
        return _Str(vals[0], True)
    if base in (PT_STRING8, PT_BINARY):
        return _Str(vals[0], False)
    return None


# Eigenschaften, die der Leser auswertet (alle übrigen werden übersprungen)
_MESSAGE_PROPS = frozenset({PR_MESSAGE_CODEPAGE, PR_INTERNET_CPID, PR_BODY, PR_SUBJECT, PR_MESSAGE_CLASS,
                            PR_SENDER_NAME, PR_SENT_REPRESENTING_NAME, PR_HTML, PR_RTF_COMPRESSED})
_ATTACHMENT_PROPS = frozenset({PR_ATTACH_LONG_FILENAME, PR_DISPLAY_NAME, PR_ATTACH_FILENAME, PR_ATTACH_MIME_TAG,
                               PR_ATTACH_CONTENT_ID, PR_ATTACH_METHOD, PR_ATTACH_FLAGS, PR_ATTACHMENT_HIDDEN,
                               PR_ATTACH_DATA})


def _apply_attachment_props(att: Attachment, props) -> None:
    for ptype, pid, vals in props:
        base = ptype & ~_MV
        if pid in (PR_ATTACH_LONG_FILENAME, PR_DISPLAY_NAME, PR_ATTACH_FILENAME, PR_ATTACH_MIME_TAG,
                   PR_ATTACH_CONTENT_ID):
            if (s := _str_of(ptype, vals)) is not None:
                att._raw[pid] = s
        elif pid == PR_ATTACH_METHOD:
            att.method = _int(vals)
        elif pid == PR_ATTACH_FLAGS:
            att.flags = _int(vals) or 0
        elif pid == PR_ATTACHMENT_HIDDEN:
            att.hidden = bool(_int(vals))
        elif pid == PR_ATTACH_DATA and vals:
            if base == PT_OBJECT:
                obj = vals[0]
                iid, payload = obj[:16], obj[16:]
                if iid == IID_IMESSAGE:
                    att.embedded = True
                elif iid == IID_ISTORAGE:
                    att.ole = True
                att._raw["obj"] = payload
            elif base == PT_BINARY:
                att._raw["bin"] = vals[0]


def _finish_attachment(att: Attachment, cp_props: int | None, cp_oem: int | None) -> None:
    raw = att._raw
    for pid, attr in ((PR_ATTACH_LONG_FILENAME, "long_name"), (PR_DISPLAY_NAME, "display_name"),
                      (PR_ATTACH_FILENAME, "filename"), (PR_ATTACH_MIME_TAG, "mime"),
                      (PR_ATTACH_CONTENT_ID, "content_id")):
        if pid in raw:
            setattr(att, attr, raw[pid].text(cp_props, MAX_NAME_CHARS) or None)
    for key, attr in (("title", "title"), ("transport", "transport_name")):
        if key in raw:
            setattr(att, attr, raw[key].text(cp_oem, MAX_NAME_CHARS) or None)
    if att.embedded and "obj" in raw:
        att.data = raw["obj"]                       # eingebettete Nachricht: der TNEF-Strom zählt
    elif att.data is None:
        att.data = raw.get("obj", raw.get("bin"))
    if att.method == ATTACH_EMBEDDED_MSG and att.data is not None and bytes(att.data[:4]) == SIGNATURE:
        att.embedded = True
    att._raw = {}
    att._seen = set()


def _apply_message_props(msg: Message, props, raw: dict) -> None:
    for ptype, pid, vals in props:
        base = ptype & ~_MV
        if pid in (PR_MESSAGE_CODEPAGE, PR_INTERNET_CPID):
            v = _int(vals)
            if v:
                raw.setdefault(f"cp{pid}", v)
        elif pid in (PR_BODY, PR_SUBJECT, PR_MESSAGE_CLASS, PR_SENDER_NAME, PR_SENT_REPRESENTING_NAME):
            if (s := _str_of(ptype, vals)) is not None:
                raw.setdefault(pid, s)
        elif pid == PR_HTML and base in (PT_UNICODE, PT_BINARY, PT_STRING8) and vals:
            raw.setdefault("html", _Str(vals[0], base == PT_UNICODE))
        elif pid == PR_RTF_COMPRESSED and base == PT_BINARY and vals:
            raw.setdefault("rtf", vals[0])


def parse(data: bytes, budget: Budget | None = None) -> Message:
    """TNEF-Strom zerlegen. Wirft nicht; was nicht lesbar ist, steht als Hinweis in Message.notes.
    budget: Arbeit für die Eigenschaftslisten (ohne Angabe: ein eigenes für diesen Strom)."""
    msg = Message()
    try:
        _parse(data, msg, Budget() if budget is None else budget)
    except MemoryError:
        raise
    except Exception as e:             # zur Sicherheit: ein Fehler im Leser darf das Einlesen nicht stoppen
        msg.notes.append(f"nicht lesbar ({type(e).__name__})")
    return msg


def _props_note(msg: Message, where: str, note: str) -> None:
    if note == BUDGET_NOTE:                         # einmal je Strom, nicht je Anhang
        if note not in msg.notes:
            msg.notes.append(note)
    else:
        msg.notes.append(f"{where}: {note}")


def _parse(data: bytes, msg: Message, budget: Budget) -> None:
    buf = memoryview(data) if not isinstance(data, memoryview) else data
    n = len(buf)
    if n < 6 or bytes(buf[:4]) != SIGNATURE:
        msg.notes.append("keine winmail.dat (TNEF-Kennung fehlt)")
        return
    msg.signature_ok = True
    pos = 6                                         # Signatur (4) + Schlüssel (2)
    raw: dict = {}
    cur: Attachment | None = None
    bad_sums = 0
    attrs = 0
    too_many = False
    while pos < n:
        if n - pos < 9:
            msg.notes.append(f"abgeschnitten ({n - pos} Bytes Rest)")
            break
        attrs += 1
        if attrs > MAX_ATTRIBUTES:
            msg.notes.append("zu viele Attribute – Rest nicht gelesen")
            break
        level = buf[pos]
        att_id, length = struct.unpack_from("<II", buf, pos + 1)
        if level not in (1, 2):
            msg.notes.append(f"beschädigt (unbekannte Ebene {level}) – Rest nicht gelesen")
            break
        start = pos + 9
        truncated = length > n - start - 2
        end = min(start + length, n) if truncated else start + length
        chunk = buf[start:end]
        if truncated:
            msg.notes.append("abgeschnitten")
        elif (sum(chunk) & 0xFFFF) != struct.unpack_from("<H", buf, end)[0]:
            bad_sums += 1
        num = att_id & 0xFFFF
        msg.n_attributes += 1
        if level == 1:
            _message_attr(msg, num, chunk, raw, budget)
        else:
            # neuer Anhang: attAttachRendData – oder (ohne sie) ein Attribut, das der Anhang schon hat, bzw. Inhalt
            # nach seinen Eigenschaften (attAttachment steht am Ende eines Anhangs)
            if num == _ATT_ATTACH_REND_DATA or cur is None or num in cur._seen \
                    or (_ATT_ATTACHMENT in cur._seen and num in _CONTENT_ATTRS):
                if len(msg.attachments) >= MAX_ATTACHMENTS:
                    if not too_many:
                        msg.notes.append(f"mehr als {MAX_ATTACHMENTS} Anhänge – Rest nicht gelesen")
                        too_many = True
                    cur = None
                    pos = end + 2
                    if truncated:
                        break
                    continue
                cur = Attachment()
                msg.attachments.append(cur)
            cur._seen.add(num)
            if num == _ATT_ATTACH_DATA:
                cur.data = bytes(chunk)
                cur.truncated = truncated
            elif num == _ATT_ATTACH_TITLE:
                cur._raw["title"] = _Str(bytes(chunk))
            elif num == _ATT_ATTACH_TRANSPORT_FILENAME:
                cur._raw["transport"] = _Str(bytes(chunk))
            elif num == _ATT_ATTACHMENT:
                props, note = parse_props(chunk, _ATTACHMENT_PROPS, budget)
                _apply_attachment_props(cur, props)
                if note:
                    _props_note(msg, f"Anhang {len(msg.attachments)}", note)
        if truncated:
            break
        pos = end + 2
    if bad_sums:
        msg.notes.append(f"Prüfsumme falsch ({bad_sums} Attribut{'e' if bad_sums > 1 else ''})")
    _finish_message(msg, raw)


def _message_attr(msg: Message, num: int, chunk, raw: dict, budget: Budget) -> None:
    if num == _ATT_OEM_CODEPAGE and len(chunk) >= 4:
        msg.oem_codepage = struct.unpack_from("<I", chunk, 0)[0] or None
    elif num == _ATT_MESSAGE_CLASS:
        raw.setdefault("class", _Str(bytes(chunk)))
    elif num == _ATT_SUBJECT:
        raw.setdefault("subject", _Str(bytes(chunk)))
    elif num == _ATT_BODY:
        raw.setdefault("body", _Str(bytes(chunk)))
    elif num == _ATT_MSG_PROPS:
        props, note = parse_props(chunk, _MESSAGE_PROPS, budget)
        _apply_message_props(msg, props, raw)
        if note:
            _props_note(msg, "Nachricht", note)


def _finish_message(msg: Message, raw: dict) -> None:
    msg.codepage = raw.get(f"cp{PR_MESSAGE_CODEPAGE}") or raw.get(f"cp{PR_INTERNET_CPID}") or None
    cp_props = msg.codepage or msg.oem_codepage
    cp_oem = msg.oem_codepage or msg.codepage

    def text(key, cp, limit=None) -> str | None:
        s = raw.get(key)
        return (s.text(cp, limit) or None) if s is not None else None
    msg.message_class = text(PR_MESSAGE_CLASS, cp_props, MAX_NAME_CHARS) or text("class", cp_oem, MAX_NAME_CHARS)
    msg.subject = text(PR_SUBJECT, cp_props, MAX_NAME_CHARS) or text("subject", cp_oem, MAX_NAME_CHARS)
    msg.sender = text(PR_SENT_REPRESENTING_NAME, cp_props, MAX_NAME_CHARS) \
        or text(PR_SENDER_NAME, cp_props, MAX_NAME_CHARS)
    msg.body = text(PR_BODY, cp_props) or text("body", cp_oem)
    msg.html = text("html", cp_props)
    msg.rtf_compressed = raw.get("rtf")
    for att in msg.attachments:
        _finish_attachment(att, cp_props, cp_oem)


# --- RTF (PR_RTF_COMPRESSED) ---------------------------------------------------------------------------

_LZFU = 0x75465A4C
_MELA = 0x414C454D
_PREBUF = (b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman \\fswiss \\fmodern \\fscript "
           b"\\fdecor MS Sans SerifSymbolArialTimes New RomanCourier{\\colortbl\\red0\\green0\\blue0\r\n\\par "
           b"\\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx")


def decompress_rtf(data: bytes, limit: int = MAX_RTF_BYTES) -> tuple[bytes | None, str | None]:
    """PR_RTF_COMPRESSED entpacken (MS-OXRTFCP: LZFu oder unkomprimiert „MELA“): (RTF, Hinweis).
    Die Größenangabe im Kopf wird nicht zum Reservieren benutzt; mehr als `limit` Bytes werden nicht erzeugt."""
    data = bytes(data)
    if len(data) < 16:
        return None, "RTF-Mailtext zu kurz"
    comp_size, raw_size, magic, crc = struct.unpack_from("<IIII", data, 0)
    if magic == _MELA:
        out = data[16:16 + min(raw_size, limit)]
        return out, ("RTF-Mailtext gekürzt" if raw_size > limit else None)
    if magic != _LZFU:
        return None, "RTF-Mailtext: unbekannte Kompression"
    src = data[16:4 + comp_size] if 12 <= comp_size and 4 + comp_size <= len(data) else data[16:]
    note = None
    if (zlib.crc32(src, 0xFFFFFFFF) ^ 0xFFFFFFFF) != crc:
        note = "RTF-Mailtext: Prüfsumme falsch"
    dic = bytearray(4096)
    dic[:len(_PREBUF)] = _PREBUF
    wp = len(_PREBUF)
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        ctrl = src[i]
        i += 1
        for bit in range(8):
            if i >= n:
                break
            if ctrl & (1 << bit):
                if i + 2 > n:
                    i = n
                    break
                ref = (src[i] << 8) | src[i + 1]
                i += 2
                off, length = ref >> 4, (ref & 0xF) + 2
                if off == wp:                       # Endmarke
                    return bytes(out), note
                for k in range(length):
                    c = dic[(off + k) & 0xFFF]
                    out.append(c)
                    dic[wp] = c
                    wp = (wp + 1) & 0xFFF
            else:
                c = src[i]
                i += 1
                out.append(c)
                dic[wp] = c
                wp = (wp + 1) & 0xFFF
            if len(out) >= limit:
                return bytes(out[:limit]), "RTF-Mailtext gekürzt (zu groß)"
    return bytes(out), note or "RTF-Mailtext ohne Endmarke"


_RTF_TOKEN = re.compile(rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|"
                        rb"([^\\{}\r\n]+)|[\r\n]+", re.S)
_SKIP_DEST = {b"fonttbl", b"colortbl", b"stylesheet", b"info", b"pict", b"object", b"objdata", b"listtable",
              b"listoverridetable", b"generator", b"themedata", b"datastore", b"latentstyles", b"xmlnstbl",
              b"rsidtbl", b"mmathPr", b"pgdsctbl", b"filetbl", b"revtbl", b"header", b"footer", b"headerl",
              b"headerr", b"headerf", b"footerl", b"footerr", b"footerf", b"fldinst", b"bkmkstart", b"bkmkend",
              b"pntext", b"pntxta", b"pntxtb", b"shpinst", b"background", b"nonshppict"}


def rtf_to_text(rtf: bytes) -> tuple[str, str]:
    """RTF als Text. Aus HTML oder Text gekapseltes RTF (\\fromhtml, \\fromtext; MS-OXRTFEX) wird entkapselt:
    ("html", ursprüngliches HTML) bzw. ("text", Text). Sonst ("text", Text des RTF, ohne Formatierung)."""
    head = rtf[:4096]
    mode = "html" if b"\\fromhtml" in head else "text"
    encapsulated = mode == "html" or b"\\fromtext" in head
    m = re.search(rb"\\ansicpg(\d{1,5})", head)
    cp = codec_name(int(m.group(1)) if m else 1252)
    out: list[str] = []
    buf = bytearray()

    def flush():
        if buf:
            out.append(decode_cp(buf, cp))
            buf.clear()

    stack: list[tuple] = []
    skip, tag, htmlrtf, uc = False, False, False, 1
    first = star = False
    pending_skip = 0                     # Ersatzzeichen nach \uN, die übersprungen werden
    for mt in _RTF_TOKEN.finditer(rtf):
        word, num, hexb, sym, brace, text = mt.groups()
        if brace is not None:
            if brace == b"{":
                if len(stack) < 1000:
                    stack.append((skip, tag, htmlrtf, uc))
                first, star = True, False
            else:
                if stack:
                    skip, tag, htmlrtf, uc = stack.pop()
                first = star = False
            pending_skip = 0
            continue
        if pending_skip and (text or hexb is not None or (sym is not None and sym != b"*")):
            if text:
                k = min(pending_skip, len(text))
                text, pending_skip = text[k:], pending_skip - k
                if not text:
                    continue
            else:
                pending_skip -= 1
                continue
        if sym == b"*":
            star = True
            continue
        if word is not None:
            if first or star:
                if word == b"htmltag" and mode == "html":
                    tag = True
                elif word == b"mhtmltag" or word in _SKIP_DEST or star:
                    skip = True
                first = star = False
                if word in (b"htmltag", b"mhtmltag"):
                    continue
            if word == b"htmlrtf":
                htmlrtf = num != b"0"
                continue
            if word == b"uc":
                uc = max(0, min(int(num or 1), 10))
                continue
            if skip or (encapsulated and htmlrtf and not tag):
                continue
            if word in (b"par", b"line", b"sect", b"page") or (word == b"row" and not encapsulated):
                flush()
                out.append("\n")
            elif word in (b"tab", b"cell"):
                flush()
                out.append("\t")
            elif word == b"u" and num:
                flush()
                v = int(num)
                out.append(chr(v + 65536 if v < 0 else v) if -65536 < v < 65536 else "\ufffd")
                pending_skip = uc
            continue
        first = star = False
        if skip or (encapsulated and htmlrtf and not tag):
            continue
        if hexb is not None:
            buf.append(int(hexb, 16))
        elif sym is not None:
            flush()
            if sym in (b"\\", b"{", b"}"):
                out.append(sym.decode())
            elif sym == b"~":
                out.append("\xa0")
            elif sym in (b"\n", b"\r"):
                out.append("\n")
            elif sym == b"_":
                out.append("-")
        elif text:
            flush()
            out.append(decode_cp(text, cp))
    flush()
    s = "".join(out)
    # \uN mit Ersatzpaaren (Emoji): zusammensetzen, einzelne Hälften ersetzen
    s = s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", errors="replace")
    return mode, s
