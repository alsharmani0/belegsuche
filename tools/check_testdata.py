#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Validiert den Testkorpus (testdata/) gegen manifest.json und die Query-CSVs.

    .venv/bin/python tools/check_testdata.py                  # Struktur-, Text- und CSV-Prüfungen
    .venv/bin/python tools/check_testdata.py --determinism    # zusätzlich: Generator 2x isoliert ausführen
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
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

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
            page = pdf[i]
            try:
                tp = page.get_textpage()
                try:
                    out.append(tp.get_text_range())
                finally:
                    tp.close()
            finally:
                page.close()
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
    docs = {}
    for d in man["documents"]:
        p = d["path"]
        rel = Path(p)
        if not p or rel.is_absolute() or ".." in rel.parts or rel.as_posix() != p:
            fail(f"ungültiger Manifest-Pfad: {p!r}")
            continue
        if any((KORPUS / Path(*rel.parts[:i])).is_symlink() for i in range(len(rel.parts) + 1)):
            fail(f"Symlink im Manifest-Pfad: {p}")
            continue
        if p in docs:
            fail(f"Manifest-Pfad mehrfach vorhanden: {p}")
            continue
        if d["kind"] not in {"digital_pdf", "scan_pdf", "garbage_textlayer_pdf", "sammel_pdf",
                              "encrypted_pdf", "corrupt_pdf", "photo_jpg", "png", "tiff_multipage",
                              "heic", "malformed_docx", "unsupported", "duplicate_of"}:
            fail(f"unbekannter Dateityp im Manifest: {p}")
            continue
        docs[p] = d
    if man.get("anzahl_dateien") != len(man["documents"]):
        fail("anzahl_dateien stimmt nicht mit documents überein")
    on_disk = {str(p.relative_to(KORPUS)) for p in KORPUS.rglob("*") if p.is_file()}
    for p in sorted(on_disk - set(docs)):
        fail(f"Datei nicht im Manifest: {p}")
    for p, d in docs.items():
        fp = KORPUS / p
        if not fp.is_file():
            fail(f"fehlt: {p}")
            continue
        data = fp.read_bytes()
        h = hashlib.sha256(data).hexdigest()
        if h != d["sha256"]:
            fail(f"sha256 weicht ab: {p}")
        if len(data) != d["size_bytes"]:
            fail(f"size_bytes weicht ab: {p}")
        if d["kind"] == "duplicate_of":
            original = docs.get(d["duplicate_of"])
            if original is None or original["kind"] == "duplicate_of" or d["duplicate_of"] == p:
                fail(f"ungültiges Dubletten-Ziel: {p}")
            elif original["sha256"] != h:
                fail(f"Dublette nicht byte-identisch: {p}")
        if not isinstance(d["pages"], int) or isinstance(d["pages"], bool) or d["pages"] < 0:
            fail(f"ungültige Seitenzahl: {p}")
            continue
        for f in d["facts"]:
            s = f["seite"]
            if not isinstance(s, int) or isinstance(s, bool) or not 1 <= s <= d["pages"]:
                fail(f"Faktenseite außerhalb des Dokuments: {p}")
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
        if not fp.is_file():
            continue  # Bereits als fehlend gemeldet; übrige Prüfungen bleiben ausführbar.
        if k in ("digital_pdf", "scan_pdf", "garbage_textlayer_pdf", "sammel_pdf"):
            texts = pdf_text(fp)
            if len(texts) != d["pages"]:
                fail(f"{p}: {len(texts)} Seiten, Manifest sagt {d['pages']}")
            for f in d["facts"]:
                if not isinstance(f["seite"], int) or not 1 <= f["seite"] <= len(texts):
                    continue
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
        elif k == "malformed_docx":
            with zipfile.ZipFile(fp) as archive:
                try:
                    ET.fromstring(archive.read("word/document.xml"))
                except ET.ParseError:
                    pass
                else:
                    fail(f"{p}: Word-Dokument enthält das erwartete beschädigte XML nicht")
        elif k in ("photo_jpg", "png"):
            with Image.open(fp) as im:
                im.verify()
        elif k == "tiff_multipage":
            with Image.open(fp) as im:
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
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail(f"{path.name}: kein gültiges UTF-8")
        return []
    lines = text.splitlines()
    if not lines or lines[0] != "suche;datei;seite;notiz":
        fail(f"{path.name}: Kopfzeile fehlt oder ist falsch")
        return []
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=";", strict=True)
        for row in reader:
            if None in row or any(v is None or not v.strip() for v in row.values()):
                fail(f"{path.name} Zeile {reader.line_num}: fehlende oder zusätzliche Felder")
                continue
            try:
                page = int(row["seite"])
            except ValueError:
                fail(f"{path.name} Zeile {reader.line_num}: Seite ist keine ganze Zahl")
                continue
            if page < 1:
                fail(f"{path.name} Zeile {reader.line_num}: Seite muss positiv sein")
                continue
            row["seite"] = str(page)
            rows.append(row)
    except csv.Error as exc:
        fail(f"{path.name}: ungültiges CSV: {exc}")
    if not rows:
        fail(f"{path.name}: keine gültigen Anfragen")
    return rows


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
            if d["kind"] in ("duplicate_of", "encrypted_pdf", "corrupt_pdf", "malformed_docx", "unsupported"):
                fail(f"{name}: Ziel ist {d['kind']}: {r['datei']}")
            if not any(f["seite"] == s for f in d["facts"]):
                fail(f"{name}: keine Fakten für Seite {s} in {r['datei']}")
        print(f"  {name}: {len(rows)} Anfragen")
    dev = {(r["datei"], r["seite"]) for r in sets["queries_dev.csv"]}
    hold = {(r["datei"], r["seite"]) for r in sets["queries_holdout.csv"]}
    if dev & hold:
        fail(f"Dev und Holdout teilen Ziele: {dev & hold}")
    dq = {" ".join(r["suche"].casefold().split()) for r in sets["queries_dev.csv"]}
    hq = {" ".join(r["suche"].casefold().split()) for r in sets["queries_holdout.csv"]}
    if dq & hq:
        fail(f"{len(dq & hq)} identische Suchstrings in Dev und Holdout")
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
    snaps = []
    with tempfile.TemporaryDirectory(prefix="belegsuche-determinism-") as td:
        for run in range(2):
            output = Path(td) / str(run)
            gen = [sys.executable, str(ROOT / "tools" / "make_testdata.py"), "--output", str(output)]
            subprocess.run(gen, check=True, capture_output=True)
            snap = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in output.rglob("*") if p.is_file()}
            snaps.append(snap)
    a, b = snaps
    if set(a) != set(b):
        fail(f"Dateiliste unterschiedlich: {set(a) ^ set(b)}")
    diff = sorted(k for k in a if k in b and a[k] != b[k])
    for k in diff:
        fail(f"nicht deterministisch: {k}")
    print(f"  {len(a)} Dateien verglichen, {len(diff)} Abweichungen")


