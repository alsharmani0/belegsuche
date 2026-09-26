"""Text-Aufbereitung für deutsche Belege.

Alles, was in den Suchindex geht, und jede Suchanfrage laufen durch dieselben
Funktionen. Dadurch passen z. B. "Müller", "Mueller" und "MÜLLER" zusammen,
und Beträge wie "1.234,56 €" werden zusätzlich als eindeutiges Token
gespeichert, das die Volltextsuche sonst in "1", "234" und "56" zerlegen würde.

Zusatz-Tokens beginnen mit einer Ziffer, damit sie nie mit getippten Wörtern
kollidieren (sonst würde z. B. "Amt" alle Beträge treffen):
    0amt1234c56   Betrag 1.234,56     0amt1234   Betrag ohne Cent
    0id202100487  Nummer ohne Trennzeichen (RE-2021/00487 -> 0idre202100487, 0id202100487)
    0dt20210312   Datum               0mo202103  Monat   0yr2021  Jahr
"""
from __future__ import annotations

import re
import unicodedata

AMT, ID, DT, MO, YR = "0amt", "0id", "0dt", "0mo", "0yr"
INTERNAL_PREFIXES = (AMT, ID, DT, MO, YR, "0hn")

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "ẞ": "ss"})
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

MONTHS = {
    "januar": 1, "jan": 1, "jaenner": 1, "februar": 2, "feb": 2, "maerz": 3, "marz": 3,
    "mrz": 3, "april": 4, "apr": 4, "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10,
    "november": 11, "nov": 11, "dezember": 12, "dez": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

# 1.234,56  89,90  1.234,-  12,5   (deutsches Format, auch eine Nachkommastelle)
AMOUNT_DE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d+),(\d{1,2}|-|–)(?![\d,])")
# 1 234,56 (DIN 5008, auch geschütztes/schmales Leerzeichen)
AMOUNT_SPACE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[    ]\d{3})+),(\d{2})(?![\d,])")
# 1234.56  1,234.56   (englisches Format; 12.03.2021 wird durch die Lookarounds ausgeschlossen)
AMOUNT_EN_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})(?![\d.,])")
# 1.151.44   (OCR hat das Komma als Punkt gelesen)
AMOUNT_OCR_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+)\.(\d{2})(?![\d.,])")
# 1.234 €  250 EUR  € 250   (ganze Euro)
AMOUNT_INT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d+)\s?(?:€|eur\b|euro\b)|(?:€|\beur\b)\s?(\d{1,3}(?:\.\d{3})+|\d+)(?![\d.,])",
    re.IGNORECASE,
)
DATE_NUM_RE = re.compile(r"(?<![\d.])(\d{1,2})\s?\.\s?(\d{1,2})\s?\.\s?(\d{4}|\d{2})(?![\d])")
DATE_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
DATE_TXT_RE = re.compile(rf"(?<![\w])(?:(\d{{1,2}})\.?\s*)?({_MONTH_ALT})\.?\s*(\d{{4}})(?!\d)")
MONTH_NUM_RE = re.compile(r"(?<![\d.,/])(\d{1,2})\s?[/.]\s?(\d{4})(?![\d.,/])")
# Nummern mit Trennzeichen: RE-2021/00487, 4711/22, INV-88213, A.12.345
ID_SEP_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9]+(?:[-/._][A-Za-z0-9]+)+)(?![\w])")


def fold(s: str) -> str:
    """Kleinschreibung, Umlaute ausschreiben (ä->ae, ß->ss), sonstige Akzente entfernen.

    NFKC zuerst, weil macOS Dateinamen zerlegt (NFD) speichert.
    """
    s = unicodedata.normalize("NFKC", s).lower().translate(_UMLAUTS)
    if s.isascii():
        return s
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """fold() Zeichen für Zeichen, mit Zuordnung jeder Position im Ergebnis zur Position im Original.

    Erwartet NFC-Text (sonst fehlt bei zerlegten Umlauten das 'e').
    """
    out: list[str] = []
    pos: list[int] = []
    for i, c in enumerate(text):
        f = fold(c)
        out.append(f)
        pos.extend([i] * len(f))
    pos.append(len(text))
    return "".join(out), pos


def words(text: str) -> list[str]:
    """Normalisierte Wort-Tokens in Textreihenfolge."""
    return WORD_RE.findall(fold(text))


def is_decimal(t: str) -> bool:
    return t.isascii() and t.isdecimal()


def _year(y: str) -> int:
    n = int(y)
    if len(y) == 2:
        n += 2000 if n < 70 else 1900
    return n


def _valid_date(y: int, m: int, d: int | None) -> bool:
    return 1900 <= y <= 2099 and 1 <= m <= 12 and (d is None or 1 <= d <= 31)


