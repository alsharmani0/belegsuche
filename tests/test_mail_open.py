"""Mails und Anhänge öffnen (Server und Oberfläche) – nur erfundene Daten, Adressen auf .example.

- „Mail öffnen“ startet Apple Mail mit der .eml, „Im Finder zeigen“ zeigt die .eml.
- „Anhang öffnen“ schreibt eine schreibgeschützte Kopie in einen eigenen Ordner im System-Temp – nie neben die
  Mail, nie in einen eingelesenen Ordner – und nur nach Prüfung der sha256.
- /datei/<id>/teil/<n> liefert PDF und Bilder aus dem Speicher; Mails, Word und alles andere nur als Download.
"""
import http.client
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from email import policy
from email.message import EmailMessage
from http.server import ThreadingHTTPServer

import pytest

from belegsuche import db as dbm
from belegsuche import server as srv
from belegsuche.indexer import run_index
from belegsuche.search import Searcher

from .conftest import BytesEngine, digital_pdf, docx_bytes, make_eml, text_image, tree_state

RUN = subprocess.run          # ungepatcht: die Server-Tests ersetzen subprocess.run (auch für node)
FILL = ["Sehr geehrte Damen und Herren, anbei erhalten Sie unsere Unterlagen zur weiteren Bearbeitung.",
        "Bitte prüfen Sie die Angaben und melden Sie sich bei Rückfragen gern telefonisch bei uns."]
XSS_SUBJECT = "<img src=x onerror=alert(1)> Rechnung Juli"
XSS_NAME = "<svg onload=alert(2)>.pdf"


@pytest.fixture(autouse=True)
def _nie_echter_index(tmp_path, monkeypatch):
    monkeypatch.setenv("BELEGSUCHE_DB", str(tmp_path / "env-index.db"))


def _pdf(tmp_path, pages, name) -> bytes:
    p = tmp_path / name
    digital_pdf(p, [list(pg) + FILL for pg in pages])
    return p.read_bytes()


def _png() -> bytes:
    buf = io.BytesIO()
    text_image(["Kassenbon Baumarkt Nord", "Summe 11,48 EUR"], size=(900, 700), font_size=36).save(buf, "PNG")
    return buf.getvalue()


def build(tmp_path):
    root = tmp_path / "ordner"
    src = tmp_path / "quellen"
    src.mkdir()
    pdf = _pdf(src, [["Sammelrechnung Blatt 1 Gartenbau Eiche"], ["Rechnungsnummer RE-2024/0815 Rasenpflege"],
                     ["Sammelrechnung Blatt 3 Gartenbau Eiche"]], "r.pdf")
    inner_pdf = _pdf(src, [["Weitergeleitete Gutschrift GS-7731 Dachrinne"]], "g.pdf")
    inner = EmailMessage(policy=policy.SMTP)
    inner["Subject"], inner["From"] = "Gutschrift", "Kowalski <info@kowalski.example>"
    inner.set_content("Bitte verrechnen")
    inner.add_attachment(inner_pdf, maintype="application", subtype="pdf", filename="Gutschrift.pdf")
    png = _png()
    docx = docx_bytes(["Bericht Wasserschaden Kellerraum Lindenweg"])
    html = b"<html><body><script>alert(3)</script><p>Hausordnung Ruhezeiten Innenhof</p></body></html>"
    xlsx = b"PK\x03\x04" + bytes(40)
    exe = b"MZ\x90\x00" + bytes(200)
    eml = make_eml(root / "Vorgang 17" / "Emails" / "rechnung.eml", subject=XSS_SUBJECT, text="Anbei die Unterlagen",
                   attachments=[("Rechnung.pdf", pdf, "application/pdf"),           # Teil 1
                                ("Foto.png", png, "application/octet-stream"),       # Teil 2
                                ("Bericht.docx", docx, "application/octet-stream"),  # Teil 3
                                ("Liste.xlsx", xlsx, "application/octet-stream"),    # Teil 4
                                ("seite.html", html, "application/octet-stream"),    # Teil 5
                                ("setup.exe", exe, "application/octet-stream"),      # Teil 6
                                inner,                                               # Teil 7 (+ 8: Gutschrift.pdf)
                                (XSS_NAME, _pdf(src, [["Mahnung Hausgeld MH-5510"]], "m.pdf"),
                                 "application/pdf")])
    plain = root / "beleg.pdf"
    digital_pdf(plain, [["Quittung Schlüsseldienst Nord", *FILL]])
    word = root / "brief.docx"
    word.write_bytes(docx_bytes(["Kündigung Stellplatz Garage 4"]))
    db = tmp_path / "i.db"
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=BytesEngine(), log=lambda s: None)
    return {"root": root, "eml": eml, "db": db, "con": con, "pdf": pdf, "png": png, "docx": docx, "html": html,
            "inner_pdf": inner_pdf}


