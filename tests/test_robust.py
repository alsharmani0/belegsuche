"""Robustheit beim Einlesen: vorübergehende Fehler, abgezogene SSD, Signale, Speicher,
gemischte PDF-Seiten, lange TIFFs, Ordner in anderer Schreibweise oder an neuem Ort, Sperre."""
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import pytest
from PIL import Image

from belegsuche import db as dbm
from belegsuche import extract as extract_mod
from belegsuche import indexer
from belegsuche.extract import extract
from belegsuche.indexer import (ROOT_LOST_MSG, acquire_lock, add_root, find_root_conflict, move_root,
                                normalize_root, retry_errors, run_index)
from belegsuche.ocr import OcrResult
from belegsuche.report import build_report, format_report

from .conftest import FONT, CountingEngine, digital_pdf, text_image

PROJECT = Path(__file__).resolve().parent.parent
LONG_TEXT = "Quittung Baumarkt Nord Musterstadt Schrauben Silikon Summe 11,48 EUR"


def quiet(_):
    pass


class FakeEngine:
    """Schnelle Ersatz-OCR ohne Vision: fester Text, auf Wunsch Fehler oder Aktion beim Aufruf."""

    def __init__(self, text: str = LONG_TEXT, fail: bool = False, on_call=None):
        self.text, self.fail, self.on_call = text, fail, on_call
        self.calls = 0

    def _run(self):
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        if self.fail:
            raise RuntimeError("Vision-Fehler: vorübergehend")
        return OcrResult(self.text, 0.9)

    def recognize_image(self, img):
        return self._run()

    def recognize_file(self, path):
        return self._run()


def rows(con):
    return {r["relpath"]: (r["status"], r["attempts"], r["error"])
            for r in con.execute("SELECT relpath, status, attempts, error FROM files")}


def photos(root: Path, n: int = 3) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        text_image([f"Bon {i}", "Baumarkt Nord"], size=(300, 200), font_size=20).save(root / f"bon{i}.png")


def pdfs(root: Path, n: int = 3, prefix: str = "d") -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        digital_pdf(root / f"{prefix}{i}.pdf", [[f"Dokument Nummer {i} {prefix}", "Inhalt Testbeleg Musterstadt Lindenweg"]])


def generation(db_path) -> int:
    con = dbm.connect(db_path, readonly=True)
    try:
        return int(dbm.get_meta(con, "generation"))
    finally:
        con.close()


# --- Vorübergehende Fehler wiederholen, SSD mitten im Lauf abgezogen -------------------------

def test_retry_errors_after_transient_failure(tmp_path):
    root = tmp_path / "belege"
    photos(root, 1)
    digital_pdf(root / "geheim.pdf", [["Vertraulich"]], password="1234")
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=FakeEngine(fail=True), log=quiet)
    assert rows(con)["bon0.png"][0] == "error"
    _, ps = run_index(con, [], engine=FakeEngine(), log=quiet)          # ohne Wiederholen: bleibt Fehler
    assert ps.done == 0 and rows(con)["bon0.png"][0] == "error"
    _, ps = run_index(con, [], engine=FakeEngine(), log=quiet, retry=True)
    assert ps.retried == 1 and ps.done == 1
    st = rows(con)
    assert st["bon0.png"] == ("done", 0, None)
    assert st["geheim.pdf"][:1] == ("error",)                            # passwortgeschützt: bleibt
    assert retry_errors(con, include_permanent=True) == 1
    assert rows(con)["geheim.pdf"] == ("pending", 0, None)


@pytest.mark.parametrize("how", ["umbenannt", "leerer Einhängepunkt"])
def test_root_lost_mid_run_keeps_files_pending(tmp_path, how):
    root, offline = tmp_path / "ssd", tmp_path / "weg"
    photos(root, 3)
    con = dbm.connect(tmp_path / "i.db")
    add_root(con, root)

    def pull(n):
        if n == 1:                         # SSD wird während der ersten Texterkennung abgezogen
            if how == "umbenannt":
                root.rename(offline)
            else:
                offline.mkdir()
                for p in root.iterdir():
                    shutil.move(str(p), offline / p.name)
            raise RuntimeError("Vision-Fehler: zero-dimensioned image")

    log: list[str] = []
    _, ps = run_index(con, [], engine=FakeEngine(on_call=pull), log=log.append)
    assert set(rows(con).values()) == {("pending", 0, None)}             # nichts 'error', nichts 'missing'
    assert ps.root_lost and ps.interrupted and ps.errors == 0
    assert ROOT_LOST_MSG in log
    if how == "umbenannt":
        offline.rename(root)
    else:
        for p in offline.iterdir():
            shutil.move(str(p), root / p.name)
    _, ps = run_index(con, [], engine=FakeEngine(), log=quiet)
    assert ps.done == 3 and {v[0] for v in rows(con).values()} == {"done"}


