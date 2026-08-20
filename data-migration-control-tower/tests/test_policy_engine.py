"""Tests for tools/policy_engine.py.

_decide() is pure (no Firestore) and is the actual ALLOW/DENY/
REQUIRE_APPROVAL logic — tested directly and exhaustively here. evaluate()
additionally records to Firestore; one live-service test covers that,
skipped automatically when Firestore isn't reachable (same pattern as
tests/test_source_catalog.py's SQL Server tests).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.policy_engine import (  # noqa: E402
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
    _decide,
    _load_permissions,
    evaluate,
)

DISCOVERY_POLICY = _load_permissions()["discovery"]
CUTOVER_POLICY = _load_permissions()["cutover"]
RISK_POLICY = _load_permissions()["risk"]


def test_explicitly_allowed_action_is_allowed():
    decision, _ = _decide(DISCOVERY_POLICY, "source.catalog.sql_server", "METADATA")
    assert decision == DECISION_ALLOW


def test_explicitly_denied_action_is_denied():
    decision, _ = _decide(DISCOVERY_POLICY, "source.raw_pii_read", "PII")
    assert decision == DECISION_DENY


def test_unlisted_action_defaults_to_deny():
    decision, _ = _decide(DISCOVERY_POLICY, "some.made.up.action", "METADATA")
    assert decision == DECISION_DENY


def test_resource_class_ceiling_denies_even_if_action_unlisted():
    # 'risk' declares data_classes [METADATA, MASKED] — PII exceeds the ceiling.
    decision, reason = _decide(RISK_POLICY, "schema.metadata.read", "PII")
    assert decision == DECISION_DENY
    assert "ceiling" in reason


def test_approval_required_action_wins_over_allowed():
    # cutover.execute.approved is in BOTH allowed_tools and
    # approval_required_actions — approval gating must win.
    decision, _ = _decide(CUTOVER_POLICY, "cutover.execute.approved", "PRODUCTION")
    assert decision == DECISION_REQUIRE_APPROVAL


def test_cutover_cannot_self_approve():
    decision, _ = _decide(CUTOVER_POLICY, "approval.self_issue", "PRODUCTION")
    assert decision == DECISION_DENY


def _firestore_reachable() -> bool:
    # Delegates to the shared probe, which performs a real round trip.
    # This used to call `get_client()` and return True — but the Firestore
    # client is lazy and does no I/O when constructed, so it answered True
    # whenever the import worked, and the skipif below never skipped.
    from tests.probes import firestore_reachable

    return firestore_reachable()


@pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")
def test_evaluate_records_and_returns_denial():
    record = evaluate("discovery", "source.raw_pii_read", "PII")
    assert record["decision"] == DECISION_DENY
    assert record["policy_id"] == "POL-SOURCE.RAW_PII_READ-DISCOVERY"


@pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")
def test_evaluate_unknown_agent_denies():
    record = evaluate("no-such-agent", "anything", "METADATA")
    assert record["decision"] == DECISION_DENY
