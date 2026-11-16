"""Nachbesserung nach der Prüfung von E-Mail und Word (Prüfer A: Code/Sicherheit, Prüfer B: echte Daten).

Nur erfundene Daten, Adressen auf .example. Jeder Test sichert einen Befund gegen Rückfall ab.
"""
import base64
import ctypes
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from belegsuche import cli
from belegsuche import db as dbm
from belegsuche import indexer
from belegsuche import mail as mailmod
from belegsuche import server as srv
from belegsuche import word as wordmod
from belegsuche.extract import PageText, extract_full, sniff
from belegsuche.indexer import run_index
from belegsuche.report import build_report, format_report
from belegsuche.search import Searcher

from .conftest import BytesEngine, digital_pdf, docx_bytes, make_eml, text_image, tree_state

PROJECT = Path(__file__).resolve().parents[1]
FILL = ["Sehr geehrte Damen und Herren, anbei erhalten Sie unsere Unterlagen zur weiteren Bearbeitung.",
        "Bitte prüfen Sie die Angaben und melden Sie sich bei Rückfragen gern telefonisch bei uns."]
PLACEHOLDER = "Die Datei '{}' wurde in die Dokumentenverwaltung von DOMUS CRM verschoben. 03.02.2023 10:15 EB"


@pytest.fixture(autouse=True)
def _nie_echter_index(tmp_path, monkeypatch):
    monkeypatch.setenv("BELEGSUCHE_DB", str(tmp_path / "env-index.db"))


def quiet(_):
    pass


def pdf_bytes(tmp_path, pages, name="r.pdf") -> bytes:
    p = tmp_path / name
    digital_pdf(p, [list(pg) + FILL for pg in pages])
    return p.read_bytes()


def png_bytes(lines=("Kassenbon Baumarkt Nord", "Summe 11,48 EUR"), size=(900, 700)) -> bytes:
    buf = io.BytesIO()
    text_image(list(lines), size=size, font_size=36).save(buf, "PNG")
    return buf.getvalue()


def index(tmp_path, root, engine=None, db_name="i.db"):
    db = tmp_path / db_name
    con = dbm.connect(db)
    run_index(con, [str(root)], engine=engine or BytesEngine(), log=quiet)
    return db, con


# --- B1: Absturz eines Arbeitsprozesses, während weitere Dateien eingereiht werden ---------------------------

def _ignore_term_and_sleep():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(120)


def test_pool_stops_workers_that_ignore_sigterm():
    """concurrent.futures beendet nach einem Absturz die übrigen Prozesse mit terminate() und wartet dabei mit
    gesperrtem Pool. Die Arbeitsprozesse ignorieren SIGTERM – terminate() muss deshalb hart beenden."""
    p = indexer._HardStopContext().Process(target=_ignore_term_and_sleep)
    p.start()
    try:
        time.sleep(1.0)                              # Signal-Handler ist gesetzt
        p.terminate()
        p.join(15)
        assert not p.is_alive() and p.exitcode == -signal.SIGKILL
    finally:
        if p.is_alive():
            p.kill()


CRASH_RUN = r"""
import os, sys, time
from pathlib import Path
from belegsuche import db as dbm, indexer
from belegsuche.indexer import run_index
from tests.conftest import make_eml

def main():
    root = Path(sys.argv[1])
    for i in range(10):
        make_eml(root / f"m{i:02d}.eml", subject=f"Mail {i}", text=f"Mailtext {i} Kowalski")
    os.environ["BELEGSUCHE_TEST_CRASH_ON"] = "m03.eml"     # stürzt ab
    os.environ["BELEGSUCHE_TEST_HANG_ON"] = "m02.eml"      # läuft gerade (im C-Code, ignoriert SIGTERM)
    indexer.STALL_TIMEOUT = 4
    prepare = indexer._prepare
    def slow(con, r):                                       # der Hauptprozess reiht gerade die nächste Datei ein
        time.sleep(0.3)
        return prepare(con, r)
    indexer._prepare = slow
    con = dbm.connect(root.parent / "i.db")
    _, ps = run_index(con, [str(root)], log=lambda s: None, workers=2)
    print(dict(con.execute("SELECT relpath, status FROM files").fetchall()), ps.errors)

if __name__ == "__main__":
    main()
"""


@pytest.mark.vision
def test_worker_crash_while_queueing_neither_hangs_nor_aborts(tmp_path):
    script = tmp_path / "lauf.py"
    script.write_text(CRASH_RUN, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(PROJECT)}
    p = subprocess.Popen([sys.executable, str(script), str(tmp_path / "ordner")], cwd=PROJECT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=150)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        pytest.fail("Einlesen hängt nach dem Absturz eines Arbeitsprozesses")
    assert p.returncode == 0, err[-2000:]
    status, errors = out.strip().splitlines()[-1].rsplit(" ", 1)
    status = eval(status)                       # dict-Ausgabe des eigenen Skripts
    assert status.pop("m03.eml") == "error" and errors == "1"
    assert set(status.values()) == {"done"} and len(status) == 9


class _ScriptedPool:
    """Pool-Ersatz: das erste submit läuft, das zweite meldet den Absturz (wie ein kaputter Pool)."""
    _processes = {}

    def __init__(self, log):
        self.log, self.futs = log, []

    def submit(self, fn, path, *_):
        self.log.append(os.path.basename(path))
        if self.futs:
            for f in self.futs:                      # der Pool ist kaputt: laufende Dateien melden das
                f.set_exception(BrokenProcessPool("abgestürzt"))
            raise BrokenProcessPool("A child process terminated abruptly")
        f = Future()
        self.futs.append(f)
        return f

    def shutdown(self, **_):
        pass


class _InlinePool:
    _processes = {}

    def submit(self, fn, path, *_):
        f = Future()
        f.set_result(([PageText(1, Path(path).read_text(), "text", None)], []))
        return f

    def shutdown(self, **_):
        pass


