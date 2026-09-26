"""Kommandozeile: belegsuche index | suche | start | bericht | test | ordner"""
from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
from pathlib import Path

from . import db as dbm

DEFAULT_PARALLEL = max(1, min(8, (os.cpu_count() or 2) - 2))


def _safe(s: str) -> str:
    """Steuerzeichen aus Dateinamen/Texten nicht ans Terminal durchreichen."""
    return "".join(c if c.isprintable() or c in "\n\t" else repr(c)[1:-1] for c in str(s))


def _db(args) -> Path:
    return Path(args.db).expanduser() if getattr(args, "db", None) else dbm.default_db_path()


def _require_db(args) -> Path | None:
    """Pfad eines vorhandenen Index – vorher ggf. auf das aktuelle Format umstellen (einmalig, meldet sich selbst)."""
    path = _db(args)
    if not path.exists() or path.stat().st_size == 0:
        print(f"Noch kein Index vorhanden ({path}). Zuerst: belegsuche index <ordner>")
        return None
    try:
        dbm.connect(path).close()
    except sqlite3.OperationalError as e:   # z. B. schreibgeschützter Ort: dann ohne Umstellung suchen
        print(f"Hinweis: Index konnte nicht aktualisiert werden ({e}) – Suche läuft mit dem vorhandenen Stand.",
              file=sys.stderr)
    return path


def _interrupt(signum, frame):
    # Terminal-Fenster geschlossen oder abgemeldet: wie Ctrl+C sauber anhalten
    raise KeyboardInterrupt


def cmd_index(args) -> int:
    from .indexer import acquire_lock, run_index
    path = _db(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(path)  # noqa: F841  (hält die Sperre bis zum Ende des Laufs)
    old = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGHUP, signal.SIGTERM)}
    con = dbm.connect(path)
    try:
        scans, ps = run_index(con, args.ordner, engine_name=args.ocr, confirm_missing=args.fehlende_bestaetigen,
                              limit=args.limit, workers=max(1, args.parallel), retry=args.fehler_wiederholen,
                              log=lambda s: print(_safe(s)))
    except KeyboardInterrupt:
        print("\nUnterbrochen. Beim nächsten Start geht es an derselben Stelle weiter.")
        return 130
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    print()
    print(f"Fertig: {ps.done} Dateien verarbeitet ({ps.reused} übernommen, {ps.ocr_pages} Seiten per Texterkennung), "
          f"{ps.errors} Fehler, {ps.seconds / 60:.1f} min.")
    if ps.skipped_offline:
        print(f"{ps.skipped_offline} Dateien warten auf einen nicht verbundenen Ordner.")
    retryable = con.execute("SELECT count(*) FROM files WHERE status = 'error' AND error NOT LIKE 'PDF ist passwort%'"
                            " AND error NOT LIKE 'PDF beschädigt%'").fetchone()[0]
    if retryable:
        print(f"{retryable} Datei(en) mit Fehler später erneut versuchen: belegsuche index --fehler-wiederholen")
    if ps.interrupted:
        return 130
    print("Überblick: belegsuche bericht   ·   Suchen: belegsuche start")
    return 0


def cmd_search(args) -> int:
    from .search import Searcher
    path = _require_db(args)
    if path is None:
        return 1
    resp = Searcher(path).search(" ".join(args.anfrage), limit=args.limit)
    if resp.unknown:
        print(f"Kommt in keinem Dokument vor: {_safe(', '.join(resp.unknown))}")
    if not resp.hits:
        print("Nichts gefunden.")
        return 1
    for i, h in enumerate(resp.hits, 1):
        page = f"  (Seite {h.page_no}/{h.pages})" if h.pages > 1 else ""
        off = "" if h.available else "  [nicht verbunden]"
        print(f"{i:>2}. {_safe(h.filename)}{page}{off}")
        print(f"    {_safe(os.path.join(h.root, h.folder))}")
        print(f"    {_safe(h.snippet_text)}")
        if h.missing:
            print(f"    nicht enthalten: {_safe(', '.join(h.missing))}")
        if h.duplicates:
            print(f"    auch unter: {_safe(' · '.join(h.duplicates))}")
    print(f"\n{len(resp.hits)} Treffer in {resp.ms:.0f} ms")
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    path = _require_db(args)
    if path is None:
        return 1
    rc = serve(path, port=args.port, open_browser=not args.kein_browser)
    return rc if isinstance(rc, int) else 0


def cmd_report(args) -> int:
    from .report import build_report, format_report, write_problems_csv
    path = _require_db(args)
    if path is None:
        return 1
    r = build_report(dbm.connect(path))
    print(_safe(format_report(r)))
    if args.csv:
        write_problems_csv(r, Path(args.csv))
        print(f"\nProblemliste gespeichert: {args.csv}")
    return 0


