"""Abdeckungsbericht: Was ist durchsuchbar, was nicht, und warum."""
from __future__ import annotations

import csv
import os
import re
import unicodedata
from pathlib import Path

from . import db as dbm

LOW_CONF = 0.5


BLANK_CHARS = 3      # weniger erkannte Zeichen auf einer Scan-Seite: vermutlich leere Seite (Rückseite)
# SQLite trim() entfernt ohne zweites Argument nur normale Leerzeichen.
# Dieselben Randzeichen wie Python str.strip() verwenden (auch OCR-Zeilenumbrüche).
_WHITESPACE = (" \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004"
               "\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")

# Umlaute im DOS-Zeichensatz (CP850), beim Kopieren als Windows-Zeichensatz gelesen (cp1252, cp1250, cp1257):
# „šbergabe“ statt „Übergabe“. Kennt der Windows-Zeichensatz das Byte nicht, bleibt ein Steuerzeichen stehen.
_MOJI_CTRL = {"\x84": "ä", "\x94": "ö", "\x81": "ü", "\x8e": "Ä", "\x99": "Ö", "\x9a": "Ü"}
_MOJI_UPPER = {"Ž": "Ä", "ˇ": "Ä", "™": "Ö", "š": "Ü"}
_MOJI_SZ = {"á": "ß", "į": "ß"}
_MOJI_SUSPECT = re.compile("[„”" + "".join(_MOJI_CTRL) + "".join(_MOJI_UPPER) + "".join(_MOJI_SZ) + "]")
_VOWELS = set("aeiouyäöüAEIOUYÄÖÜ")


def repair_umlauts(name: str) -> str | None:
    """Vermutlich richtiger Name, wenn `name` beschädigte Umlaute enthält – sonst None.

    Ersetzt wird nur, wo ein Umlaut stehen kann, damit echte Zeichen (Anführungszeichen, Namen wie „Dušek“
    oder „Novák“) nicht als Fehler zählen: ä/ö im Wort, Ä/Ö/Ü am Wortanfang oder in Großbuchstaben, ß nach
    einem Vokal. Nur ein Vorschlag für den Bericht – umbenannt wird nichts.
    """
    s = unicodedata.normalize("NFC", name)
    if not _MOJI_SUSPECT.search(s):
        return None
    out = list(s)
    for i, c in enumerate(s):
        prev = out[i - 1] if i else ""
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if c in _MOJI_CTRL:                                        # Steuerzeichen haben in Namen nichts zu suchen
            out[i] = _MOJI_CTRL[c]
        elif c == "„" and prev.isalpha():                          # M„rz – nicht das öffnende „Zitat“
            out[i] = "ä"
        elif c == "”" and nxt.isalpha() and (prev.isalpha() or nxt.islower()):   # L”schung – nicht Zitat”
            out[i] = "ö"
        elif c in _MOJI_UPPER and nxt.isalpha() and (not prev.isalpha() or prev.isupper() or nxt.isupper()):
            out[i] = _MOJI_UPPER[c]                                # šbergabe, MšLLER – nicht Dušek
        elif c in _MOJI_SZ and prev in _VOWELS:                    # Straáe – nicht Novák
            out[i] = _MOJI_SZ[c]
    fixed = "".join(out)
    return fixed if fixed != s else None


