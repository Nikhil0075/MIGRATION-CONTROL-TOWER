"""Tests for tools/data_classifier.py — pure function, no live services needed."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.data_classifier import classify_table  # noqa: E402


def test_table_with_no_sensitive_columns_is_metadata():
    table = {"columns": [{"name": "CustomerID"}, {"name": "CategoryName"}]}
    classification, matches = classify_table(table)
    assert classification == "METADATA"
    assert matches == []


def test_table_with_email_and_phone_is_pii():
    table = {
        "columns": [
            {"name": "CustomerID"},
            {"name": "EmailAddress"},
            {"name": "PhoneNumber"},
        ]
    }
    classification, matches = classify_table(table)
    assert classification == "PII"
    matched_columns = {m["column"] for m in matches}
    assert matched_columns == {"EmailAddress", "PhoneNumber"}


def test_pii_outranks_masked_when_both_present():
    table = {
        "columns": [
            {"name": "CustomerName"},  # matches MASKED (personal name)
            {"name": "NationalID"},  # matches PII
        ]
    }
    classification, _ = classify_table(table)
    assert classification == "PII"


def test_name_only_column_is_masked_not_pii():
    table = {"columns": [{"name": "FirstName"}, {"name": "LastName"}]}
    classification, matches = classify_table(table)
    assert classification == "MASKED"
    assert len(matches) == 2


def test_missing_columns_key_defaults_to_metadata():
    assert classify_table({}) == ("METADATA", [])
