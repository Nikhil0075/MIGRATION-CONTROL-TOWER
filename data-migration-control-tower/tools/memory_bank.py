"""Memory Bank (Block C, master doc §5.3, §21.1, §21.2 proof 3).

    Store reusable migration knowledge, not arbitrary chat history.
    [...] When a future run has the same pattern, the fleet retrieves
    the prior incident as evidence and proposes the known remediation,
    while still running deterministic validation.

Deliberately the §19 Rung-3 shape: a Firestore keyed lookup on a
normalized incident signature, not vector similarity retrieval (Rung 2)
or the full Agent Platform Memory Bank (Rung 1) — no embedding
infrastructure exists in this project, so claiming semantic recall would
be unverifiable. A signature like 'row_loss:Sales.Customers' is exactly
the kind of normalized key §21.1 describes, and it's honest about what
it does: exact-match recall, not fuzzy similarity.

Global collection (`memory_bank`), NOT under migration_runs/{run_id} —
§21.1's session-vs-memory distinction is enforced structurally, not just
by convention: session data (catalog, risk_findings, reconciliation,
incidents, ...) is run-scoped and discarded with the run; memory here is
cross-run and holds only the durable fact (signature, root_cause, fix),
never a full incident dump or anything resembling a chat transcript.

recall() is a pure read — it does not mutate reuse tracking. Callers
that actually cite a recalled fact as evidence call mark_recalled()
explicitly, so the "this was reused" audit trail only reflects genuine
reuse, not every lookup attempt (including ones that found nothing).
"""

from __future__ import annotations

import datetime as dt
import re

from tools.firestore_client import get_client

COLLECTION = "memory_bank"


def _safe_doc_id(signature: str) -> str:
    return re.sub(r"[/\s]+", "_", signature)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def record(signature: str, root_cause: str, fix: str, source_run_id: str) -> dict:
    """Upserts a durable remediation fact after an Incident is confirmed RESOLVED.

    First call creates the fact; subsequent calls for the same signature
    confirm/reinforce it (reuse_count increments, source_run_ids grows) —
    this is what makes a recalled fact something more than an unverified
    guess by the time it's recalled a second time.
    """
    client = get_client()
    doc_ref = client.collection(COLLECTION).document(_safe_doc_id(signature))
    existing = doc_ref.get()

    if existing.exists:
        data = existing.to_dict()
        source_run_ids = data.get("source_run_ids", [])
        if source_run_id not in source_run_ids:
            source_run_ids.append(source_run_id)
        updated = {
            **data,
            "root_cause": root_cause,
            "fix": fix,
            "source_run_ids": source_run_ids,
            "reuse_count": data.get("reuse_count", 0) + 1,
            "last_confirmed_at": _now(),
        }
    else:
        updated = {
            "signature": signature,
            "root_cause": root_cause,
            "fix": fix,
            "source_run_ids": [source_run_id],
            "reuse_count": 0,
            "recalled_by_run_ids": [],
            "created_at": _now(),
            "last_confirmed_at": _now(),
        }

    doc_ref.set(updated)
    return updated


def recall(signature: str) -> dict | None:
    """Pure read: returns the memory fact for signature, or None."""
    client = get_client()
    doc = client.collection(COLLECTION).document(_safe_doc_id(signature)).get()
    return doc.to_dict() if doc.exists else None


def mark_recalled(signature: str, run_id: str) -> None:
    """Records that `run_id` actually used this fact as evidence
    (called by the recovery loop, not by recall() itself)."""
    client = get_client()
    doc_ref = client.collection(COLLECTION).document(_safe_doc_id(signature))
    existing = doc_ref.get()
    if not existing.exists:
        return
    data = existing.to_dict()
    recalled_by = data.get("recalled_by_run_ids", [])
    if run_id not in recalled_by:
        recalled_by.append(run_id)
    doc_ref.update({"recalled_by_run_ids": recalled_by})


def list_facts() -> list[dict]:
    client = get_client()
    return [d.to_dict() for d in client.collection(COLLECTION).stream()]