def test_broken_pool_at_submit_is_a_crash_not_an_abort(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    root.mkdir()
    for n in ("a", "b", "c"):
        (root / f"{n}.pdf").write_text(f"Rechnung {n} Dachdecker Kowalski")
    pools, submitted = [], []

    def make(*_):
        pools.append(_ScriptedPool(submitted) if not pools else _InlinePool())
        return pools[-1]
    monkeypatch.setattr(indexer, "_make_pool", make)
    con = dbm.connect(tmp_path / "i.db")
    log = []
    _, ps = run_index(con, [str(root)], workers=2, log=log.append)
    assert not ps.interrupted and ps.errors == 0 and ps.done == 3
    assert dict(con.execute("SELECT relpath, status FROM files").fetchall()) == {
        "a.pdf": "done", "b.pdf": "done", "c.pdf": "done"}
    assert submitted == ["a.pdf", "b.pdf"] and len(pools) == 2
    assert any("abgestürzt" in line for line in log)


def test_broken_pool_without_running_file_counts_an_attempt(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    root.mkdir()
    (root / "a.pdf").write_text("Rechnung Dachdecker Kowalski")

    class Broken(_InlinePool):
        def submit(self, *a):
            raise BrokenProcessPool("Prozess im Leerlauf beendet")
    pools = []

    def make(*_):
        pools.append(Broken() if len(pools) < 5 else _InlinePool())
        return pools[-1]
    monkeypatch.setattr(indexer, "_make_pool", make)
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], workers=2, log=quiet)
    # endet sicher: nach MAX_ATTEMPTS Abbrüchen ist die Datei ein Fehler, keine Endlosschleife
    assert con.execute("SELECT status FROM files").fetchone()[0] == "error" and ps.errors == 1
    assert len(pools) <= indexer.MAX_ATTEMPTS + 1


# --- B3: gleicher Anhang in gleichzeitig laufenden Mails nur einmal erkennen ----------------------------------

class _ThreadPool:
    """Arbeitsprozesse als Threads (gleicher Prozess): die Texterkennung lässt sich zählen."""
    _processes = {}

    def __init__(self, delay=0.3):
        self.ex = ThreadPoolExecutor(2)
        self.delay = delay

    def submit(self, fn, path, marker, timeout, *rest):
        def job():
            time.sleep(self.delay)                   # Texterkennung dauert: andere Mails werden derweil eingereiht
            return fn(path, None, None, *rest)
        return self.ex.submit(job)

    def shutdown(self, wait=True, cancel_futures=False):
        self.ex.shutdown(wait=wait, cancel_futures=cancel_futures)


@pytest.mark.parametrize("standalone", [True, False])
def test_same_attachment_in_parallel_mails_is_recognized_once(tmp_path, monkeypatch, standalone):
    root = tmp_path / "ordner"
    act = root / "090001"
    foto = png_bytes()
    if standalone:
        act.mkdir(parents=True)
        (act / "Foto.png").write_bytes(foto)
    for i in range(3):
        make_eml(act / "Emails" / f"m{i}.eml", subject=f"Foto {i}", text=f"Mail {i}",
                 attachments=[("IMG_0815.png", foto, "application/octet-stream")])
    eng = BytesEngine()
    monkeypatch.setattr(indexer, "_WORKER_ENGINE", eng)
    monkeypatch.setattr(indexer, "_make_pool", lambda *a: _ThreadPool())
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], workers=2, log=quiet)
    assert ps.done == (4 if standalone else 3) and ps.errors == 0
    assert eng.calls == 1                                   # ohne Warten: 3 bzw. 4 Mal dasselbe Foto
    notes = [r[0] for r in con.execute("SELECT note FROM parts WHERE kind = 'image' ORDER BY file_id")]
    assert notes.count(mailmod.REUSED_NOTE) == (3 if standalone else 2)


def test_waiting_mail_is_released_after_the_blocking_one(tmp_path, monkeypatch):
    """Eine Mail wartet nur auf ihren Anhang, nicht bis alle laufenden Dateien fertig sind."""
    root = tmp_path / "ordner"
    foto = png_bytes()
    make_eml(root / "a.eml", text="A", attachments=[("x.png", foto, "image/png")])
    make_eml(root / "b.eml", text="B", attachments=[("x.png", foto, "image/png")])
    eng = BytesEngine()
    monkeypatch.setattr(indexer, "_WORKER_ENGINE", eng)
    monkeypatch.setattr(indexer, "_make_pool", lambda *a: _ThreadPool(delay=0.05))
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], workers=2, log=quiet)
    assert ps.done == 2 and eng.calls == 1
    assert con.execute("SELECT count(*) FROM files WHERE status != 'done'").fetchone()[0] == 0


# --- A7: Temp-Dateien der Arbeitsprozesse, TMPDIR im eingelesenen Ordner ----------------------------------

def test_worker_temp_files_go_to_the_run_folder(tmp_path):
    code = ("import tempfile, os, sys\n"
            "from belegsuche.indexer import _init_worker\n"
            f"_init_worker('apple', {str(tmp_path)!r})\n"
            "print(tempfile.gettempdir(), os.environ['TMPDIR'])\n")
    p = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == [str(tmp_path), str(tmp_path)]


def test_parallel_run_removes_its_temp_folder(tmp_path, monkeypatch):
    base = tmp_path / "systemtmp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    root = tmp_path / "ordner"
    make_eml(root / "a.eml", text="A", attachments=[("x.png", png_bytes(), "image/png")])
    seen = []

    class Pool(_ThreadPool):
        def submit(self, fn, path, marker, timeout, *rest):
            seen.append(os.path.dirname(marker))
            # ein hart beendeter Arbeitsprozess hinterlässt seine HEIC-Kopie im Ordner des Laufs
            d = tempfile.mkdtemp(prefix="belegsuche-anhang-", dir=os.path.dirname(marker))
            Path(d, "anhang.heic").write_bytes(b"x")
            return super().submit(fn, path, marker, timeout, *rest)
    monkeypatch.setattr(indexer, "_WORKER_ENGINE", BytesEngine())
    monkeypatch.setattr(indexer, "_make_pool", lambda *a: Pool(delay=0))
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], workers=2, log=quiet)
    assert seen and Path(seen[0]).parent == base
    assert os.listdir(base) == []


