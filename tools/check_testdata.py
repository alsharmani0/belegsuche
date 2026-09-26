#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Validiert den Testkorpus (testdata/) gegen manifest.json und die Query-CSVs.

    .venv/bin/python tools/check_testdata.py                  # Struktur-, Text- und CSV-Prüfungen
    .venv/bin/python tools/check_testdata.py --determinism    # zusätzlich: Generator 2x laufen lassen, Hashes vergleichen
    .venv/bin/python tools/check_testdata.py --ocr            # zusätzlich: Bildbelege mit Apple Vision prüfen
    .venv/bin/python tools/check_testdata.py --render DIR     # zusätzlich: einige Scans als PNG nach DIR rendern

Die lexikalische Mehrdeutigkeitsprüfung zeigt für jede Anfrage, welche Belegseiten ALLE
Suchwörter (normalisiert, Teilstring) enthalten. Ein Treffer außerhalb des Ziels ist ein
Warnsignal; kein Treffer ist bei Tippfehler-/Synonym-Anfragen beabsichtigt.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "testdata"
KORPUS = TESTDATA / "korpus"
sys.path.insert(0, str(ROOT / "tools"))
sys.dont_write_bytecode = True

FAILS: list[str] = []


def fail(msg: str) -> None:
    FAILS.append(msg)
    print("  FEHLER:", msg)


def norm(s: str) -> str:
    s = s.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return s


def squash(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", norm(s))


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z]+", norm(s)) if t]


def pdf_text(path: Path, password=None) -> list[str]:
    pdf = pdfium.PdfDocument(str(path), password=password)
    try:
        out = []
        for i in range(len(pdf)):
            tp = pdf[i].get_textpage()
            out.append(tp.get_text_range())
        return out
    finally:
        pdf.close()


def fact_needles(f: dict) -> dict[str, str]:
    street = (f.get("objektadresse") or "").split(",")[0]
    return {
        "rechnungsnummer": f["rechnungsnummer"].replace("Bon-Nr. ", ""),
        "gesamtbetrag_text": f["gesamtbetrag_text"],
        "firma": f["firma"],
        "objekt": street,
    }


def contains_ws(hay: str, needle: str) -> bool:
    return re.sub(r"\s+", " ", needle).strip() in re.sub(r"\s+", " ", hay)


# --------------------------------------------------------------------------------------

def check_files(man: dict) -> dict[str, dict]:
    print("== Dateien / Manifest")
    docs = {d["path"]: d for d in man["documents"]}
    on_disk = {str(p.relative_to(KORPUS)) for p in KORPUS.rglob("*") if p.is_file()}
    for p in sorted(on_disk - set(docs)):
        fail(f"Datei nicht im Manifest: {p}")
    for p, d in docs.items():
        fp = KORPUS / p
        if not fp.exists():
            fail(f"fehlt: {p}")
            continue
        h = hashlib.sha256(fp.read_bytes()).hexdigest()
        if h != d["sha256"]:
            fail(f"sha256 weicht ab: {p}")
        if d["kind"] == "duplicate_of":
            if docs[d["duplicate_of"]]["sha256"] != h:
                fail(f"Dublette nicht byte-identisch: {p}")
    seen_amount, seen_nr = {}, {}
    for p, d in docs.items():
        if d["kind"] == "duplicate_of":
            continue
        for f in d["facts"]:
            for key, seen in (("gesamtbetrag", seen_amount), ("rechnungsnummer", seen_nr)):
                other = seen.setdefault(f[key], f["doc_id"])
                if other != f["doc_id"]:
                    fail(f"{key} {f[key]} doppelt: {other} / {f['doc_id']}")
    print(f"  {len(docs)} Manifest-Einträge, {len(on_disk)} Dateien auf Platte, "
          f"{len(seen_amount)} eindeutige Beträge/Rechnungsnummern")
    return docs


