from pathlib import Path

import pytest

from belegsuche import db as dbm
from belegsuche.indexer import run_index
from belegsuche.report import build_report
from belegsuche.search import Searcher

from .conftest import build_corpus, tree_state


def top(db_path: Path, query: str, n: int = 1) -> list[str]:
    return [h.filename for h in Searcher(db_path).search(query, limit=10).hits[:n]]


def test_originals_untouched(tmp_path):
    root = tmp_path / "korpus"
    root.mkdir()
    build_corpus(root)
    before = tree_state(root)
    con = dbm.connect(tmp_path / "index.db")
    run_index(con, [str(root)], log=lambda s: None)
    assert tree_state(root) == before     # nichts verändert, nichts hinzugefügt


@pytest.mark.vision
def test_statuses_and_report(indexed):
    root, files, db_path, con, _ = indexed
    st = {r["relpath"]: (r["status"], r["error"]) for r in con.execute("SELECT relpath, status, error FROM files")}
    rel = lambda k: str(files[k].relative_to(root))  # noqa: E731
    for k in ("heizung", "hauswart", "mueller", "scan", "bon", "sammel"):
        assert st[rel(k)][0] == "done", k
    assert st[rel("encrypted")] == ("error", "PDF ist passwortgeschützt")
    assert st[rel("corrupt")][0] == "error"
    assert st[rel("xlsx")][0] == "skipped"
    assert not any(".DS_Store" in p for p in st)
    rep = build_report(con)
    reasons = {p["grund"] for p in rep["problems"]}
    assert "PDF ist passwortgeschützt" in reasons
    assert rep["unsupported"] == {".xlsx": 1}


@pytest.mark.parametrize("query,expected", [
    ("RE-2021/00487", "Dokument (3).pdf"),
    ("2021-00487", "Dokument (3).pdf"),
    ("RE 2021 00487", "Dokument (3).pdf"),
    ("202100487", "Dokument (3).pdf"),
    ("1234,56", "Dokument (3).pdf"),
    ("1.234,56 €", "Dokument (3).pdf"),
    ("Wartung Heizung Musterstraße", "Dokument (3).pdf"),
    ("Heizungswartung", "Dokument (3).pdf"),
    ("Hausmeister Lindenweg", "20220630_1422.pdf"),
    ("Müler Elektro", "Unbenannt.pdf"),
    ("Mueller", "Unbenannt.pdf"),
    ("INV88213", "Unbenannt.pdf"),
    pytest.param("Schornsteinfger Birkenallee", "Scan_00487.pdf", marks=pytest.mark.vision),  # Tippfehler + Scan
    pytest.param("4711/22", "Scan_00487.pdf", marks=pytest.mark.vision),
    pytest.param("Silikon Baumarkt", "IMG_2231.png", marks=pytest.mark.vision),  # Bild
    pytest.param("Aufzug Wartung Am Hang", "scan0001.pdf", marks=pytest.mark.vision),  # Seite 3 im Sammel-Scan
    ("März 2021", "Dokument (3).pdf"),
    ("12.3.2021", "Dokument (3).pdf"),
])
def test_search_finds_first(indexed, query, expected):
    _, _, db_path, _, _ = indexed
    assert top(db_path, query) == [expected], query


@pytest.mark.vision
def test_sammel_pdf_reports_right_page(indexed):
    _, _, db_path, _, _ = indexed
    hit = Searcher(db_path).search("Aufzug Nord Wartungsrechnung").hits[0]
    assert hit.filename == "scan0001.pdf" and hit.page_no == 3 and hit.pages == 3


def test_snippet_highlights(indexed):
    _, _, db_path, _, _ = indexed
    hit = Searcher(db_path).search("RE-2021/00487 Musterstraße").hits[0]
    assert "<mark>" in hit.snippet_html
    assert "RE-2021/00487" in hit.snippet_text


def test_unknown_terms_reported(indexed):
    _, _, db_path, _, _ = indexed
    resp = Searcher(db_path).search("Musterstraße Zzyzxquux")
    assert "zzyzxquux" in resp.unknown
    assert resp.hits and resp.hits[0].filename == "Dokument (3).pdf"


@pytest.mark.vision
def test_general_query_lists_many(indexed):
    _, _, db_path, _, _ = indexed
    assert len(Searcher(db_path).search("Rechnung").hits) >= 4
