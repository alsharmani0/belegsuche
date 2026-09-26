"""Regressionstests: »belegsuche test« ordnet die erwartete Datei eindeutig zu und
unterscheidet bei Scans nicht, ob ein Begriff falsch erkannt wurde oder gar nicht dasteht;
/api/open meldet ein fehlgeschlagenes Öffnen."""
import http.client
import json
import shutil
import subprocess
import threading
import unicodedata
from http.server import ThreadingHTTPServer

import pytest

from belegsuche import db as dbm
from belegsuche import server as srv
from belegsuche.evaluate import diagnose, evaluate, format_eval
from belegsuche.indexer import run_index
from belegsuche.search import Searcher

from .conftest import CountingEngine, digital_pdf, text_image, tree_state

PETERSEN = ["Elektro Petersen GmbH", "Rechnung 88221", "Austausch Sicherungskasten"]
GRUENWERK = ["Gartenpflege Grünwerk", "Rechnung GW-5521", "Rasenpflege Lindenweg 3"]
BRANDL = ["Dachdecker Brandl", "Angebot 2023-17", "Erneuerung Dachrinne"]
SCHLUESSEL = ["Schlüsseldienst Kaltenbach", "Notdienst Birkenallee 21", "Betrag 89,00 Euro"]
RUN = subprocess.run          # ungepatcht: der Server-Test ersetzt subprocess.run (auch für node)


def _index(db_path, folders):
    con = dbm.connect(db_path)
    run_index(con, [str(f) for f in folders], engine=CountingEngine(), log=lambda s: None)
    con.close()


@pytest.fixture(scope="module")
def belege(tmp_path_factory):
    """Ein Ordner: gleichnamige Datei im Unterordner, inhaltsgleiche Kopie, Scan, Umlaut-Pfad."""
    base = tmp_path_factory.mktemp("eval1")
    root = base / "Belege"
    for d in ("2023", "backup/2023", "dach/kopie", "Übersicht"):
        (root / d).mkdir(parents=True)
    digital_pdf(root / "2023" / "rechnung.pdf", [PETERSEN])
    digital_pdf(root / "backup" / "2023" / "rechnung.pdf", [GRUENWERK])
    digital_pdf(root / "dach" / "angebot.pdf", [BRANDL])
    shutil.copyfile(root / "dach" / "angebot.pdf", root / "dach" / "kopie" / "angebot.pdf")
    digital_pdf(root / "Übersicht" / "grün.pdf", [["Fensterputz Glanzwerk", "Rechnung 311"]])
    text_image(SCHLUESSEL, size=(1200, 600), font_size=40).save(root / "scan.png")
    db_path = base / "index.db"
    _index(db_path, [root])
    return root, db_path


@pytest.fixture(scope="module")
def drei_ordner(tmp_path_factory):
    """Derselbe relative Pfad in drei Ordnern, zwei davon heißen gleich."""
    base = tmp_path_factory.mktemp("eval3")
    roots = {"belege": base / "Belege", "archiv": base / "Archiv", "belege2": base / "x" / "Belege"}
    texts = {"belege": PETERSEN, "archiv": ["Heizöl Nord", "Lieferung Heizöl 2000 Liter"],
             "belege2": ["Schornsteinfeger Probe", "Kehrung und Messung"]}
    for key, root in roots.items():
        (root / "2023").mkdir(parents=True)
        digital_pdf(root / "2023" / "rechnung.pdf", [texts[key]])
    db_path = base / "index.db"
    _index(db_path, roots.values())
    return roots, db_path


def _eval(db_path, *queries):
    return evaluate(Searcher(db_path), [{"suche": s, "datei": d, "seite": "1"} for s, d in queries])


# --- A: erwartete Datei eindeutig zuordnen, kein Endvergleich ----------------------------------

def test_same_name_in_subfolder_is_not_the_expected_file(belege):
    root, db_path = belege
    before = tree_state(root)
    results, summary = _eval(db_path, ("Rasenpflege", "2023/rechnung.pdf"))
    r = results[0]
    # gefunden wird nur backup/2023/rechnung.pdf – das ist nicht die erwartete Datei
    assert r.rang is None and r.kategorie == "Wortwahl", r
    assert r.ursache == "Im Dokument nicht gefunden: rasenpflege"
    assert summary["platz_1"] == 0 and summary["nicht_gefunden"] == 1
    assert tree_state(root) == before                     # Originale unverändert