def check_pdfs(docs: dict[str, dict]) -> None:
    print("== PDF-Inhalte (pypdfium2)")
    for p, d in docs.items():
        k = d["kind"]
        fp = KORPUS / p
        if k in ("digital_pdf", "scan_pdf", "garbage_textlayer_pdf", "sammel_pdf"):
            texts = pdf_text(fp)
            if len(texts) != d["pages"]:
                fail(f"{p}: {len(texts)} Seiten, Manifest sagt {d['pages']}")
            for f in d["facts"]:
                t = texts[f["seite"] - 1]
                if f["quelle"] == "textlayer":
                    for name, needle in fact_needles(f).items():
                        if needle and not contains_ws(t, needle):
                            fail(f"{p} S.{f['seite']}: '{needle}' ({name}) nicht im Textlayer")
                elif k == "garbage_textlayer_pdf":
                    if len(t.strip()) < 200:
                        fail(f"{p}: Müll-Textlayer fehlt/zu kurz")
                    nt, st = norm(t), squash(t)
                    bad = []
                    for name, needle in fact_needles(f).items():
                        if not needle:
                            continue
                        if name == "firma":
                            for w in re.findall(r"\w{4,}", needle):
                                if norm(w) in nt:
                                    bad.append(w)
                        elif squash(needle) and squash(needle) in st:
                            bad.append(needle)
                    if f["datum_text"] in t:
                        bad.append(f["datum_text"])
                    if bad:
                        fail(f"{p}: Müll-Textlayer enthält echte Fakten: {bad}")
                else:
                    if t.strip():
                        fail(f"{p} S.{f['seite']}: Bildseite hat Text ({t[:40]!r})")
        elif k == "encrypted_pdf":
            try:
                pdf_text(fp)
                fail(f"{p}: verschlüsselte PDF lässt sich ohne Passwort öffnen")
            except pdfium.PdfiumError:
                pass
            t = pdf_text(fp, password=d["password"])[0]
            if d["facts"][0]["rechnungsnummer"] not in t:
                fail(f"{p}: mit Passwort kein korrekter Text")
        elif k == "corrupt_pdf":
            try:
                pdf_text(fp)
                fail(f"{p}: defekte PDF lässt sich öffnen")
            except pdfium.PdfiumError:
                pass
        elif k in ("photo_jpg", "png"):
            Image.open(fp).verify()
        elif k == "tiff_multipage":
            im = Image.open(fp)
            if im.n_frames != d["pages"]:
                fail(f"{p}: {im.n_frames} Frames statt {d['pages']}")
        elif k == "heic":
            r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "format", str(fp)], capture_output=True, text=True)
            if "heic" not in r.stdout:
                fail(f"{p}: kein HEIC laut sips")
    print("  ok" if not FAILS else f"  {len(FAILS)} Fehler bisher")


def read_csv(path: Path) -> list[dict]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        fail(f"{path.name}: BOM vorhanden")
    text = raw.decode("utf-8")
    if text.splitlines()[0] != "suche;datei;seite;notiz":
        fail(f"{path.name}: Kopfzeile falsch: {text.splitlines()[0]!r}")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def doc_texts() -> dict[str, str]:
    """Volltext jedes Belegs (aus dem Generator gerendert), auch für reine Bildbelege."""
    import make_testdata as mt
    mt.register_fonts()
    out = {}
    for did, doc in mt.DOCS.items():
        _, lines = mt.digital_pdf(doc)
        out[did] = "\n".join(l[4] for l in lines)
    return out


def check_queries(docs: dict[str, dict]) -> dict[str, list[dict]]:
    print("== Query-CSVs")
    sets = {}
    for name in ("queries_dev.csv", "queries_holdout.csv"):
        rows = read_csv(TESTDATA / name)
        sets[name] = rows
        for r in rows:
            d = docs.get(r["datei"])
            if d is None:
                fail(f"{name}: Datei existiert nicht: {r['datei']}")
                continue
            s = int(r["seite"])
            if not 1 <= s <= d["pages"]:
                fail(f"{name}: Seite {s} außerhalb 1..{d['pages']} ({r['datei']})")
            if d["kind"] in ("duplicate_of", "encrypted_pdf", "corrupt_pdf", "unsupported"):
                fail(f"{name}: Ziel ist {d['kind']}: {r['datei']}")
            if not any(f["seite"] == s for f in d["facts"]):
                fail(f"{name}: keine Fakten für Seite {s} in {r['datei']}")
        print(f"  {name}: {len(rows)} Anfragen")
    dev = {(r["datei"], r["seite"]) for r in sets["queries_dev.csv"]}
    hold = {(r["datei"], r["seite"]) for r in sets["queries_holdout.csv"]}
    if dev & hold:
        fail(f"Dev und Holdout teilen Ziele: {dev & hold}")
    dq = {r["suche"].lower() for r in sets["queries_dev.csv"]}
    hq = {r["suche"].lower() for r in sets["queries_holdout.csv"]}
    if dq & hq:
        fail(f"identische Suchstrings in Dev und Holdout: {dq & hq}")
    return sets


