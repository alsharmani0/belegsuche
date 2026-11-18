"""Suchqualität messen: Findet die Suche das erwartete Dokument, und wenn nicht, warum?

Testdatei (CSV, Semikolon oder Komma):
    suche;datei;seite;notiz
    Wartung Heizung Musterstraße;2021/Objekte/Scan_00487.pdf;1;Beschreibung
`datei` ist der Pfad relativ zum eingelesenen Ordner, „<Ordnername>/<relativer Pfad>“ oder der
vollständige Pfad (Groß/Klein egal). Liegt derselbe relative Pfad in mehreren eingelesenen Ordnern,
muss der Ordnername oder der vollständige Pfad dabeistehen.
`seite` bleibt die Gesamtseite der Datei. Bei Mails können `teil` (0 = Mailtext) und
`teilseite` (Seite innerhalb dieses Teils, ab 1) die erwartete Fundstelle genauer bestimmen.
"""
from __future__ import annotations

import csv
import json
import os
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from .normalize import decode_local_text
from .search import HitLocation, Searcher, _group_score

EVAL_LIMIT = 50


def _norm(p: str) -> str:
    return unicodedata.normalize("NFC", p).replace("\\", "/").rstrip("/").casefold()


def _page_number(value, context: str, name: str = "seite", minimum: int = 1) -> int | None:
    """Optionale Seitenzahl prüfen; Fehler dürfen die Seitenprüfung nicht still abschalten."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        digits = value[1:] if value.startswith("+") else value
        if digits.isdecimal():
            try:
                number = int(value)
            except ValueError:
                pass
            else:
                if number >= minimum:
                    return number
    elif type(value) is int and value >= minimum:
        return value
    requirement = "eine positive ganze Zahl ab 1" if minimum == 1 else f"eine ganze Zahl ab {minimum}"
    raise ValueError(f"{context}: {name} muss {requirement} sein oder leer bleiben")


def _target_parts(query: dict, context: str) -> tuple[int | None, int | None]:
    part = _page_number(query.get("teil"), context, "teil", 0)
    page = _page_number(query.get("teilseite"), context, "teilseite")
    if page is not None and part is None:
        raise ValueError(f"{context}: teilseite braucht eine Teilnummer in teil")
    return part, page


def load_queries(path: Path) -> list[dict]:
    """Suchfragen aus der CSV. FileNotFoundError geht an den Aufrufer, unbrauchbarer Inhalt wird ValueError."""
    raw = decode_local_text(path.read_bytes())
    lines = raw.splitlines(keepends=True)
    skipped_lines = 0
    while skipped_lines < len(lines) and not lines[skipped_lines].strip():
        skipped_lines += 1
    lines = lines[skipped_lines:]
    expected = "Erwartet Spalten suche;datei – gefunden: "
    if not lines:
        raise ValueError(expected + f"nichts ({path.name} ist leer)")
    try:
        fmt = {"dialect": csv.Sniffer().sniff(lines[0], delimiters=";,\t")}
    except csv.Error:
        fmt = {"delimiter": ";"}
    rows = []
    try:
        # Eine gewöhnliche Kopfzeile verrät dem Sniffer nicht, wie "" in einem Feld
        # kodiert wird. Tabellenprogramme verdoppeln es gemäß CSV-Konvention.
        reader = csv.DictReader(lines, doublequote=True, strict=True, **fmt)
        header = [(k or "").strip() for k in reader.fieldnames or []]
        if not {"suche", "datei"} <= {h.lower() for h in header}:
            raise ValueError(expected + (", ".join(h for h in header if h) or "keine Spaltennamen"))
        seen, duplicates = set(), set()
        for name in (h.lower() for h in header if h):
            if name in seen:
                duplicates.add(name)
            seen.add(name)
        if duplicates:
            raise ValueError(f"{path.name}, Zeile {reader.line_num + skipped_lines}: "
                             f"Doppelte Spaltennamen: {', '.join(sorted(duplicates))}")
        for r in reader:
            extra = [(v or "").strip() for v in r.pop(None, None) or []]   # mehr Felder als Spalten
            if not any((v or "").strip() for v in r.values()) and not any(extra):
                continue                                                   # vollständig leere Zeilen
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            if extra:                                                       # z. B. ';' in der Notiz
                r["notiz"] = "; ".join(x for x in [r.get("notiz", ""), *extra] if x)
            context = f"{path.name}, Zeile {reader.line_num + skipped_lines}"
            missing = [name for name in ("suche", "datei") if not r.get(name)]
            if missing:
                raise ValueError(f"{context}: Jede Suchfrage braucht suche und datei "
                                 f"(fehlt: {', '.join(missing)})")
            _page_number(r.get("seite"), context)
            _target_parts(r, context)
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
    kategorie: str            # ok | Ranking | Wortwahl | OCR oder Wortwahl | OCR | Text/Format | Index | Fundstelle
    notiz: str = ""
    teil_erwartet: int | None = None
    teil_gefunden: int | None = None
    teilseite_erwartet: int | None = None
    teilseite_gefunden: int | None = None
    ziel_erfuellt: bool = False     # innerhalb Top k und sämtliche angegebenen Seiten-/Teilbedingungen erfüllt


def _full(r) -> str:
    return os.path.join(r["root"], r["relpath"])      # wie Hit.fullpath und Hit.duplicates


def _path_forms(r) -> tuple[str, str, str]:
    name = os.path.basename(r["root"].rstrip("/"))
    return r["relpath"], os.path.join(name, r["relpath"]), _full(r)


def _file_index(con, targets: Iterable[str]) -> dict[str, list]:
    """Nur angefragte Pfade speichern, einschließlich aller Kandidaten für mehrdeutige Namen."""
    keys = {_norm(path) for path in targets}
    names = {key.rsplit("/", 1)[-1] for key in keys}
    idx: dict[str, list] = {}
    if not keys:
        return idx
    for r in con.execute("SELECT f.id, f.relpath, f.status, f.error, f.text_chars, f.ocr_pages, r.path AS root"
                         " FROM files f JOIN roots r ON r.id = f.root_id"):
        if _norm(r["relpath"]).rsplit("/", 1)[-1] in names:
            for key in keys.intersection(_norm(path) for path in _path_forms(r)):
                idx.setdefault(key, []).append(r)
    return idx


def _find(idx: dict[str, list], datei: str) -> tuple[list, tuple[str, str] | None]:
    """Einträge der erwarteten Datei (mehrere nur bei verschachtelten Ordnern: dieselbe Datei) oder Ursache."""
    rows = idx.get(_norm(datei), [])
    if not rows:
        return [], ("Datei nicht im Index – Pfad prüfen oder Ordner nicht eingelesen", "Index")
    if len({_full(r) for r in rows}) > 1:
        exact = [r for r in rows if datei in _path_forms(r)]
        if len({_full(r) for r in exact}) != 1:
            return [], ("Pfad mehrdeutig – bitte vollständigen Pfad angeben", "Index")
        return exact, None
    return rows, None


def _target_location(hit, file_ids: set[int], part: int | None = None) -> HitLocation | None:
    """Fundstelle der erwarteten Datei – niemals einen Dubletten-Anzeigetext als Pfad auswerten."""
    locations = hit.locations or [HitLocation(hit.file_id, hit.fullpath, hit.page_no, hit.pages,
                                             hit.part_no, hit.part_page, hit.part_pages)]
    matching = [location for location in locations if location.file_id in file_ids]
    return next((location for location in matching if part is None or location.part_no == part),
                next(iter(matching), None))


def diagnose(con, searcher: Searcher, query: str, target: str) -> tuple[str, str]:
    """Warum steht die Datei `target` (Angabe wie in der Spalte datei) nicht vorn? (Ursache, Kategorie)"""
    rows, err = _find(_file_index(con, [target]), target)
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
        if not any(_group_score(g, set(r["body"].split()), set(r["path"].split()),
                                f" {r['body']} ", f" {r['path']} ")[0] > 0
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
    if not isinstance(k, int) or not 1 <= k <= EVAL_LIMIT:
        raise ValueError(f"k muss zwischen 1 und {EVAL_LIMIT} liegen")
    results: list[EvalRow] = []
    con = searcher._connect()
    try:
        con.execute("BEGIN")
        idx = _file_index(con, (q["datei"] for q in queries))
        for q in queries:
            context = f"Suchfrage {len(results) + 1}"
            want_page = _page_number(q.get("seite"), context)
            want_part, want_part_page = _target_parts(q, context)
            rows, err = _find(idx, q["datei"])
            rank, page = None, None
            part, part_page = None, None
            if rows:
                file_ids = {row["id"] for row in rows}
                for i, h in enumerate(searcher.search(q["suche"], limit=EVAL_LIMIT, snapshot=con).hits, 1):
                    location = _target_location(h, file_ids, want_part)
                    if location is not None:
                        rank, page = i, location.page_no
                        part, part_page = location.part_no, location.part_page
                        break
            wrong_part = want_part is not None and part != want_part
            wrong_part_page = want_part_page is not None and part_page != want_part_page
            if rank is not None and rank <= k:
                cause, cat = "ok", "ok"
                if wrong_part:
                    found = f"Teil {part}" if part is not None else "kein Mailteil"
                    cause, cat = f"gefunden, aber {found} statt Teil {want_part}", "Fundstelle"
                elif wrong_part_page:
                    cause, cat = f"gefunden, aber Teilseite {part_page} statt {want_part_page}", "Fundstelle"
                elif want_page and page != want_page:
                    cause = f"gefunden, aber Seite {page} statt {want_page}"
            else:
                cause, cat = err or _diagnose_file(con, searcher, q["suche"], rows)
                if cat == "Ranking" and rank:
                    cause = f"Platz {rank}: {cause}"
            exact = rank is not None and rank <= k and not wrong_part and not wrong_part_page \
                and (want_page is None or page == want_page)
            results.append(EvalRow(q["suche"], q["datei"], rank, want_page, page, cause, cat, q.get("notiz", ""),
                                   teil_erwartet=want_part, teil_gefunden=part,
                                   teilseite_erwartet=want_part_page, teilseite_gefunden=part_page,
                                   ziel_erfuellt=exact))
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
        "teil_falsch": sum(1 for r in results if r.rang and r.teil_erwartet is not None and
                           r.teil_gefunden != r.teil_erwartet),
        "teilseite_falsch": sum(1 for r in results if r.rang and r.teilseite_erwartet is not None and
                               r.teilseite_gefunden != r.teilseite_erwartet),
        f"ziel_top_{k}": sum(1 for r in results if r.ziel_erfuellt),
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
    if any(r.teil_erwartet is not None for r in results):
        lines.append(f"Ziel in Top {k}: {pct(summary[f'ziel_top_{k}'])}")
        if summary["teil_falsch"]:
            lines.append(f"Falscher Teil:  {summary['teil_falsch']}")
        if summary["teilseite_falsch"]:
            lines.append(f"Falsche Teilseite: {summary['teilseite_falsch']}")
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
