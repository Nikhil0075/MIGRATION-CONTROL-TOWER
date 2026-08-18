"""Tests for tools/reconciliation.py — pure functions, no live services needed."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.reconciliation import (  # noqa: E402
    check_aggregate,
    check_hash,
    check_null_profile,
    check_row_count,
    check_schema,
    check_schema_types,
    check_uniqueness,
    default_null_tolerance,
)


def test_schema_check_passes_on_identical_column_sets():
    result = check_schema(["A", "B", "C"], ["a", "b", "c"])  # case-insensitive
    assert result["status"] == "PASS"


def test_schema_check_fails_and_reports_missing_column():
    result = check_schema(["A", "B", "C"], ["A", "B"])
    assert result["status"] == "FAIL"
    assert result["detail"]["missing_in_target"] == ["c"]


def test_row_count_check_passes_on_exact_match():
    assert check_row_count(663, 663)["status"] == "PASS"


def test_row_count_check_fails_on_seeded_row_loss():
    result = check_row_count(663, 656)
    assert result["status"] == "FAIL"
    assert result["detail"]["difference"] == 7


def test_aggregate_check_passes_within_tolerance():
    assert check_aggregate(1000.001, 1000.0, tolerance=0.01)["status"] == "PASS"


def test_aggregate_check_fails_outside_tolerance():
    assert check_aggregate(1000.0, 950.0, tolerance=0.01)["status"] == "FAIL"


def test_null_profile_check():
    assert check_null_profile(5, 5)["status"] == "PASS"
    # Day 10: null_profile now allows a configured tolerance (Appendix D
    # S-06) instead of requiring an exact match — assert the boundary
    # explicitly rather than assuming tolerance=0.
    assert check_null_profile(5, 3, tolerance=0)["status"] == "FAIL"
    assert check_null_profile(5, 3)["status"] == "PASS"  # within the configured default tolerance
    assert check_null_profile(5, 0)["status"] == "FAIL"  # far beyond any reasonable tolerance


def test_hash_check_passes_on_identical_key_sets():
    keys = ["1", "2", "3", "4"]
    assert check_hash(keys, list(reversed(keys)))["status"] == "PASS"  # order-independent


def test_hash_check_fails_and_names_missing_keys():
    source_keys = [str(i) for i in range(1, 11)]
    target_keys = [str(i) for i in range(1, 8)]  # keys 8, 9, 10 missing
    result = check_hash(source_keys, target_keys)
    assert result["status"] == "FAIL"
    assert result["detail"]["missing_in_target"] == ["10", "8", "9"]  # string sort
    assert result["detail"]["missing_in_target_count"] == 3


# --- Day 10 additions (master doc Appendix D) ---------------------------


def test_default_null_tolerance_is_configured_not_hardcoded():
    tolerance = default_null_tolerance()
    assert isinstance(tolerance, int)
    assert tolerance > 0


def test_schema_types_check_passes_on_identical_types():
    types = {"CustomerID": "int", "CreditLimit": "decimal(18,2)"}
    result = check_schema_types(types, dict(types))
    assert result["status"] == "PASS"
    assert result["detail"]["type_mismatches"] == {}


def test_schema_types_check_fails_on_narrowed_precision():
    source = {"CustomerID": "int", "CreditLimit": "decimal(18,2)"}
    target = {"CustomerID": "int", "CreditLimit": "decimal(10,2)"}
    result = check_schema_types(source, target)
    assert result["status"] == "FAIL"
    assert result["detail"]["type_mismatches"] == {
        "CreditLimit": {"source": "decimal(18,2)", "target": "decimal(10,2)"}
    }


def test_uniqueness_check_passes_on_distinct_keys():
    assert check_uniqueness(["1", "2", "3"])["status"] == "PASS"


def test_uniqueness_check_fails_and_names_duplicate_keys():
    result = check_uniqueness(["1", "2", "2", "3", "3", "3"])
    assert result["status"] == "FAIL"
    assert result["detail"]["duplicate_keys"] == ["2", "3"]
    assert result["detail"]["duplicate_key_count"] == 2
