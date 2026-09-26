"""Suche: Anfrage zerlegen, erweitern und Treffer bewerten.

Ablauf
1. Die Anfrage wird in Teile zerlegt: Beträge, Datumsangaben, Nummern, Wörter, "Phrasen".
2. Jeder Teil bekommt Alternativen mit Gewicht:
   exakt 1.0 · Straßen-Schreibweise 1.0 · Wortanfang (Heizung -> Heizungswartung) 0.85 ·
   Wortende (Wartung -> Heizungswartung) 0.75 · Synonym 0.75 · eingeschobener Wortteil 0.7 ·
   Tippfehler/OCR-Fehler 0.5–0.65 · Wortteil mitten im Wort 0.55.
   Nummern und Beträge werden nie unscharf gesucht, nur exakt bzw. über ihre
   zusammengezogene Form (RE-2021/00487 = 2021-00487 = RE 2021 00487).
3. Kandidaten kommen aus dem Volltextindex (SQLite FTS5).
4. Bewertung pro Datei: Wie viele Teile der Anfrage kommen vor (auf der besten Seite voll,
   auf anderen Seiten desselben Dokuments leicht abgewertet), wie genau, und wie selten ist
   der Teil im Bestand (seltene Begriffe und Nummern zählen mehr).
5. Inhaltsgleiche Dateien werden zusammengefasst; eine erreichbare Kopie hat Vorrang.
"""
from __future__ import annotations

import bisect
import heapq
import html
import math
import os
import re
import threading
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process
from rapidfuzz.distance import OSA

from . import db as dbm
from .normalize import (AMT, HN, ID, ID_SEP_RE, WORD_RE, YR, _compact, amounts, date_spans, dates, fold, id_tokens_for,
                        ids, is_decimal, words)
from .synonyms import STOPWORDS, load_synonyms

KIND_WEIGHT = {"word": 1.0, "phrase": 1.5, "number": 1.3, "amount": 2.0, "id": 2.2, "date": 1.6}
W_PREFIX, W_SUFFIX, W_INNER, W_SYN, W_BRIDGE = 0.85, 0.75, 0.55, 0.75, 0.7
W_FUZZY_KNOWN, W_FUZZY = 0.5, 0.65
MAX_ALTS = 80
MAX_GROUPS = 12              # längere Anfragen: nur die 12 aussagekräftigsten (seltensten) Teile
GROUP_FETCH_MAX_DF = 1500
CANDIDATE_LIMIT = 400
MAX_CANDIDATES = 4000
BM25_MAX_DF = 20000
TF_TOP = 300
OTHER_PAGE_FACTOR = 0.8      # Treffer auf einer benachbarten Seite desselben Dokuments …
PAGE_WINDOW = 2              # … höchstens so viele Seiten entfernt (Sammel-PDFs sind keine Einheit)
PATH_FACTOR = 0.7            # Treffer nur im Ordner-/Dateinamen zählen weniger als im Belegtext
COMPOUND_FACTOR = 0.9        # "Müllgebühren" gefunden als "Abfallentsorgung … Gebühren" (Teile nah beieinander)
COMPOUND_FAR, COMPOUND_SPAN = 0.6, 8   # Teile weit auseinander: nur schwacher Hinweis
W_PLURAL = 0.9               # Singular/Plural (Therme/Thermen, Konto/Konten, Haus/Häuser)
PROX_BONUS, PROX_SPAN = 0.03, 40   # Suchbegriffe nah beieinander (gleiche Tabellenzeile/Absatz)
DOC_START_MARKERS = (" seite 1 von ", " blatt 1 von ", " seite 1 1 ")
QUANTIFIERS = {"alle", "allen", "saemtliche", "saemtlichen", "jede", "jeder", "jedes", "gesamte", "gesamten"}
SCOPE_FACTOR = 0.3


@dataclass
class Group:
    label: str
    kind: str
    alts: dict[str, float]
    importance: float = 1.0
    df: int = 0
    parts: list = field(default_factory=list)   # Kompositum: Teile, die getrennt vorkommen dürfen
    factor: float = 1.0                         # < 1: Umfangsangabe ("alle Häuser") statt Suchbegriff

    def compile(self) -> None:
        """Alternativen nach Art sortieren – schneller Abgleich pro Seite."""
        for p in self.parts:
            p.compile()
        self._singles = {}
        self._prefixes, self._phrases, self._alls = [], [], []
        for a, w in self.alts.items():
            if a.startswith("all:"):
                self._alls.append((a[4:].split(), w))
            elif a.endswith("*"):
                self._prefixes.append((a[:-1], w))
            elif " " in a:
                self._phrases.append((f" {a} ", a.split(), w))
            else:
                self._singles[a] = w


@dataclass
class Hit:
    file_id: int
    relpath: str
    root: str
    filename: str
    folder: str
    page_no: int
    pages: int
    score: float
    snippet_html: str
    snippet_text: str
    matched: list[str]
    missing: list[str]
    ocr: bool
    available: bool
    duplicates: list[str] = field(default_factory=list)

    @property
    def fullpath(self) -> str:
        return os.path.join(self.root, self.relpath)


@dataclass
class SearchResponse:
    query: str
    hits: list[Hit]
    unknown: list[str]
    candidates: int
    ms: float


