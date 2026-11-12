"""Finder starter: simulate dialog/CLI exits without opening Finder or touching a real index."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("chooser_rc,index_rc,report_rc,choice,expected_rc,commands", [
    (1, 0, 0, "", 1, []),
    (0, 130, 0, "/Belege mit Leerzeichen", 130, ["index:/Belege mit Leerzeichen"]),
    (0, 0, 2, "", 2, ["index:", "bericht:"]),
    (0, 0, 0, "", 0, ["index:", "bericht:"]),
])
def test_starter_preserves_errors(tmp_path, chooser_rc, index_rc, report_rc, choice, expected_rc, commands):
    name = "Ordner einlesen.command"
    launcher = tmp_path / name
    shutil.copy2(Path(__file__).resolve().parents[1] / name, launcher)
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for path, script in [
        (bin_dir / "osascript", 'echo "$CHOICE"; exit "$CHOOSER_RC"'),
        (bin_dir / "belegsuche", 'echo "$1:$2" >> "$CALLS"; if [ "$1" = index ]; then exit "$INDEX_RC"; fi; exit "$REPORT_RC"'),
    ]:
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(0o700)
    log = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CHOICE=choice, CHOOSER_RC=str(chooser_rc),
               INDEX_RC=str(index_rc), REPORT_RC=str(report_rc), CALLS=str(log))
    result = subprocess.run([str(launcher)], cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=10)
    assert (result.returncode, log.read_text().splitlines() if log.exists() else []) == (expected_rc, commands)
