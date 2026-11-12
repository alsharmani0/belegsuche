"""Corpus maintenance must preserve existing fixtures and reject broken metadata."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f"review_{name}", ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker(tmp_path, monkeypatch):
    module = load_tool("check_testdata")
    monkeypatch.setattr(module, "TESTDATA", tmp_path)
    monkeypatch.setattr(module, "KORPUS", tmp_path / "korpus")
    module.KORPUS.mkdir()
    return module


def entry(path="document.pdf", data=b"fixture"):
    return {
        "path": path, "kind": "digital_pdf", "pages": 1,
        "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
        "duplicate_of": None, "facts": [],
    }


def manifest(*entries):
    return {"anzahl_dateien": len(entries), "documents": list(entries)}


def test_generator_refuses_existing_corpus_before_rendering(tmp_path, monkeypatch):
    generator = load_tool("make_testdata")
    corpus = tmp_path / "korpus"
    corpus.mkdir()
    marker = corpus / "user-file.txt"
    marker.write_bytes(b"keep this")
    monkeypatch.setattr(generator, "KORPUS", corpus)
    monkeypatch.setattr(generator, "PLAN", [])
    monkeypatch.setattr(generator, "register_fonts", lambda: None)
    with pytest.raises(FileExistsError):
        generator.build()
    assert marker.read_bytes() == b"keep this"


def test_generator_refuses_nonempty_explicit_destination(tmp_path):
    generator = load_tool("make_testdata")
    marker = tmp_path / "manifest.json"
    marker.write_bytes(b"published metadata")
    with pytest.raises(SystemExit) as exc:
        generator.main(["--output", str(tmp_path)])
    assert exc.value.code == 2
    assert marker.read_bytes() == b"published metadata"
    assert not (tmp_path / "korpus").exists()


def test_standalone_manifest_resolves_corpus_relative_to_itself(tmp_path, monkeypatch):
    generator = load_tool("make_testdata")
    monkeypatch.setattr(generator, "PLAN", [])
    monkeypatch.setattr(generator, "register_fonts", lambda: None)
    monkeypatch.setattr(generator, "dev_queries", lambda: [])
    monkeypatch.setattr(generator, "holdout_queries", lambda: [])
    output = tmp_path / "standalone"
    assert generator.main(["--output", str(output)]) == 0
    metadata = json.loads((output / "manifest.json").read_text())
    assert not Path(metadata["korpus_root"]).is_absolute()
    assert (output / metadata["korpus_root"]).samefile(output / "korpus")


def test_generator_default_call_resets_paths_after_explicit_output(tmp_path, monkeypatch):
    generator = load_tool("make_testdata")
    monkeypatch.setattr(generator, "ROOT", tmp_path / "project")
    monkeypatch.setattr(generator, "PLAN", [])
    monkeypatch.setattr(generator, "register_fonts", lambda: None)
    monkeypatch.setattr(generator, "dev_queries", lambda: [])
    monkeypatch.setattr(generator, "holdout_queries", lambda: [])
    standalone = tmp_path / "standalone"
    assert generator.main(["--output", str(standalone)]) == 0
    original = {p.name: p.read_bytes() for p in standalone.iterdir() if p.is_file()}
    assert generator.main([]) == 0
    default = generator.ROOT / "testdata"
    assert generator.KORPUS == default / "korpus"
    assert generator.MANIFEST == default / "manifest.json"
    assert generator.DEV_CSV == default / "queries_dev.csv"
    assert generator.HOLDOUT_CSV == default / "queries_holdout.csv"
    assert generator.README == default / "README.md"
    assert generator.KORPUS.is_dir()
    assert all(path.is_file() for path in (generator.MANIFEST, generator.DEV_CSV,
                                          generator.HOLDOUT_CSV, generator.README))
    assert original == {p.name: p.read_bytes() for p in standalone.iterdir() if p.is_file()}


@pytest.mark.parametrize("changed", [False, True])
def test_determinism_uses_two_temporary_destinations(checker, monkeypatch, changed):
    marker = checker.TESTDATA / "published.txt"
    marker.write_bytes(b"untouched")
    runs = []

    def generate(command, **kwargs):
        assert "--output" in command
        output = Path(command[command.index("--output") + 1])
        assert output != checker.TESTDATA
        output.mkdir(parents=True)
        output.joinpath("fixture").write_bytes(b"different" if changed and runs else b"stable")
        runs.append(output)

    monkeypatch.setattr(checker.subprocess, "run", generate)
    checker.check_determinism()
    assert len(set(runs)) == 2
    assert all(not path.exists() for path in runs)
    assert marker.read_bytes() == b"untouched"
    assert bool(checker.FAILS) is changed


@pytest.mark.parametrize("raw", [
    b"", b"wrong;header\n", b"\xff\n",
    b"suche;datei;seite;notiz\nquery;document.pdf;one;name: test\n",
    b"suche;datei;seite;notiz\nquery;document.pdf;0;name: test\n",
    b"suche;datei;seite;notiz\nquery;document.pdf;1\n",
    b"suche;datei;seite;notiz\nquery;document.pdf;1;name: test;extra\n",
    b"suche;datei;seite;notiz\n ;document.pdf;1;name: test\n",
])
def test_invalid_csv_fails_without_crashing(checker, raw):
    path = checker.TESTDATA / "queries.csv"
    path.write_bytes(raw)
    assert checker.read_csv(path) == []
    assert checker.FAILS


def test_query_split_detects_equivalent_page_numbers(checker):
    doc = entry()
    doc["facts"] = [{"seite": 1}]
    header = "suche;datei;seite;notiz\n"
    (checker.TESTDATA / "queries_dev.csv").write_text(header + "first;document.pdf;1;name: test\n")
    (checker.TESTDATA / "queries_holdout.csv").write_text(header + "second;document.pdf;01;name: test\n")
    checker.check_queries({doc["path"]: doc})
    assert any("teilen Ziele" in message for message in checker.FAILS)


def test_manifest_rejects_duplicate_paths_and_incorrect_sizes(checker):
    doc = entry()
    (checker.KORPUS / doc["path"]).write_bytes(b"fixture")
    duplicate = copy.deepcopy(doc)
    doc["size_bytes"] += 1
    checker.check_files(manifest(doc, duplicate))
    assert any("mehrfach" in message for message in checker.FAILS)
    assert any("size_bytes" in message for message in checker.FAILS)


def test_manifest_rejects_escape_and_symlink_paths(checker):
    outside = checker.TESTDATA / "outside.pdf"
    outside.write_bytes(b"fixture")
    (checker.KORPUS / "link.pdf").symlink_to(outside)
    docs = checker.check_files(manifest(entry("../outside.pdf"), entry("link.pdf")))
    assert docs == {}
    assert any("ungültiger Manifest-Pfad" in message for message in checker.FAILS)
    assert any("Symlink" in message for message in checker.FAILS)
    assert outside.read_bytes() == b"fixture"


def test_missing_document_is_reported_without_pdf_crash(checker):
    docs = checker.check_files(manifest(entry()))
    checker.check_pdfs(docs)
    assert any("fehlt" in message for message in checker.FAILS)


def test_dangling_duplicate_reference_is_reported(checker):
    doc = entry()
    doc.update(kind="duplicate_of", duplicate_of="missing.pdf")
    (checker.KORPUS / doc["path"]).write_bytes(b"fixture")
    checker.check_files(manifest(doc))
    assert any("Dubletten-Ziel" in message for message in checker.FAILS)


@pytest.mark.parametrize("include_holdout", [False, True])
def test_holdout_content_diagnostics_require_opt_in(checker, monkeypatch, include_holdout):
    source = checker.TESTDATA
    (source / "manifest.json").write_text("{}")
    sets = {"queries_dev.csv": [{"suche": "development"}], "queries_holdout.csv": [{"suche": "reserved"}]}
    observed = []
    monkeypatch.setattr(checker, "check_files", lambda _: {})
    monkeypatch.setattr(checker, "check_pdfs", lambda _: None)
    monkeypatch.setattr(checker, "check_queries", lambda _: sets)
    monkeypatch.setattr(checker, "ambiguity_report", lambda docs, rows, verbose: observed.append(rows))
    args = ["--testdata", str(source)] + (["--include-holdout"] if include_holdout else [])
    assert checker.main(args) == 0
    assert ("queries_holdout.csv" in observed[0]) is include_holdout