class Vocab:
    """Alle Wörter des Index mit Dokumenthäufigkeit – für Tippfehler und Wortteile.

    Nur rein alphabetische Begriffe; Zahlen und interne Zusatz-Tokens (0amt…, 0id…) werden
    nie unscharf gesucht und müssen daher nicht in den Speicher.
    """

    def __init__(self, con):
        self.df: dict[str, int] = dict(con.execute("SELECT term, doc FROM vocab WHERE term NOT GLOB '*[^a-z]*'"))
        self.words = [w for w in self.df if 3 <= len(w) <= 40]
        self.by_len: dict[int, list[str]] = defaultdict(list)
        for w in self.words:
            self.by_len[len(w)].append(w)
        self.sorted_words = sorted(self.words)
        self._blob = "\n" + "\n".join(self.words) + "\n"
        row = con.execute("SELECT count(*) FROM pages p JOIN files f ON f.id = p.file_id WHERE f.status = 'done'"
                          ).fetchone()
        self.n_pages = row[0] if row else 0
        self.cache: dict[str, dict[str, float]] = {}

    def with_prefix(self, p: str) -> list[str]:
        i = bisect.bisect_left(self.sorted_words, p)
        j = bisect.bisect_left(self.sorted_words, p + "￿")
        return self.sorted_words[i:j]

    def has_prefix(self, p: str) -> bool:
        i = bisect.bisect_left(self.sorted_words, p)
        return i < len(self.sorted_words) and self.sorted_words[i].startswith(p)

    def _words_containing(self, t: str):
        """(Wort, Position von t im Wort) für alle Wörter, die t enthalten."""
        blob, start = self._blob, 0
        while True:
            i = blob.find(t, start)
            if i < 0:
                return
            ls = blob.rfind("\n", 0, i) + 1
            le = blob.find("\n", i)
            yield blob[ls:le], i - ls
            start = le

    def containing(self, t: str) -> tuple[list[str], list[str]]:
        """Wörter, die t enthalten, aber nicht damit beginnen: (… am Wortende, … mitten im Wort)."""
        suffix, inner = [], []
        for w, pos in self._words_containing(t):
            if pos == 0:
                continue
            (suffix if pos + len(t) == len(w) else inner).append(w)
        key = lambda w: -self.df.get(w, 0)  # noqa: E731
        return sorted(suffix, key=key), sorted(inner, key=key)

    def fuzzy(self, t: str, limit: int) -> list[str]:
        """Tippfehler/OCR-Fehler; Buchstabendreher zählen als ein Fehler (OSA-Distanz)."""
        n = len(t)
        if n < 4:
            return []
        k = 1 if n <= 8 else 2
        found = []
        for L in range(n - k, n + k + 1):
            bucket = self.by_len.get(L)
            if bucket:
                found += [(w, d) for w, d, _ in process.extract(t, bucket, scorer=OSA.distance, score_cutoff=k,
                                                                limit=limit * 3) if w != t and w not in STOPWORDS]
        found.sort(key=lambda x: (x[1], -self.df.get(x[0], 0)))
        return [w for w, _ in found[:limit]]

    def bridged(self, t: str, limit: int = 10) -> list[str]:
        """Wortteil eingeschoben: rauchmelder -> rauchwarnmelder, heizkosten -> heizungskosten."""
        out: dict[str, int] = {}
        for i in range(4, len(t) - 3):
            pre, head = t[:i], t[i:]
            for w in self.with_prefix(pre):
                if len(w) > len(t) and w.endswith(head):
                    out[w] = self.df.get(w, 0)
        return sorted(out, key=lambda w: -out[w])[:limit]

    def fuzzy_inside(self, t: str, limit: int = 10) -> list[str]:
        """Tippfehler in einem Wortteil: schornsteinfger -> bezirksschornsteinfegermeister."""
        if len(t) < 7:
            return []
        pool = {w for part in (t[:4], t[-4:]) for w, _ in self._words_containing(part) if len(w) > len(t)}
        found = process.extract(t, list(pool), scorer=fuzz.partial_ratio, score_cutoff=88, limit=limit * 2)
        found.sort(key=lambda x: (-x[1], -self.df.get(x[0], 0)))
        return [w for w, _, _ in found[:limit]]

    def decompound(self, t: str) -> list[str] | None:
        """heizungswartung -> [heizung, wartung], wenn beide Teile im Bestand vorkommen."""
        best = None
        for i in range(4, len(t) - 3):
            left, right = t[:i], t[i:]
            if right not in self.df:
                continue
            for cand in _fugen(left):
                if len(cand) >= 4 and cand in self.df:
                    score = min(self.df[cand], self.df[right])
                    if best is None or score > best[0]:
                        best = (score, [cand, right])
                    break
        return best[1] if best else None

    def _evidence(self, part: str) -> int:
        """Wie gut ist `part` als Wortteil belegt? Summe der Häufigkeiten aller Wörter, die
        genau so heißen, so beginnen oder so enden (nicht mitten im Wort – dort steckt oft OCR-Müll)."""
        total = self.df.get(part, 0) * (2 if len(part) >= 5 else 1)   # ganzes Wort zählt doppelt
        if len(part) >= 4:
            for w in self.with_prefix(part)[:200]:
                if w != part:
                    total += self.df.get(w, 0)
            for w, pos in self._words_containing(part):
                if pos > 0 and pos + len(part) == len(w):
                    total += self.df.get(w, 0)
        return total

    def split_unknown(self, t: str) -> list[str] | None:
        """Unbekanntes Kompositum in zwei Hälften teilen, die (auch als Wortteil) vorkommen: tueroeffnung -> tuer, oeffnung."""
        best = None
        for i in range(3, len(t) - 3):
            right = t[i:]
            ev_r = self._evidence(right)
            if ev_r < 2:
                continue
            for left in _fugen(t[:i]):
                ev_l = self._evidence(left) if len(left) >= 4 else 0
                if ev_l >= 2:
                    score = (min(ev_l, ev_r), -abs(len(left) - len(right)))
                    if best is None or score > best[0]:
                        best = (score, [left, right])
                    break
        return best[1] if best else None


def _fugen(left: str) -> list[str]:
    """Linke Hälfte mit und ohne Fugenelement (heizungs -> heizung)."""
    out = [left]
    for suf in ("es", "s", "n"):
        if left.endswith(suf) and len(left) - len(suf) >= 3:
            out.append(left[: -len(suf)])
    return out


