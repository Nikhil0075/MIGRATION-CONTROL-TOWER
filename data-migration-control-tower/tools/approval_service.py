"""Human approval service (master doc §8.1's event table: "cutover.approved
| Human approval service | Cutover agent"; §5.2, §21.1).

Deliberately its own module with its own identity ("human-approval-
service"), never imported by agents/cutover/agent.py's *approve* path —
only Cutover's *request* and *consume* sides touch it. `approve()` is
called by a separate script (agents/cutover/approve_cutover.py)
representing an actual human/ops action, never by agent code. This is
the literal implementation of §5.2's rule: "Production write/cutover
requires a human approval token that the Cutover Agent cannot issue to
itself."

Token shape follows §21.1's run-record example: single-use, bound to
run_id + plan_hash (a changed plan invalidates the approval), with an
explicit expiry.

Day 10 hardening: `request_approval()`/`approve()` used to only ever
`.set()` the same `approval/current` doc — overwrite, not append. A
second write (e.g. a re-request after a plan change) silently erased the
prior record with nothing left to dispute later. `approval_history` below
is the append-only side of that same data: one auto-ID doc per event,
never updated or deleted, so `current` stays the convenient
"what's true now" read while `approval_history` is the immutable trail.
"""

from __future__ import annotations

import datetime as dt
import uuid

from tools.firestore_client import get_client

RUN_COLLECTION = "migration_runs"
DEFAULT_EXPIRES_AFTER_DAYS = 30


def _record_history(run_id: str, event: str, record: dict) -> None:
    client = get_client()
    client.collection(RUN_COLLECTION).document(run_id).collection("approval_history").add(
        {"event": event, "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(), **record}
    )


def request_approval(run_id: str, plan_hash: str, requested_by: str) -> dict:
    """Called by the Cutover Agent: opens a PENDING approval request."""
    current = get_approval(run_id)
    if (
        current
        and current.get("status") == "PENDING"
        and current.get("plan_hash") == plan_hash
        and current.get("requested_by") == requested_by
    ):
        # Pub/Sub redelivery must not overwrite requested_at or append a
        # second audit event for the same logical approval request.
        return current
    record = {
        "status": "PENDING",
        "plan_hash": plan_hash,
        "requested_by": requested_by,
        "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "token_id": None,
        "approved_by": None,
        "approved_at": None,
        "justification": None,
        "expires_after_days": DEFAULT_EXPIRES_AFTER_DAYS,
    }
    client = get_client()
    client.collection(RUN_COLLECTION).document(run_id).collection("approval").document(
        "current"
    ).set(record)
    _record_history(run_id, "REQUESTED", record)
    return record


def get_approval(run_id: str) -> dict | None:
    client = get_client()
    doc = (
        client.collection(RUN_COLLECTION)
        .document(run_id)
        .collection("approval")
        .document("current")
        .get()
    )
    return doc.to_dict() if doc.exists else None


def approve(run_id: str, approver_identity: str, justification: str | None = None) -> dict:
    """Issues the approval token. NEVER called by agent code — this
    function IS the human-in-the-loop step, invoked by
    agents/cutover/approve_cutover.py on a human's behalf, or by
    frontend/app.py's approve endpoint after verifying a real Firebase ID
    token (approver_identity comes from the verified token's email claim
    there, never from client-supplied request data — Day 10 hardening).
    """
    current = get_approval(run_id)
    if current is None:
        raise ValueError(f"No approval request found for run {run_id!r} — Cutover must request first.")
    if current["status"] != "PENDING":
        raise ValueError(f"Approval for run {run_id!r} is not PENDING (status={current['status']!r}).")

    updated = {
        **current,
        "status": "APPROVED",
        "token_id": str(uuid.uuid4()),
        "approved_by": approver_identity,
        "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "justification": justification,
    }
    client = get_client()
    client.collection(RUN_COLLECTION).document(run_id).collection("approval").document(
        "current"
    ).set(updated)
    _record_history(run_id, "APPROVED", updated)
    return updated


def consume(run_id: str, expected_plan_hash: str) -> dict:
    """Called by the Cutover Agent before performing cutover. Verifies
    the token is present, APPROVED, and bound to the exact current plan —
    raises PermissionError otherwise. This IS the real gate; the policy
    engine's REQUIRE_APPROVAL decision (recorded separately) is a static
    declaration that approval is needed, not the enforcement itself.
    """
    current = get_approval(run_id)
    if current is None or current["status"] != "APPROVED" or not current.get("token_id"):
        raise PermissionError(f"No valid approval token for run {run_id!r} — cutover refused.")
    if current["plan_hash"] != expected_plan_hash:
        raise PermissionError(
            f"Approval token for run {run_id!r} is bound to a different plan_hash "
            "(the plan changed after approval was issued) — cutover refused."
        )
    return current
