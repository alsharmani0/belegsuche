"""Suchqualität messen: Findet die Suche das erwartete Dokument, und wenn nicht, warum?

Testdatei (CSV, Semikolon oder Komma):
    suche;datei;seite;notiz
    Wartung Heizung Musterstraße;2021/Objekte/Scan_00487.pdf;1;Beschreibung
`datei` ist der Pfad relativ zum eingelesenen Ordner, „<Ordnername>/<relativer Pfad>“ oder der
vollständige Pfad (Groß/Klein egal). Liegt derselbe relative Pfad in mehreren eingelesenen Ordnern,
muss der Ordnername oder der vollständige Pfad dabeistehen.
"""
from __future__ import annotations

import csv
import json
import os
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from .search import Searcher, _page_match


def _norm(p: str) -> str:
    return unicodedata.normalize("NFC", p).replace("\\", "/").strip("/").casefold()


def load_queries(path: Path) -> list[dict]:
    """Suchfragen aus der CSV. FileNotFoundError geht an den Aufrufer, unbrauchbarer Inhalt wird ValueError."""
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):          # Excel „Unicode-Text“
        raw = data.decode("utf-16", errors="replace")
    else:
        try:
            raw = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Excel/Windows (cp1252) oder ältere Mac-Programme (MacRoman): die Kodierung mit mehr Umlauten gewinnt
            raw = max((data.decode(enc, errors="replace") for enc in ("cp1252", "mac_roman")),
                      key=lambda t: sum(t.count(c) for c in "äöüÄÖÜß"))
    lines = raw.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    expected = "Erwartet Spalten suche;datei – gefunden: "
    if not lines:
        raise ValueError(expected + f"nichts ({path.name} ist leer)")
    try:
        fmt = {"dialect": csv.Sniffer().sniff(lines[0], delimiters=";,\t")}
    except csv.Error:
        fmt = {"delimiter": ";"}
    rows = []
    try:
        reader = csv.DictReader(lines, **fmt)
        header = [(k or "").strip() for k in reader.fieldnames or []]
        if not {"suche", "datei"} <= {h.lower() for h in header}:
            raise ValueError(expected + (", ".join(h for h in header if h) or "keine Spaltennamen"))
        for r in reader:
            extra = [(v or "").strip() for v in r.pop(None, None) or []]   # mehr Felder als Spalten
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            if extra:                                                       # z. B. ';' in der Notiz
                r["notiz"] = "; ".join(x for x in [r.get("notiz", ""), *extra] if x)
            if r.get("suche") and r.get("datei"):
                rows.append(r)
    except csv.Error as e:
        raise ValueError(f"{path.name} ist keine lesbare CSV-Datei: {e}") from None
    if not rows:
        raise ValueError(f"Keine Suchfragen in {path.name} – jede Zeile braucht suche und datei")
    return rows


@dataclass
class EvalRow:
    suche: str
    datei: str
    rang: int | None          # 1 = erster Treffer; None = nicht unter den ersten 50
    seite_erwartet: int | None
    seite_gefunden: int | None
    ursache: str
    kategorie: str            # ok | Ranking | Wortwahl | OCR oder Wortwahl | OCR | Text/Format | Index
    notiz: str = ""


def _full(r) -> str:
    return os.path.join(r["root"], r["relpath"])      # wie Hit.fullpath und Hit.duplicates


def _file_index(con) -> dict[str, list]:
    """Alle Dateien des Index, auffindbar über relativen Pfad, „Ordnername/relativer Pfad“ und vollen Pfad."""
    idx: dict[str, list] = {}
    for r in con.execute("SELECT f.id, f.relpath, f.status, f.error, f.text_chars, f.ocr_pages, r.path AS root"
                         " FROM files f JOIN roots r ON r.id = f.root_id"):
        name = os.path.basename(r["root"].rstrip("/"))
        for key in {_norm(r["relpath"]), _norm(os.path.join(name, r["relpath"])), _norm(_full(r))}:
            idx.setdefault(key, []).append(r)
    return idx


def _find(idx: dict[str, list], datei: str) -> tuple[list, tuple[str, str] | None]:
    """Einträge der erwarteten Datei (mehrere nur bei verschachtelten Ordnern: dieselbe Datei) oder Ursache."""
    rows = idx.get(_norm(datei), [])
    if not rows:
        return [], ("Datei nicht im Index – Pfad prüfen oder Ordner nicht eingelesen", "Index")
    if len({_norm(_full(r)) for r in rows}) > 1:
        return [], ("Pfad mehrdeutig – bitte vollständigen Pfad angeben", "Index")
    return rows, None


def _matches(hit, target: str) -> bool:
    """Ist die erwartete Datei (target = normierter voller Pfad) der Treffer oder eine inhaltsgleiche Kopie?"""
    return any(_norm(p) == target for p in [hit.fullpath, *hit.duplicates])


def diagnose(con, searcher: Searcher, query: str, target: str) -> tuple[str, str]:
    """Warum steht die Datei `target` (Angabe wie in der Spalte datei) nicht vorn? (Ursache, Kategorie)"""
    rows, err = _find(_file_index(con), target)
    return err or _diagnose_file(con, searcher, query, rows)