def amount_tokens(int_part: str, cents: str | None) -> list[str]:
    n = int(re.sub(r"[.,\s   ]", "", int_part))
    if cents is None or cents in ("-", "–"):
        cents = "00"
    elif len(cents) == 1:
        cents += "0"
    return [f"{AMT}{n}c{cents}", f"{AMT}{n}"]


def amounts(text: str) -> list[tuple[int, int, list[str]]]:
    """Beträge mit Position im Originaltext: (start, ende, tokens)."""
    out = []
    for rx in (AMOUNT_DE_RE, AMOUNT_SPACE_RE, AMOUNT_EN_RE, AMOUNT_OCR_RE):
        for m in rx.finditer(text):
            out.append((m.start(), m.end(), amount_tokens(m.group(1), m.group(2))))
    for m in AMOUNT_INT_RE.finditer(text):
        num = m.group(1) or m.group(2)
        out.append((m.start(), m.end(), [f"{AMT}{int(num.replace('.', ''))}"]))
    return out


def _date_matches(f: str) -> list[tuple[int, int, list[str]]]:
    out = []

    def full(y, mo, d):
        return [f"{DT}{y:04d}{mo:02d}{d:02d}", f"{MO}{y:04d}{mo:02d}", f"{YR}{y:04d}"]

    for m in DATE_NUM_RE.finditer(f):
        d, mo, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
        if _valid_date(y, mo, d):
            out.append((m.start(), m.end(), full(y, mo, d)))
    for m in DATE_ISO_RE.finditer(f):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid_date(y, mo, d):
            out.append((m.start(), m.end(), full(y, mo, d)))
    for m in DATE_TXT_RE.finditer(f):
        mo, y = MONTHS[m.group(2)], int(m.group(3))
        d = int(m.group(1)) if m.group(1) else None
        if _valid_date(y, mo, d):
            out.append((m.start(), m.end(), full(y, mo, d) if d else [f"{MO}{y:04d}{mo:02d}", f"{YR}{y:04d}"]))
    for m in MONTH_NUM_RE.finditer(f):
        mo, y = int(m.group(1)), int(m.group(2))
        if _valid_date(y, mo, None):
            out.append((m.start(), m.end(), [f"{MO}{y:04d}{mo:02d}", f"{YR}{y:04d}"]))
    return out


def dates(text: str) -> list[tuple[int, int, list[str]]]:
    """Datumsangaben: (start, ende, tokens) – Positionen beziehen sich auf fold(text)."""
    return _date_matches(fold(text))


def date_spans(text: str) -> list[tuple[int, int, list[str]]]:
    """Datumsangaben mit Positionen im Originaltext (für die Markierung im Textausschnitt)."""
    f, pos = fold_with_map(text)
    return [(pos[s], pos[e - 1] + 1, toks) for s, e, toks in _date_matches(f)]


def _compact(s: str) -> str:
    return re.sub(r"[\s\-/._]", "", s).lower()


def id_tokens_for(compact: str) -> list[str]:
    """id-Tokens für eine zusammengezogene Nummer (vollständig und ab der ersten Ziffer)."""
    if len(compact) < 4 or not any(c.isdecimal() for c in compact):
        return []
    toks = [f"{ID}{compact}"]
    first_digit = next(i for i, c in enumerate(compact) if c.isdecimal())
    tail = compact[first_digit:]
    if first_digit > 0 and len(tail) >= 4:
        toks.append(f"{ID}{tail}")
    return toks


def ids(text: str) -> list[tuple[int, int, list[str]]]:
    """Rechnungs-/Kunden-/Vertragsnummern: (start, ende, tokens) im Originaltext."""
    out = []
    for m in ID_SEP_RE.finditer(text):
        toks = id_tokens_for(_compact(m.group(1)))
        if toks:
            out.append((m.start(), m.end(), toks))
    # Nummern, die durch Leerzeichen getrennt sind: "R2021 0487", "RE 2021 00487"
    raw = [(m.start(), m.end(), m.group()) for m in WORD_RE.finditer(text)]
    i = 0
    while i < len(raw):
        s0, e0, t0 = raw[i]
        starts_ok = any(c.isdecimal() for c in t0) or (t0.isalpha() and t0.isascii() and 1 <= len(t0) <= 4 and t0.isupper())
        if not starts_ok:
            i += 1
            continue
        run = [raw[i]]
        j = i + 1
        while j < len(raw) and len(run) < 4:
            s, e, t = raw[j]
            gap = text[run[-1][1]:s]
            if gap not in (" ", "-", "/", ".", " - ", " / ") or not any(c.isdecimal() for c in t) or len(t) < 2:
                break
            run.append(raw[j])
            j += 1
        if len(run) >= 2 and (any(c.isalpha() for c in run[0][2]) or all(len(r[2]) >= 3 for r in run)):
            compact = "".join(r[2] for r in run).lower()
            if 5 <= len(compact) <= 24:
                out.append((run[0][0], run[-1][1], id_tokens_for(compact)))
        i = j if len(run) >= 2 else i + 1
    # Einzelne gemischte Tokens wie INV88213 oder R20210487
    for s, e, t in raw:
        if any(c.isdecimal() for c in t) and any(c.isalpha() for c in t) and len(t) >= 4:
            out.append((s, e, id_tokens_for(t.lower())))
    return out