def test_temp_folder_inside_indexed_folder_refuses_to_index(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    make_eml(root / "a.eml", text="A")
    inner = root / "tmp"
    inner.mkdir()
    before = tree_state(root)
    monkeypatch.setattr(tempfile, "tempdir", str(inner))
    con = dbm.connect(tmp_path / "i.db")
    with pytest.raises(SystemExit, match="Temp-Ordner"):
        run_index(con, [str(root)], workers=2, log=quiet)
    assert tree_state(root) == before and os.listdir(inner) == []


# --- B2: Suche nach einem Dateinamen: die Datei vor der Mail ------------------------------------------------

def test_filename_search_prefers_the_file_over_the_mail(tmp_path):
    root = tmp_path / "ordner"
    act = root / "090001"
    act.mkdir(parents=True)
    pdf = pdf_bytes(tmp_path, [["Beleg XY-17 Dachrinne Reparatur"]])
    (act / "Abrechnung Wasserschaden Lindenweg.pdf").write_bytes(pdf)
    make_eml(act / "Emails" / "m.eml", subject="Unterlagen", text="Anbei die Unterlagen",
             attachments=[("Abrechnung Wasserschaden Lindenweg.pdf", pdf, "application/pdf")])
    db, con = index(tmp_path, root)
    hits = Searcher(db).search("Abrechnung Wasserschaden Lindenweg").hits
    assert hits[0].filename == "Abrechnung Wasserschaden Lindenweg.pdf"
    assert any(d.endswith("m.eml (Anhang 1)") for d in hits[0].duplicates)        # die Mail als „auch unter“
    page1 = con.execute("SELECT text FROM pages p JOIN files f ON f.id = p.file_id WHERE f.kind = 'mail'"
                        " AND page_no = 1").fetchone()[0]
    assert "Anhänge: Abrechnung Wasserschaden Lindenweg.pdf" in page1                # angezeigt wird sie weiter


def test_domus_placeholder_name_search_prefers_the_linked_file(tmp_path):
    root = tmp_path / "ordner"
    act = root / "090002"
    act.mkdir(parents=True)
    digital_pdf(act / "Mietvertrag Kowalski.pdf", [["Vertragsurkunde Stellplatz 4", *FILL]])
    for i in range(3):
        make_eml(act / "Emails" / f"m{i}.eml", subject=f"Vorgang {i}", text="siehe DOMUS", attachments=[
            ("Mietvertrag Kowalski.pdf.txt", PLACEHOLDER.format("Mietvertrag Kowalski.pdf").encode(),
             "application/octet-stream")])
    db, _ = index(tmp_path, root)
    hits = Searcher(db).search("Mietvertrag Kowalski").hits
    assert [h.filename for h in hits] == ["Mietvertrag Kowalski.pdf"]
    assert sorted(os.path.basename(d) for d in hits[0].duplicates) == [   # die Mails: „auch unter“
        "m0.eml (Mail mit Verweis)", "m1.eml (Mail mit Verweis)", "m2.eml (Mail mit Verweis)"]
    assert Searcher(db).search("Vorgang 1").hits[0].filename == "m1.eml"      # eigene Suche: die Mail


def test_mail_text_that_matches_better_stays_ahead_of_the_linked_file(tmp_path):
    root = tmp_path / "ordner"
    act = root / "090005"
    act.mkdir(parents=True)
    digital_pdf(act / "Mietvertrag Kowalski.pdf", [["Vertragsurkunde Stellplatz 4", *FILL]])
    make_eml(act / "Emails" / "m.eml", subject="Kündigung Stellplatz", text="Kowalski kündigt den Stellplatz",
             attachments=[("x.pdf.txt", PLACEHOLDER.format("Mietvertrag Kowalski.pdf").encode(),
                           "application/octet-stream")])
    db, _ = index(tmp_path, root)
    hits = Searcher(db).search("Kowalski Kündigung").hits     # nur die Mail enthält beides
    assert hits[0].filename == "m.eml" and not hits[0].duplicates


def test_unevaluated_attachment_name_still_finds_the_mail(tmp_path):
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", subject="Liste", text="anbei",
             attachments=[("Nebenkostenaufstellung Birkenweg.xlsx", b"PK\x03\x04" + bytes(40),
                           "application/octet-stream")])
    db, _ = index(tmp_path, root)
    hits = Searcher(db).search("Nebenkostenaufstellung Birkenweg").hits
    assert [h.filename for h in hits] == ["m.eml"]


# --- A4: Mail ohne Date-Kopf: Dateidatum nicht als Datum durchsuchbar ----------------------------------------

def test_file_time_of_mails_without_date_does_not_flood_date_search(tmp_path):
    root = tmp_path / "ordner"
    t = time.mktime((2023, 2, 3, 10, 0, 0, 0, 0, -1))
    for i in range(5):
        p = make_eml(root / f"m{i}.eml", date=None, subject=f"Vorgang {i}", text=f"Text {i} ohne Datum")
        os.utime(p, (t, t))
    make_eml(root / "rechnung.eml", subject="Rechnung", text="Rechnung vom 03.02.2023 RE-1")
    db, con = index(tmp_path, root)
    assert [h.filename for h in Searcher(db).search("03.02.2023").hits] == ["rechnung.eml"]
    hit = Searcher(db).search("Vorgang 3").hits[0]
    assert hit.mail_date == "03.02.2023 10:00 (Dateidatum)"
    assert con.execute("SELECT count(*) FROM mails WHERE sent_at IS NULL").fetchone()[0] == 5
    text = con.execute("SELECT text FROM pages p JOIN files f ON f.id = p.file_id WHERE f.relpath = 'm3.eml'"
                       ).fetchone()[0]
    assert f"Datum: 03.02.2023 10:00 {mailmod.NO_DATE_SUFFIX}" in text               # angezeigt wird es


# --- A5: rohe 8-Bit-Kopfzeilen in Latin-1 ---------------------------------------------------------------------