def _diagnose_file(con, searcher: Searcher, query: str, entries: list) -> tuple[str, str]:
    f = next((r for r in entries if r["status"] == "done"), entries[0])
    if f["status"] == "error":
        return f"Fehler beim Einlesen: {f['error']}", "Text/Format"
    if f["status"] == "skipped":
        return "Dateityp nicht unterstützt", "Text/Format"
    if f["status"] in ("missing", "pending", "processing"):
        return f"Datei ist im Status '{f['status']}'", "Index"
    if not f["text_chars"]:
        return "Kein Text erkannt", "OCR" if f["ocr_pages"] else "Text/Format"
    live, unknown, _ = searcher.prepare(con, query)
    missing = list(unknown)
    pages = con.execute("SELECT f.path, f.body FROM fts f JOIN pages p ON p.id = f.rowid WHERE p.file_id = ?",
                        (f["id"],)).fetchall()
    for g in live:
        if not any(_page_match(g, set(r["body"].split()) | set(r["path"].split()), f" {r['body']} ")[0] > 0
                   for r in pages):
            missing.append(g.label)
    if missing and f["ocr_pages"]:
        # fehlt im erkannten Text: falsch erkannt oder steht gar nicht da – das zeigt nur das Original
        return (f"Begriff nicht im erkannten Text: {', '.join(missing)} – am Original prüfen, ob er dort steht "
                "(dann OCR-Fehler) oder nicht (andere Wortwahl)", "OCR oder Wortwahl")
    if missing:
        return f"Im Dokument nicht gefunden: {', '.join(missing)}", "Wortwahl"
    return "Alle Begriffe im Dokument, aber zu weit unten sortiert", "Ranking"


def evaluate(searcher: Searcher, queries: list[dict], k: int = 5) -> tuple[list[EvalRow], dict]:
    results: list[EvalRow] = []
    con = searcher._connect()
    try:
        idx = _file_index(con)
        for q in queries:
            rows, err = _find(idx, q["datei"])
            rank, page = None, None
            if rows:
                target = _norm(_full(rows[0]))
                for i, h in enumerate(searcher.search(q["suche"], limit=50).hits, 1):
                    if _matches(h, target):
                        rank, page = i, h.page_no
                        break
            try:
                want_page = int(q.get("seite") or 0) or None
            except ValueError:
                want_page = None
            if rank is not None and rank <= k:
                cause, cat = "ok", "ok"
                if want_page and page != want_page:
                    cause = f"gefunden, aber Seite {page} statt {want_page}"
            else:
                cause, cat = err or _diagnose_file(con, searcher, q["suche"], rows)
                if cat == "Ranking" and rank:
                    cause = f"Platz {rank}: {cause}"
            results.append(EvalRow(q["suche"], q["datei"], rank, want_page, page, cause, cat, q.get("notiz", "")))
    finally:
        con.close()
    n = len(results) or 1
    summary = {
        "anfragen": len(results),
        "platz_1": sum(1 for r in results if r.rang == 1),
        f"top_{k}": sum(1 for r in results if r.rang and r.rang <= k),
        "top_20": sum(1 for r in results if r.rang and r.rang <= 20),
        "nicht_gefunden": sum(1 for r in results if not r.rang),
        "mrr": round(sum(1 / r.rang for r in results if r.rang) / n, 3),
        "seite_falsch": sum(1 for r in results if r.rang and r.seite_erwartet and r.seite_gefunden != r.seite_erwartet),
        "ursachen": {},
    }
    for r in results:
        if r.kategorie != "ok":
            summary["ursachen"][r.kategorie] = summary["ursachen"].get(r.kategorie, 0) + 1
    return results, summary


def format_eval(results: list[EvalRow], summary: dict, k: int = 5) -> str:
    n = summary["anfragen"] or 1
    pct = lambda x: f"{x}/{summary['anfragen']} ({x / n:.0%})"  # noqa: E731
    lines = [
        f"Platz 1:        {pct(summary['platz_1'])}",
        f"Unter Top {k}:    {pct(summary[f'top_{k}'])}",
        f"Unter Top 20:   {pct(summary['top_20'])}",
        f"Nicht gefunden: {summary['nicht_gefunden']}",
        f"MRR:            {summary['mrr']}",
    ]
    if summary["seite_falsch"]:
        lines.append(f"Falsche Seite:  {summary['seite_falsch']}")
    if summary["ursachen"]:
        lines.append("Ursachen:       " + ", ".join(f"{v}× {k_}" for k_, v in summary["ursachen"].items()))
    lines += ["", f"{'Platz':>5}  {'Suche':<38} Ergebnis"]
    for r in results:
        rank = str(r.rang) if r.rang else "–"
        mark = "✓" if r.kategorie == "ok" and not r.ursache.startswith("gefunden, aber") else "✗"
        lines.append(f"{rank:>5}  {mark} {r.suche[:36]:<36} {'' if r.ursache == 'ok' else r.ursache}")
    return "\n".join(lines)


def write_json(results: list[EvalRow], summary: dict, path: Path) -> None:
    path.write_text(json.dumps({"summary": summary, "results": [asdict(r) for r in results]},
                               ensure_ascii=False, indent=2), encoding="utf-8")
