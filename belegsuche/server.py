"""Lokale Suchoberfläche im Browser (nur auf diesem Mac erreichbar: 127.0.0.1)."""
from __future__ import annotations

import errno
import json
import mimetypes
import os
import stat
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db as dbm
from .search import Searcher

UI_FILE = Path(__file__).with_name("ui.html")
MAX_ID = 2**63 - 1                     # größter Wert, den SQLite als INTEGER speichert
MAX_BODY = 10_000
# Anfragen fremder Webseiten abweisen: der eigene Browser-Tab schickt same-origin,
# Adresszeile/Lesezeichen/webbrowser.open schicken none, ältere Browser gar nichts.
ALLOWED_FETCH_SITES = (None, "same-origin", "none")
CSP_DEFAULT = "frame-ancestors 'none'"
CSP_UI = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
          "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# Pfad darf nirgends (macOS: auch nicht in übergeordneten Ordnern) ein Symlink sein; O_NOFOLLOW_ANY
# und O_NOFOLLOW zusammen ergeben EINVAL. O_NONBLOCK, damit eine untergeschobene Named Pipe das
# Öffnen nicht blockiert (bei normalen Dateien wirkungslos).
OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW_ANY", os.O_NOFOLLOW)


def _file_row(db_path: Path, file_id: int):
    con = dbm.connect(db_path, readonly=True)
    try:
        return con.execute(
            "SELECT f.relpath, f.status, r.path AS root FROM files f JOIN roots r ON r.id = f.root_id WHERE f.id = ?",
            (file_id,)).fetchone()
    finally:
        con.close()


def _resolve(db_path: Path, file_id: int) -> tuple[str | None, str | None]:
    """(echter Pfad ohne Symlinks, Fehlermeldung). Nur Dateien aus dem Index, nur innerhalb ihres Ordners."""
    row = _file_row(db_path, file_id)
    if row is None or row["status"] != "done":
        return None, "Unbekannte Datei"
    if not os.path.isdir(row["root"]):
        return None, f"Ordner nicht verbunden: {row['root']} – SSD anschließen?"
    full = os.path.join(row["root"], row["relpath"])
    real_root = os.path.realpath(row["root"])
    real = os.path.realpath(full)
    if not real.startswith(real_root.rstrip("/") + "/"):
        return None, "Pfad liegt außerhalb des Ordners"
    if not os.path.isfile(real):
        return None, "Datei nicht mehr vorhanden (verschoben oder gelöscht?) – neu einlesen"
    return real, None


def _valid_id(v) -> bool:
    return type(v) is int and 1 <= v <= MAX_ID