def _raw_8bit_mail(enc: str) -> bytes:
    s = "Rechnung für Wohnung Müllerstraße".encode(enc)
    f = "Jürgen Weiß <j@firma.example>".encode(enc)
    n = "Übergabe-Protokoll.pdf".encode(enc)
    return (b"From: " + f + b"\r\nTo: x@kunde.example\r\nSubject: " + s + b" =?utf-8?q?gr=C3=BC=C3=9Fe?=\r\n"
            b"Date: Mon, 3 Feb 2025 10:00:00 +0100\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="X"\r\n\r\n--X\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
            + "Grüße aus der Müllerstraße".encode(enc) + b"\r\n"
            b'--X\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="' + n + b'"\r\n'
            b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQKJSVFT0YK\r\n--X--\r\n")


@pytest.mark.parametrize("enc", ["utf-8", "cp1252"])
def test_raw_8bit_headers_latin1_and_utf8(tmp_path, enc):
    p = tmp_path / "m.eml"
    p.write_bytes(_raw_8bit_mail(enc))
    r = mailmod.extract_mail(p, None)
    assert r.mail.subject == "Rechnung für Wohnung Müllerstraße grüße"
    assert r.mail.sender == "Jürgen Weiß <j@firma.example>"
    assert r.parts[1].name == "Übergabe-Protokoll.pdf"
    assert "Grüße aus der Müllerstraße" in r.pages[0].text and "�" not in r.pages[0].text


def test_declared_utf8_body_that_is_cp1252():
    assert mailmod._decode_bytes("Müller zahlt 45,00 €".encode("cp1252"), "utf-8") == "Müller zahlt 45,00 €"
    good = "Müller".encode("utf-8") + b"\xff" + " Straße".encode("utf-8")
    assert mailmod._decode_bytes(good, "utf-8") == "Müller� Straße"          # einzelnes kaputtes Byte


# --- A2: Typerkennung an den ersten Bytes nicht zu großzügig ------------------------------------------------

def test_sniff_bmp_and_pdf_strict():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), "white").save(buf, "BMP")
    assert sniff(buf.getvalue()) == "bmp"
    assert sniff(b"BMW Niederlassung Musterstadt, Rechnung 4711 fuer Inspektion " * 3) is None
    assert sniff(b"Hinweis: die Datei ist ein %PDF-1.4 Dokument, bitte oeffnen\n" * 3) is None
    assert sniff(bytes(128) + b"%PDF-1.4\n%%EOF") == "pdf"                       # binärer Vorspann (MacBinary)
    assert sniff(b"\xef\xbb\xbf%PDF-1.7\n") == "pdf"
    assert sniff(b"\r\n %PDF-1.3\n") == "pdf"


def test_text_attachment_starting_with_bm_is_text(tmp_path):
    p = make_eml(tmp_path / "m.eml", text="anbei", attachments=[
        ("notiz.txt", b"BMW Niederlassung Nord, Auftrag 4711, Inspektion " * 4, "text/plain")])
    r = extract_full(p, BytesEngine())
    assert r.parts[1].kind == "text" and "Auftrag 4711" in r.pages[1].text


# --- A3/B7: Vorabblick im Hauptprozess schnell und ohne quadratischen Aufwand ---------------------------------

def test_attachment_shas_fast_on_hostile_headers():
    head = (b"From: a@firma.example\r\nTo: b@kunde.example\r\nSubject: x\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="X"\r\n\r\n--X\r\nContent-Type: text/plain\r\n\r\nt\r\n--X\r\n'
            b"Content-Type: application/pdf\r\nContent-Disposition: attachment")
    data = (head + b"".join(b"; filename*%d*=utf-8''a%%C3%%BC" % i for i in range(20000))
            + b"\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--X--\r\n")
    t0 = time.time()
    shas = mailmod.attachment_shas(data)
    assert time.time() - t0 < 2.0                              # vorher 5 s (policy.default, quadratisch)
    assert len(shas) == 1


def test_attachment_shas_match_what_the_worker_stores(tmp_path):
    pdf = pdf_bytes(tmp_path, [["Rechnung 4711"]])
    inner = EmailMessage(policy=policy.SMTP)
    inner["Subject"] = "Innen"
    inner.set_content("innen")
    inner.add_attachment(png_bytes(), maintype="image", subtype="png", filename="Foto.png")
    p = make_eml(tmp_path / "m.eml", subject="Fünf Anhänge – Größe", text="anbei", attachments=[
        ("Rechnung.pdf", pdf, "application/octet-stream"), ("Brief.docx", docx_bytes(["Kündigung"]),
                                                            "application/octet-stream"), inner])
    r = extract_full(p, BytesEngine())
    stored = {t.sha256 for t in r.parts if t.kind in mailmod.REUSABLE_KINDS}
    assert set(mailmod.attachment_shas(p.read_bytes())) == stored and len(stored) == 3
    assert mailmod.attachment_shas(make_eml(tmp_path / "t.eml", text="nur Text").read_bytes()) == []


# --- A6: übernommene Seiten am Seitenlimit gekürzt -------------------------------------------------------------

def test_truncated_reused_pages_are_marked_and_not_reused_again(tmp_path, monkeypatch):
    monkeypatch.setattr(mailmod, "MAX_MAIL_PAGES", 3)
    root = tmp_path / "ordner"
    (root / "V1" / "Emails").mkdir(parents=True)
    (root / "V2").mkdir()
    five = pdf_bytes(tmp_path, [[f"Seite {i} Rechnung RE-55{i} Dachdecker"] for i in range(1, 6)], "f.pdf")
    one = pdf_bytes(tmp_path, [["Erste Rechnung Glaser Sommer GL-1"]], "g.pdf")
    (root / "V1" / "a_first.pdf").write_bytes(five)
    make_eml(root / "V1" / "Emails" / "m.eml", attachments=[("g.pdf", one, "application/pdf"),
                                                            ("f.pdf", five, "application/pdf")])
    db, con = index(tmp_path, root)
    note = con.execute("SELECT note FROM parts WHERE name = 'f.pdf'").fetchone()[0]
    assert note == f"{mailmod.REUSED_NOTE}; nur die ersten 1 von 5 Seiten eingelesen"
    assert "Seitenlimit der Mail" in con.execute("SELECT error FROM files WHERE kind = 'mail'").fetchone()[0]
    (root / "V1" / "a_first.pdf").unlink()
    con.execute("DELETE FROM files WHERE relpath = 'V1/a_first.pdf'")
    con.commit()
    (root / "V2" / "kopie.pdf").write_bytes(five)
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    assert con.execute("SELECT pages FROM files WHERE relpath = 'V2/kopie.pdf'").fetchone()[0] == 5


