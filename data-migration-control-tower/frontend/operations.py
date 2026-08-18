"""Durable, idempotent operation requests issued by the operator console."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

from google.cloud import firestore

from tools.events import publish
from tools.firestore_client import get_client


def _operation_id(actor: str, kind: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{actor}\0{kind}\0{idempotency_key}".encode()).hexdigest()
    return f"op_{digest[:32]}"


def _validated_key(idempotency_key: str) -> str:
    key = idempotency_key.strip()
    if len(key) < 8 or len(key) > 200:
        raise ValueError("Idempotency-Key must contain between 8 and 200 characters.")
    return key


def get_operation(*, actor: str, kind: str, idempotency_key: str) -> dict | None:
    """Return an existing request for a repeated write without side effects."""
    operation_id = _operation_id(actor, kind, _validated_key(idempotency_key))
    snapshot = get_client().collection("operation_requests").document(operation_id).get()
    if not snapshot.exists:
        return None
    return {**(snapshot.to_dict() or {}), "idempotent_replay": True}


def queue_operation(
    *,
    actor: str,
    roles: list[str],
    kind: str,
    idempotency_key: str,
    justification: str,
    topic: str,
    event: dict,
) -> dict:
    """Creates one operation request and publishes its command event.

    The Firestore document is the durable system of record. Reusing the same
    Idempotency-Key returns that record without publishing a second command.
    """
    key = _validated_key(idempotency_key)

    client = get_client()
    operation_id = _operation_id(actor, kind, key)
    operation_ref = client.collection("operation_requests").document(operation_id)
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    @firestore.transactional
    def _create(transaction: firestore.Transaction) -> tuple[dict, bool]:
        snapshot = operation_ref.get(transaction=transaction)
        if snapshot.exists:
            return snapshot.to_dict(), False
        record = {
            "operation_id": operation_id,
            "kind": kind,
            "status": "queued",
            "actor": actor,
            "roles": sorted(roles),
            "justification": justification,
            "topic": topic,
            "event": event,
            # Kept at the top level so authorization, progress polling and
            # cleanup never need to interpret a command-specific payload.
            "estate_id": event.get("estate_id"),
            "run_id": event.get("run_id"),
            "created_at": now,
            "updated_at": now,
        }
        transaction.set(operation_ref, record)
        return record, True

    record, created = _create(client.transaction())
    if not created:
        return {**record, "idempotent_replay": True}

    audit_ref = client.collection("operation_audit").document(str(uuid.uuid4()))
    audit_ref.set(
        {
            "operation_id": operation_id,
            "kind": kind,
            "actor": actor,
            "justification": justification,
            "event": "requested",
            "recorded_at": now,
        }
    )

    try:
        message_id = publish(topic, {**event, "operation_id": operation_id, "requested_by": actor})
    except Exception as exc:
        operation_ref.update(
            {
                "status": "publish_failed",
                "error": str(exc),
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )
        raise

    published_at = dt.datetime.now(dt.timezone.utc).isoformat()
    operation_ref.update({"status": "published", "message_id": message_id, "updated_at": published_at})
    return {**record, "status": "published", "message_id": message_id, "updated_at": published_at}


def record_wave_override(
    *, source_id: str, state: str, actor: str, justification: str, expires_at: str | None
) -> dict:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    record = {
        "source_id": source_id,
        "state": state,
        "actor": actor,
        "justification": justification,
        "expires_at": expires_at,
        "updated_at": now,
    }
    client = get_client()
    client.collection("wave_overrides").document(source_id).set(record)
    client.collection("operation_audit").document(str(uuid.uuid4())).set(
        {**record, "kind": "wave.override", "event": "applied", "recorded_at": now}
    )
    return record