CURRENCY = {"eur", "euro"}


def lexical_hit(query: str, hay_norm: str, line_squashes: list[str]) -> bool:
    """Alle Suchwörter als Teilstring vorhanden? ID-/Betrags-artige Anfragen (nur Ziffern-Tokens und
    Kürzel <= 3 Buchstaben) müssen zusammengezogen in EINER Zeile stehen (z. B. 'GR 24-0102' -> 'gr240102')."""
    toks = [t for t in tokens(query) if t not in CURRENCY]
    has_digit = [any(ch.isdigit() for ch in t) for t in toks]
    if any(has_digit) and all(h or len(t) <= 3 for t, h in zip(toks, has_digit)):
        needle = "".join(toks)
        return any(needle in ls for ls in line_squashes)
    for t, h in zip(toks, has_digit):
        if h:
            if not any(t in ls for ls in line_squashes):
                return False
        elif t not in hay_norm:
            return False
    return True


def ambiguity_report(docs, sets, verbose: bool) -> None:
    print("== Lexikalische Mehrdeutigkeit (alle Suchwörter als Teilstring)")
    texts = doc_texts()
    pages = []  # (datei, seite, haystack_norm, [squash je Zeile])
    for p, d in docs.items():
        if d["kind"] in ("duplicate_of",):
            continue
        for f in d["facts"]:
            lines = texts[f["doc_id"]].split("\n") + [p]
            pages.append((p, f["seite"], norm("\n".join(lines)), [squash(l) for l in lines]))
    for name, rows in sets.items():
        warn = 0
        for r in rows:
            hits = [(p, s) for p, s, hn, ls in pages if lexical_hit(r["suche"], hn, ls)]
            target = (r["datei"], int(r["seite"]))
            others = [h for h in hits if h != target]
            status = "ok " if target in hits and not others else ("---" if not hits else "!! ")
            if others:
                warn += 1
            if verbose or others:
                print(f"  [{status}] {name[8:-4]:7s} {r['suche']!r:40s} -> Ziel {'getroffen' if target in hits else 'nicht lexikalisch'}"
                      + (f"; ANDERE: {others}" if others else ""))
        print(f"  {name}: {warn} Anfragen mit lexikalischen Treffern außerhalb des Ziels")


# --------------------------------------------------------------------------------------
# OCR (Apple Vision)
# --------------------------------------------------------------------------------------

def vision_ocr(img: Image.Image) -> str:
    import Vision
    from Foundation import NSData
    b = io.BytesIO()
    img.convert("RGB").save(b, "PNG")
    data = NSData.dataWithBytes_length_(b.getvalue(), len(b.getvalue()))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["de-DE"])
    req.setUsesLanguageCorrection_(True)
    handler.performRequests_error_([req], None)
    return "\n".join(o.topCandidates_(1)[0].string() for o in (req.results() or []))


def page_images(path: Path, kind: str) -> list[Image.Image]:
    if kind in ("scan_pdf", "garbage_textlayer_pdf", "sammel_pdf"):
        pdf = pdfium.PdfDocument(str(path))
        imgs = [pdf[i].render(scale=200 / 72, draw_annots=False).to_pil() for i in range(len(pdf))]
        pdf.close()
        return imgs
    if kind == "heic":
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "x.png"
            subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(out)], check=True,
                           capture_output=True)
            im = Image.open(out)
            im.load()
            return [ImageOps.exif_transpose(im)]
    im = Image.open(path)
    if kind == "tiff_multipage":
        out = []
        for i in range(im.n_frames):
            im.seek(i)
            out.append(im.convert("L").copy())
        return out
    im.load()
    return [ImageOps.exif_transpose(im)]