def test_word_attachment_cut_at_page_limit_gets_a_note(tmp_path, monkeypatch):
    monkeypatch.setattr(mailmod, "MAX_MAIL_PAGES", 2)
    docx = docx_bytes(["Seite eins", "Seite zwei", "Seite drei"], page_break_before=1)
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx)) as z, zipfile.ZipFile(buf, "w") as out:
        for n in z.namelist():
            data = z.read(n)
            if n == "word/document.xml":
                data = data.replace(b"<w:t xml:space=\"preserve\">Seite drei",
                                    b"<w:br w:type=\"page\"/><w:t xml:space=\"preserve\">Seite drei")
            out.writestr(n, data)
    p = make_eml(tmp_path / "m.eml", text="anbei", attachments=[("Brief.docx", buf.getvalue(),
                                                                  "application/octet-stream")])
    r = extract_full(p, BytesEngine())
    assert r.parts[1].n_pages == 1 and "nur die ersten 1 von 3 Seiten" in r.parts[1].note
    assert any("Seitenlimit" in n for n in r.notes)


# --- A8: kaputte MIME-Struktur ---------------------------------------------------------------------------------

def test_wrong_boundary_keeps_text_but_drops_base64(tmp_path):
    pdf = b"%PDF-1.4\n" + os.urandom(200_000)
    data = (b"From: a@firma.example\r\nTo: b@kunde.example\r\nSubject: Rechnung\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="RICHTIG"\r\n\r\n--FALSCH\r\nContent-Type: text/plain\r\n\r\n'
            b"Anbei RE-4711\r\n--FALSCH\r\nContent-Type: application/pdf;\r\n name=\"r.pdf\"\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            + base64.encodebytes(pdf).replace(b"\n", b"\r\n") + b"--FALSCH--\r\n")
    p = tmp_path / "m.eml"
    p.write_bytes(data)
    r = extract_full(p, None)
    t = r.pages[0].text
    assert "Anbei RE-4711" in t and len(t) < 1000
    assert "JVBERi0" not in t and "Content-Type" not in t and 'name="r.pdf"' not in t


def test_deeply_nested_mail_keeps_its_head(tmp_path):
    head = (b"From: Erika Beispiel <erika@firma.example>\r\nTo: max@kunde.example\r\nSubject: Tief\r\n"
            b"Date: Mon, 3 Feb 2025 10:00:00 +0100\r\nMIME-Version: 1.0\r\n")
    data = head + b"".join(b'Content-Type: multipart/mixed; boundary="b%d"\r\n\r\n--b%d\r\n' % (i, i)
                           for i in range(3000)) + b"Content-Type: text/plain\r\n\r\nTief 4711\r\n"
    p = tmp_path / "m.eml"
    p.write_bytes(data)
    r = extract_full(p, None)
    assert r.mail.subject == "Tief" and "Betreff: Tief" in r.pages[0].text
    assert any("zu tief verschachtelt" in n for n in r.notes)


def test_forwarded_mail_with_base64_transfer_encoding(tmp_path):
    inner = EmailMessage(policy=policy.SMTP)
    inner["From"], inner["Subject"] = "a@b.example", "Innen"
    inner.set_content("Innere Rechnung 9876")
    pdf = pdf_bytes(tmp_path, [["Gutschrift GS-7731 Dachrinne"]])
    inner.add_attachment(pdf, maintype="application", subtype="pdf", filename="Gutschrift.pdf")
    raw = (b"From: Erika <erika@firma.example>\r\nTo: x@kunde.example\r\nSubject: Weiterleitung\r\n"
           b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="X"\r\n\r\n--X\r\n'
           b"Content-Type: text/plain\r\n\r\nsiehe weitergeleitet\r\n--X\r\nContent-Type: message/rfc822\r\n"
           b"Content-Transfer-Encoding: base64\r\n\r\n" + base64.encodebytes(inner.as_bytes()) + b"--X--\r\n")
    p = tmp_path / "m.eml"
    p.write_bytes(raw)
    r = extract_full(p, BytesEngine())
    kinds = [(t.kind, t.name, t.mime_path) for t in r.parts]
    assert kinds[1][0] == "mail" and ("pdf", "Gutschrift.pdf", "2/2") in kinds
    assert any("Innere Rechnung 9876" in pg.text for pg in r.pages)
    assert any("GS-7731" in pg.text for pg in r.pages)
    gut = next(t for t in r.parts if t.name == "Gutschrift.pdf")
    assert mailmod.find_part(raw, gut.mime_path) == pdf                     # „Anhang öffnen“ findet ihn
    assert gut.sha256 in mailmod.attachment_shas(raw)


# --- A9/B5: Word-Sonderformate und Feldbefehle -----------------------------------------------------------------

def _strict(docx: bytes) -> bytes:
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx)) as z, zipfile.ZipFile(buf, "w") as out:
        for n in z.namelist():
            out.writestr(n, z.read(n).replace(b"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                                              b"http://purl.oclc.org/ooxml/wordprocessingml/main"))
    return buf.getvalue()


def test_strict_ooxml_docx_is_read():
    pages = wordmod.word_pages(_strict(docx_bytes(["Kündigung Stellplatz Garage 4"], header="Hausverwaltung")), [])
    assert "Kündigung Stellplatz Garage 4" in pages[0].text and "Hausverwaltung" in pages[0].text


@pytest.mark.parametrize("data,expected", [
    (b"<html><head><title>x</title><script>alert(1)</script></head><body><p>Mahnung Hausgeld MH-5510</p>"
     b"<img src='http://tracker.example/p.gif'></body></html>", "Mahnung Hausgeld MH-5510"),
    ('<?xml version="1.0" encoding="UTF-8"?><?mso-application progid="Word.Document"?>'
     '<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml"><w:body><w:p><w:r>'
     '<w:t>Übergabeprotokoll Wohnung 3</w:t></w:r></w:p></w:body></w:wordDocument>'.encode(),
     "Übergabeprotokoll Wohnung 3"),
    ("Mietbescheinigung für Frau Beispiel\r\nKaltmiete 512,00 €\r\n".encode("cp1252"), "Kaltmiete 512,00 €"),
])
def test_doc_that_is_html_wordml_or_text(tmp_path, monkeypatch, data, expected):
    def no_textutil(*a, **k):
        raise AssertionError("HTML/Text nicht über textutil (lädt nichts aus dem Netz)")
    monkeypatch.setattr(wordmod.subprocess, "run", no_textutil)
    p = tmp_path / "brief.doc"
    p.write_bytes(data)
    pages = extract_full(p, None).pages
    assert expected in pages[0].text and "alert" not in pages[0].text and "tracker" not in pages[0].text


def test_unknown_word_format_is_an_error_but_not_retryable(tmp_path, monkeypatch, capsys):
    root = tmp_path / "ordner"
    root.mkdir()
    (root / "kaputt.doc").write_bytes(bytes(range(256)) * 4)
    db = tmp_path / "i.db"
    monkeypatch.setattr(sys, "argv", ["belegsuche", "--db", str(db), "index", str(root), "--parallel", "1"])
    cli.main()
    out = capsys.readouterr().out
    con = dbm.connect(db, readonly=True)
    assert con.execute("SELECT error FROM files").fetchone()[0] == "Word-Datei nicht lesbar (unbekanntes Format)"
    assert "--fehler-wiederholen" not in out


def test_field_codes_from_textutil_are_removed():
    raw = ('Datum: TIME \\@ "dd. MMMM yyyy" 03. Februar 2009\n'
           'Tel. SYMBOL 40 \\f "Wingdings" \\s 10 (0711) 1234567\n'
           'Sehr geehrte MERGEFIELD Anrede Frau MERGEFIELD Name \\* MERGEFORMAT Beispiel,\n'
           'Konto SYMBOL 215 \\f "Symbol" Sparkasse (BLZ 600 501 01)\n'
           'Bild INCLUDEPICTURE "http://bilder.example/logo.png" \\* MERGEFORMATINET Ende\n'
           'Symbol und Datum bleiben als Wörter stehen, ebenso TIME und SYMBOL ohne Argumente.')
    t = wordmod.strip_field_codes(raw)
    assert t.splitlines() == ["Datum: 03. Februar 2009", "Tel. (0711) 1234567", "Sehr geehrte Frau Beispiel,",
                              "Konto Sparkasse (BLZ 600 501 01)", "Bild Ende",
                              "Symbol und Datum bleiben als Wörter stehen, ebenso TIME und SYMBOL ohne Argumente."]


def test_doc_text_from_textutil_without_field_codes(tmp_path, monkeypatch):
    calls = []

    def fake_textutil(cmd, **kw):
        calls.append(cmd)
        out = 'Kündigung SYMBOL 215 \\f "Symbol" Stellplatz\nDatum TIME \\@ "dd.MM.yyyy" 03.02.2009'
        return subprocess.CompletedProcess(cmd, 0, out.encode(), b"")
    monkeypatch.setattr(wordmod.subprocess, "run", fake_textutil)
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100) + "WordDocument".encode("utf-16-le")
    pages = wordmod.word_pages(ole, [])
    assert pages[0].text == "Kündigung Stellplatz\nDatum 03.02.2009"
    assert "-stdin" in calls[0] and "-stdout" in calls[0]


# --- A10: Chip „Scan“ nur, wenn die Fundseite erkannt wurde ---------------------------------------------------

def test_scan_flag_of_mail_hit_is_the_page_not_the_mail(tmp_path):
    import random

    from PIL import Image
    rnd = random.Random(1)
    img = Image.new("RGB", (600, 200))
    img.putdata([tuple(rnd.randrange(256) for _ in range(3)) for _ in range(600 * 200)])
    b = io.BytesIO()
    img.save(b, "PNG")
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", text="anbei", inline=[("image001.png", b.getvalue(), "image/png")],
             attachments=[("r.pdf", pdf_bytes(tmp_path, [["Digitale Rechnung RE-31337 Glaserei Sommer"]]),
                           "application/pdf")])
    db, con = index(tmp_path, root)
    assert con.execute("SELECT ocr_pages FROM files").fetchone()[0] == 1          # das Bild
    pdf_hit = Searcher(db).search("RE-31337").hits[0]
    assert pdf_hit.part_kind == "pdf" and pdf_hit.ocr is False
    img_hit = Searcher(db).search("Belegfoto Baumarkt").hits[0]
    assert img_hit.part_kind == "image" and img_hit.ocr is True