def make_handler(searcher: Searcher, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Belegsuche"
        timeout = 15                   # halbe oder hängende Anfragen belegen nicht ewig einen Thread
        _sent = False                  # Kopfzeilen schon gesendet? (dann keine 500-Antwort mehr möglich)
        _csp = CSP_DEFAULT

        def log_message(self, fmt, *args):  # ruhig bleiben
            pass

        def end_headers(self):
            # gilt für jede Antwort, auch für Fehlerseiten von BaseHTTPRequestHandler
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", self._csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self._sent = True
            super().end_headers()

        def _host_ok(self) -> bool:
            # Schutz gegen DNS-Rebinding: nur Anfragen an die lokale Adresse beantworten
            return self.headers.get("Host", "") in allowed_hosts

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, route):
            self._sent, self._csp = False, CSP_DEFAULT
            try:
                if not self._host_ok() or self.headers.get("Sec-Fetch-Site") not in ALLOWED_FETCH_SITES:
                    return self._json({"error": "forbidden"}, 403)
                route(urlparse(self.path))
            except (ConnectionError, TimeoutError):
                self.close_connection = True       # Browser hat abgebrochen oder Anfrage hängt
            except Exception as e:
                where = self.path.split("?", 1)[0][:200]   # ohne Suchbegriffe
                print(f"Belegsuche: Fehler bei {self.command} {where}: {type(e).__name__}: {e}", file=sys.stderr)
                self.close_connection = True
                if not self._sent:
                    try:
                        self._json({"error": "interner Fehler"}, 500)
                    except OSError:
                        pass

        def do_GET(self):
            self._handle(self._get)

        def do_POST(self):
            self._handle(self._post)

        def _get(self, url):
            if url.path == "/":
                body = UI_FILE.read_bytes()
                self._csp = CSP_UI
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/search":
                qs = parse_qs(url.query)
                q = (qs.get("q") or [""])[0][:300]
                try:
                    limit = max(1, min(100, int((qs.get("limit") or ["30"])[0])))
                except ValueError:
                    limit = 30
                resp = searcher.search(q, limit=limit)
                self._json({
                    "query": resp.query, "ms": round(resp.ms), "unknown": resp.unknown,
                    "hits": [{
                        "id": h.file_id, "filename": h.filename, "folder": h.folder, "root": h.root,
                        "page": h.page_no, "pages": h.pages, "score": h.score, "snippet": h.snippet_html,
                        "matched": h.matched, "missing": h.missing, "ocr": h.ocr, "available": h.available,
                        "duplicates": h.duplicates, "pdf": h.filename.lower().endswith(".pdf"),
                    } for h in resp.hits]})
            elif url.path == "/api/status":
                con = dbm.connect(searcher.db_path, readonly=True)
                try:
                    roots = [{"path": r["path"], "online": os.path.isdir(r["path"])}
                             for r in con.execute("SELECT path FROM roots ORDER BY id")]
                    done = con.execute("SELECT count(*) FROM files WHERE status = 'done'").fetchone()[0]
                finally:
                    con.close()
                self._json({"roots": roots, "files": done})
            elif url.path.startswith("/datei/"):
                s = url.path[len("/datei/"):]
                if not (s.isascii() and s.isdigit() and len(s) <= 18 and int(s) >= 1):
                    return self._json({"error": "bad id"}, 400)
                path, err = _resolve(searcher.db_path, int(s))
                if err:
                    return self._json({"error": err}, 404)
                # geprüften Pfad ohne Symlinks öffnen und nur das so geöffnete Objekt ausliefern:
                # ein zwischen Prüfung und Öffnen eingetauschter Symlink wird nicht verfolgt
                try:
                    fd = os.open(path, OPEN_FLAGS)
                except OSError:
                    return self._json({"error": "Datei nicht lesbar (verschoben oder ersetzt?) – neu einlesen"}, 404)
                with os.fdopen(fd, "rb") as f:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        return self._json({"error": "Keine normale Datei"}, 404)
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(st.st_size))
                    self.send_header("Content-Disposition", "inline")
                    self.end_headers()
                    left = st.st_size
                    while left > 0 and (chunk := f.read(min(1 << 16, left))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
            else:
                self._json({"error": "not found"}, 404)

        def _post(self, url):
            if self.headers.get("X-Belegsuche") != "1":
                return self._json({"error": "forbidden"}, 403)
            if url.path != "/api/open":
                return self._json({"error": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= MAX_BODY:
                    raise ValueError
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, RecursionError):
                return self._json({"error": "bad request"}, 400)
            if not isinstance(data, dict) or not _valid_id(data.get("id")):
                return self._json({"error": "bad request"}, 400)
            path, err = _resolve(searcher.db_path, data["id"])
            if err:
                return self._json({"error": err}, 404)
            reveal = data.get("reveal") is True
            cmd = ["open", "-R", path] if reveal else ["open", path]
            result = subprocess.run(cmd, check=False)
            code = getattr(result, "returncode", 0)      # None/ohne returncode: als Erfolg werten
            if isinstance(code, int) and code != 0:
                # z. B. kein Programm für diesen Dateityp oder Datei gesperrt
                msg = (f"Im Finder zeigen fehlgeschlagen (Programm meldet Fehler {code})" if reveal else
                       f"Öffnen fehlgeschlagen (Programm meldet Fehler {code}) – "
                       "Datei im Finder zeigen und von Hand öffnen")
                return self._json({"error": msg}, 500)
            self._json({"ok": True})

    return Handler


def serve(db_path: Path, port: int = 8765, open_browser: bool = True) -> int:
    """Startet die Oberfläche; 0 nach Ctrl+C, 1 wenn der Port nicht zu öffnen ist."""
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), None)
    except (OSError, OverflowError) as e:
        if getattr(e, "errno", None) == errno.EADDRINUSE:
            url = f"http://127.0.0.1:{port}/"
            print(f"Belegsuche läuft vermutlich schon: {url} – sonst anderen Port mit --port wählen")
            if open_browser:
                webbrowser.open(url)
        else:
            print(f"Port {port} lässt sich nicht öffnen: {getattr(e, 'strerror', None) or e} "
                  f"– anderen Port mit --port wählen")
        return 1
    port = httpd.server_address[1]          # bei --port 0 vergibt das System den Port
    httpd.RequestHandlerClass = make_handler(Searcher(db_path), port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Belegsuche läuft: {url}  (beenden mit Ctrl+C)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