def _q(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _alt_expr(a: str) -> str:
    if a.startswith("all:"):
        return "(" + " AND ".join(_q(x) for x in a[4:].split()) + ")"
    if a.endswith("*"):
        return _q(a[:-1]) + "*"
    return _q(a)


def _group_expr(g: Group) -> str:
    ors = [_alt_expr(a) for a in g.alts]
    if g.parts:
        ors.append("(" + " AND ".join(_group_expr(p) for p in g.parts) + ")")
    return "(" + " OR ".join(ors) + ")"


def _mask(s: str, start: int, end: int) -> str:
    return s[:start] + " " * (end - start) + s[end:]


_QUOTES = {ord(c): '"' for c in "„“”«»"}
_THOUSANDS_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+)(?![\d.,])")
_GLUED_RE = re.compile(r"(?<![\w.])([a-z]{4,})([.\-]?)(\d{1,4}[a-z]?)(?![\w])")
_STREET = r"(?:strasse|str|weg|allee|platz|ring|damm|gasse|chaussee|ufer|stieg|steig|twiete|kamp|redder|deich|pfad|wall|koppel|markt|berg|hof)"
_ADDR_RE = re.compile(rf"(?<![\w])([a-z]*{_STREET})\.?\s+(\d{{1,3}})(?:\s?([a-z]))?(?![\w])")
_FLOOR_WORDS = r"(?:etage|og|obergeschoss|stock|stockwerk)"
_FLOOR_RE = re.compile(rf"(?<![\w])(\d{{1,2}})\s?\.?\s?{_FLOOR_WORDS}(?![\w])")
_ORDINALS = {1: ("erste", "ersten", "erstes"), 2: ("zweite", "zweiten", "zweites"), 3: ("dritte", "dritten", "drittes"),
             4: ("vierte", "vierten", "viertes"), 5: ("fuenfte", "fuenften", "fuenftes")}
_ORD_FLOOR_RE = re.compile(rf"(?<![\w])({'|'.join(w for ws in _ORDINALS.values() for w in ws)})\s+{_FLOOR_WORDS}(?![\w])")
_YEARS_RE = re.compile(r"(?<![\d/.\-])((?:19|20)\d{2})\s?[/\-–]\s?((?:19|20)\d{2}|\d{2})(?![\d/.\-])")
_PREFIX_ID_RE = re.compile(r"(?<![\w])([a-z]{1,4})\.?\s+(\d{3,})(?![\w.,/])")
# Dokumentart als hinterer Teil eines Kompositums: der vordere Teil ist das Thema und darf selbst in
# Komposita stehen ("Hausmeisterrechnung" -> Rechnung über "Hausmeisterdienstleistung")
DOC_TYPE_HEADS = {"rechnung", "rechnungen", "beleg", "belege", "liste", "uebersicht", "bescheid", "abrechnung",
                  "vertrag", "schreiben", "brief", "protokoll", "auftrag", "angebot", "mahnung", "gutschrift",
                  "quittung", "bestaetigung", "antrag", "nachweis", "aufstellung", "unterlagen", "kosten"}


def _floor_alts(n: int) -> dict[str, float]:
    """"1. Etage" = "1. OG" = "erste Etage" – keine Hausnummer."""
    alts = {f"{n} {w}": 1.0 for w in ("etage", "og", "obergeschoss", "stock", "stockwerk")}
    for o in _ORDINALS.get(n, ()):
        for w in ("etage", "obergeschoss", "stock"):
            alts[f"{o} {w}"] = 1.0
    return alts


def _address_alts(street: str, num: str, letter: str | None) -> dict[str, float]:
    """Hausnummer nur zusammen mit ihrer Straße voll werten ("Musterstädter Weg 7" ≠ "Musterstädter Weg 9")."""
    variants = {street}
    if street.endswith("strasse"):
        variants.add(street[:-7] + "str")
    elif street.endswith("str"):
        variants.add(street + "asse")
    alts = {}
    for v in variants:
        alts[f"{v} {num}"] = 1.0
        alts[f"{HN}{v}{int(num)}"] = 1.0       # auch innerhalb eines Bereichs "Weg 7-11"
        if letter:
            alts[f"{v} {num}{letter}"] = 1.0
            alts[f"{v} {num} {letter}"] = 1.0
    alts[num] = 0.3            # Zahl irgendwo anders auf der Seite: nur ein schwacher Hinweis
    return alts


def _number_alts(t: str) -> dict[str, float]:
    n = int(t)
    alts = {t: 0.9 if len(t) >= 3 else 1.0}
    if len(t) == 4 and 1900 <= n <= 2099:
        alts[f"{YR}{t}"] = 1.0
    if len(t) >= 3:
        alts[f"{AMT}{n}"] = 1.0
    if len(t) >= 4:
        alts[f"{ID}{t}"] = 1.0
        alts[f"{ID}{t}*"] = 0.6
    return alts