# --- A11: Kleinigkeiten ------------------------------------------------------------------------------------------

def test_identical_mail_in_other_activity_links_only_its_own_files(tmp_path):
    root = tmp_path / "ordner"
    (root / "V1").mkdir(parents=True)
    digital_pdf(root / "V1" / "Rechnung.pdf", [["Rechnung 4711", *FILL]])
    p = make_eml(root / "V1" / "Emails" / "m.eml", text="siehe DOMUS", attachments=[
        ("Rechnung.pdf.txt", PLACEHOLDER.format("Rechnung.pdf").encode(), "application/octet-stream")])
    (root / "V2" / "Emails").mkdir(parents=True)
    (root / "V2" / "Emails" / "m.eml").write_bytes(p.read_bytes())
    db, con = index(tmp_path, root)
    notes = dict(con.execute("SELECT f.relpath, t.note FROM parts t JOIN files f ON f.id = t.file_id"
                             " WHERE t.kind = 'link'").fetchall())
    assert notes == {"V1/Emails/m.eml": "V1/Rechnung.pdf", "V2/Emails/m.eml": None}


def test_encrypted_forwarded_mail_note_is_on_the_part(tmp_path):
    inner = EmailMessage(policy=policy.SMTP)
    inner["Subject"] = "Geheim"
    inner.set_content(b"0\x82\x05" + bytes(200), maintype="application", subtype="pkcs7-mime")
    p = make_eml(tmp_path / "m.eml", text="weitergeleitet", attachments=[inner])
    r = extract_full(p, BytesEngine())
    assert r.notes == []
    assert r.parts[1].kind == "mail" and r.parts[1].note == mailmod.ENCRYPTED_NOTE
    assert "Betreff: Geheim" in r.pages[1].text


def test_log_line_counts_only_fresh_ocr(tmp_path):
    root = tmp_path / "ordner"
    act = root / "V"
    act.mkdir(parents=True)
    foto = png_bytes()
    (act / "Foto.png").write_bytes(foto)
    make_eml(act / "Emails" / "m.eml", text="anbei", attachments=[("Foto.png", foto, "image/png")])
    con = dbm.connect(tmp_path / "i.db")
    log = []
    _, ps = run_index(con, [str(root)], engine=BytesEngine(), log=log.append)
    line = next(x for x in log if "m.eml" in x)
    assert "2 S., 0 per OCR, 1 OCR-Seiten übernommen" in line and ps.ocr_pages == 1