def test_root_lost_in_parallel_mode(tmp_path, monkeypatch):
    root, offline = tmp_path / "ssd", tmp_path / "weg"
    pdfs(root, 3)
    con = dbm.connect(tmp_path / "i.db")
    real, calls = indexer.sha256_file, []

    def sha_and_pull(path):
        calls.append(path)
        if len(calls) == 2:
            root.rename(offline)
        return real(path)

    monkeypatch.setattr(indexer, "sha256_file", sha_and_pull)
    log: list[str] = []
    _, ps = run_index(con, [str(root)], log=log.append, workers=2)
    assert set(rows(con).values()) == {("pending", 0, None)}
    assert ps.root_lost and ps.interrupted and ROOT_LOST_MSG in log
    monkeypatch.setattr(indexer, "sha256_file", real)
    offline.rename(root)
    _, ps = run_index(con, [], engine=FakeEngine(), log=quiet)
    assert ps.done == 3


# --- Fenster schließen / Abmelden zählt nicht als Absturz ------------------------------------

def test_workers_ignore_hangup_and_term():
    code = ("import os, signal\n"
            "from belegsuche.indexer import _init_worker\n"
            "_init_worker('apple')\n"
            "os.kill(os.getpid(), signal.SIGHUP)\n"
            "os.kill(os.getpid(), signal.SIGTERM)\n"
            "print('lebt')\n")
    proc = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "lebt" in proc.stdout, proc.stderr


def test_resource_tracker_survives_hangup():
    code = ("import os, signal, time\n"
            "from multiprocessing import resource_tracker\n"
            "from belegsuche.indexer import _start_resource_tracker\n"
            "_start_resource_tracker()\n"
            "pid = resource_tracker._resource_tracker._pid\n"
            "os.kill(pid, signal.SIGHUP)\n"
            "time.sleep(0.5)\n"
            "print('lebt' if os.waitpid(pid, os.WNOHANG) == (0, 0) else 'beendet')\n")
    proc = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, timeout=60)
    assert proc.stdout.strip() == "lebt", proc.stderr


def test_interrupt_during_prepare_in_parallel_mode(tmp_path, monkeypatch):
    root = tmp_path / "r"
    pdfs(root, 3)
    con = dbm.connect(tmp_path / "i.db")
    real, calls = indexer.sha256_file, []

    def sha_or_interrupt(path):
        calls.append(path)
        if len(calls) == 2:                # Ctrl+C / SIGHUP, während die 2. Datei vorbereitet wird
            raise KeyboardInterrupt
        return real(path)

    monkeypatch.setattr(indexer, "sha256_file", sha_or_interrupt)
    _, ps = run_index(con, [str(root)], log=quiet, workers=2)
    assert ps.interrupted
    assert set(rows(con).values()) == {("pending", 0, None)}             # kein 'processing', kein Versuch gezählt
    deadline = time.time() + 10                                          # Arbeitsprozesse sind beendet
    while mp.active_children() and time.time() < deadline:
        time.sleep(0.1)
    assert not mp.active_children()


# --- Speicher der Texterkennung -------------------------------------------------------------

