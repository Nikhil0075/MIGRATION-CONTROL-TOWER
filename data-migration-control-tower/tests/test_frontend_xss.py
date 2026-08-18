"""Day 10 hardening: shells out to frontend/static/test_esc.js (plain
Node, no new JS test-framework dependency) so `pytest tests/` still
exercises the XSS-escaping regression check as part of one command.
Skips automatically if `node` isn't on PATH — same "skip, don't fail
the whole suite over an optional tool" pattern as the SQL Server/
Firestore live-service skips elsewhere in this suite.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

skip_if_no_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


@skip_if_no_node
def test_app_js_escapes_xss_payloads():
    result = subprocess.run(
        ["node", str(REPO_ROOT / "frontend" / "static" / "test_esc.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"app.js XSS escaping regression:\n{result.stdout}\n{result.stderr}"