def score(ocr: str, f: dict) -> dict[str, bool]:
    so, no = squash(ocr), norm(ocr)
    res = {}
    for name, needle in fact_needles(f).items():
        if not needle:
            continue
        if name == "firma":
            words = [w for w in re.findall(r"\w{4,}", needle)]
            res[name] = sum(norm(w) in no for w in words) >= max(1, len(words) // 2)
        else:
            res[name] = squash(needle) in so
    return res


def check_ocr(docs, sets) -> None:
    print("== OCR-Stichprobe (Apple Vision, alle Bildbelege)")
    targets = {}
    for name, rows in sets.items():
        for r in rows:
            targets.setdefault((r["datei"], int(r["seite"])), []).append(r["suche"])
    total, found = 0, 0
    for p, d in docs.items():
        if d["kind"] not in ("scan_pdf", "garbage_textlayer_pdf", "sammel_pdf", "photo_jpg", "png", "heic",
                             "tiff_multipage"):
            continue
        imgs = page_images(KORPUS / p, d["kind"])
        for f in d["facts"]:
            if f["quelle"] != "nur_bild":
                continue
            img = imgs[f["seite"] - 1]
            best, best_rot, best_txt = None, 0, ""
            for rot in (0, 90, 180, 270):
                txt = vision_ocr(img.rotate(rot, expand=True) if rot else img)
                sc = score(txt, f)
                if best is None or sum(sc.values()) > sum(best.values()):
                    best, best_rot, best_txt = sc, rot, txt
                if all(sc.values()):
                    break
            total += len(best)
            found += sum(best.values())
            qinfo = ""
            for q in targets.get((p, f["seite"]), []):
                toks = tokens(q)
                hit = [t for t in toks if t in norm(best_txt) or t in squash(best_txt)]
                qinfo += f" | Query {q!r}: {len(hit)}/{len(toks)} Wörter im OCR"
            miss = [k for k, v in best.items() if not v]
            print(f"  {p} S.{f['seite']} (rot {best_rot}°): {sum(best.values())}/{len(best)} Fakten"
                  + (f", fehlt: {miss}" if miss else "") + qinfo)
    print(f"  gesamt {found}/{total} Fakten per OCR wiedergefunden")


def render_samples(docs, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    picks = [p for p, d in docs.items() if d["kind"] in ("scan_pdf", "garbage_textlayer_pdf")][:6]
    for p in picks:
        pdf = pdfium.PdfDocument(str(KORPUS / p))
        img = pdf[0].render(scale=110 / 72).to_pil()
        name = re.sub(r"[^\w.-]+", "_", p)[:80] + ".png"
        img.save(outdir / name)
        pdf.close()
        print("  gerendert:", outdir / name)


def check_determinism() -> None:
    print("== Determinismus (Generator 2x)")
    gen = [sys.executable, str(ROOT / "tools" / "make_testdata.py")]
    snaps = []
    for _ in range(2):
        subprocess.run(gen, check=True, capture_output=True)
        snap = {str(p.relative_to(TESTDATA)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in TESTDATA.rglob("*") if p.is_file()}
        snaps.append(snap)
    a, b = snaps
    if set(a) != set(b):
        fail(f"Dateiliste unterschiedlich: {set(a) ^ set(b)}")
    diff = sorted(k for k in a if k in b and a[k] != b[k])
    for k in diff:
        fail(f"nicht deterministisch: {k}")
    print(f"  {len(a)} Dateien verglichen, {len(diff)} Abweichungen")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--determinism", action="store_true")
    ap.add_argument("--ocr", action="store_true")
    ap.add_argument("--render", metavar="DIR")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.determinism:
        check_determinism()
    man = json.loads((TESTDATA / "manifest.json").read_text(encoding="utf-8"))
    docs = check_files(man)
    check_pdfs(docs)
    sets = check_queries(docs)
    ambiguity_report(docs, sets, a.verbose)
    if a.render:
        render_samples(docs, Path(a.render))
    if a.ocr:
        check_ocr(docs, sets)
    print()
    print("ERGEBNIS:", "OK" if not FAILS else f"{len(FAILS)} Fehler")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
