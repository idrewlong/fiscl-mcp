"""Runs scripts/smoke.py (every tool, resource, and prompt over real stdio against demo data)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke.py"


@pytest.mark.smoke
@pytest.mark.skipif(not SCRIPT.exists(), reason="scripts/smoke.py is not in this checkout")
def test_smoke_script_passes():
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=180, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert " 0 failed" in r.stdout
