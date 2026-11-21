"""Kommandozeile: belegsuche index | suche | start | bericht | test | ordner"""
from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from contextlib import closing, nullcontext
from pathlib import Path

from . import db as dbm

DEFAULT_PARALLEL = max(1, min(8, (os.cpu_count() or 2) - 2))


def _non_negative(v: str) -> int:
    """argparse-Typ für --limit: negative Zahlen würden sonst als Python-Slice wirken (todo[:-1])."""
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"keine ganze Zahl: {v!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"muss 0 oder größer sein: {n}")
    return n


def _positive(v: str) -> int:
    """argparse-Typ für suche --limit: 0 oder negativ ergäbe "Nichts gefunden" bzw. schnitte Treffer ab."""
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"keine ganze Zahl: {v!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"muss 1 oder größer sein: {n}")
    return n


def _eval_k(v: str) -> int:
    from .evaluate import EVAL_LIMIT
    n = _positive(v)
    if n > EVAL_LIMIT:
        raise argparse.ArgumentTypeError(f"höchstens {EVAL_LIMIT} Treffer werden ausgewertet")
    return n


def _root_id(v: str) -> int:
    n = _positive(v)
    if n > 2**63 - 1:
        raise argparse.ArgumentTypeError("Ordner-ID liegt außerhalb des SQLite-Zahlenbereichs")
    return n


def _safe(s: str) -> str:
    """Steuerzeichen aus Dateinamen/Texten nicht ans Terminal durchreichen."""
    return "".join(c if c.isprintable() or c in "\n\t" else repr(c)[1:-1] for c in str(s))


def _db(args) -> Path:
    return Path(args.db).expanduser() if getattr(args, "db", None) else dbm.default_db_path()


def _require_db(args) -> Path | None:
    """Pfad eines vorhandenen Index – vorher ggf. auf das aktuelle Format umstellen (einmalig, meldet sich selbst)."""
    path = _db(args)
    if not path.is_file() or path.stat().st_size == 0:
        print(f"Noch kein Index vorhanden ({_safe(path)}). Zuerst: belegsuche index <ordner>")
        return None
    with closing(dbm.connect(path, readonly=True)) as con:
        current = dbm.get_meta(con, "index_format") == dbm.INDEX_FORMAT
    if not current:
        try:
            dbm.connect(path).close()
        except sqlite3.OperationalError as e:
            raise sqlite3.OperationalError(f"Indexformat konnte nicht aktualisiert werden: {e}") from e
    return path


def _inside_folder(path: Path, root: Path) -> bool:
    # Auch auf Dateisystemen mit unterschiedlichen Schreibweisen für denselben Ordner.
    return path.is_relative_to(root.resolve()) or (
        root.is_dir() and any(p.exists() and p.samefile(root) for p in path.parents))