@pytest.mark.vision
def test_vision_memory_stays_flat():
    code = f"""
import os, subprocess
from PIL import Image, ImageDraw, ImageFont
from belegsuche.ocr import AppleVisionOCR
img = Image.new("RGB", (2482, 3509), "white")
d = ImageDraw.Draw(img)
font = ImageFont.truetype({FONT!r}, 48)
for i in range(12):
    d.text((150, 150 + i * 90), f"Rechnung Nr. RE-2021/{{i:05d}}  Betrag 1.234,56 EUR", fill="black", font=font)
eng = AppleVisionOCR()
rss = lambda: int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])) // 1024
for _ in range(2):
    eng.recognize_image(img)
start = rss()
for _ in range(10):
    res = eng.recognize_image(img)
print(rss() - start, "RE-2021/00003" in res.text)
"""
    proc = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    grow_mb, found = proc.stdout.split()
    assert found == "True"
    assert int(grow_mb) < 200, f"Speicher wächst um {grow_mb} MB bei 10 Seiten"   # vorher ~650 MB


def test_worker_processes_are_recycled():
    pool = indexer._make_pool("apple", 1)
    try:
        assert pool._max_tasks_per_child == indexer.MAX_TASKS_PER_WORKER
    finally:
        pool.shutdown()


# --- Laufender Suchserver sieht neue Belege schon während des Einlesens ---------------------

def test_generation_bumped_during_run(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "BUMP_EVERY_FILES", 2, raising=False)
    root = tmp_path / "viele"
    pdfs(root, 5)
    db_path = tmp_path / "i.db"
    con = dbm.connect(db_path)
    g0 = generation(db_path)
    seen: list[int] = []

    def log(msg):
        if msg.startswith("["):            # eine Datei ist fertig
            seen.append(generation(db_path))

    run_index(con, [str(root)], engine=FakeEngine(), log=log)
    assert len(seen) == 5
    assert seen[1] > g0                    # nach der 2. Datei, nicht erst am Ende des Laufs
    assert generation(db_path) > seen[-1]  # Rest am Ende


def test_generation_unchanged_without_changes(tmp_path):
    root = tmp_path / "r"
    pdfs(root, 2)
    db_path = tmp_path / "i.db"
    con = dbm.connect(db_path)
    run_index(con, [str(root)], engine=FakeEngine(), log=quiet)
    g1 = generation(db_path)
    run_index(con, [], engine=FakeEngine(), log=quiet)
    assert generation(db_path) == g1       # nichts geändert -> Server muss nichts neu laden
    (root / "d0.pdf").unlink()
    run_index(con, [], engine=FakeEngine(), log=quiet)
    assert generation(db_path) > g1        # gelöschte Datei ändert den Index


# --- Belegfoto auf einer digitalen Seite ----------------------------------------------------

def pdf_with_image(path: Path, lines: list[str], img: Image.Image, box: tuple[float, float, float, float]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    if "Arial" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Arial", FONT))
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setFont("Arial", 11)
    y = 780
    for line in lines:
        c.drawString(60, y, line)
        y -= 18
    x, y, w, h = box
    c.drawImage(ImageReader(img), x, y, w, h)
    c.showPage()
    c.save()


MAIL = ["Von: Buchhaltung Hausverwaltung Nord", "Betreff: Rechnung Dachdecker Lindenweg",
        "Anbei die Rechnung für die Reparatur am Dach, bitte zahlen.", "Mit freundlichen Grüßen", "Erika Beispiel"]


def coverage(path: Path) -> float:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    pdf = pdfium.PdfDocument(str(path))
    try:
        return extract_mod._image_coverage(pdf[0], pdfium_c)
    finally:
        pdf.close()


@pytest.mark.vision
def test_embedded_receipt_photo_gets_ocr(tmp_path):
    photo = text_image(["Dachdecker Kowalski GmbH", "Rechnung Nr. KV-778899", "Betrag 1.845,00 EUR"],
                       size=(1200, 700), font_size=60)
    path = tmp_path / "mail_30.pdf"
    pdf_with_image(path, MAIL, photo, (70, 250, 450, 333))
    assert 0.25 < coverage(path) < 0.35
    pages = extract(path, CountingEngine())
    assert pages[0].source == "text+ocr"
    assert "KV-778899" in pages[0].text and "Kowalski" in pages[0].text
    assert "Erika Beispiel" in pages[0].text                      # Textebene bleibt erhalten


def test_small_logo_needs_no_ocr(tmp_path):
    logo = text_image(["NORD"], size=(240, 200), font_size=60)
    path = tmp_path / "mail_logo.pdf"
    pdf_with_image(path, MAIL, logo, (450, 740, 100, 80))
    assert coverage(path) < 0.05
    engine = FakeEngine()
    pages = extract(path, engine)
    assert pages[0].source == "text" and engine.calls == 0


# --- Lange TIFFs ----------------------------------------------------------------------------

def multipage_tiff(path: Path, n: int) -> None:
    frames = [Image.new("1", (160, 80), 1) for _ in range(n)]
    frames[0].save(path, "TIFF", compression="group4", save_all=True, append_images=frames[1:])


def test_tiff_with_more_than_200_pages_is_read_completely(tmp_path):
    path = tmp_path / "fax.tif"
    multipage_tiff(path, 205)
    engine = FakeEngine()
    pages = extract(path, engine)
    assert len(pages) == 205 and pages[-1].page_no == 205 and engine.calls == 205


def test_truncated_tiff_gets_note_in_db_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_mod, "MAX_IMAGE_PAGES", 3, raising=False)
    root = tmp_path / "r"
    root.mkdir()
    multipage_tiff(root / "fax.tif", 5)
    con = dbm.connect(tmp_path / "i.db")
    _, ps = run_index(con, [str(root)], engine=FakeEngine(), log=quiet)
    note = "Hinweis: nur die ersten 3 von 5 Seiten eingelesen"
    assert ps.done == 1 and rows(con)["fax.tif"] == ("done", 0, note)
    assert con.execute("SELECT pages FROM files").fetchone()[0] == 3
    r = build_report(con)
    assert [p["grund"] for p in r["problems"]] == [note]
    assert note in format_report(r)
    # Kopie (Text übernommen) trägt denselben Hinweis, Wiederholen fasst 'done' nicht an
    shutil.copy2(root / "fax.tif", root / "fax_kopie.tif")
    engine = FakeEngine()
    _, ps = run_index(con, [], engine=engine, log=quiet)
    assert ps.reused == 1 and engine.calls == 0
    assert rows(con)["fax_kopie.tif"] == ("done", 0, note)
    assert retry_errors(con) == 0


