"""Deterministic source-to-target reconciliation checks (master doc §7.3,
§9). Every check here is arithmetic/set comparison — no model call, no
threshold decided by an LLM. Per §9: "reconciliation calculations and
pass/fail thresholds" belong on the deterministic side, because a
migration must not be approved for cutover based on a model's opinion
that the data "looks right."

Six check types, matching §7.3 and the ValidationResult schema in
contracts/metadata_model.json:
  - schema:       column name sets match (or, via check_schema_types,
                   column *type* signatures match — Day 10, Appendix D
                   S-01)
  - row_count:    exact row counts match
  - aggregate:    a numeric column's SUM matches within tolerance
  - null_profile: a column's null count matches within a configured
                   tolerance (Day 10, Appendix D S-06)
  - hash:         the full set of primary-key values matches (catches
                   exactly which rows are missing, not just how many)
  - uniqueness:   a target key column has no duplicate values (Day 10,
                   Appendix D S-05)
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import yaml
import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "metadata_model.json"
TOLERANCES_PATH = REPO_ROOT / "policies" / "reconciliation_tolerances.yaml"


def _validation_result_schema() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["definitions"]["ValidationResult"]


_SCHEMA = _validation_result_schema()


def _validate(record: dict) -> dict:
    jsonschema.validate(instance=record, schema=_SCHEMA)
    return record


def default_null_tolerance() -> int:
    """Reads the null-drift tolerance from policies/reconciliation_tolerances.yaml
    rather than a hardcoded number in code (Appendix D S-06's assertion:
    "tolerance is configured, not hardcoded") — an operator changes the
    allowed drift by editing the YAML, not by editing this module.
    """
    config = yaml.safe_load(TOLERANCES_PATH.read_text(encoding="utf-8"))
    return int(config["null_profile"]["default_tolerance"])


def check_schema(source_columns: list[str], target_columns: list[str]) -> dict:
    source_set = {c.lower() for c in source_columns}
    target_set = {c.lower() for c in target_columns}
    status = "PASS" if source_set == target_set else "FAIL"
    return _validate(
        {
            "check_type": "schema",
            "source_value": sorted(source_set),
            "target_value": sorted(target_set),
            "status": status,
            "detail": {
                "missing_in_target": sorted(source_set - target_set),
                "extra_in_target": sorted(target_set - source_set),
            },
        }
    )


def check_row_count(source_count: int, target_count: int) -> dict:
    status = "PASS" if source_count == target_count else "FAIL"
    return _validate(
        {
            "check_type": "row_count",
            "source_value": source_count,
            "target_value": target_count,
            "status": status,
            "tolerance": 0,
            "detail": {"difference": source_count - target_count},
        }
    )


def check_aggregate(source_sum: float, target_sum: float, tolerance: float = 0.01) -> dict:
    diff = abs(source_sum - target_sum)
    status = "PASS" if diff <= tolerance else "FAIL"
    return _validate(
        {
            "check_type": "aggregate",
            "source_value": source_sum,
            "target_value": target_sum,
            "status": status,
            "tolerance": tolerance,
            "detail": {"difference": diff},
        }
    )


def check_null_profile(source_nulls: int, target_nulls: int, tolerance: int | None = None) -> dict:
    if tolerance is None:
        tolerance = default_null_tolerance()
    diff = abs(source_nulls - target_nulls)
    status = "PASS" if diff <= tolerance else "FAIL"
    return _validate(
        {
            "check_type": "null_profile",
            "source_value": source_nulls,
            "target_value": target_nulls,
            "status": status,
            "tolerance": tolerance,
            "detail": {"difference": source_nulls - target_nulls},
        }
    )


def check_schema_types(source_types: dict[str, str], target_types: dict[str, str]) -> dict:
    """Column *type* signature comparison (Appendix D S-01: "source
    precision change"). check_schema (above) only compares name sets, so
    a column that survives migration under the same name but a narrowed
    type (e.g. DECIMAL(18,2) -> DECIMAL(10,2)) would pass it silently.
    Keys are column names, values are the type signature string as
    reported by the source/target catalog.
    """
    mismatches = {
        col: {"source": source_types[col], "target": target_types[col]}
        for col in source_types.keys() & target_types.keys()
        if source_types[col] != target_types[col]
    }
    status = "PASS" if not mismatches else "FAIL"
    return _validate(
        {
            "check_type": "schema",
            "source_value": sorted(f"{c}:{t}" for c, t in source_types.items()),
            "target_value": sorted(f"{c}:{t}" for c, t in target_types.items()),
            "status": status,
            "detail": {"type_mismatches": mismatches},
        }
    )


def check_hash(source_keys: list[str], target_keys: list[str], max_evidence: int = 25) -> dict:
    """Compares the full keyed row set, not just a count.

    This is what lets the fleet say *which* rows are missing, not merely
    that a count mismatched — the evidence a Lineage/root-cause step
    would need on a later build day.
    """
    source_sorted = sorted(source_keys)
    target_sorted = sorted(target_keys)
    source_hash = hashlib.sha256(",".join(source_sorted).encode()).hexdigest()
    target_hash = hashlib.sha256(",".join(target_sorted).encode()).hexdigest()
    status = "PASS" if source_hash == target_hash else "FAIL"

    missing = sorted(set(source_keys) - set(target_keys))
    extra = sorted(set(target_keys) - set(source_keys))
    return _validate(
        {
            "check_type": "hash",
            "source_value": source_hash,
            "target_value": target_hash,
            "status": status,
            "evidence_hash": source_hash,
            "detail": {
                "missing_in_target": missing[:max_evidence],
                "missing_in_target_count": len(missing),
                "extra_in_target": extra[:max_evidence],
                "extra_in_target_count": len(extra),
            },
        }
    )


def check_uniqueness(target_keys: list[str], max_evidence: int = 25) -> dict:
    """Detects duplicate key values *within the target alone* (Appendix D
    S-05: "Duplicate key in target") — independent of check_hash, which
    compares source vs target sets and would not by itself flag a key
    that legitimately exists in source but was loaded twice into target.
    """
    counts = Counter(target_keys)
    duplicates = sorted(k for k, n in counts.items() if n > 1)
    status = "PASS" if not duplicates else "FAIL"
    return _validate(
        {
            "check_type": "uniqueness",
            "source_value": len(set(target_keys)),
            "target_value": len(target_keys),
            "status": status,
            "detail": {
                "duplicate_keys": duplicates[:max_evidence],
                "duplicate_key_count": len(duplicates),
            },
        }
    )