def cmd_eval(args) -> int:
    from .evaluate import evaluate, format_eval, load_queries, write_json
    from .search import Searcher
    path = _require_db(args)
    if path is None:
        return 1
    try:
        queries = load_queries(Path(args.datei))
    except FileNotFoundError:
        print(f"Datei nicht gefunden: {args.datei}")
        return 2
    except ValueError as e:
        print(e)
        return 2
    results, summary = evaluate(Searcher(path), queries, k=args.k)
    print(_safe(format_eval(results, summary, k=args.k)))
    if args.json:
        write_json(results, summary, Path(args.json))
    return 0


def cmd_roots(args) -> int:
    from .indexer import move_root
    path = _require_db(args)
    if path is None:
        return 1
    con = dbm.connect(path)
    if args.aktion == "umziehen":
        err = move_root(con, args.id, args.neuer_pfad)
        if err:
            print(err)
            return 1
        print(f"Ordner {args.id} zeigt jetzt auf {_safe(args.neuer_pfad)}. Danach: belegsuche index")
        return 0
    if args.aktion == "entfernen":
        if not con.execute("SELECT 1 FROM roots WHERE id = ?", (args.id,)).fetchone():
            print(f"Unbekannte Ordner-ID {args.id} (siehe: belegsuche ordner)")
            return 1
        con.execute("DELETE FROM fts WHERE rowid IN (SELECT p.id FROM pages p JOIN files f ON f.id = p.file_id"
                    " WHERE f.root_id = ?)", (args.id,))
        con.execute("DELETE FROM fts_doc WHERE rowid IN (SELECT id FROM files WHERE root_id = ?)", (args.id,))
        con.execute("DELETE FROM roots WHERE id = ?", (args.id,))
        dbm.bump_generation(con)
        con.commit()
        print(f"Ordner {args.id} aus dem Index entfernt (Originale unberührt).")
        return 0
    for r in con.execute("SELECT r.id, r.path, (SELECT count(*) FROM files f WHERE f.root_id = r.id) AS n"
                         " FROM roots r ORDER BY r.id"):
        state = "verbunden" if os.path.isdir(r["path"]) else "NICHT verbunden"
        print(f"{r['id']}: {_safe(r['path'])}  ({r['n']} Dateien, {state})")
    return 0


def main(argv: list[str] | None = None) -> int:
    db_help = f"Index-Datei (Standard: {dbm.default_db_path()})"
    p = argparse.ArgumentParser(prog="belegsuche", description="Belege und Dokumente lokal durchsuchen.")
    p.add_argument("--db", help=db_help)
    # --db auch nach dem Unterbefehl erlauben (belegsuche index ORDNER --db x.db)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help=db_help)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("index", parents=[common], help="Ordner einlesen bzw. aktualisieren (fortsetzbar)")
    s.add_argument("ordner", nargs="*", help="Ordner; ohne Angabe: alle bekannten Ordner aktualisieren")
    s.add_argument("--ocr", choices=["apple", "tesseract"], default="apple", help="Texterkennung (Standard: apple)")
    s.add_argument("--limit", type=int, help="höchstens so viele Dateien verarbeiten (zum Testen)")
    s.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL,
                   help=f"Anzahl paralleler Prozesse für die Texterkennung (Standard hier: {DEFAULT_PARALLEL})")
    s.add_argument("--fehler-wiederholen", action="store_true",
                   help="Dateien mit Fehlern erneut versuchen (z. B. nach abgezogener SSD)")
    s.add_argument("--fehlende-bestaetigen", action="store_true",
                   help="viele fehlende Dateien wirklich als gelöscht markieren")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("suche", parents=[common], help="in der Kommandozeile suchen")
    s.add_argument("anfrage", nargs="+")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("start", parents=[common], help="Suchoberfläche im Browser öffnen")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--kein-browser", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("bericht", parents=[common], help="Was ist durchsuchbar, was nicht?")
    s.add_argument("--csv", help="Problemliste als CSV speichern")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("test", parents=[common], help="Suchqualität mit einer Liste von Suchfragen messen")
    s.add_argument("datei", help="CSV mit Spalten suche;datei;seite;notiz")
    s.add_argument("--k", type=int, default=5)
    s.add_argument("--json", help="Ergebnis zusätzlich als JSON speichern")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("ordner", parents=[common], help="eingelesene Ordner anzeigen, umziehen oder entfernen")
    s.add_argument("aktion", nargs="?", choices=["liste", "umziehen", "entfernen"], default="liste")
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("neuer_pfad", nargs="?")
    s.set_defaults(func=cmd_roots)

    args = p.parse_args(argv)
    if args.cmd == "ordner" and args.aktion != "liste" and args.id is None:
        p.error("ordner umziehen/entfernen braucht eine ID (siehe: belegsuche ordner)")
    if args.cmd == "ordner" and args.aktion == "umziehen" and not args.neuer_pfad:
        p.error("ordner umziehen ID NEUER_PFAD")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