# --- B6: DOMUS-Platzhalter anzeigen und öffnen; NFC/NFD ---------------------------------------------------------

def _link_env(tmp_path):
    root = tmp_path / "ordner"
    act = root / "090003"
    act.mkdir(parents=True)
    on_disk = unicodedata.normalize("NFD", "Übergabe Müller.pdf")                  # vom Mac: zerlegte Umlaute
    digital_pdf(act / on_disk, [["Übergabeprotokoll Wohnung 3", *FILL]])
    make_eml(act / "Emails" / "m.eml", subject="Übergabe", text="siehe DOMUS Kowalski", attachments=[
        ("a.pdf.txt", PLACEHOLDER.format("Übergabe Müller.pdf").encode(), "application/octet-stream"),
        ("b.pdf.txt", PLACEHOLDER.format("Fehlt.pdf").encode(), "application/octet-stream")])
    db, con = index(tmp_path, root)
    ids = {unicodedata.normalize("NFC", r[0]): r[1] for r in con.execute("SELECT relpath, id FROM files")}
    return root, db, con, ids, on_disk


def test_link_keeps_on_disk_spelling_and_hit_lists_it(tmp_path):
    root, db, con, ids, on_disk = _link_env(tmp_path)
    note = con.execute("SELECT note FROM parts WHERE kind = 'link' AND name = 'Übergabe Müller.pdf'").fetchone()[0]
    assert note == f"090003/{on_disk}"                         # wie files.relpath, nicht umgeformt
    hit = Searcher(db).search("Kowalski").hits[0]
    assert hit.links == [
        {"part_no": 1, "name": "Übergabe Müller.pdf", "path": f"090003/{on_disk}",
         "file_id": ids["090003/Übergabe Müller.pdf"]},
        {"part_no": 2, "name": "Fehlt.pdf", "path": None, "file_id": None}]
    con.execute("UPDATE parts SET note = ? WHERE kind = 'link' AND part_no = 1",
                (unicodedata.normalize("NFC", note),))                              # ältere Fassung: NFC gespeichert
    con.commit()
    assert Searcher(db).search("Kowalski").hits[0].links[0]["file_id"] == ids["090003/Übergabe Müller.pdf"]


def test_cli_shows_linked_files(tmp_path, monkeypatch, capsys):
    root, db, con, ids, on_disk = _link_env(tmp_path)
    monkeypatch.setattr(sys, "argv", ["belegsuche", "--db", str(db), "suche", "Kowalski"])
    cli.main()
    out = unicodedata.normalize("NFC", capsys.readouterr().out)
    assert "In DOMUS abgelegt: Übergabe Müller.pdf → " in out and "090003/Übergabe Müller.pdf" in out
    assert "In DOMUS abgelegt: Fehlt.pdf → (nicht im Vorgangsordner)" in out


# --- Server: Quarantäne, Word als Kopie, Platzhalter öffnen, Signale -----------------------------------------

@pytest.fixture()
def server_env(tmp_path, monkeypatch):
    import http.client
    from http.server import ThreadingHTTPServer
    root, db, con, ids, on_disk = _link_env(tmp_path)
    (root / "brief.docx").write_bytes(docx_bytes(["Kündigung Stellplatz Garage 4"]))
    make_eml(root / "anhang.eml", text="Unterlagen Sommer", attachments=[
        ("Bericht.doc", docx_bytes(["Bericht Wasserschaden"]), "application/octet-stream")])
    run_index(con, [str(root)], engine=BytesEngine(), log=quiet)
    ids = {unicodedata.normalize("NFC", r[0]): r[1] for r in con.execute("SELECT relpath, id FROM files")}
    systmp = tmp_path / "systemtmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    calls = []
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(Searcher(db), port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def req(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Host": f"127.0.0.1:{port}", "X-Belegsuche": "1"}
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        return r.status, r.read(), r.headers
    before = tree_state(root)
    yield {"root": root, "ids": ids, "calls": calls, "req": req, "systmp": systmp, "before": before,
           "on_disk": on_disk}
    httpd.shutdown()
    httpd.server_close()
    httpd.RequestHandlerClass.temp.cleanup()


def _xattr(path: str, name: bytes) -> bytes | None:
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.getxattr
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                   ctypes.c_int]
    fn.restype = ctypes.c_ssize_t
    buf = ctypes.create_string_buffer(256)
    n = fn(os.fsencode(path), name, buf, 256, 0, 0)
    return buf.raw[:n] if n >= 0 else None


def test_opened_attachment_copy_is_quarantined(server_env):
    e = server_env
    mail_id = e["ids"]["anhang.eml"]
    st, body, _ = e["req"]("POST", "/api/open", {"id": mail_id, "part": 1})
    assert st == 200, body
    path = e["calls"][-1][1]
    assert path.endswith("Bericht.docx")
    q = _xattr(path, b"com.apple.quarantine")
    assert q is not None and q.startswith(b"0081;") and b";Belegsuche;" in q
    assert tree_state(e["root"]) == e["before"]


def test_word_original_opens_as_readonly_copy(server_env):
    e = server_env
    st, body, _ = e["req"]("POST", "/api/open", {"id": e["ids"]["brief.docx"]})
    assert st == 200, body
    cmd = e["calls"][-1]
    assert cmd[0] == "open" and len(cmd) == 2
    copy = cmd[1]
    assert os.path.basename(copy) == "brief.docx" and Path(copy).parent.parent == e["systmp"]
    assert Path(copy).read_bytes() == (e["root"] / "brief.docx").read_bytes()
    assert oct(os.stat(copy).st_mode & 0o777) == oct(0o400)
    assert _xattr(copy, b"com.apple.quarantine") is None                    # eigene Datei: keine Quarantäne
    st, _, _ = e["req"]("POST", "/api/open", {"id": e["ids"]["brief.docx"], "reveal": True})
    assert e["calls"][-1] == ["open", "-R", os.path.realpath(e["root"] / "brief.docx")]   # Finder: das Original
    assert tree_state(e["root"]) == e["before"]


def test_placeholder_opens_the_linked_file(server_env):
    e = server_env
    mail_id = e["ids"]["090003/Emails/m.eml"]
    target = e["ids"]["090003/Übergabe Müller.pdf"]
    st, body, _ = e["req"]("POST", "/api/open", {"id": mail_id, "part": 1})
    assert st == 200, body
    assert e["calls"][-1] == ["open", os.path.realpath(e["root"] / "090003" / e["on_disk"])]
    st, body, _ = e["req"]("POST", "/api/open", {"id": mail_id, "part": 1, "reveal": True})
    assert e["calls"][-1] == ["open", "-R", os.path.realpath(e["root"] / "090003" / e["on_disk"])]
    st, body, _ = e["req"]("POST", "/api/open", {"id": mail_id, "part": 2})
    assert st == 404 and "nicht im Vorgangsordner" in json.loads(body)["error"]
    st, _, headers = e["req"]("GET", f"/datei/{mail_id}/teil/1")
    assert st == 303 and headers["Location"] == f"/datei/{target}"


SERVE_RUN = r"""
import sys
from pathlib import Path
from belegsuche.server import serve
serve(Path(sys.argv[1]), port=0, open_browser=False)
"""


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM])
def test_serve_removes_copies_when_terminal_closes(tmp_path, sig):
    import http.client
    root = tmp_path / "ordner"
    make_eml(root / "m.eml", text="anbei", attachments=[("r.pdf", pdf_bytes(tmp_path, [["Rechnung 4711"]]),
                                                         "application/pdf")])
    db, con = index(tmp_path, root)
    fid = con.execute("SELECT id FROM files").fetchone()[0]
    con.close()
    systmp = tmp_path / "systemtmp"
    systmp.mkdir()
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "open").write_text("#!/bin/sh\nexit 0\n")
    (fakebin / "open").chmod(0o755)
    env = {**os.environ, "TMPDIR": str(systmp), "PATH": f"{fakebin}:{os.environ['PATH']}",
           "PYTHONPATH": str(PROJECT)}
    p = subprocess.Popen([sys.executable, "-u", "-c", SERVE_RUN, str(db)], cwd=PROJECT, env=env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        import select
        assert select.select([p.stdout], [], [], 30)[0], "Server meldet sich nicht"
        line = p.stdout.readline()
        port = int(line.split("127.0.0.1:")[1].split("/")[0])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/api/open", body=json.dumps({"id": fid, "part": 1}),
                  headers={"Host": f"127.0.0.1:{port}", "X-Belegsuche": "1"})
        assert c.getresponse().status == 200
        assert [d for d in os.listdir(systmp) if d.startswith("belegsuche-anhang-")]
        p.send_signal(sig)
        assert p.wait(20) == 0
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
    assert [d for d in os.listdir(systmp) if d.startswith("belegsuche-anhang-")] == []


