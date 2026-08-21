"""Generic atomic claim/complete idempotency, factored out of the pattern
`agents/orchestrator/orchestrator.py::_dedup_claim()`/`_dedup_complete()`
proved out for Pub/Sub message redelivery (Deploy & Harden Phase 2c).

That pair is keyed specifically to `payload["_pubsub_message_id"]` and
lives in orchestrator.py; this module is the same claimed/done/stale_claim
transaction shape, generalized to any (collection, key) pair, so
tools/capability_http_server.py can dedupe by `invocation_id` (an HTTP
capability call, not a Pub/Sub message) without depending on orchestrator
internals or duplicating the transaction logic a second time by hand.

Left orchestrator.py's own functions unchanged rather than refactoring
them onto this — that refactor is real but separate work with its own
regression risk, not bundled into this phase.
"""

from __future__ import annotations

import datetime as dt

from google.cloud import firestore

from tools.firestore_client import get_client


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def claim(collection: str, key: str, *, actor: str) -> tuple[str, dict | None]:
    """Atomically claims `key` for `actor` under `collection`.

    Returns one of:
      ("done", cached_result) — already completed; caller must return
        cached_result as-is, doing no work.
      ("claimed", None) — claimed for the first time; caller does the
        work, then calls complete().
      ("stale_claim", None) — a prior claim exists but was never
        completed (a crash mid-handler). Caller should redo the work;
        safe only if the caller's own side effects are themselves safe
        to repeat — the same precondition orchestrator.py's _dedup_claim
        documents for its handlers.

    Same atomicity guarantee as orchestrator.py's version: the existence
    check and the claim write happen inside one transaction, so two
    callers racing on the same key cannot both observe "not yet claimed."
    """
    doc_ref = get_client().collection(collection).document(key)

    @firestore.transactional
    def _txn(transaction: firestore.Transaction) -> tuple[str, dict | None]:
        snapshot = doc_ref.get(transaction=transaction)
        now = _now()
        if not snapshot.exists:
            transaction.set(doc_ref, {"actor": actor, "status": "claimed", "claimed_at": now})
            return "claimed", None
        data = snapshot.to_dict() or {}
        if data.get("status") == "done":
            return "done", {**(data.get("result") or {}), "deduped": True}
        transaction.set(doc_ref, {"actor": actor, "status": "claimed", "claimed_at": now})
        return "stale_claim", None

    return _txn(get_client().transaction())


def complete(collection: str, key: str, *, actor: str, result: dict) -> None:
    """Marks `key` done, with `result` cached for a future duplicate call
    to return without redoing the work."""
    get_client().collection(collection).document(key).set(
        {"actor": actor, "status": "done", "result": result, "completed_at": _now()}, merge=True
    )
