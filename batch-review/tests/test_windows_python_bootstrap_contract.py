from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "tools" / "resolve_python.cmd"
LAUNCHERS = ("init.cmd", "review.cmd", "run.cmd", "status.cmd")


def test_windows_launchers_share_project_python_bootstrap():
    assert BOOTSTRAP.is_file()
    for name in LAUNCHERS:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert 'tools\\resolve_python.cmd' in text
        assert "SKILL_REVIEW_RESOLVED_PYTHON" in text


def test_bootstrap_prefers_project_python_and_can_install_bundled_313():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    local_marker = ".scanner-tools\\_python313\\python.exe"
    system_marker = "for %%V in (3.14 3.13 3.12 3.11)"

    assert local_marker in text
    assert text.index(local_marker) < text.index(system_marker)
    assert "python-3.13.15-amd64.exe" in text
    assert "Get-FileHash" in text
    assert "certutil.exe -hashfile" in text
    assert 'start "" /wait' in text
    assert "InstallAllUsers=0" in text
    assert "PrependPath=0" in text
    assert "AppendPath=0" in text


def test_bootstrap_hash_matches_python_installer_contract():
    bootstrap_text = BOOTSTRAP.read_text(encoding="utf-8")
    installer_text = (ROOT / "tools" / "install_scanners.py").read_text(encoding="utf-8")

    bootstrap_match = re.search(r'set "EXPECTED_SHA256=([0-9a-f]{64})"', bootstrap_text)
    installer_match = re.search(r'WINDOWS_PYTHON_SHA256 = "([0-9a-f]{64})"', installer_text)
    assert bootstrap_match is not None
    assert installer_match is not None
    assert bootstrap_match.group(1) == installer_match.group(1)


@pytest.mark.skipif(os.name != "nt", reason="Windows CMD bootstrap smoke test")
def test_bootstrap_cmd_resolves_a_usable_python_on_windows():
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "call", str(BOOTSTRAP)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