def _write_export(db_path: Path, destination: str, writer: Callable[[Path], None], *, source: Path | None = None):
    """Bericht atomar ersetzen; weder SQLite-Dateien noch Quelldaten überschreiben."""
    out = Path(destination).expanduser()
    resolved = out.resolve()
    if out.is_symlink():
        raise OSError(f"Ausgabepfad ist ein symbolischer Link: {out}")
    if out.exists() and not out.is_file():
        raise OSError(f"Ausgabepfad ist keine normale Datei: {out}")
    database = db_path.resolve()
    protected = [database, *(Path(str(database) + ext) for ext in ("-wal", "-shm", "-journal"))]
    if source is not None:
        protected.append(source)
    for p in protected:
        if resolved == p.resolve() or (out.exists() and p.exists() and out.samefile(p)):
            raise OSError(f"Ausgabedatei würde den Index oder die Eingabedatei überschreiben: {out}")
    if out.exists():
        with closing(dbm.connect(db_path, readonly=True)) as con:
            roots = [Path(r[0]) for r in con.execute("SELECT path FROM roots")]
        for root in roots:
            if _inside_folder(resolved, root):
                raise OSError(f"Vorhandene Datei im eingelesenen Ordner bleibt unverändert: {out}")
    # Auch ein Hardlink auf ein Original außerhalb des Ausgabeordners wird dadurch nicht durchschrieben.
    with tempfile.NamedTemporaryFile(dir=out.parent, prefix=".belegsuche-", delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        writer(temp_path)
        temp_path.replace(out)
    finally:
        temp_path.unlink(missing_ok=True)


def _interrupt(signum, frame):
    # Terminal-Fenster geschlossen oder abgemeldet: wie Ctrl+C sauber anhalten
    raise KeyboardInterrupt


def cmd_index(args) -> int:
    from .indexer import PERMANENT_ERRORS, acquire_lock, count_retryable_mails, count_safe_mails, run_index
    path = _db(args)
    roots = [Path(p).expanduser() for p in args.ordner]
    if path.is_file() and path.stat().st_size:
        with closing(dbm.connect(path, readonly=True)) as existing:
            roots.extend(Path(r[0]) for r in existing.execute("SELECT path FROM roots"))
    if any(_inside_folder(path.resolve(), root) for root in roots):
        raise OSError("Die Index-Datenbank muss außerhalb der eingelesenen Ordner liegen (--db).")
    with acquire_lock(path, roots=roots):
        old = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGHUP, signal.SIGTERM)}
        try:
            with closing(dbm.connect(path)) as con:
                scans, ps = run_index(con, args.ordner, engine_name=args.ocr,
                                      confirm_missing=args.fehlende_bestaetigen, limit=args.limit,
                                      workers=max(1, args.parallel), retry=args.fehler_wiederholen,
                                      refresh_mail_heads=args.mailkopf_erneuern,
                                      log=lambda s: print(_safe(s)))
                if ps.interrupted:
                    return 130
                # Eine bekannte beschädigte PDF lässt sich manuell erneut versuchen,
                # soll aber nicht nach jedem unveränderten Lauf denselben Hinweis auslösen.
                retryable = con.execute(
                    "SELECT count(*) FROM files WHERE status='error' AND coalesce(error,'') NOT LIKE 'PDF beschädigt%'"
                    " AND coalesce(error,'') NOT LIKE 'Word-Datei beschädigt%'"
                    " AND coalesce(error,'') NOT LIKE 'Word-Datei nicht lesbar%'"
                    f" AND coalesce(error,'') NOT IN ({','.join('?' for _ in PERMANENT_ERRORS)})",
                    PERMANENT_ERRORS).fetchone()[0]
                safe_mails = count_safe_mails(con)
                partial_mails = count_retryable_mails(con)
        except KeyboardInterrupt:
            print("\nUnterbrochen. Beim nächsten Start geht es an derselben Stelle weiter.")
            return 130
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
    print()
    print(f"Fertig: {ps.done} Dateien verarbeitet ({ps.reused} übernommen, {ps.ocr_pages} Seiten per Texterkennung), "
          f"{ps.errors} Fehler, {ps.seconds / 60:.1f} min.")
    renamed = sum(s.renamed for s in scans)
    if renamed:
        print(f"umbenannt/verschoben: {renamed} (alte Einträge entfernt, Text übernommen)")
    if ps.skipped_offline:
        print(f"{ps.skipped_offline} Dateien warten auf einen nicht verbundenen Ordner.")
    if retryable:
        print(f"{retryable} Datei(en) mit Fehler später erneut versuchen: belegsuche index --fehler-wiederholen")
    if safe_mails:
        print(f"{safe_mails} Mail(s) nur mit Mailtext eingelesen (Anhänge nach Zeitlimit oder Absturz übersprungen) – "
              "später erneut versuchen: belegsuche index --fehler-wiederholen")
    if partial_mails:
        print(f"{partial_mails} Mail(s) mit vorübergehend nicht lesbaren Anhängen – "
              "erneut versuchen: belegsuche index --fehler-wiederholen")
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
        page = f"  (Seite {h.page_no}/{h.pages})" if h.pages > 1 and h.kind != "mail" else ""
        off = "" if h.available else "  [nicht verbunden]"
        if h.outdated:
            off += "  [geändert – wird neu eingelesen]"
        print(f"{i:>2}. {_safe(h.filename)}{page}{off}")
        print(f"    {_safe(os.path.join(h.root, h.folder))}")
        if h.kind == "mail":
            head = " · ".join(x for x in (h.mail_subject or "(ohne Betreff)", h.mail_from, h.mail_date) if x)
            print(f"    Mail: {_safe(head)}")
            if h.part_no:
                origin = f" (aus {_safe(h.part_from)})" if h.part_from else ""
                print(f"    Anhang {h.part_no}: {_safe(h.part_name or '(ohne Namen)')}{origin}, "
                      f"Seite {h.part_page}/{h.part_pages}")
            for link in h.links[:5]:
                where = os.path.join(h.root, link["path"]) if link["path"] else "(nicht im Vorgangsordner)"
                print(f"    In DOMUS abgelegt: {_safe(link['name'] or '(ohne Namen)')} → {_safe(where)}")
            if len(h.links) > 5:
                print(f"    … und {len(h.links) - 5} weitere Anhänge in DOMUS")
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
    with closing(dbm.connect(path, readonly=True)) as con:
        con.execute("BEGIN")
        r = build_report(con)
    print(_safe(format_report(r)))
    if args.csv:
        _write_export(path, args.csv, lambda output: write_problems_csv(r, output))
        print(f"\nProblemliste gespeichert: {_safe(args.csv)}")
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
        print(f"Datei nicht gefunden: {_safe(args.datei)}")
        return 2
    except ValueError as e:
        print(_safe(e))
        return 2
    results, summary = evaluate(Searcher(path), queries, k=args.k)
    print(_safe(format_eval(results, summary, k=args.k)))
    if args.json:
        _write_export(path, args.json, lambda output: write_json(results, summary, output), source=Path(args.datei))
    return 0