def test_diagnose_uses_exact_file(belege):
    _, db_path = belege
    s = Searcher(db_path)
    con = s._connect()
    try:
        assert diagnose(con, s, "Rasenpflege", "2023/rechnung.pdf") == (
            "Im Dokument nicht gefunden: rasenpflege", "Wortwahl")
        assert diagnose(con, s, "Rasenpflege", "rechnung.pdf")[1] == "Index"
    finally:
        con.close()


def test_expected_file_forms(belege):
    root, db_path = belege
    full = root / "backup" / "2023" / "rechnung.pdf"
    nfd = unicodedata.normalize("NFD", "Übersicht/grün.pdf")
    results, summary = _eval(db_path,
                             ("Rasenpflege", "backup/2023/rechnung.pdf"),
                             ("Rasenpflege", "BACKUP/2023/Rechnung.PDF"),
                             ("Rasenpflege", "Belege/backup/2023/rechnung.pdf"),
                             ("Rasenpflege", str(full)),
                             ("Rasenpflege", "backup\\2023\\rechnung.pdf"),
                             ("Fensterputz Glanzwerk", nfd),
                             ("Rasenpflege", "rechnung.pdf"))
    assert [(r.rang, r.kategorie) for r in results[:6]] == [(1, "ok")] * 6, results
    assert results[6].rang is None and results[6].kategorie == "Index"     # kein Endvergleich
    assert results[6].ursache.startswith("Datei nicht im Index")


def test_identical_copy_counts_as_found(belege):
    _, db_path = belege
    hits = Searcher(db_path).search("Dachrinne Brandl").hits
    assert len(hits[0].duplicates) == 1                  # beide Kopien in einem Treffer
    results, _ = _eval(db_path, ("Dachrinne Brandl", "dach/angebot.pdf"),
                       ("Dachrinne Brandl", "dach/kopie/angebot.pdf"))
    assert [(r.rang, r.kategorie) for r in results] == [(1, "ok"), (1, "ok")]


def test_ambiguous_relative_path(drei_ordner):
    roots, db_path = drei_ordner
    results, summary = _eval(db_path,
                             ("Heizöl", "2023/rechnung.pdf"),
                             ("Heizöl", "Archiv/2023/rechnung.pdf"),
                             ("Schornsteinfeger", "Belege/2023/rechnung.pdf"),
                             ("Schornsteinfeger", str(roots["belege2"] / "2023" / "rechnung.pdf")),
                             ("Sicherungskasten", str(roots["belege"] / "2023" / "rechnung.pdf")),
                             ("Heizöl", str(roots["belege"] / "2023" / "rechnung.pdf")))
    mehrdeutig = ("Pfad mehrdeutig – bitte vollständigen Pfad angeben", "Index")
    assert (results[0].rang, results[0].ursache, results[0].kategorie) == (None, *mehrdeutig)
    assert (results[1].rang, results[1].kategorie) == (1, "ok")
    assert (results[2].rang, results[2].ursache, results[2].kategorie) == (None, *mehrdeutig)
    assert (results[3].rang, results[3].kategorie) == (1, "ok")
    assert (results[4].rang, results[4].kategorie) == (1, "ok")
    # Heizöl steht nur im Archiv – die angegebene Datei in Belege wird nicht verwechselt
    assert results[5].rang is None and results[5].kategorie == "Wortwahl"
    assert summary["ursachen"] == {"Index": 2, "Wortwahl": 1}


# --- B: Begriff fehlt im erkannten Text eines Scans --------------------------------------------

@pytest.mark.vision
def test_scan_missing_term_is_ocr_or_wording(belege):
    _, db_path = belege
    results, summary = _eval(db_path,
                             ("Kaltenbach", "scan.png"),              # Scan ist lesbar
                             ("Hubschrauber", "scan.png"),        # steht nirgends
                             ("Rasenpflege", "scan.png"),         # steht woanders, nicht im Scan
                             ("Sicherungskasten", "backup/2023/rechnung.pdf"))
    assert (results[0].rang, results[0].kategorie) == (1, "ok")
    for r, term in ((results[1], "hubschrauber"), (results[2], "rasenpflege")):
        assert r.rang is None and r.kategorie == "OCR oder Wortwahl", r
        assert r.ursache == (f"Begriff nicht im erkannten Text: {term} – am Original prüfen, ob er dort steht "
                             "(dann OCR-Fehler) oder nicht (andere Wortwahl)")
    assert results[3].kategorie == "Wortwahl"             # digitale Textebene: eindeutig
    assert summary["ursachen"] == {"OCR oder Wortwahl": 2, "Wortwahl": 1}
    out = format_eval(results, summary)
    assert "Ursachen:       2× OCR oder Wortwahl, 1× Wortwahl" in out
    assert "Begriff nicht im erkannten Text: hubschrauber" in out