def street_tokens(toks: list[str]) -> list[str]:
    """Musterstraße <-> Musterstr. <-> Muster Str. <-> Muster-Straße."""
    out = []
    for i, t in enumerate(toks):
        if len(t) > 7 and t.endswith("strasse"):
            out.append(t[:-7] + "str")
        elif len(t) >= 5 and t.endswith("str") and t.isalpha():
            out.append(t + "asse")
        if t in ("str", "strasse") and i > 0 and toks[i - 1].isalpha() and len(toks[i - 1]) >= 3:
            out.append(toks[i - 1] + "strasse")
            out.append(toks[i - 1] + "str")
    return out


_GLUED_PARTS_RE = re.compile(r"[a-z]{3,}|\d{2,}")


def glued_parts(toks: list[str]) -> list[str]:
    """Zusammengeklebte Ziffern und Wörter trennen ("98765432beispiel" -> 98765432, beispiel) – häufig in
    Listen, Kontoauszügen und bei der Texterkennung."""
    out = []
    for t in toks:
        if len(t) >= 5 and not t.isalpha() and not t.isdecimal() and any(c.isalpha() for c in t):
            parts = _GLUED_PARTS_RE.findall(t)
            if len(parts) > 1:
                out.extend(parts)
    return out


HN = "0hn"   # Hausnummer an ihrer Straße: 0hnweg7 = "... Weg 7" (auch innerhalb von "Weg 7-11")
STREET_SUFFIX = (r"(?:strasse|str|weg|allee|platz|ring|damm|gasse|chaussee|ufer|stieg|steig|twiete|kamp|redder|"
                 r"deich|pfad|wall|koppel|markt|berg|hof)")
_HOUSE_RE = re.compile(rf"(?<![\w])([a-z]*{STREET_SUFFIX})\.?\s+(\d{{1,3}})(?:\s?[-–/,]\s?(\d{{1,3}}))?(?![\d])")


def house_number_tokens(folded: str) -> list[str]:
    """Straße + Hausnummer als ein Token; Bereiche/Aufzählungen ("7-11", "7/9", "9, 11") für jede Nummer darin."""
    out = []
    for m in _HOUSE_RE.finditer(folded):
        street, a = m.group(1), int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        if not (a <= b <= a + 40):
            b = a
        variants = {street}
        if street.endswith("strasse"):
            variants.add(street[:-7] + "str")
        elif street.endswith("str"):
            variants.add(street + "asse")
        out += [f"{HN}{v}{n}" for v in variants for n in range(a, b + 1)]
    return out


def split_word_repairs(toks: list[str]) -> list[str]:
    """Von der Texterkennung zerrissene Wörter wieder zusammensetzen ("besche id" -> bescheid)."""
    out = []
    for a, b in zip(toks, toks[1:]):
        if a.isalpha() and b.isalpha() and len(a) + len(b) >= 7 and (
                (len(a) >= 4 and len(b) <= 3) or (len(a) <= 3 and len(b) >= 4)):
            out.append(a + b)
    return out


def extra_tokens(text: str) -> list[str]:
    """Alle Zusatz-Tokens (Beträge, Nummern, Daten, Straßenvarianten, getrennte Klebewörter) eines Textes."""
    extra: set[str] = set()
    for _, _, toks in amounts(text):
        extra.update(toks)
    for _, _, toks in dates(text):
        extra.update(toks)
    for _, _, toks in ids(text):
        extra.update(toks)
    ws = words(text)
    extra.update(street_tokens(ws))
    extra.update(glued_parts(ws))
    extra.update(split_word_repairs(ws))
    extra.update(house_number_tokens(fold(text)))
    return sorted(extra)


def index_text(text: str) -> str:
    """Text für die FTS-Spalte: normalisierte Wörter in Originalreihenfolge + Zusatz-Tokens."""
    return " ".join(words(text) + extra_tokens(text))


_COMPACT_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)")


def path_index_text(relpath: str) -> str:
    """FTS-Text für den Speicherort: Ordnernamen + Dateiname (ohne Endung)."""
    stem, dot, ext = relpath.rpartition(".")
    base = stem if dot and len(ext) <= 5 else relpath
    base = re.sub(r"[/\\_]+", " ", base)
    extra = []
    for m in _COMPACT_DATE_RE.finditer(base):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid_date(y, mo, d):
            extra += [f"{DT}{y:04d}{mo:02d}{d:02d}", f"{MO}{y:04d}{mo:02d}", f"{YR}{y:04d}"]
    return " ".join(words(base) + extra_tokens(base) + extra)