def cmd_roots(args) -> int:
    from .indexer import acquire_lock
    path = _require_db(args)
    if path is None:
        return 1
    if args.aktion == "umziehen" and _inside_folder(path.resolve(), Path(args.neuer_pfad).expanduser()):
        raise OSError("Der neue Ordner darf die Index-Datenbank nicht enthalten.")
    mutate = args.aktion != "liste"
    lock = nullcontext()
    if mutate:
        with closing(dbm.connect(path, readonly=True)) as existing:
            roots = [Path(r[0]) for r in existing.execute("SELECT path FROM roots")]
        if args.aktion == "umziehen":
            roots.append(Path(args.neuer_pfad).expanduser())
        lock = acquire_lock(path, roots=roots)
    with lock, closing(dbm.connect(path, readonly=not mutate)) as con:
        return _roots_action(args, con)


def _roots_action(args, con) -> int:
    from .indexer import move_root
    if args.aktion == "umziehen":
        err = move_root(con, args.id, args.neuer_pfad)
        if err:
            print(_safe(err))
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
    s.add_argument("--limit", type=_non_negative, help="höchstens so viele Dateien verarbeiten (zum Testen)")
    s.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL,
                   help=f"Anzahl paralleler Prozesse für die Texterkennung (Standard hier: {DEFAULT_PARALLEL})")
    s.add_argument("--fehler-wiederholen", action="store_true",
                   help="Fehlerhafte Dateien und Mails mit wiederholbaren Anhangfehlern erneut einlesen")
    s.add_argument("--mailkopf-erneuern", action="store_true",
                   help="E-Mails neu einlesen, deren Kopf noch einen Vermerk einer früheren Version zeigt "
                        "(„Entwurf“ bei gesendeten CRM-Mails, alter Datumszusatz); ohne neue Texterkennung")
    s.add_argument("--fehlende-bestaetigen", action="store_true",
                   help="viele fehlende Dateien wirklich als gelöscht markieren")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("suche", parents=[common], help="in der Kommandozeile suchen")
    s.add_argument("anfrage", nargs="+")
    s.add_argument("--limit", type=_positive, default=10, help="höchstens so viele Treffer anzeigen (Standard: 10)")
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
    s.add_argument("--k", type=_eval_k, default=5, help="Ranggrenze für die Auswertung (Standard: 5)")
    s.add_argument("--json", help="Ergebnis zusätzlich als JSON speichern")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("ordner", parents=[common], help="eingelesene Ordner anzeigen, umziehen oder entfernen")
    s.add_argument("aktion", nargs="?", choices=["liste", "umziehen", "entfernen"], default="liste")
    s.add_argument("id", nargs="?", type=_root_id)
    s.add_argument("neuer_pfad", nargs="?")
    s.set_defaults(func=cmd_roots)

    args = p.parse_args(argv)
    if args.cmd == "ordner" and args.aktion != "liste" and args.id is None:
        p.error("ordner umziehen/entfernen braucht eine ID (siehe: belegsuche ordner)")
    if args.cmd == "ordner" and args.aktion == "umziehen" and not args.neuer_pfad:
        p.error("ordner umziehen ID NEUER_PFAD")
    if args.cmd == "ordner" and ((args.aktion == "liste" and args.id is not None)
                                 or (args.aktion == "entfernen" and args.neuer_pfad is not None)):
        p.error("unerwartete Argumente für ordner " + args.aktion)
    try:
        return args.func(args)
    except (OSError, sqlite3.DatabaseError) as e:
        print(f"Fehler bei {args.cmd}: {_safe(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