# --- Seitentext in NFC ----------------------------------------------------------------------

def test_page_text_is_stored_nfc(tmp_path):
    root = tmp_path / "r"
    photos(root, 1)
    nfd = unicodedata.normalize("NFD", "Hausverwaltung Müller, Heizkörper-Wartung Straße 5, Betrag 120,00 EUR")
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(root)], engine=FakeEngine(text=nfd), log=quiet)
    text = con.execute("SELECT text FROM pages").fetchone()[0]
    assert unicodedata.is_normalized("NFC", text) and "Müller" in text


# --- Derselbe Ordner in anderer Schreibweise, Überschneidungen, Umziehen -------------------

def test_same_folder_in_other_spelling_is_one_root(tmp_path):
    folder = tmp_path / "Hausverwaltung Müller"
    pdfs(folder, 1)
    con = dbm.connect(tmp_path / "i.db")
    rid = add_root(con, str(folder))
    variants = [unicodedata.normalize("NFD", str(folder)), str(folder) + "/"]
    link = tmp_path / "Verknüpfung"
    link.symlink_to(folder)
    variants.append(str(link))
    other_case = str(folder).swapcase()
    if os.path.isdir(other_case):          # Groß/Klein nur auf Dateisystemen ohne Unterscheidung
        variants.append(other_case)
    for v in variants:
        assert add_root(con, v) == rid, v
        assert "bereits eingetragen" in find_root_conflict(con, v)
    assert con.execute("SELECT count(*) FROM roots").fetchone()[0] == 1
    assert con.execute("SELECT path FROM roots").fetchone()[0] == normalize_root(folder)


def test_run_index_with_nfd_path_does_not_duplicate(tmp_path):
    folder = tmp_path / "Hausverwaltung Müller"
    pdfs(folder, 3)
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(folder)], engine=FakeEngine(), log=quiet)
    scans, ps = run_index(con, [unicodedata.normalize("NFD", str(folder))], engine=FakeEngine(), log=quiet)
    assert len(scans) == 1 and scans[0].new == 0 and ps.done == 0
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 3