def main(argv=None) -> int:
    global TESTDATA, KORPUS
    FAILS.clear()
    ap = argparse.ArgumentParser()
    ap.add_argument("--testdata", type=Path, default=ROOT / "testdata", metavar="DIR")
    ap.add_argument("--determinism", action="store_true")
    ap.add_argument("--ocr", action="store_true")
    ap.add_argument("--render", metavar="DIR")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--include-holdout", action="store_true", help="auch Holdout-Anfragen in Inhaltsdiagnosen zeigen")
    a = ap.parse_args(argv)
    TESTDATA = a.testdata.expanduser().absolute()
    KORPUS = TESTDATA / "korpus"
    try:
        if a.determinism:
            check_determinism()
        man = json.loads((TESTDATA / "manifest.json").read_text(encoding="utf-8"))
        docs = check_files(man)
        check_pdfs(docs)
        sets = check_queries(docs)
        diagnostic_sets = sets if a.include_holdout else {"queries_dev.csv": sets["queries_dev.csv"]}
        ambiguity_report(docs, diagnostic_sets, a.verbose)
        if a.render:
            render_samples(docs, Path(a.render))
        if a.ocr:
            check_ocr(docs, diagnostic_sets)
    except (OSError, ValueError, KeyError, IndexError, TypeError, pdfium.PdfiumError,
            subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        fail(f"Prüfung nicht ausführbar: {exc}")
    print()
    print("ERGEBNIS:", "OK" if not FAILS else f"{len(FAILS)} Fehler")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
