"""Deterministic column-level data classifier (master doc §5.2, §9).

Applies policies/data_classification.yaml's regex rules against a Table
record's column names. No LLM call — per §9's architecture rule, "PII
classification rules that must never be bypassed" belong on the
deterministic side. A table's description, comments, or column metadata
are treated purely as data to pattern-match against, never as
instructions (§7.2's "malicious instruction in metadata" fault class).

Used by the Risk & Compliance Agent (agents/risk/agent.py) to turn
Discovery's placeholder classification='UNCLASSIFIED' into a real
classification.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "policies" / "data_classification.yaml"

# Severity order, most sensitive first — a table's overall classification
# is the most sensitive classification found across any of its columns.
_SEVERITY = ["PII", "MASKED", "METADATA"]


@lru_cache(maxsize=1)
def _load_rules() -> dict:
    with open(RULES_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def classify_table(table_record: dict) -> tuple[str, list[dict]]:
    """Returns (classification, matches) for a Table record.

    `matches` is a list of {"column": ..., "classification": ..., "label": ...}
    for every column that matched a rule — used as RiskFinding evidence.
    """
    config = _load_rules()
    rules = config["rules"]
    default = config.get("default_classification", "METADATA")

    matches: list[dict] = []
    for column in table_record.get("columns", []):
        name = column.get("name", "")
        for rule in rules:
            if re.search(rule["pattern"], name, re.IGNORECASE):
                matches.append(
                    {
                        "column": name,
                        "classification": rule["classification"],
                        "label": rule["label"],
                    }
                )
                break  # first matching rule wins for this column

    if not matches:
        return default, matches

    best = min(
        matches,
        key=lambda m: _SEVERITY.index(m["classification"])
        if m["classification"] in _SEVERITY
        else len(_SEVERITY),
    )
    return best["classification"], matches
