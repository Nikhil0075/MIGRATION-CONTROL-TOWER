"""Append-only, sanitized audit events for model and deterministic agent work.

The public console intentionally exposes final rationale summaries and evidence,
not private chain-of-thought.  Every event written here is safe for an operator
with access to the run's estate to inspect.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from typing import Any

from tools.firestore_client import get_client

COLLECTION = "agent_execution_events"
_SECRET_KEY = re.compile(r"password|secret|token|credential|connection_string|authorization", re.I)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded, JSON-safe value with credential-shaped keys removed."""
    if depth > 7:
        return "[depth-limited]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SECRET_KEY.search(str(key)) else sanitize(item, depth=depth + 1)
            for key, item in list(value.items())[:250]
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item, depth=depth + 1) for item in list(value)[:250]]
    if isinstance(value, str):
        return value[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4000]


def evidence_hash(value: Any) -> str:
    encoded = json.dumps(sanitize(value), sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def append(run_id: str, event: dict) -> dict:
    event_id = event.get("event_id") or str(uuid.uuid4())
    record = {
        **sanitize(event),
        "event_id": event_id,
        "run_id": run_id,
        "recorded_at": event.get("recorded_at") or now(),
        "audit_schema_version": "1.0",
    }
    ref = get_client().collection("migration_runs").document(run_id).collection(COLLECTION).document(event_id)
    try:
        ref.create(record)
    except Exception as exc:
        # Stable ids make deterministic handler replays audit-idempotent.
        # Do not broadly swallow storage errors: only an already-created
        # document is a successful append from a prior delivery.
        if type(exc).__name__ != "AlreadyExists":
            raise
        existing = ref.get()
        return {"event_id": existing.id, **(existing.to_dict() or record)}
    return record


def append_deterministic(
    run_id: str,
    *,
    agent_id: str,
    capability: str,
    stage: str,
    output_summary: str,
    evidence_refs: list[str] | None = None,
    tool_calls: list[dict] | None = None,
    generated_output: Any = None,
    trace_id: str | None = None,
    agent_version: str | None = None,
    idempotency_ref: str | None = None,
) -> dict:
    """Record a non-model step without implying that Gemini was invoked."""
    at = now()
    event_id = None
    if idempotency_ref:
        digest = hashlib.sha256(f"{run_id}|{agent_id}|{capability}|{stage}|{idempotency_ref}".encode()).hexdigest()
        event_id = f"det-{digest[:40]}"
    return append(run_id, {
        "event_id": event_id,
        "agent_id": agent_id,
        "agent_version": agent_version,
        "capability": capability,
        "stage": stage,
        "framework": "deterministic-python",
        "model": None,
        "thinking_level": None,
        "status": "COMPLETED",
        "started_at": at,
        "completed_at": at,
        "duration_ms": 0,
        "evidence_refs": evidence_refs or [],
        "tool_calls": tool_calls or [],
        "generated_output": generated_output,
        "output_summary": output_summary,
        "fallback_used": False,
        "validation_status": "DETERMINISTIC",
        "trace_id": trace_id,
        "attempt": 1,
    })