def parse_query(query: str) -> list[Group]:
    """Zerlegt die Anfrage in Teile – noch ohne Tippfehler-/Wortteil-Erweiterung."""
    groups: list[Group] = []
    q = fold(query.translate(_QUOTES))
    for m in re.finditer(r'"([^"]+)"', q):
        ws = words(m.group(1))
        if ws:
            groups.append(Group(m.group(1), "phrase", {" ".join(ws): 1.0}))
    q = re.sub(r'"[^"]*"', lambda m: " " * len(m.group()), q)
    q = q.replace('"', " ")
    for s, e, toks in dates(q):
        if q[s:e].strip() == "":
            continue
        alts = {toks[0]: 1.0, toks[1]: 0.35} if toks[0].startswith("0dt") else {toks[0]: 1.0}
        groups.append(Group(q[s:e].strip(), "date", alts))
        q = _mask(q, s, e)
    for s, e, toks in amounts(q):
        if q[s:e].strip() == "":
            continue
        alts = {toks[0]: 1.0}
        if len(toks) > 1 and toks[0].endswith("c00"):
            alts[toks[1]] = 0.9
        groups.append(Group(q[s:e].strip(), "amount", alts))
        q = _mask(q, s, e)
    for m in _THOUSANDS_RE.finditer(q):     # "2.500" = Betrag (oder Nummer)
        n = int(m.group(1).replace(".", ""))
        groups.append(Group(m.group(1), "amount", {f"{AMT}{n}": 1.0, f"{ID}{n}": 0.9}))
        q = _mask(q, m.start(), m.end())
    for m in _GLUED_RE.finditer(q):         # "Mühlenstr.40" = Straße + Hausnummer
        word, num = m.group(1), m.group(3)
        groups.append(Group(word, "word", {word: 1.0}))
        joined = word + num
        alts = {num: 1.0, joined: 1.0}
        for t in id_tokens_for(joined):
            alts[t] = 1.0
        groups.append(Group(num, "number", alts))
        q = _mask(q, m.start(), m.end())
    for m in _YEARS_RE.finditer(q):         # "2024/2025", "2024/25" = Zeitraum, keine Nummer
        y1, y2 = m.group(1), m.group(2)
        y2 = y1[:2] + y2 if len(y2) == 2 else y2
        alts = {f"{y1} {y2}": 1.0, f"all:{YR}{y1} {YR}{y2}": 1.0, f"{YR}{y1}": 0.6, f"{YR}{y2}": 0.6,
                f"{ID}{y1}{y2}": 0.8}
        groups.append(Group(f"{y1}/{y2}", "date", alts))
        q = _mask(q, m.start(), m.end())
    for m in _FLOOR_RE.finditer(q):         # "1. Etage", "3. OG"
        groups.append(Group(f"{int(m.group(1))}. etage", "number", _floor_alts(int(m.group(1)))))
        q = _mask(q, m.start(), m.end())
    for m in _ORD_FLOOR_RE.finditer(q):     # "erste Etage"
        n = next(k for k, ws in _ORDINALS.items() if m.group(1) in ws)
        groups.append(Group(f"{n}. etage", "number", _floor_alts(n)))
        q = _mask(q, m.start(), m.end())
    for m in _ADDR_RE.finditer(q):          # "Musterstädter Weg 7" = Straße + Hausnummer als Einheit
        street, num, letter = m.group(1), m.group(2), m.group(3)
        groups.append(Group(f"{street} {num}{letter or ''}", "number", _address_alts(street, num, letter)))
        q = _mask(q, m.start(2), m.end())
    for m in ID_SEP_RE.finditer(q):
        toks = id_tokens_for(_compact(m.group(1)))
        if not toks:
            continue
        alts = {toks[0]: 1.0}
        for t in toks[1:]:
            alts[t] = 0.95
        parts = words(m.group(1))
        if len(parts) > 1:
            alts[" ".join(parts)] = 1.0
        alts[toks[0] + "*"] = 0.7
        groups.append(Group(m.group(1), "id", alts))
        q = _mask(q, m.start(), m.end())
    for m in _PREFIX_ID_RE.finditer(q):     # "RE 9142", "Kd 12345678" = Nummer mit Kürzel davor
        pre, num = m.group(1), m.group(2)
        if pre in STOPWORDS or (len(num) == 4 and 1900 <= int(num) <= 2099):
            continue
        groups.append(Group(f"{pre} {num}", "id", {f"{ID}{pre}{num}": 1.0, f"{pre} {num}": 1.0,
                                                   f"{ID}{num}": 0.9, num: 0.85}))
        q = _mask(q, m.start(), m.end())
    toks = WORD_RE.findall(q)
    scope = {toks[i + 1] for i, t in enumerate(toks[:-1]) if t in QUANTIFIERS}
    content = [t for t in toks if t not in STOPWORDS] or toks
    for t in content:
        if any(c.isdecimal() for c in t) and any(c.isalpha() for c in t):
            idt = id_tokens_for(t)
            alts = {t: 1.0}
            for i, x in enumerate(idt):
                alts[x] = 1.0 if i == 0 else 0.95
            if idt:
                alts[idt[0] + "*"] = 0.7
            groups.append(Group(t, "id", alts))
        elif is_decimal(t):
            groups.append(Group(t, "number", _number_alts(t)))
        elif t.isdigit() or any(c.isdigit() for c in t):
            groups.append(Group(t, "word", {t: 1.0}))   # exotische Ziffern: nur wörtlich
        else:
            groups.append(Group(t, "word", {t: 1.0}, factor=SCOPE_FACTOR if t in scope else 1.0))
    seen, out = set(), []
    for g in groups:                        # "Heizung Heizung" zählt einmal
        if (g.kind, g.label) not in seen:
            seen.add((g.kind, g.label))
            out.append(g)
    return out