# --- C: /api/open meldet, wenn »open« scheitert ------------------------------------------------

@pytest.fixture()
def server(tmp_path, monkeypatch):
    root = tmp_path / "Belege"
    root.mkdir()
    digital_pdf(root / "Rechnung.pdf", [PETERSEN])
    db_path = tmp_path / "index.db"
    _index(db_path, [root])
    con = dbm.connect(db_path, readonly=True)
    fid = con.execute("SELECT id FROM files WHERE relpath = 'Rechnung.pdf'").fetchone()[0]
    con.close()
    state = {"result": None, "calls": []}

    def fake_run(cmd, check=False):
        state["calls"].append(cmd)
        return state["result"](cmd) if callable(state["result"]) else state["result"]
    monkeypatch.setattr(srv.subprocess, "run", fake_run)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = srv.make_handler(Searcher(db_path), port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port, fid, state
    httpd.shutdown()
    httpd.server_close()


def _open(port, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/api/open", body=json.dumps(body),
              headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", "X-Belegsuche": "1"})
    r = c.getresponse()
    return r.status, json.loads(r.read())


OPEN_ERR = "Öffnen fehlgeschlagen (Programm meldet Fehler 1) – Datei im Finder zeigen und von Hand öffnen"


def test_open_failure_is_reported(server):
    port, fid, state = server
    state["result"] = lambda cmd: subprocess.CompletedProcess(cmd, 1)
    assert _open(port, {"id": fid}) == (500, {"error": OPEN_ERR})
    assert state["calls"][-1][0] == "open" and state["calls"][-1][1].endswith("Rechnung.pdf")
    assert _open(port, {"id": fid, "reveal": True}) == (
        500, {"error": "Im Finder zeigen fehlgeschlagen (Programm meldet Fehler 1)"})
    assert state["calls"][-1][:2] == ["open", "-R"]


@pytest.mark.parametrize("result", [None, lambda cmd: subprocess.CompletedProcess(cmd, 0)])
def test_open_success(server, result):
    port, fid, state = server
    state["result"] = result
    assert _open(port, {"id": fid}) == (200, {"ok": True})
    assert _open(port, {"id": fid, "reveal": True}) == (200, {"ok": True})


NODE_PRELUDE = r"""
function mkEl() {
  return {value: '', innerHTML: '', textContent: '', className: '', children: [], addEventListener() {},
          appendChild(c) { if (!this.children.includes(c)) this.children.push(c); },
          querySelector(sel) { return this.children.find(c => '.' + c.className === sel) || null; }};
}
const els = {};
globalThis.document = {getElementById: (id) => els[id] ||= mkEl(), createElement: () => mkEl()};
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node nicht installiert")
def test_ui_shows_open_failure(server):
    port, fid, state = server
    state["result"] = lambda cmd: subprocess.CompletedProcess(cmd, 1)
    status, payload = _open(port, {"id": fid})              # echte Antwort des Servers an die Oberfläche
    script = srv.UI_FILE.read_text(encoding="utf-8").split("<script>")[1].split("</script>")[0]
    fetch = (f"globalThis.fetch = async () => ({{ok: {str(200 <= status < 300).lower()}, status: {status}, "
             f"json: async () => ({json.dumps(payload)})}});\n")
    scenario = """
(async () => {
  const actions = mkEl(), btn = {parentElement: actions};
  await openFile(1, false, btn);
  console.log(JSON.stringify(actions.children.map(c => [c.className, c.textContent])));
})();
"""
    p = RUN(["node", "-e", NODE_PRELUDE + fetch + script + scenario],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout.strip().splitlines()[-1]) == [["err", OPEN_ERR]]
