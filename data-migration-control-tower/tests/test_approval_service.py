"""Tests for tools/approval_service.py.

Needs live Firestore (the approval record is persisted there), so these
skip automatically when it isn't reachable — same pattern as the other
live-service tests in this suite.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools import approval_service  # noqa: E402


def _firestore_reachable() -> bool:
    # Delegates to the shared probe, which performs a real round trip.
    # This used to call `get_client()` and return True — but the Firestore
    # client is lazy and does no I/O when constructed, so it answered True
    # whenever the import worked, and the skipif below never skipped.
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


@skip_if_no_firestore
def test_consume_fails_without_a_request():
    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    with pytest.raises(PermissionError):
        approval_service.consume(run_id, expected_plan_hash="anything")


@skip_if_no_firestore
def test_approve_fails_without_a_request():
    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    with pytest.raises(ValueError, match="No approval request found"):
        approval_service.approve(run_id, approver_identity="test@example.internal")


@skip_if_no_firestore
def test_full_request_approve_consume_round_trip():
    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    plan_hash = "abc123"

    approval_service.request_approval(run_id, plan_hash, requested_by="cutover-agent")
    with pytest.raises(PermissionError):
        approval_service.consume(run_id, expected_plan_hash=plan_hash)  # not yet approved

    approval_service.approve(run_id, approver_identity="ops-lead@example.internal")
    token = approval_service.consume(run_id, expected_plan_hash=plan_hash)
    assert token["status"] == "APPROVED"
    assert token["token_id"] is not None


@skip_if_no_firestore
def test_consume_rejects_mismatched_plan_hash():
    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    approval_service.request_approval(run_id, "original-hash", requested_by="cutover-agent")
    approval_service.approve(run_id, approver_identity="ops-lead@example.internal")

    with pytest.raises(PermissionError, match="different plan_hash"):
        approval_service.consume(run_id, expected_plan_hash="changed-hash")


@skip_if_no_firestore
def test_double_approve_rejected():
    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    approval_service.request_approval(run_id, "h", requested_by="cutover-agent")
    approval_service.approve(run_id, approver_identity="ops-lead@example.internal")

    with pytest.raises(ValueError, match="not PENDING"):
        approval_service.approve(run_id, approver_identity="someone-else@example.internal")


@skip_if_no_firestore
def test_approve_records_justification():
    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    approval_service.request_approval(run_id, "h", requested_by="cutover-agent")
    token = approval_service.approve(
        run_id, approver_identity="ops-lead@example.internal", justification="scoped, reviewed the plan hash"
    )
    assert token["justification"] == "scoped, reviewed the plan hash"


@skip_if_no_firestore
def test_approval_history_is_append_only():
    """Day 10 hardening: request_approval()/approve() used to only ever
    .set() the same 'current' doc — a second write silently erased the
    prior record. approval_history is the immutable side of the same
    data: one event per call, never overwritten."""
    from tools.firestore_client import get_client

    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    approval_service.request_approval(run_id, "h", requested_by="cutover-agent")
    approval_service.approve(run_id, approver_identity="ops-lead@example.internal", justification="looks good")

    client = get_client()
    history = [
        d.to_dict()
        for d in client.collection("migration_runs")
        .document(run_id)
        .collection("approval_history")
        .stream()
    ]
    events = sorted(h["event"] for h in history)
    assert events == ["APPROVED", "REQUESTED"]
    approved_entry = next(h for h in history if h["event"] == "APPROVED")
    assert approved_entry["justification"] == "looks good"
    assert approved_entry["approved_by"] == "ops-lead@example.internal"


@skip_if_no_firestore
def test_matching_pending_request_is_reused_without_duplicate_history():
    from agents.orchestrator.run_lifecycle import delete_run
    from tools.firestore_client import get_client

    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    try:
        first = approval_service.request_approval(run_id, "same-hash", requested_by="cutover-agent")
        second = approval_service.request_approval(run_id, "same-hash", requested_by="cutover-agent")
        history = list(
            get_client().collection("migration_runs").document(run_id)
            .collection("approval_history").stream()
        )
        assert second == first
        assert len(history) == 1
    finally:
        delete_run(run_id)
