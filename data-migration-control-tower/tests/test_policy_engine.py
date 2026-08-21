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

from tools.invocation_context import InvocationContext  # noqa: E402
from tools.policy_engine import (  # noqa: E402
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
    PolicyDenied,
    _decide,
    _load_permissions,
    authorize,
    evaluate,
)

DISCOVERY_POLICY = _load_permissions()["discovery"]
CUTOVER_POLICY = _load_permissions()["cutover"]
RISK_POLICY = _load_permissions()["risk"]


# -- Capability-dispatch gate coverage (Deploy & Harden Phase 1a, ADR 0001) --
# Every capability tools/registry.py::invoke_capability() can resolve to a
# real handler must have a matching "capability:<string>" allow entry, or
# the outer gate would deny every real migration run the moment it went
# live. Pure/no-Firestore — checks the declared policy file directly.

_CAPABILITY_BY_PERMISSIONS_KEY = {
    "discovery": "discovery.catalog.estate",
    "lineage": "lineage.graph.build",
    "risk": "risk.assess.estate",
    "planner": "planner.plan.propose",
    "validation": "validation.reconcile.source_target",
    "cutover": "cutover.request_approval",
    "finance": "impact.assessment.finance_reporting",
}


@pytest.mark.parametrize("permissions_key,capability", _CAPABILITY_BY_PERMISSIONS_KEY.items())
def test_every_seeded_agent_capability_passes_the_dispatch_gate(permissions_key, capability):
    policy = _load_permissions()[permissions_key]
    decision, _ = _decide(policy, f"capability:{capability}", "METADATA")
    assert decision == DECISION_ALLOW


def test_an_unrelated_capability_string_is_denied():
    # Sanity check that the allow entries above are specific, not a
    # blanket "capability:*" that would make the gate a no-op.
    decision, _ = _decide(DISCOVERY_POLICY, "capability:cutover.request_approval", "METADATA")
    assert decision == DECISION_DENY


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


# -- authorize() / InvocationContext (Deploy & Harden Phase 1b, ADR 0001) ---


@pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")
def test_authorize_allows_a_legitimately_scoped_tool_call():
    ctx = InvocationContext(
        agent_id="discovery",
        action="source.catalog.sql_server",
        resource_class="METADATA",
    )
    record = authorize(ctx)
    assert record["decision"] == DECISION_ALLOW


@pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")
def test_authorize_raises_policy_denied_on_deny():
    ctx = InvocationContext(
        agent_id="discovery",
        action="source.raw_pii_read",
        resource_class="PII",
    )
    with pytest.raises(PolicyDenied) as excinfo:
        authorize(ctx)
    assert excinfo.value.record["decision"] == DECISION_DENY


@pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")
def test_authorize_fails_closed_when_resource_class_missing():
    # Constructing InvocationContext with an empty resource_class must not
    # silently fall back to METADATA — it must deny before evaluate() is
    # even consulted (the whole point of the fail-closed rule).
    ctx = InvocationContext(
        agent_id="discovery",
        action="source.catalog.sql_server",
        resource_class="",
    )
    with pytest.raises(PolicyDenied) as excinfo:
        authorize(ctx)
    assert excinfo.value.record["resource_class"] is None
    assert "fail closed" in excinfo.value.record["reason"]


@pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")
def test_authorize_raises_policy_denied_on_require_approval():
    ctx = InvocationContext(
        agent_id="cutover",
        action="cutover.execute.approved",
        resource_class="PRODUCTION",
    )
    with pytest.raises(PolicyDenied) as excinfo:
        authorize(ctx)
    assert excinfo.value.record["decision"] == DECISION_REQUIRE_APPROVAL