def _broken_names(con) -> tuple[int, list[dict]]:
    """Dateien, deren Datei- oder Ordnername beschädigte Umlaute enthält, und die betroffenen Namen."""
    files = 0
    names: dict[str, str] = {}
    for row in con.execute("SELECT f.relpath, r.path AS root FROM files f JOIN roots r ON r.id = f.root_id"
                           " WHERE f.status != 'missing'"):
        parts = row["relpath"].split(os.sep)
        hit = False
        for i, part in enumerate(parts):
            fixed = repair_umlauts(part)
            if fixed:
                hit = True
                names.setdefault(os.path.join(row["root"], *parts[:i + 1]), fixed)
        files += hit
    return files, [{"pfad": p, "vorschlag": v} for p, v in sorted(names.items())]


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
    # Wie die Suche: auch bisherige Seiten geänderter Dateien und Belege ohne
    # erkannten Text (über Dateiname/Pfad), aber keine neuen Dateien ohne Seiten.
    searchable = con.execute(
        "SELECT count(*) AS total, coalesce(sum(f.text_chars > 0), 0) AS with_text,"
        " coalesce(sum(f.text_chars > 0 AND f.ocr_pages > 0), 0) AS with_ocr_text,"
        " coalesce(sum(f.status != 'done'), 0) AS outdated"
        f" FROM files f WHERE f.status IN {dbm.SEARCHABLE_SQL}"
        " AND EXISTS (SELECT 1 FROM pages p WHERE p.file_id = f.id)"
    ).fetchone()
    r["searchable"] = searchable["total"]
    r["searchable_with_text"] = searchable["with_text"]
    r["searchable_name_only"] = searchable["total"] - searchable["with_text"]
    r["searchable_with_ocr_text"] = searchable["with_ocr_text"]
    r["searchable_outdated"] = searchable["outdated"]
    r["pages"] = q("SELECT count(*) FROM pages p JOIN files f ON f.id = p.file_id WHERE f.status = 'done'")
    r["ocr_pages"] = q("SELECT count(*) FROM pages p JOIN files f ON f.id = p.file_id"
                       " WHERE f.status = 'done' AND p.source != 'text'")
    r["duplicates"] = q("SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM files"
                        " WHERE status = 'done' AND sha256 IS NOT NULL GROUP BY sha256 HAVING n > 1)")
    r["broken_names"], r["broken_name_list"] = _broken_names(con)

    # Auffällige Scan-Seiten je Datei: leer (vermutlich Rückseite) oder unsicher erkannt
    blank: dict[int, list[int]] = {}
    low: dict[int, list[tuple[int, float]]] = {}
    for row in con.execute(
            "SELECT p.file_id, p.page_no, p.ocr_conf, length(trim(p.text, ?)) AS n FROM pages p"
            " JOIN files f ON f.id = p.file_id WHERE f.status = 'done' AND f.text_chars > 0 AND p.source != 'text'"
            " AND (length(trim(p.text, ?)) < ? OR (p.ocr_conf IS NOT NULL AND p.ocr_conf < ?))"
            " ORDER BY p.file_id, p.page_no", (_WHITESPACE, _WHITESPACE, BLANK_CHARS, LOW_CONF)):
        if row["n"] < BLANK_CHARS:
            blank.setdefault(row["file_id"], []).append(row["page_no"])
        else:
            low.setdefault(row["file_id"], []).append((row["page_no"], row["ocr_conf"]))
    r["blank_pages"] = sum(len(v) for v in blank.values())
    r["low_conf_pages"] = sum(len(v) for v in low.values())

    notes = {row["id"]: row for row in con.execute(
        "SELECT f.id, f.relpath, r.path AS root, f.status, f.error, f.text_chars,"
        " EXISTS (SELECT 1 FROM pages p WHERE p.file_id = f.id) AS has_pages FROM files f"
        " JOIN roots r ON r.id = f.root_id WHERE f.status IN ('error', 'missing', 'pending')"
        " OR (f.status = 'done' AND (f.text_chars = 0 OR f.error IS NOT NULL))")}
    rows = dict(notes)
    # in Blöcken abfragen: mehr IDs als SQLite-Variablen erlaubt ("too many SQL variables")
    extra_ids = list((set(blank) | set(low)) - set(notes))
    for chunk in range(0, len(extra_ids), 900):
        ids_ = extra_ids[chunk:chunk + 900]
        rows.update({row["id"]: row for row in con.execute(
            f"SELECT f.id, f.relpath, r.path AS root, f.status, f.error, f.text_chars FROM files f"
            f" JOIN roots r ON r.id = f.root_id WHERE f.id IN ({','.join('?' * len(ids_))})", ids_)})
    order = {"error": 0, "missing": 1, "pending": 2, "done": 3}
    for fid, row in sorted(rows.items(), key=lambda kv: (order.get(kv[1]["status"], 9), kv[1]["relpath"])):
        if row["status"] == "error":
            reason = row["error"] or "Fehler"
        elif row["status"] == "missing":
            reason = "Datei fehlt (gelöscht oder verschoben)"
        elif row["status"] == "pending":
            reason = ("geändert – noch nicht neu eingelesen; bisheriger Stand weiter durchsuchbar"
                      if row["has_pages"] else "noch nicht eingelesen")
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
    lines = ["Belegsuche – Bericht", ""]
    for root in r["roots"]:
        lines.append(f"Ordner: {root['path']}  ({'verbunden' if root['online'] else 'NICHT verbunden'}, "
                     f"{root['files']} Dateien)")
    lines += [
        "",
        f"Dateien gesamt:            {total}",
        f"  durchsuchbar:            {r['searchable']}"
        + (f"  (davon mit bisherigem Stand: {r['searchable_outdated']})" if r["searchable_outdated"] else ""),
        f"  mit erkanntem Text:      {r['searchable_with_text']}"
        f"  (davon mit Texterkennung: {r['searchable_with_ocr_text']})",
        f"  nur Dateiname/Pfad:      {r['searchable_name_only']}",
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
    if r.get("broken_names"):
        names = r["broken_name_list"]
        lines += ["", f"Dateinamen mit beschädigten Umlauten: {r['broken_names']}  (beim Kopieren falsch übertragen? "
                      "Nichts wurde umbenannt – bitte von Hand korrigieren und neu einlesen)"]
        for n in names[:max_problems]:
            lines.append(f"  - {n['pfad']}  →  vermutlich: {n['vorschlag']}")
        if len(names) > max_problems:
            lines.append(f"  … und {len(names) - max_problems} weitere Namen")
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
