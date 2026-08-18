"""Tests for tools/fast_pii_screen.py — pure functions, no live services needed."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.fast_pii_screen import compare_screens, fast_screen_table  # noqa: E402


def test_fast_screen_flags_broadly_via_substring():
    table = {"table_id": "t1", "columns": [{"name": "CustomerID"}, {"name": "OrderDate"}]}
    flagged = fast_screen_table(table)
    assert "CustomerID" in flagged  # 'id' substring
    assert "OrderDate" not in flagged  # no marker substring


def test_disagreement_when_fast_screen_over_flags():
    table = {"table_id": "t1", "columns": [{"name": "CustomerID"}]}
    findings = compare_screens(table)
    assert len(findings) == 1
    assert findings[0]["finding_type"] == "SENSITIVITY_SCREEN_DISAGREEMENT"
    assert findings[0]["detail"]["fast_screen"] == "FLAGGED"
    assert findings[0]["detail"]["careful_pass"] == "CLEARED"
    assert findings[0]["severity"] == "LOW"


def test_disagreement_when_fast_screen_under_flags():
    table = {"table_id": "t1", "columns": [{"name": "PassportNumber"}]}
    findings = compare_screens(table)
    assert len(findings) == 1
    assert findings[0]["detail"]["fast_screen"] == "CLEARED"
    assert findings[0]["detail"]["careful_pass"] == "FLAGGED"
    assert findings[0]["severity"] == "MEDIUM"  # the more concerning direction


def test_no_disagreement_when_both_screens_agree():
    table = {"table_id": "t1", "columns": [{"name": "EmailAddress"}, {"name": "OrderDate"}]}
    findings = compare_screens(table)
    assert findings == []  # EmailAddress: both flag; OrderDate: neither flags