def test_ui_lists_linked_files_with_open_button():
    from .test_mail_open import run_ui
    hit = {"id": 5, "filename": "m.eml", "folder": "Emails", "root": "/r", "page": 1, "pages": 1,
           "snippet": "Text", "matched": [], "missing": [], "ocr": False, "available": True, "duplicates": [],
           "pdf": False, "kind": "mail", "part_no": 0, "part_name": None, "part_kind": "body", "part_page": 1,
           "part_pages": 1, "mail_subject": "S", "mail_from": None, "mail_date": "03.02.2023 10:00 (Dateidatum)",
           "links": [{"part_no": 1, "name": "<b>Vertrag</b>.pdf", "path": "V/x.pdf", "file_id": 9},
                     {"part_no": 2, "name": "Fehlt.pdf", "path": None, "file_id": None}]}
    row = run_ui(json.dumps([hit]), "console.log(JSON.stringify(HITS.map(hitRow)));")[0]
    assert "In DOMUS abgelegt: &lt;b&gt;Vertrag&lt;/b&gt;.pdf" in row and "<b>Vertrag" not in row
    assert "openFile(5, false, this, 1)" in row and "openFile(5, false, this, 2)" not in row
    assert "Fehlt.pdf (nicht im Vorgangsordner)" in row
    assert "03.02.2023 10:00 (Dateidatum)" in row


# --- B10: doppelter Anhang in derselben Mail im Bericht ---------------------------------------------------------

def test_report_counts_duplicate_attachments_separately(tmp_path):
    root = tmp_path / "ordner"
    pdf = pdf_bytes(tmp_path, [["Rechnung 4711 Dachdecker"]])
    make_eml(root / "m.eml", text="anbei", attachments=[("a.pdf", pdf, "application/pdf"),
                                                        ("Kopie von a.pdf", pdf, "application/pdf"),
                                                        ("Liste.xlsx", b"PK\x03\x04" + bytes(40),
                                                         "application/octet-stream")])
    db, con = index(tmp_path, root)
    r = build_report(con)
    assert r["attachments"] == {"n": 3, "evaluated": 1, "reused": 0, "links": 0, "duplicates": 1, "skipped": 1}
    assert "1 nicht ausgewertet, 1 doppelt in derselben Mail" in format_report(r)


# --- Originalschutz über alles: keine Datei im eingelesenen Ordner verändert ----------------------------------

def test_review_fixes_leave_originals_untouched(tmp_path, monkeypatch):
    root = tmp_path / "ordner"
    act = root / "090004"
    act.mkdir(parents=True)
    pdf = pdf_bytes(tmp_path, [["Rechnung 4711"]])
    (act / "Rechnung.pdf").write_bytes(pdf)
    p = tmp_path / "raw.eml"
    p.write_bytes(_raw_8bit_mail("cp1252"))
    (act / "Emails").mkdir()
    (act / "Emails" / "latin.eml").write_bytes(p.read_bytes())
    make_eml(act / "Emails" / "m.eml", text="anbei", attachments=[("Rechnung.pdf", pdf, "application/pdf"),
                                                                  ("x.pdf.txt", PLACEHOLDER.format("Rechnung.pdf")
                                                                   .encode(), "application/octet-stream")])
    (act / "brief.doc").write_bytes(b"<html><body><p>Mahnung</p></body></html>")
    before = tree_state(root)
    monkeypatch.setattr(indexer, "_WORKER_ENGINE", BytesEngine())
    monkeypatch.setattr(indexer, "_make_pool", lambda *a: _ThreadPool(delay=0))
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], workers=2, log=quiet)
    assert ps.errors == 0 and tree_state(root) == before
    assert sorted(os.listdir(act)) == ["Emails", "Rechnung.pdf", "brief.doc"]