@pytest.fixture()
def mailsrv(tmp_path, monkeypatch):
    env = build(tmp_path)
    systmp = tmp_path / "systemtmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    state = {"calls": [], "result": None}

    def fake_run(cmd, check=False):
        state["calls"].append(cmd)
        return state["result"](cmd) if callable(state["result"]) else state["result"]
    monkeypatch.setattr(srv.subprocess, "run", fake_run)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(Searcher(env["db"]), port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    con = env["con"]
    ids = {r[0]: r[1] for r in con.execute("SELECT relpath, id FROM files")}
    env.update(port=port, state=state, calls=state["calls"], systmp=systmp, httpd=httpd,
               mail=ids["Vorgang 17/Emails/rechnung.eml"], plain=ids["beleg.pdf"], word=ids["brief.docx"],
               before=tree_state(env["root"]))
    yield env
    httpd.shutdown()
    httpd.server_close()
    httpd.RequestHandlerClass.temp.cleanup()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body) if body is not None else None
    c.request(method, path, body=data, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    r = c.getresponse()
    return r.status, r.read(), r.headers


def post(env, body, headers=None):
    st, raw, _ = req(env["port"], "POST", "/api/open", body, {"X-Belegsuche": "1", **(headers or {})})
    return st, json.loads(raw)


def part_no(env, name):
    return env["con"].execute("SELECT part_no FROM parts WHERE file_id = ? AND name = ?",
                              (env["mail"], name)).fetchone()[0]


def test_mail_parts_as_expected(mailsrv):
    rows = mailsrv["con"].execute("SELECT part_no, kind, name, mime_path FROM parts WHERE file_id = ? ORDER BY part_no",
                                  (mailsrv["mail"],)).fetchall()
    assert [tuple(r) for r in rows] == [
        (0, "body", None, ""), (1, "pdf", "Rechnung.pdf", "2"), (2, "image", "Foto.png", "3"),
        (3, "word", "Bericht.docx", "4"), (4, "skipped", "Liste.xlsx", "5"), (5, "text", "seite.html", "6"),
        (6, "skipped", "setup.exe", "7"), (7, "mail", "Weitergeleitete Mail.eml", "8"),
        (8, "pdf", "Gutschrift.pdf", "8/2"), (9, "pdf", XSS_NAME, "9")]


# --- Mail öffnen und im Finder zeigen ----------------------------------------------------------------

def test_open_mail_uses_apple_mail(mailsrv):
    assert post(mailsrv, {"id": mailsrv["mail"]}) == (200, {"ok": True})
    assert mailsrv["calls"] == [["open", "-b", "com.apple.mail", os.path.realpath(mailsrv["eml"])]]


def test_open_mail_falls_back_to_default_app(mailsrv):
    mailsrv["state"]["result"] = lambda cmd: subprocess.CompletedProcess(cmd, 1 if "-b" in cmd else 0)
    assert post(mailsrv, {"id": mailsrv["mail"]}) == (200, {"ok": True})
    eml = os.path.realpath(mailsrv["eml"])
    assert mailsrv["calls"] == [["open", "-b", "com.apple.mail", eml], ["open", eml]]
    mailsrv["state"]["result"] = lambda cmd: subprocess.CompletedProcess(cmd, 1)
    st, body = post(mailsrv, {"id": mailsrv["mail"]})
    assert st == 500 and "Fehler 1" in body["error"]


def test_reveal_shows_the_eml_also_for_attachments(mailsrv):
    eml = os.path.realpath(mailsrv["eml"])
    assert post(mailsrv, {"id": mailsrv["mail"], "reveal": True}) == (200, {"ok": True})
    assert post(mailsrv, {"id": mailsrv["mail"], "reveal": True, "part": 1}) == (200, {"ok": True})
    assert mailsrv["calls"] == [["open", "-R", eml], ["open", "-R", eml]]
    assert not os.listdir(mailsrv["systmp"])                 # keine Temp-Kopie fürs Zeigen im Finder


def test_other_files_still_open_normally(mailsrv):
    assert post(mailsrv, {"id": mailsrv["plain"]}) == (200, {"ok": True})
    assert mailsrv["calls"] == [["open", os.path.realpath(mailsrv["root"] / "beleg.pdf")]]


# --- Anhang öffnen -----------------------------------------------------------------------------------

def _opened(env) -> str:
    cmd = env["calls"][-1]
    assert cmd[0] == "open" and len(cmd) == 2
    return cmd[1]


def test_open_attachment_writes_readonly_copy_outside_the_folder(mailsrv):
    assert post(mailsrv, {"id": mailsrv["mail"], "part": 1}) == (200, {"ok": True})
    path = _opened(mailsrv)
    folder = os.path.dirname(path)
    assert os.path.basename(path) == "Rechnung.pdf"
    assert os.path.dirname(folder) == os.path.realpath(mailsrv["systmp"]) or \
        os.path.dirname(folder) == str(mailsrv["systmp"])
    assert os.path.basename(folder).startswith("belegsuche-anhang-")
    assert not os.path.realpath(path).startswith(os.path.realpath(mailsrv["root"]) + "/")
    assert open(path, "rb").read() == mailsrv["pdf"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o400
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert tree_state(mailsrv["root"]) == mailsrv["before"]                # Originale unverändert


@pytest.mark.parametrize("name, part, ext", [("Foto.png", 2, ".png"), ("Bericht.docx", 3, ".docx"),
                                              ("Gutschrift.pdf", 8, ".pdf")])
def test_open_image_word_and_attachment_of_forwarded_mail(mailsrv, name, part, ext):
    assert post(mailsrv, {"id": mailsrv["mail"], "part": part}) == (200, {"ok": True})
    path = _opened(mailsrv)
    assert os.path.basename(path) == name and path.endswith(ext)
    if part == 8:
        assert open(path, "rb").read() == mailsrv["inner_pdf"]


def test_each_open_gets_its_own_folder(mailsrv):
    post(mailsrv, {"id": mailsrv["mail"], "part": 1})
    post(mailsrv, {"id": mailsrv["mail"], "part": 1})
    a, b = [c[1] for c in mailsrv["calls"]]
    assert a != b and os.path.basename(a) == os.path.basename(b) == "Rechnung.pdf"


def test_html_attachment_opens_as_plain_text(mailsrv):
    assert post(mailsrv, {"id": mailsrv["mail"], "part": 5}) == (200, {"ok": True})
    path = _opened(mailsrv)
    assert os.path.basename(path) == "seite.txt"                        # nie .html (Browser, Skripte)
    assert open(path, "rb").read() == mailsrv["html"]


def test_xss_name_is_harmless_on_disk(mailsrv):
    assert post(mailsrv, {"id": mailsrv["mail"], "part": 9}) == (200, {"ok": True})
    assert os.path.basename(_opened(mailsrv)) == "<svg onload=alert(2)>.pdf"
    assert os.path.dirname(os.path.dirname(_opened(mailsrv))) in (str(mailsrv["systmp"]),
                                                                 os.path.realpath(mailsrv["systmp"]))


@pytest.mark.parametrize("stored, expected", [
    ("../../x.pdf", "x.pdf"),
    ("..\\..\\x.pdf", "x.pdf"),
    ("Rechnung‮fdp.exe", "Rechnungfdp.pdf"),
    ("...versteckt.pdf", "versteckt.pdf"),
    ("a\x00b\x07c\r\nd.pdf", "abcd.pdf"),
    ("Scan.EXE", "Scan.pdf"),
    ("", "Anhang.pdf"),
    ("..", "Anhang.pdf"),
    ("Rechnung vom 12.03", "Rechnung vom 12.03.pdf"),
])
def test_attachment_name_is_sanitized(mailsrv, stored, expected):
    con = mailsrv["con"]
    con.execute("UPDATE parts SET name = ? WHERE file_id = ? AND part_no = 1", (stored, mailsrv["mail"]))
    con.commit()
    assert post(mailsrv, {"id": mailsrv["mail"], "part": 1}) == (200, {"ok": True})
    path = _opened(mailsrv)
    assert os.path.basename(path) == expected
    assert os.path.dirname(os.path.dirname(path)) in (str(mailsrv["systmp"]), os.path.realpath(mailsrv["systmp"]))


def test_safe_filename_limits_length_and_takes_extension_from_content():
    name = srv.safe_filename("R" * 400 + ".pdf", ".pdf")
    assert name.endswith(".pdf") and len(name) <= srv.MAX_NAME_CHARS + 4
    assert srv.safe_filename("über.JPG", ".png") == "über.png"       # NFC, Endung aus den Bytes
    assert "‮" not in srv.safe_filename("a‮b.pdf", ".pdf")
    assert "/" not in srv.safe_filename("a/b/../../c:d.pdf", ".pdf")


def test_sha_mismatch_gives_409(mailsrv):
    con = mailsrv["con"]
    con.execute("UPDATE parts SET sha256 = ? WHERE file_id = ? AND part_no = 1", ("0" * 64, mailsrv["mail"]))
    con.commit()
    assert post(mailsrv, {"id": mailsrv["mail"], "part": 1}) == (409, {"error": "Mail geändert – neu einlesen"})
    st, body, _ = req(mailsrv["port"], "GET", f"/datei/{mailsrv['mail']}/teil/1")
    assert st == 409 and b"%PDF" not in body
    assert not mailsrv["calls"] and not os.listdir(mailsrv["systmp"])


def test_changed_mail_on_disk_gives_409(mailsrv, tmp_path):
    other = _pdf(tmp_path, [["Ganz andere Rechnung"]], "anders.pdf")
    make_eml(tmp_path / "neu.eml", subject="x", text="y", attachments=[("Rechnung.pdf", other, "application/pdf")])
    os.replace(tmp_path / "neu.eml", mailsrv["eml"])           # die Mail wurde ersetzt, Index noch alt
    assert post(mailsrv, {"id": mailsrv["mail"], "part": 1}) == (409, {"error": "Mail geändert – neu einlesen"})
    assert post(mailsrv, {"id": mailsrv["mail"], "part": 8})[0] == 409             # Teil gibt es nicht mehr
    assert not mailsrv["calls"]


@pytest.mark.parametrize("part", [4, 6, 7, 10, 99])
def test_dangerous_or_unknown_parts_are_refused(mailsrv, part):
    st, body = post(mailsrv, {"id": mailsrv["mail"], "part": part})
    assert st == (404 if part in (10, 99) else 415) and body["error"]
    st, raw, headers = req(mailsrv["port"], "GET", f"/datei/{mailsrv['mail']}/teil/{part}")
    assert st == (404 if part in (10, 99) else 415) and b"MZ" not in raw and b"PK" not in raw
    assert not mailsrv["calls"] and not os.listdir(mailsrv["systmp"])


def test_part_kind_tampered_in_index_still_refused(mailsrv):
    """Auch wenn parts.kind (z. B. durch einen alten Stand) „pdf“ sagt: zählt der Inhalt (hier: .exe)."""
    con = mailsrv["con"]
    con.execute("UPDATE parts SET kind = 'pdf' WHERE file_id = ? AND part_no = 6", (mailsrv["mail"],))
    con.commit()
    st, body = post(mailsrv, {"id": mailsrv["mail"], "part": 6})
    assert st == 415 and not mailsrv["calls"] and not os.listdir(mailsrv["systmp"])


@pytest.mark.parametrize("part", ["1", 0, -1, True, 1.5, 10**6, [1], {"n": 1}])
def test_malformed_part_is_rejected(mailsrv, part):
    st, body = post(mailsrv, {"id": mailsrv["mail"], "part": part})
    assert st == 400 and not mailsrv["calls"]


def test_part_of_a_file_that_is_no_mail(mailsrv):
    st, body = post(mailsrv, {"id": mailsrv["plain"], "part": 1})
    assert st == 400 and "keine E-Mail" in body["error"] and not mailsrv["calls"]
    st, _, _ = req(mailsrv["port"], "GET", f"/datei/{mailsrv['plain']}/teil/1")
    assert st == 400


def test_temp_folder_inside_an_indexed_folder_is_refused(mailsrv, monkeypatch, tmp_path):
    for base in [mailsrv["root"], mailsrv["root"] / "Vorgang 17"]:
        monkeypatch.setattr(tempfile, "tempdir", str(base))
        st, body = post(mailsrv, {"id": mailsrv["mail"], "part": 1})
        assert st == 500 and "eingelesenen Ordner" in body["error"]
    link = tmp_path / "tmp-link"                           # über einen Symlink in den Ordner
    link.symlink_to(mailsrv["root"] / "Vorgang 17", target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    st, body = post(mailsrv, {"id": mailsrv["mail"], "part": 1})
    assert st == 500
    assert not mailsrv["calls"]
    assert tree_state(mailsrv["root"]) == mailsrv["before"]


# --- /datei: Teile und Downloads ---------------------------------------------------------------------

def test_pdf_part_is_served_from_memory(mailsrv):
    st, body, headers = req(mailsrv["port"], "GET", f"/datei/{mailsrv['mail']}/teil/1")
    assert st == 200 and body == mailsrv["pdf"] and body.startswith(b"%PDF")
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"].startswith("inline;") and "Rechnung.pdf" in headers["Content-Disposition"]
    assert headers["X-Content-Type-Options"] == "nosniff" and "frame-ancestors 'none'" in \
        headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert not os.listdir(mailsrv["systmp"])                     # ohne Temp-Datei


def test_image_part_inline_with_sandbox(mailsrv):
    st, body, headers = req(mailsrv["port"], "GET", f"/datei/{mailsrv['mail']}/teil/2")
    assert st == 200 and body == mailsrv["png"] and headers["Content-Type"] == "image/png"
    assert "sandbox" in headers["Content-Security-Policy"]


@pytest.mark.parametrize("part", [3, 5])          # Word und HTML
def test_other_parts_are_downloads_never_inline(mailsrv, part):
    st, body, headers = req(mailsrv["port"], "GET", f"/datei/{mailsrv['mail']}/teil/{part}")
    assert st == 200
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Content-Disposition"].startswith("attachment;")
    csp = headers["Content-Security-Policy"]
    assert "sandbox" in csp and "default-src 'none'" in csp
    if part == 5:
        assert "seite.txt" in headers["Content-Disposition"] and "html" not in headers["Content-Type"]


def test_mail_and_word_files_are_downloads(mailsrv):
    for fid, name in [(mailsrv["mail"], "rechnung.eml"), (mailsrv["word"], "brief.docx")]:
        st, body, headers = req(mailsrv["port"], "GET", f"/datei/{fid}")
        assert st == 200 and len(body) > 0
        assert headers["Content-Type"] == "application/octet-stream", name
        assert headers["Content-Disposition"].startswith("attachment;") and name in headers["Content-Disposition"]
        assert "sandbox" in headers["Content-Security-Policy"]
    st, body, headers = req(mailsrv["port"], "GET", f"/datei/{mailsrv['plain']}")    # PDF wie bisher
    assert st == 200 and body.startswith(b"%PDF") and headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"] == "inline" and "sandbox" not in headers["Content-Security-Policy"]


@pytest.mark.parametrize("suffix", ["/teil/0", "/teil/abc", "/teil/1/x", "/teil/", "/teil/999999", "/teil/+1",
                                    "/teil/%31", "/teile/1", "/teil/1.0"])
def test_malformed_part_paths(mailsrv, suffix):
    st, body, _ = req(mailsrv["port"], "GET", f"/datei/{mailsrv['mail']}{suffix}")
    assert st == 400 and b"%PDF" not in body


@pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                                     {"Origin": "http://evil.example"}, {"Referer": "http://evil.example/x"},
                                     {"Host": "evil.example"}])
def test_foreign_sites_get_403_on_new_routes(mailsrv, headers):
    port, mail = mailsrv["port"], mailsrv["mail"]
    for path in [f"/datei/{mail}/teil/1", f"/datei/{mail}"]:
        st, body, _ = req(port, "GET", path, headers=headers)
        assert st == 403 and b"%PDF" not in body and b"Subject" not in body
    st, _, _ = req(port, "POST", "/api/open", {"id": mail, "part": 1}, {"X-Belegsuche": "1", **headers})
    assert st == 403
    st, _, _ = req(port, "POST", "/api/open", {"id": mail}, {"X-Belegsuche": "1", **headers})
    assert st == 403
    assert not mailsrv["calls"] and not os.listdir(mailsrv["systmp"])


def test_open_part_needs_custom_header(mailsrv):
    st, _, _ = req(mailsrv["port"], "POST", "/api/open", {"id": mailsrv["mail"], "part": 1})
    assert st == 403 and not mailsrv["calls"]


def test_part_of_disconnected_folder(mailsrv, tmp_path):
    mailsrv["root"].rename(tmp_path / "abgezogen")
    st, body = post(mailsrv, {"id": mailsrv["mail"], "part": 1})
    assert st == 404 and "nicht verbunden" in body["error"]


def test_part_with_swapped_symlink_is_not_followed(mailsrv, monkeypatch, tmp_path):
    secret = tmp_path / "draussen.eml"
    shutil.copy(mailsrv["eml"], secret)
    orig = srv._resolve

    def racing(db_path, fid):
        result = orig(db_path, fid)
        tmp_link = mailsrv["eml"].with_name("tausch")
        tmp_link.symlink_to(secret)
        os.replace(tmp_link, mailsrv["eml"])
        return result
    monkeypatch.setattr(srv, "_resolve", racing)
    st, body, _ = req(mailsrv["port"], "GET", f"/datei/{mailsrv['mail']}/teil/1")
    assert st == 404 and b"%PDF" not in body


# --- Aufräumen -----------------------------------------------------------------------------------------

def test_cleanup_removes_copies(mailsrv):
    post(mailsrv, {"id": mailsrv["mail"], "part": 1})
    post(mailsrv, {"id": mailsrv["mail"], "part": 2})
    assert len(os.listdir(mailsrv["systmp"])) == 2
    mailsrv["httpd"].RequestHandlerClass.temp.cleanup()
    assert os.listdir(mailsrv["systmp"]) == []


def test_old_unmanaged_folders_are_preserved(tmp_path, monkeypatch):
    base = tmp_path / "systemtmp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    old = time.time() - 25 * 3600

    def mk(name, age):
        d = base / name
        d.mkdir()
        f = d / "Rechnung.pdf"
        f.write_bytes(b"%PDF")
        f.chmod(0o400)
        os.utime(d, (age, age))
        return d
    stale = mk("belegsuche-anhang-alt", old)
    fresh = mk("belegsuche-anhang-neu", time.time())
    foreign = mk("anderes-programm", old)
    outside = tmp_path / "woanders"
    outside.mkdir()
    (outside / "wichtig.txt").write_text("bleibt")
    link = base / "belegsuche-anhang-link"
    link.symlink_to(outside, target_is_directory=True)
    os.utime(link, (old, old), follow_symlinks=False)
    assert srv.AttachmentTemp.cleanup_stale(roots=[]) == 0
    assert stale.exists() and fresh.exists() and foreign.exists()
    assert link.is_symlink() and (outside / "wichtig.txt").read_text() == "bleibt"


def test_serve_keeps_unmanaged_old_folders_and_cleans_its_copies(tmp_path, monkeypatch):
    env = build(tmp_path)
    base = tmp_path / "systemtmp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    stale = base / "belegsuche-anhang-vorgestern"
    stale.mkdir()
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))
    calls = []
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: pytest.fail("kein Browser"))
    servers, result = [], []

    class Capture(ThreadingHTTPServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            servers.append(self)
    monkeypatch.setattr(srv, "ThreadingHTTPServer", Capture)
    t = threading.Thread(target=lambda: result.append(srv.serve(env["db"], port=0, open_browser=False)), daemon=True)
    t.start()
    for _ in range(100):
        if servers and servers[0].RequestHandlerClass is not None:
            break
        time.sleep(0.05)
    port = servers[0].server_address[1]
    fid = env["con"].execute("SELECT id FROM files WHERE relpath LIKE '%.eml'").fetchone()[0]
    st, _, _ = req(port, "POST", "/api/open", {"id": fid, "part": 1}, {"X-Belegsuche": "1"})
    assert st == 200 and stale.exists()
    copy = calls[-1][1]
    assert os.path.isfile(copy)
    servers[0].shutdown()
    t.join(5)
    assert result == [0]
    assert not os.path.exists(copy) and os.listdir(base) == [stale.name]


# --- Suche (JSON) und Oberfläche -----------------------------------------------------------------------

def test_search_json_has_mail_fields(mailsrv):
    st, body, _ = req(mailsrv["port"], "GET", "/api/search?q=RE-2024%2F0815")
    hit = json.loads(body)["hits"][0]
    assert st == 200 and hit["filename"] == "rechnung.eml"
    assert {k: hit[k] for k in ("kind", "part_no", "part_name", "part_kind", "part_page", "part_pages",
                                "mail_subject", "mail_from", "mail_date")} == {
        "kind": "mail", "part_no": 1, "part_name": "Rechnung.pdf", "part_kind": "pdf", "part_page": 2,
        "part_pages": 3, "mail_subject": XSS_SUBJECT, "mail_from": "Erika Beispiel <erika@firma.example>",
        "mail_date": "12.03.2021 14:05"}
    st, body, _ = req(mailsrv["port"], "GET", "/api/search?q=Schl%C3%BCsseldienst")
    plain = json.loads(body)["hits"][0]
    assert plain["kind"] == "pdf" and plain["part_no"] is None and plain["mail_subject"] is None


UI_PRELUDE = r"""
function mkEl() {
  return {value: '', innerHTML: '', textContent: '', className: '', children: [], addEventListener() {},
          appendChild(c) { if (!this.children.includes(c)) this.children.push(c); },
          querySelector(sel) { return this.children.find(c => '.' + c.className === sel) || null; }};
}
const els = {};
globalThis.document = {getElementById: (id) => els[id] ||= mkEl(), createElement: () => mkEl()};
const sent = [];
globalThis.fetch = async (url, opts) => {
  sent.push([url, opts && opts.body]);
  return {ok: true, status: 200, json: async () => ({ok: true})};
};
"""


def run_ui(hits_json: str, scenario: str):
    if not shutil.which("node"):
        pytest.skip("node nicht installiert")
    script = srv.UI_FILE.read_text(encoding="utf-8").split("<script>")[1].split("</script>")[0]
    p = RUN(["node", "-e", UI_PRELUDE + script + f"\nconst HITS = {hits_json};\n" + scenario],
            capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_ui_renders_mail_hit_with_escaped_subject_and_attachment(mailsrv):
    st, body, _ = req(mailsrv["port"], "GET", "/api/search?q=RE-2024%2F0815")
    hits = json.loads(body)["hits"]
    st, body2, _ = req(mailsrv["port"], "GET", "/api/search?q=MH-5510")
    hits += json.loads(body2)["hits"]
    st, body3, _ = req(mailsrv["port"], "GET", "/api/search?q=Schl%C3%BCsseldienst")
    hits += json.loads(body3)["hits"]
    res = run_ui(json.dumps(hits), r"""
(async () => {
  const rows = HITS.map(hitRow);
  const actions = mkEl(), btn = {parentElement: actions};
  await openFile(HITS[0].id, false, btn, HITS[0].part_no);
  await openFile(HITS[0].id, false, btn);
  console.log(JSON.stringify({rows, sent}));
})();
""")
    mail_row, xss_row, plain_row = res["rows"]
    for row in (mail_row, xss_row):
        assert "<img" not in row and "onerror=alert" not in row.replace("&lt;img src=x onerror=alert(1)&gt;", "")
        assert "&lt;img src=x onerror=alert(1)&gt; Rechnung Juli" in row
        assert "Erika Beispiel &lt;erika@firma.example&gt; · 12.03.2021 14:05" in row
        assert ">MAIL<" in row and "Mail öffnen" in row and "Anhang öffnen" in row and "Im Finder zeigen" in row
    assert "Anhang 1 · Rechnung.pdf · Seite 2 von 3" in mail_row
    assert f'href="/datei/{mailsrv["mail"]}/teil/1#page=2"' in mail_row and "Seite 2 im Browser" in mail_row
    assert f"openFile({mailsrv['mail']}, false, this, 1)" in mail_row
    assert "<svg onload" not in xss_row and "Anhang 9 · &lt;svg onload=alert(2)&gt;.pdf" in xss_row
    assert "/teil/" not in xss_row                    # einseitiger Anhang: kein Link „Seite Y im Browser“
    assert ">PDF<" in plain_row and "Mail öffnen" not in plain_row and "Anhang öffnen" not in plain_row
    assert "</svg>Öffnen</button>" in plain_row
    assert [json.loads(b) for url, b in res["sent"] if url == "/api/open"] == [
        {"id": mailsrv["mail"], "reveal": False, "part": 1}, {"id": mailsrv["mail"], "reveal": False}]


def test_ui_mail_body_hit_and_word_tile():
    hits = [{"id": 5, "filename": "m.eml", "folder": "Emails", "root": "/r", "page": 1, "pages": 4,
             "snippet": "Text", "matched": [], "missing": [], "ocr": True, "available": True, "duplicates": [],
             "pdf": False, "kind": "mail", "part_no": 0, "part_name": None, "part_kind": "body", "part_page": 1,
             "part_pages": 1, "mail_subject": None, "mail_from": None, "mail_date": None},
            {"id": 6, "filename": "brief.docx", "folder": "", "root": "/r", "page": 1, "pages": 1, "snippet": "s",
             "matched": [], "missing": [], "ocr": False, "available": False, "duplicates": [], "pdf": False,
             "kind": "word", "part_no": None, "part_name": None, "part_kind": None, "part_page": None,
             "part_pages": None, "mail_subject": None, "mail_from": None, "mail_date": None},
            {"id": 7, "filename": "m2.eml", "folder": "", "root": "/r", "page": 3, "pages": 3, "snippet": "s",
             "matched": [], "missing": [], "ocr": False, "available": True, "duplicates": [], "pdf": False,
             "kind": "mail", "part_no": 2, "part_name": "Liste.xlsx", "part_kind": "skipped", "part_page": 1,
             "part_pages": 1, "mail_subject": "S", "mail_from": None, "mail_date": None}]
    res = run_ui(json.dumps(hits), "console.log(JSON.stringify(HITS.map(hitRow)));")
    body, word, skipped = res
    assert "(ohne Betreff)" in body and "Anhang " not in body and "Anhang öffnen" not in body
    assert "Seite 1 von 4" not in body and "Scan" not in body       # Mailtext: keine Seite der ganzen Mail
    assert ">DOC<" in word and 'class="sheet word"' in word and "disabled" in word
    assert "Anhang 2 · Liste.xlsx" in skipped and "Anhang öffnen" not in skipped


def test_ui_keeps_existing_actions_for_documents():
    ui = srv.UI_FILE.read_text(encoding="utf-8")
    assert re.search(r"\.chip\.part \{[^}]*white-space:normal", ui)       # lange Anhangnamen brechen um (mobil)
    assert "i-mail" in ui and "i-clip" in ui