def _plural_variants(t: str, vocab: Vocab) -> list[str]:
    """Singular/Plural-Formen, die im Bestand vorkommen: therme<->thermen, konto<->konten, haus<->haeuser."""
    out = set()
    # "-er" nicht abschneiden: Mieter, Meister, Vermieter sind Einzahl (Mehrzahl auf -er nur mit Umlaut, s. u.)
    for suf in ("en", "n", "e", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            out.add(t[: -len(suf)])
    for suf in ("en", "n", "e", "es", "s", "er"):
        out.add(t + suf)
    if t.endswith("o"):
        out.add(t[:-1] + "en")
    if t.endswith("en"):
        out.add(t[:-2] + "o")
    for a, b in (("ae", "a"), ("oe", "o"), ("ue", "u")):
        i = t.rfind(a)
        if i > 0:
            base = t[:i] + b + t[i + 2:]
            out.update({base, base[:-2] if base.endswith("er") else base, base[:-1] if base.endswith("e") else base})
    return [v for v in out if v != t and len(v) >= 3 and v in vocab.df]


def _word_alts(t: str, vocab: Vocab, synonyms: dict[str, dict[str, float]], typos: bool = True) -> dict[str, float]:
    key = t if typos else t + " "
    cached = vocab.cache.get(key)
    if cached is not None:
        return dict(cached)
    alts: dict[str, float] = {t: 1.0}
    if len(t) > 7 and t.endswith("strasse"):
        alts[t[:-7] + "str"] = 1.0
    elif len(t) >= 5 and t.endswith("str"):
        alts[t + "asse"] = 1.0
    if len(t) >= 4:
        alts[t + "*"] = W_PREFIX             # alle Komposita, die so beginnen – ohne Obergrenze
        suffix, inner = vocab.containing(t)
        for c in suffix[:30]:
            alts.setdefault(c, W_SUFFIX)
        for c in inner[:15]:
            alts.setdefault(c, W_INNER)
        for c in vocab.bridged(t):
            alts.setdefault(c, W_BRIDGE)
    elif len(t) == 3 and t.isalpha():
        alts[t + "*"] = 0.8                  # "gas" -> gasrechnung, gastherme
    for v in _plural_variants(t, vocab):
        alts[v] = max(alts.get(v, 0.0), W_PLURAL)
        if len(v) >= 5:
            alts.setdefault(v + "*", W_PREFIX - 0.05)
    syns = synonyms.get(t, {})
    for s_, w in syns.items():
        alts[s_] = max(alts.get(s_, 0.0), w)
        if " " not in s_ and len(s_) >= 5:
            alts.setdefault(s_ + "*", w - 0.05)
    known = t in vocab.df or (len(t) >= 4 and vocab.has_prefix(t))
    if len(t) >= 4 and typos:
        fz = vocab.fuzzy(t, 4 if known else 8)
        for f in fz:
            alts.setdefault(f, W_FUZZY_KNOWN if known else W_FUZZY)
        if not known:
            if fz:
                alts.setdefault(fz[0] + "*", 0.6)
            for c in vocab.fuzzy_inside(t):
                alts.setdefault(c, 0.6)
    if len(alts) > MAX_ALTS:
        alts = dict(sorted(alts.items(), key=lambda kv: -kv[1])[:MAX_ALTS])
    if len(vocab.cache) > 5000:
        vocab.cache.clear()
    vocab.cache[key] = dict(alts)
    return alts


def _part_alts(p: str, vocab: Vocab, synonyms: dict[str, dict[str, float]], topic: bool = False) -> dict[str, float]:
    """Varianten eines Kompositum-Teils, wenn er getrennt steht: das Wort selbst, Einzahl/Mehrzahl, Synonyme
    und Wörter, die darauf ENDEN (Therme -> Gastherme, Müll -> Restmüll) – nicht solche, die damit beginnen
    (Mieter -> Mieternummer wäre ein anderer Begriff)."""
    alts: dict[str, float] = {p: 1.0}
    for v in _plural_variants(p, vocab):
        alts[v] = max(alts.get(v, 0.0), W_PLURAL)
    if len(p) >= 4:
        for c in vocab.containing(p)[0][:30]:
            alts.setdefault(c, W_SUFFIX)
        if topic:                            # Thema vor einer Dokumentart: auch "Hausmeisterdienstleistung"
            alts[p + "*"] = max(alts.get(p + "*", 0.0), 0.8)
    for s_, w in synonyms.get(p, {}).items():
        alts[s_] = max(alts.get(s_, 0.0), w)
    return alts


def _alt_exists(a: str, vocab: Vocab) -> bool:
    if a.startswith("all:"):
        return all(_alt_exists(p, vocab) for p in a[4:].split())
    if " " in a:
        return all(_alt_exists(p, vocab) for p in a.split())
    if a.endswith("*"):
        p = a[:-1]
        return not p.isalpha() or not p.isascii() or p in vocab.df or vocab.has_prefix(p)
    if not a.isalpha() or not a.isascii():
        return True                          # Zahlen/Zusatz-Tokens: entscheidet die Volltextsuche
    return a in vocab.df


def expand(groups: list[Group], vocab: Vocab, synonyms: dict[str, dict[str, float]]) -> list[Group]:
    """Wort-Teile um Wortteile, Synonyme und Tippfehler erweitern. Gibt die (evtl. längere) Liste zurück."""
    out: list[Group] = []
    for g in groups:
        if g.kind != "word":
            out.append(g)
            continue
        t = g.label if g.label in g.alts else next(iter(g.alts))
        alts = _word_alts(t, vocab, synonyms)
        alts.update({a: w for a, w in g.alts.items() if a not in alts})
        g2 = Group(g.label, g.kind, alts, factor=g.factor)
        # Kompositum: Teile dürfen auch getrennt (und in eigenen Varianten)
        # vorkommen – "Müllgebühren" findet "Abfallentsorgung … Gebühren", "Türöffnung" die "Öffnung der Wohnungstür"
        if len(t) >= 8:
            parts = vocab.decompound(t) or vocab.split_unknown(t)
            if parts:
                topic = parts[-1] in DOC_TYPE_HEADS
                g2.parts = [Group(p, "word", _part_alts(p, vocab, synonyms, topic and i < len(parts) - 1))
                            for i, p in enumerate(parts)]
        out.append(g2)
    return out


def _prune_unknown(g: Group, vocab: Vocab) -> None:
    """Alternativen entfernen, die im Index sicher nicht vorkommen (hält die FTS-Abfrage klein).

    Das eingegebene Wort selbst bleibt immer drin – ob es vorkommt, entscheidet die Volltextsuche
    (der Wortschatz im Speicher kann während eines laufenden Einlesens veraltet sein).
    """
    first = g.label if g.label in g.alts else next(iter(g.alts), None)
    g.alts = {a: w for a, w in g.alts.items() if a == first or _alt_exists(a, vocab)}
    for p in g.parts:
        _prune_unknown(p, vocab)


def _with_prefix(sorted_toks: list[str], pre: str) -> list[str]:
    i = bisect.bisect_left(sorted_toks, pre)
    out = []
    while i < len(sorted_toks) and sorted_toks[i].startswith(pre):
        out.append(sorted_toks[i])
        i += 1
    return out


def _page_match(g: Group, alltoks: set[str], padded: str, ppadded: str = "",
                sorted_toks: list[str] | None = None) -> tuple[float, list[str]]:
    """Bestes Gewicht dieses Anfrage-Teils auf der Seite + die dazu passenden Begriffe (fürs Markieren).

    padded: Seitentext mit Leerzeichen am Rand, ppadded: ebenso der Ordner-/Dateiname (für Phrasen),
    sorted_toks: alltoks sortiert (schneller Wortanfang-Vergleich; wird sonst hier erzeugt)."""
    if not hasattr(g, "_singles"):
        g.compile()
    hits: list[tuple[float, list[str]]] = []
    for tok in alltoks.intersection(g._singles):
        hits.append((g._singles[tok], [tok]))
    if g._prefixes:
        if sorted_toks is None:
            sorted_toks = sorted(alltoks)
        for pre, w in g._prefixes:
            m = _with_prefix(sorted_toks, pre)
            if m:
                hits.append((w, m))
    for padded_phrase, parts, w in g._phrases:
        if padded_phrase in padded or padded_phrase in ppadded:
            hits.append((w, parts))
    for parts, w in g._alls:
        if all(p in alltoks for p in parts):
            hits.append((w, parts))
    if g.parts:
        sub = [_page_match(p, alltoks, padded, ppadded, sorted_toks) for p in g.parts]
        if all(w > 0 for w, _ in sub):
            factor = COMPOUND_FAR
            if padded.strip():
                pos: dict[str, list[int]] = defaultdict(list)
                for i, tok in enumerate(padded.split()):
                    pos[tok].append(i)
                lists = [sorted({p for t in set(ts) for p in pos.get(t, ())}) for _, ts in sub]
                if all(lists) and _min_span(lists) <= COMPOUND_SPAN:
                    factor = COMPOUND_FACTOR
            hits.append((factor * min(w for w, _ in sub), [t for _, ts in sub for t in ts]))
    if not hits:
        return 0.0, []
    best = max(w for w, _ in hits)
    return best, [t for w, ts in hits if w >= best - 0.15 for t in ts]


def _group_score(g: Group, btoks: set[str], ptoks: set[str], padded: str, ppadded: str,
                 bsorted: list[str] | None = None, psorted: list[str] | None = None) -> tuple[float, list[str]]:
    """Treffer im Belegtext voll, Treffer nur im Ordner-/Dateinamen abgewertet."""
    w, t = _page_match(g, btoks, padded, "", bsorted)
    if w < 1.0 and ptoks:
        wp, tp = _page_match(g, ptoks, "", ppadded, psorted)
        if wp * PATH_FACTOR > w:
            return wp * PATH_FACTOR, tp
    return w, t


def _min_span(lists: list[list[int]]) -> int:
    """Kleinster Abstand (in Wörtern), der aus jeder Liste eine Position enthält."""
    heap = [(lst[0], i, 0) for i, lst in enumerate(lists)]
    heapq.heapify(heap)
    hi = max(lst[0] for lst in lists)
    best = hi - heap[0][0]
    while True:
        v, i, j = heapq.heappop(heap)
        best = min(best, hi - v)
        if j + 1 == len(lists[i]):
            return best
        nv = lists[i][j + 1]
        hi = max(hi, nv)
        heapq.heappush(heap, (nv, i, j + 1))


def _proximity(padded: str, terms: list[list[str]]) -> float:
    """1.0 = alle gefundenen Suchbegriffe stehen dicht beieinander (z. B. in einer Tabellenzeile), 0 = weit verstreut."""
    toks = padded.split()
    pos: dict[str, list[int]] = defaultdict(list)
    for i, tok in enumerate(toks):
        pos[tok].append(i)
    lists = []
    for ts in terms:
        found = sorted({p for t in set(ts) for p in pos.get(t, ())})[:200]
        if found:
            lists.append(found)
    if len(lists) < 2:
        return 0.0
    return max(0.0, 1.0 - _min_span(lists) / PROX_SPAN)


def make_snippet(text: str, terms: set[str], width: int = 260) -> tuple[str, str]:
    """Textausschnitt um die Fundstellen. Rückgabe: (HTML mit <mark>, Klartext mit »«)."""
    text = unicodedata.normalize("NFC", text)
    spans: list[tuple[int, int, str]] = []
    for m in WORD_RE.finditer(text):
        fw = WORD_RE.findall(fold(m.group()))
        hit = [w for w in fw if w in terms]
        if hit:
            spans.append((m.start(), m.end(), hit[0]))
    for s, e, toks in amounts(text) + ids(text) + date_spans(text):
        hit = [t for t in toks if t in terms]
        if hit:
            spans.append((s, e, hit[0]))
    spans.sort()
    merged: list[tuple[int, int, str]] = []
    for s, e, k in spans:
        if merged and s <= merged[-1][1]:
            ps, pe, pk = merged[-1]
            merged[-1] = (ps, max(pe, e), pk)
        else:
            merged.append((s, e, k))
    if not merged:
        start, end = 0, min(len(text), width)
    else:
        best_i, best_n = 0, -1
        for i, (s, _, _) in enumerate(merged):
            keys = {k for (s2, e2, k) in merged[i:] if e2 <= s + width}
            if len(keys) > best_n:
                best_i, best_n = i, len(keys)
        start = max(0, merged[best_i][0] - 50)
        end = min(len(text), start + width)
        while start > 0 and not text[start - 1].isspace() and merged[best_i][0] - start < 70:
            start -= 1
        while end < len(text) and not text[end].isspace() and end - start < width + 30:
            end += 1
    h_parts, t_parts = [], []
    pos = start
    for s, e, _ in merged:
        if e <= start or s >= end:
            continue
        s, e = max(s, start), min(e, end)
        h_parts.append(html.escape(text[pos:s]))
        t_parts.append(text[pos:s])
        h_parts.append("<mark>" + html.escape(text[s:e]) + "</mark>")
        t_parts.append("»" + text[s:e] + "«")
        pos = e
    h_parts.append(html.escape(text[pos:end]))
    t_parts.append(text[pos:end])
    pre = "… " if start > 0 else ""
    post = " …" if end < len(text) else ""
    squash = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731
    return pre + squash("".join(h_parts)) + post, pre + squash("".join(t_parts)) + post


class Searcher:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._vocab: Vocab | None = None
        self._vocab_gen: str | None = None
        self._vocab_checked = 0.0
        self._reloading = False
        self.synonyms = load_synonyms(self.db_path.parent / "synonyme.txt")

    def _connect(self):
        return dbm.connect(self.db_path, readonly=True)

    def vocab(self, con) -> Vocab:
        """Wortschatz des Index. Nach dem Einlesen wird im Hintergrund neu geladen;
        bis dahin wird mit dem bisherigen weitergesucht."""
        with self._lock:
            now = time.time()
            if self._vocab is not None and now - self._vocab_checked <= 5:
                return self._vocab
            self._vocab_checked = now
            gen = dbm.get_meta(con, "generation", "0")
            if self._vocab is None:
                self._vocab, self._vocab_gen = Vocab(con), gen
            elif gen != self._vocab_gen and not self._reloading:
                self._reloading = True
                threading.Thread(target=self._reload, args=(gen,), daemon=True).start()
            return self._vocab

    def _reload(self, gen: str) -> None:
        try:
            con = self._connect()
            try:
                v = Vocab(con)
            finally:
                con.close()
            with self._lock:
                self._vocab, self._vocab_gen = v, gen
        finally:
            self._reloading = False

    def prepare(self, con, query: str) -> tuple[list[Group], list[str], Vocab]:
        """Anfrage zerlegen, erweitern, Häufigkeiten bestimmen. Rückgabe: (aktive Teile, unbekannte, Wortschatz)."""
        vocab = self.vocab(con)
        groups = expand(parse_query(query), vocab, self.synonyms)
        n = max(vocab.n_pages, 1)
        unknown, live = [], []
        for g in groups:
            _prune_unknown(g, vocab)
            g.df = con.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", (_group_expr(g),)).fetchone()[0] \
                if g.alts else 0
            if g.df == 0:
                unknown.append(g.label)
                continue
            idf = math.log((n + 1) / (g.df + 1)) / math.log(n + 1) if n > 1 else 1.0
            g.importance = KIND_WEIGHT[g.kind] * (0.4 + idf) * g.factor
            g.compile()
            live.append(g)
        if len(live) > MAX_GROUPS:
            keep = set(id(g) for g in sorted(live, key=lambda g: -g.importance)[:MAX_GROUPS])
            live = [g for g in live if id(g) in keep]
        return live, unknown, vocab

    def search(self, query: str, limit: int = 30) -> SearchResponse:
        t0 = time.time()
        con = self._connect()
        try:
            return self._search(con, query, limit, t0)
        finally:
            con.close()

    def _has_doc_index(self, con) -> bool:
        if not hasattr(self, "_doc_index"):
            self._doc_index = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'fts_doc'").fetchone() is not None
        return self._doc_index

    def _candidates(self, con, live: list[Group]) -> set[int]:
        def fetch(expr: str, df: int) -> list[int]:
            # BM25-Sortierung in SQLite kostet Zeit proportional zur Trefferzahl; bei sehr
            # allgemeinen Begriffen nehmen wir die zuletzt eingelesenen Seiten und sortieren selbst.
            order = "bm25(fts, 2.0, 1.0)" if df <= BM25_MAX_DF else "rowid DESC"
            return [r[0] for r in con.execute(
                f"SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY {order} LIMIT ?", (expr, CANDIDATE_LIMIT))]

        def count(expr: str) -> int:
            return con.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", (expr,)).fetchone()[0]

        cands: set[int] = set()
        and_df = 0
        if len(live) > 1:
            expr = " AND ".join(_group_expr(g) for g in live)
            and_df = count(expr)
            if and_df:
                cands.update(fetch(expr, and_df))
        if 2 < len(live) <= 8 and and_df < 50:
            # Kein (oder kaum ein) Dokument enthält alles: die besten Teiltreffer (alle bis auf einen Teil) holen
            for i in range(len(live)):
                sub = " AND ".join(_group_expr(g) for j, g in enumerate(live) if j != i)
                c = count(sub)
                if c:
                    cands.update(fetch(sub, c))
        if len(live) > 1 and self._has_doc_index(con):
            # Begriffe auf verschiedenen Seiten desselben Dokuments: über den Dokument-Index finden
            def doc_files(expr: str) -> list[int]:
                c = con.execute("SELECT count(*) FROM fts_doc WHERE fts_doc MATCH ?", (expr,)).fetchone()[0]
                if not c:
                    return []
                order = "bm25(fts_doc)" if c <= BM25_MAX_DF else "rowid DESC"
                return [r[0] for r in con.execute(
                    f"SELECT rowid FROM fts_doc WHERE fts_doc MATCH ? ORDER BY {order} LIMIT ?",
                    (expr, CANDIDATE_LIMIT))]
            fids = set(doc_files(" AND ".join(_group_expr(g) for g in live)))
            if 2 < len(live) <= 8 and len(fids) < 50:
                for i in range(len(live)):
                    fids.update(doc_files(" AND ".join(_group_expr(g) for j, g in enumerate(live) if j != i)))
            # nur Seiten dieser Dateien, die mindestens einen Suchbegriff enthalten
            any_group = " OR ".join(_group_expr(g) for g in live)
            fid_list = list(fids)
            for chunk in range(0, len(fid_list), 400):
                files_expr = " OR ".join(_q(f"{dbm.FILE_TOKEN}{f}") for f in fid_list[chunk:chunk + 400])
                cands.update(r[0] for r in con.execute(
                    "SELECT rowid FROM fts WHERE fts MATCH ? LIMIT ?",
                    (f"({files_expr}) AND ({any_group})", MAX_CANDIDATES)))
        for g in sorted(live, key=lambda g: g.df):
            if len(cands) >= MAX_CANDIDATES:
                break
            if g.df <= GROUP_FETCH_MAX_DF:
                cands.update(r[0] for r in con.execute("SELECT rowid FROM fts WHERE fts MATCH ?", (_group_expr(g),)))
            elif len(live) == 1 or and_df < 50:
                cands.update(fetch(_group_expr(g), g.df))
        return cands

    def _search(self, con, query: str, limit: int, t0: float) -> SearchResponse:
        if not parse_query(query):
            return SearchResponse(query, [], [], 0, 0.0)
        live, unknown, _ = self.prepare(con, query)
        if not live:
            return SearchResponse(query, [], unknown, 0, (time.time() - t0) * 1000)
        cands = self._candidates(con, live)
        total_imp = sum(g.importance for g in live)
        cand_list = list(cands)

        # 1. Jede Kandidatenseite bewerten
        pages: dict[int, tuple] = {}         # rowid -> (Gewichte je Teil, Begriffe, body, path-Tokens)
        for chunk in range(0, len(cand_list), 900):
            ids_ = cand_list[chunk:chunk + 900]
            for rid, path, body in con.execute(
                    f"SELECT rowid, path, body FROM fts WHERE rowid IN ({','.join('?' * len(ids_))})", ids_):
                ptoks = set(path.split())
                btoks = set(body.split())
                bsorted, psorted = sorted(btoks), sorted(ptoks)
                padded, ppadded = f" {body} ", f" {path} "
                ws, terms = [], []
                for g in live:
                    w, t = _group_score(g, btoks, ptoks, padded, ppadded, bsorted, psorted)
                    ws.append(w)
                    terms.append(t)
                if any(ws):
                    pages[rid] = (ws, terms, padded, ptoks)
        if not pages:
            return SearchResponse(query, [], unknown, len(cands), (time.time() - t0) * 1000)

        # 2. Seiten ihren Dateien zuordnen
        meta: dict[int, dict] = {}
        pids = list(pages)
        for chunk in range(0, len(pids), 900):
            ids_ = pids[chunk:chunk + 900]
            for r in con.execute(
                    "SELECT p.id AS pid, p.page_no, p.file_id, f.relpath, f.sha256, f.pages, f.ocr_pages, f.mtime,"
                    " r.path AS root FROM pages p JOIN files f ON f.id = p.file_id JOIN roots r ON r.id = f.root_id"
                    f" WHERE f.status = 'done' AND p.id IN ({','.join('?' * len(ids_))})", ids_):
                meta[r["pid"]] = r
        by_file: dict[int, list[int]] = defaultdict(list)
        for rid in pages:
            if rid in meta:
                by_file[meta[rid]["file_id"]].append(rid)

        # 3. Pro Datei: beste Seite samt ihren Nachbarseiten (±PAGE_WINDOW) – ein Beleg in einem
        #    Sammel-PDF umfasst ein paar Seiten, nicht das ganze PDF
        files = []
        n_live = len(live)
        for fid, rids in by_file.items():
            srt = sorted(rids, key=lambda r: meta[r]["page_no"])
            # Seite beginnt einen neuen Beleg ("Seite 1 von 3"): Nachbarseiten davor gehören nicht dazu
            starts = {r for r in srt if any(m in pages[r][2] for m in DOC_START_MARKERS)}
            cands_p = []                     # (Fensterwert, eigener Wert, -Seite, rowid, Gewichte)
            for i, r in enumerate(srt):
                pn = meta[r]["page_no"]
                near = [q for q in srt[max(0, i - PAGE_WINDOW):i] + srt[i + 1:i + 1 + PAGE_WINDOW]
                        if abs(meta[q]["page_no"] - pn) <= PAGE_WINDOW
                        and not any(x in starts for x in srt[min(i, srt.index(q)) + 1:max(i, srt.index(q)) + 1])]
                own = pages[r][0]
                W = [max(own[k], OTHER_PAGE_FACTOR * max((pages[q][0][k] for q in near), default=0.0))
                     for k in range(n_live)]
                cands_p.append((sum(g.importance * w for g, w in zip(live, W)),
                                sum(g.importance * w for g, w in zip(live, own)), -pn, r, W))
            top_w = max(c[0] for c in cands_p)
            # Angezeigt wird die Seite, die selbst am meisten enthält – unter denen, deren Umfeld fast gleich gut passt
            win, own_s, _, best, best_W = max((c for c in cands_p if c[0] >= 0.97 * top_w), key=lambda c: (c[1], c[2]))
            files.append([top_w / total_imp, fid, best, best_W])
        files.sort(key=lambda x: -x[0])

        # 4. Inhaltsgleiche Dateien zusammenfassen – vor dem Kürzen, sonst verdrängen viele Kopien andere Belege
        groups_by_sha: dict[str, list] = {}
        order: list[list] = []
        for f in files:
            sha = meta[f[2]]["sha256"] or f"file{f[1]}"
            if sha in groups_by_sha:
                groups_by_sha[sha].append(f)
            else:
                groups_by_sha[sha] = [f]
                order.append(groups_by_sha[sha])
        order = order[:TF_TOP]

        # 5. Nähe der Suchbegriffe zueinander (kleiner Bonus) und bei Gleichstand: Häufigkeit der Begriffe
        #    (BM25-artig) und Treffer im Ordner-/Dateinamen
        tf_scores = []
        for grp in order:
            ws, terms, padded, ptoks = pages[grp[0][2]]
            grp[0][0] += PROX_BONUS * _proximity(padded, [t for w, t in zip(ws, terms) if w]) \
                * sum(1 for w in ws if w) / n_live
            n_toks = padded.count(" ")
            tf_score = 0.0
            for g, w, t in zip(live, ws, terms):
                if w:
                    tf = sum(padded.count(f" {x} ") for x in set(t))
                    tf_score += g.importance * (tf / (tf + 1.2 * (0.25 + 0.75 * n_toks / 300)))
                    if ptoks.intersection(t):
                        tf_score += g.importance * 0.5
            tf_scores.append(tf_score)
        max_tf = max(tf_scores, default=0) or 1.0
        ranked = sorted(zip(order, tf_scores), key=lambda x: (-round(x[0][0][0] + 0.002 * x[1] / max_tf, 6),
                                                                -(meta[x[0][0][2]]["mtime"] or 0)))
        order = [grp for grp, _ in ranked]

        # 6. Pro Gruppe eine erreichbare Kopie als Haupttreffer, auf limit kürzen
        root_online: dict[str, bool] = {}

        def available(m) -> bool:
            if m["root"] not in root_online:
                root_online[m["root"]] = os.path.isdir(m["root"])
            return root_online[m["root"]] and os.path.exists(os.path.join(m["root"], m["relpath"]))

        hits: list[Hit] = []
        for grp in order[:limit]:
            main = next((f for f in grp if available(meta[f[2]])), grp[0])
            score, fid, rid, W = main
            m = meta[rid]
            text = con.execute("SELECT text FROM pages WHERE id = ?", (rid,)).fetchone()[0]
            snip_h, snip_t = make_snippet(text, set(t for ts in pages[rid][1] for t in ts))
            folder, filename = os.path.split(m["relpath"])
            hits.append(Hit(
                file_id=fid, relpath=m["relpath"], root=m["root"], filename=filename, folder=folder,
                page_no=m["page_no"], pages=m["pages"] or 1, score=round(score, 4), snippet_html=snip_h,
                snippet_text=snip_t, matched=[g.label for g, w in zip(live, W) if w > 0],
                missing=[g.label for g, w in zip(live, W) if w == 0], ocr=bool(m["ocr_pages"]),
                available=available(m),
                duplicates=[os.path.join(meta[f[2]]["root"], meta[f[2]]["relpath"]) for f in grp if f is not main]))
        return SearchResponse(query, hits, unknown, len(cands), (time.time() - t0) * 1000)