def test_overlap_is_detected_through_symlink(tmp_path):
    base = tmp_path / "Belege"
    pdfs(base / "2021", 1)
    con = dbm.connect(tmp_path / "i.db")
    rid = add_root(con, base / "2021")
    link = tmp_path / "abkuerzung"
    link.symlink_to(base)
    with pytest.raises(SystemExit, match="Überschneidet"):
        add_root(con, link)                                  # Elternordner, über Symlink
    (base / "2021" / "Q1").mkdir()
    with pytest.raises(SystemExit, match="Überschneidet"):
        add_root(con, link / "2021" / "Q1")                  # Unterordner
    assert "Überschneidet" in find_root_conflict(con, link)
    assert find_root_conflict(con, link, exclude_id=rid) is None


def test_move_root_checks(tmp_path):
    a, b, c = tmp_path / "A", tmp_path / "X" / "B", tmp_path / "C"
    pdfs(a, 1)
    pdfs(b, 1, prefix="b")
    c.mkdir()
    con = dbm.connect(tmp_path / "i.db")
    ida, idb = add_root(con, a), add_root(con, b)
    assert "Unbekannte Ordner-ID 99" in move_root(con, 99, c)
    assert "nicht gefunden" in move_root(con, ida, tmp_path / "fehlt")
    assert "bereits eingetragen" in move_root(con, ida, b)                  # statt IntegrityError
    assert "Überschneidet" in move_root(con, ida, tmp_path / "X")          # Elternordner von B
    assert move_root(con, ida, a) is None                                    # auf sich selbst: ok
    assert move_root(con, ida, str(c) + "/") is None
    assert con.execute("SELECT path FROM roots WHERE id = ?", (ida,)).fetchone()[0] == normalize_root(c)
    assert con.execute("SELECT path FROM roots WHERE id = ?", (idb,)).fetchone()[0] == normalize_root(b)


# --- SSD unter neuem Namen ------------------------------------------------------------------

def test_renamed_ssd_is_moved_not_duplicated(tmp_path):
    ssd = tmp_path / "SSD"
    pdfs(ssd / "Belege", 4)
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(ssd / "Belege")], engine=FakeEngine(), log=quiet)
    rid = con.execute("SELECT id FROM roots").fetchone()[0]
    renamed = tmp_path / "SSD 1"
    ssd.rename(renamed)                    # so benennt macOS eine SSD, wenn der Name belegt ist
    log: list[str] = []
    scans, ps = run_index(con, [str(renamed / "Belege")], engine=FakeEngine(), log=log.append)
    assert any("umgezogen" in m for m in log)
    assert [tuple(r) for r in con.execute("SELECT id, path FROM roots")] == [(rid, normalize_root(renamed / "Belege"))]
    assert scans[0].new == 0 and scans[0].missing == 0 and ps.done == 0
    assert {v[0] for v in rows(con).values()} == {"done"}


def test_other_folder_is_not_taken_for_moved_root(tmp_path):
    ssd = tmp_path / "SSD"
    pdfs(ssd / "Belege", 4)
    con = dbm.connect(tmp_path / "i.db")
    run_index(con, [str(ssd / "Belege")], engine=FakeEngine(), log=quiet)
    ssd.rename(tmp_path / "weg")           # alte SSD nicht verbunden
    other = tmp_path / "Andere" / "Belege"
    other.mkdir(parents=True)
    for i in range(4):                     # gleiche Namen, anderer Inhalt (andere Größe)
        digital_pdf(other / f"d{i}.pdf", [[f"Ganz anderes Dokument {i}", "mit deutlich mehr Text als das Original",
                                           "und noch einer Zeile"]])
    run_index(con, [str(other)], engine=FakeEngine(), log=quiet)
    assert con.execute("SELECT count(*) FROM roots").fetchone()[0] == 2


# --- Zwei Einlese-Läufe gleichzeitig --------------------------------------------------------

def test_lock_prevents_second_run(tmp_path):
    db_path = tmp_path / "index.db"
    h = acquire_lock(db_path)
    try:
        with pytest.raises(SystemExit, match="läuft bereits"):
            acquire_lock(db_path)
        code = f"from belegsuche.indexer import acquire_lock; acquire_lock({str(db_path)!r})"
        proc = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, timeout=60)
        assert proc.returncode != 0 and "läuft bereits" in proc.stderr
    finally:
        h.close()
    acquire_lock(db_path).close()          # nach dem Ende des ersten Laufs wieder frei
