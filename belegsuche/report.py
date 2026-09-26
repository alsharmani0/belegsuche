"""Abdeckungsbericht: Was ist durchsuchbar, was nicht, und warum."""
from __future__ import annotations

import csv
import os
from pathlib import Path

LOW_CONF = 0.5


BLANK_CHARS = 3      # weniger erkannte Zeichen auf einer Scan-Seite: vermutlich leere Seite (Rückseite)


def _pages_list(nums: list[int]) -> str:
    return ", ".join(str(n) for n in nums)


def build_report(con) -> dict:
    r: dict = {"roots": [], "status": {}, "unsupported": {}, "problems": []}
    for row in con.execute("SELECT id, path FROM roots ORDER BY id"):
        n = con.execute("SELECT count(*) FROM files WHERE root_id = ?", (row["id"],)).fetchone()[0]
        r["roots"].append({"id": row["id"], "path": row["path"], "online": os.path.isdir(row["path"]), "files": n})
    for row in con.execute("SELECT status, count(*) AS n FROM files GROUP BY status"):
        r["status"][row["status"]] = row["n"]
    for row in con.execute("SELECT ext, count(*) AS n FROM files WHERE status = 'skipped' GROUP BY ext ORDER BY n DESC"):
        r["unsupported"][row["ext"] or "(ohne Endung)"] = row["n"]
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
    r["done_no_text"] = q("SELECT count(*) FROM files WHERE status = 'done' AND text_chars = 0")
    r["done_with_ocr"] = q("SELECT count(*) FROM files WHERE status = 'done' AND ocr_pages > 0")
    r["pages"] = q("SELECT count(*) FROM pages p JOIN files f ON f.id = p.file_id WHERE f.status = 'done'")
    r["ocr_pages"] = q("SELECT count(*) FROM pages p JOIN files f ON f.id = p.file_id"
                       " WHERE f.status = 'done' AND p.source != 'text'")
    r["duplicates"] = q("SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM files"
                        " WHERE status = 'done' AND sha256 IS NOT NULL GROUP BY sha256 HAVING n > 1)")

    # Auffällige Scan-Seiten je Datei: leer (vermutlich Rückseite) oder unsicher erkannt
    blank: dict[int, list[int]] = {}
    low: dict[int, list[tuple[int, float]]] = {}
    for row in con.execute(
            "SELECT p.file_id, p.page_no, p.ocr_conf, length(trim(p.text)) AS n FROM pages p"
            " JOIN files f ON f.id = p.file_id WHERE f.status = 'done' AND f.text_chars > 0 AND p.source != 'text'"
            " AND (length(trim(p.text)) < ? OR (p.ocr_conf IS NOT NULL AND p.ocr_conf < ?))"
            " ORDER BY p.file_id, p.page_no", (BLANK_CHARS, LOW_CONF)):
        if row["n"] < BLANK_CHARS:
            blank.setdefault(row["file_id"], []).append(row["page_no"])
        else:
            low.setdefault(row["file_id"], []).append((row["page_no"], row["ocr_conf"]))
    r["blank_pages"] = sum(len(v) for v in blank.values())
    r["low_conf_pages"] = sum(len(v) for v in low.values())

    notes = {row["id"]: row for row in con.execute(
        "SELECT f.id, f.relpath, r.path AS root, f.status, f.error, f.text_chars FROM files f"
        " JOIN roots r ON r.id = f.root_id WHERE f.status IN ('error', 'missing', 'pending')"
        " OR (f.status = 'done' AND (f.text_chars = 0 OR f.error IS NOT NULL))")}
    ids = set(notes) | set(blank) | set(low)
    rows = {row["id"]: row for row in con.execute(
        f"SELECT f.id, f.relpath, r.path AS root, f.status, f.error, f.text_chars FROM files f"
        f" JOIN roots r ON r.id = f.root_id WHERE f.id IN ({','.join('?' * len(ids))})", list(ids))} if ids else {}
    order = {"error": 0, "missing": 1, "pending": 2, "done": 3}
    for fid, row in sorted(rows.items(), key=lambda kv: (order.get(kv[1]["status"], 9), kv[1]["relpath"])):
        if row["status"] == "error":
            reason = row["error"] or "Fehler"
        elif row["status"] == "missing":
            reason = "Datei fehlt (gelöscht oder verschoben)"
        elif row["status"] == "pending":
            reason = "noch nicht eingelesen"
        else:   # eingelesen, aber mit Einschränkung
            parts = [row["error"]] if row["error"] else []   # Hinweis, z. B. nicht alle Seiten eingelesen
            if row["text_chars"] == 0:
                parts.append("kein Text erkannt")
            if fid in low:
                parts.append("unsichere Texterkennung: " + ", ".join(f"Seite {n} ({c:.0%})" for n, c in low[fid]))
            if fid in blank:
                parts.append(f"leere Seite {_pages_list(blank[fid])} – vermutlich Rückseite, bitte kurz prüfen")
            reason = "; ".join(parts)
        r["problems"].append({"pfad": os.path.join(row["root"], row["relpath"]), "status": row["status"],
                              "grund": reason})
    return r


def format_report(r: dict, max_problems: int = 25) -> str:
    st = r["status"]
    total = sum(st.values())
    done = st.get("done", 0)
    searchable = done - r["done_no_text"]
    lines = ["Belegsuche – Bericht", ""]
    for root in r["roots"]:
        lines.append(f"Ordner: {root['path']}  ({'verbunden' if root['online'] else 'NICHT verbunden'}, "
                     f"{root['files']} Dateien)")
    lines += [
        "",
        f"Dateien gesamt:            {total}",
        f"  durchsuchbar:            {searchable}  (davon mit Texterkennung: {r['done_with_ocr']})",
        f"  ohne erkennbaren Text:   {r['done_no_text']}",
        f"  Fehler:                  {st.get('error', 0)}",
        f"  nicht unterstützt:       {st.get('skipped', 0)}"
        + (f"  ({', '.join(f'{k} {v}' for k, v in list(r['unsupported'].items())[:8])})" if r["unsupported"] else ""),
        f"  fehlen im Ordner:        {st.get('missing', 0)}",
        f"  noch offen:              {st.get('pending', 0) + st.get('processing', 0)}",
        f"  doppelt vorhanden:       {r['duplicates']}",
        "",
        f"Seiten: {r['pages']} (per Texterkennung: {r['ocr_pages']}, davon unsicher: {r['low_conf_pages']}, "
        f"leer: {r['blank_pages']})",
    ]
    if r["problems"]:
        lines += ["", f"Problemfälle ({len(r['problems'])}):"]
        for p in r["problems"][:max_problems]:
            lines.append(f"  - {p['pfad']}  →  {p['grund']}")
        if len(r["problems"]) > max_problems:
            lines.append(f"  … und {len(r['problems']) - max_problems} weitere (vollständig mit --csv)")
    return "\n".join(lines)


def write_problems_csv(r: dict, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["pfad", "status", "grund"])
        for p in r["problems"]:
            w.writerow([p["pfad"], p["status"], p["grund"]])
