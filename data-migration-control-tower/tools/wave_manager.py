"""Deterministic wave/backpressure scheduler (Day 10 Phase 4, master doc
§32.12's scale model — "build the pattern, not the scale").

Same discipline as tools/policy_engine.py: a scheduling decision is
arithmetic/comparison over declared limits (policies/wave_limits.yaml),
never a model's judgment call about what's safe to run concurrently. A
"wave" here is the set of pending items admitted to run right now, given
already-running items and the declared concurrency caps — per-source
transfer limits, a global CRITICAL-risk concurrency cap, backlog-age
escalation, and an optional approval-time-window gate.

Demonstrated at this project's own small scale directly and at a
bounded synthetic scale (evaluation/scale_harness.py, 100-500 defs) —
not the master doc's 20,000-definition claim, which needs a live estate
that large to prove honestly and is explicitly out of this phase's
scope (see the Day 10 hardening plan).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from functools import lru_cache
from pathlib import Path

import yaml
from google.cloud import firestore

from tools.firestore_client import get_client

REPO_ROOT = Path(__file__).resolve().parents[1]
LIMITS_PATH = REPO_ROOT / "policies" / "wave_limits.yaml"

DECISION_ADMIT = "ADMIT"
DECISION_HOLD = "HOLD"

# Real dispatch (agents/orchestrator/orchestrator.py::handle_planned) needs
# an atomically-updated state doc — evaluate_wave() below is a pure
# function over caller-supplied pending/running lists, fine for simulation
# and the scale harness, but two concurrently-processed runs calling it
# independently share no mutable state and could both slip past the same
# cap. reserve_slot()/release_slot() are the transactional counterpart
# that closes that race for real dispatch.
#
# One document PER ESTATE (Day 11 Phase 4), not one globally. The single
# `wave_state/slots` document this replaced was a shared arena across
# every estate, so a second onboarded customer's runs would have been
# held by the first customer's load.
WAVE_STATE_COLLECTION = "wave_state"

#: Pre-Phase-4 document id, kept only so a deployment upgrading in place
#: can find and delete the orphaned global document. Nothing reads it.
LEGACY_WAVE_STATE_DOC = "slots"


@lru_cache(maxsize=1)
def _load_limits() -> dict:
    return yaml.safe_load(LIMITS_PATH.read_text(encoding="utf-8"))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def backlog_minutes(requested_at: str, now: dt.datetime | None = None) -> float:
    requested = dt.datetime.fromisoformat(requested_at)
    return ((now or _now()) - requested).total_seconds() / 60.0


def within_approval_window(now: dt.datetime | None = None) -> bool:
    """True if `now` (default: real current time) falls inside
    policies/wave_limits.yaml's approval_window — always True while
    that window is disabled (this project's own demo/evaluation runs
    must not be blocked by wall-clock time; a real deployment would
    enable it with real business hours)."""
    window = _load_limits().get("approval_window", {})
    if not window.get("enabled", False):
        return True
    hour = (now or _now()).hour
    start, end = window["start_hour_utc"], window["end_hour_utc"]
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end  # window wraps past midnight UTC


def evaluate_wave(pending: list[dict], running: list[dict], now: dt.datetime | None = None) -> list[dict]:
    """Simulates one full wave-admission pass over `pending` items,
    given `running` items already in flight.

    Each pending item: {"item_id", "source_id", "risk_class"
    ("CRITICAL"|"HIGH"|"MEDIUM"|"LOW", optional), "requested_at" (ISO
    timestamp)}. Each running item: {"item_id", "source_id", "risk_class"}.

    Returns one decision dict per pending item — {"item_id", "decision":
    ADMIT|HOLD, "reason", "backlog_minutes", "escalated"} — in the order
    items were actually evaluated (backlog-escalated items first, then
    oldest-first), NOT the caller's input order; an item admitted here
    counts against the concurrency caps every later item in the same
    call is checked against, exactly as a real scheduler processing one
    wave would.
    """
    limits = _load_limits()
    per_source_caps = limits.get("max_concurrent_per_source", {})
    default_cap = per_source_caps.get("default", 1)
    critical_cap = limits.get("max_concurrent_critical", 1)
    escalation_minutes = limits.get("backlog_age_escalation_minutes", 30)
    now = now or _now()

    running_by_source = Counter(r["source_id"] for r in running)
    running_critical = sum(1 for r in running if r.get("risk_class") == "CRITICAL")

    annotated = []
    for item in pending:
        backlog = backlog_minutes(item["requested_at"], now=now)
        escalated = backlog >= escalation_minutes
        annotated.append((item, backlog, escalated))
    # Escalated items first; within each group, oldest (largest backlog) first.
    annotated.sort(key=lambda t: (not t[2], -t[1]))

    decisions = []
    for item, backlog, escalated in annotated:
        source_id = item["source_id"]
        risk_class = item.get("risk_class")
        cap = per_source_caps.get(source_id, default_cap)
        source_count = running_by_source[source_id]

        if source_count >= cap:
            decisions.append(
                {
                    "item_id": item["item_id"],
                    "decision": DECISION_HOLD,
                    "reason": f"source {source_id!r} at concurrency cap ({cap})",
                    "backlog_minutes": round(backlog, 2),
                    "escalated": escalated,
                }
            )
            continue

        if risk_class == "CRITICAL" and running_critical >= critical_cap:
            decisions.append(
                {
                    "item_id": item["item_id"],
                    "decision": DECISION_HOLD,
                    "reason": f"CRITICAL-risk concurrency cap ({critical_cap}) reached",
                    "backlog_minutes": round(backlog, 2),
                    "escalated": escalated,
                }
            )
            continue

        decisions.append(
            {
                "item_id": item["item_id"],
                "decision": DECISION_ADMIT,
                "reason": "within concurrency caps",
                "backlog_minutes": round(backlog, 2),
                "escalated": escalated,
            }
        )
        running_by_source[source_id] += 1
        if risk_class == "CRITICAL":
            running_critical += 1

    return decisions


def estate_of(wave_key: str) -> str:
    """The estate a wave key belongs to.

    Wave keys are "{estate_id}:{source_id}" since Day 11 Phase 3b. A bare
    source id — used by the standalone scripts and by callers that predate
    estate scoping — resolves to the demo estate, the same "missing
    estate_id means the default estate" rule applied to run documents.
    """
    from tools.connection_context import DEFAULT_ESTATE_ID

    return wave_key.split(":", 1)[0] if ":" in wave_key else DEFAULT_ESTATE_ID


def _slot_doc_ref(wave_key: str):
    """Concurrency state for one estate.

    Day 11 Phase 4 split this from a single global `wave_state/slots`
    document. That one document was a shared arena: every estate's running
    items counted against the same map, so onboarding a second customer
    would have made their runs contend with — and be held by — the first
    customer's load. Partitioning by estate is what makes the per-source
    caps in policies/wave_limits.yaml mean anything once more than one
    estate exists.
    """
    return get_client().collection(WAVE_STATE_COLLECTION).document(estate_of(wave_key))


def _active_override(source_id: str) -> dict | None:
    """Operator HOLD/OPEN for a wave key, most specific first.

    Same cascade as _cap_for, and for the same reason: reservations are
    keyed "{estate_id}:{source_id}" but an override placed through
    PUT /api/v1/waves/{source_id}/override names the bare source. Without
    the fallback an operator HOLD would appear to apply and silently do
    nothing — the worst possible failure for a safety control.
    """
    client = get_client()
    snapshot = client.collection("wave_overrides").document(source_id).get()
    if not snapshot.exists and ":" in source_id:
        snapshot = (
            client.collection("wave_overrides").document(source_id.split(":", 1)[1]).get()
        )
    if not snapshot.exists:
        return None
    override = snapshot.to_dict()
    expires_at = override.get("expires_at")
    if expires_at:
        try:
            parsed = dt.datetime.fromisoformat(expires_at)
            if parsed.tzinfo is None or parsed <= _now():
                return None
        except (TypeError, ValueError):
            return None
    return override


def _wave_event(event: str, source_id: str, item_id: str, **detail) -> None:
    get_client().collection("wave_events").document(str(uuid.uuid4())).set(
        {
            "event": event,
            "source_id": source_id,
            "item_id": item_id,
            "recorded_at": _now().isoformat(),
            **detail,
        }
    )


def _cap_for(source_id: str, per_source_caps: dict) -> int:
    """Concurrency cap for a wave key, most specific first.

    Day 11 Phase 3b keys reservations by "{estate_id}:{source_id}" so two
    estates using the same source_id do not contend for one another's
    slots. The cascade — exact estate:source, then bare source, then
    default — is what keeps policies/wave_limits.yaml's existing
    source-only keys working as the middle tier rather than silently
    falling through to the default of 1 and throttling the demo path.
    """
    if source_id in per_source_caps:
        return per_source_caps[source_id]
    if ":" in source_id:
        bare_source = source_id.split(":", 1)[1]
        if bare_source in per_source_caps:
            return per_source_caps[bare_source]
    return per_source_caps.get("default", 1)


def reserve_slot(source_id: str, item_id: str, risk_class: str | None = None) -> dict:
    """Atomically ADMITs or HOLDs one item against the live concurrency
    caps in policies/wave_limits.yaml, via a single Firestore transaction
    on wave_state/slots — this is what real dispatch calls (see
    agents/orchestrator/orchestrator.py::handle_planned), as opposed to
    evaluate_wave() above which only simulates a wave over a
    caller-supplied snapshot and has no shared state to arbitrate a race
    between two concurrently-processed runs.

    Idempotent: calling this again for an item_id already recorded as
    running for source_id returns ADMIT without double-booking a slot —
    safe to retry (e.g. from a redelivered Pub/Sub message that reserved
    a slot before the process died) without leaking capacity.

    Returns {"decision": ADMIT|HOLD, "reason": ...}. Callers MUST call
    release_slot(source_id, item_id) once the item finishes (success or
    failure) — typically from a try/finally around the guarded work.
    """
    override = _active_override(source_id)
    if override and override.get("state") == "HOLD":
        decision = {
            "decision": DECISION_HOLD,
            "reason": f"source {source_id!r} is under an operator HOLD: {override.get('justification', 'no reason supplied')}",
        }
        _wave_event("HELD", source_id, item_id, risk_class=risk_class, reason=decision["reason"], origin="operator_override")
        return decision

    limits = _load_limits()
    per_source_caps = limits.get("max_concurrent_per_source", {})
    critical_cap = limits.get("max_concurrent_critical", 1)
    cap = _cap_for(source_id, per_source_caps)

    doc_ref = _slot_doc_ref(source_id)

    @firestore.transactional
    def _txn(transaction: firestore.Transaction) -> dict:
        snapshot = doc_ref.get(transaction=transaction)
        state = snapshot.to_dict() if snapshot.exists else {}
        running_by_source = dict(state.get("running_by_source", {}))
        running_critical = list(state.get("running_critical", []))
        source_running = list(running_by_source.get(source_id, []))

        if item_id in source_running:
            return {"decision": DECISION_ADMIT, "reason": "already reserved (idempotent re-check)"}

        if len(source_running) >= cap:
            return {
                "decision": DECISION_HOLD,
                "reason": f"source {source_id!r} at concurrency cap ({cap})",
            }

        if risk_class == "CRITICAL" and len(running_critical) >= critical_cap:
            return {
                "decision": DECISION_HOLD,
                "reason": f"CRITICAL-risk concurrency cap ({critical_cap}) reached",
            }

        source_running.append(item_id)
        running_by_source[source_id] = source_running
        if risk_class == "CRITICAL":
            running_critical.append(item_id)

        transaction.set(
            doc_ref,
            {"running_by_source": running_by_source, "running_critical": running_critical},
        )
        return {"decision": DECISION_ADMIT, "reason": "within concurrency caps"}

    decision = _txn(get_client().transaction())
    _wave_event(
        "ADMITTED" if decision["decision"] == DECISION_ADMIT else "HELD",
        source_id,
        item_id,
        risk_class=risk_class,
        reason=decision["reason"],
        origin="concurrency_policy",
    )
    return decision


def release_slot(source_id: str, item_id: str) -> None:
    """Releases a slot reserved by reserve_slot(). Safe to call even if
    the item was never actually admitted (e.g. an unconditional release
    in a finally block after a HOLD) — a no-op in that case."""
    doc_ref = _slot_doc_ref(source_id)

    @firestore.transactional
    def _txn(transaction: firestore.Transaction) -> None:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return
        state = snapshot.to_dict()
        running_by_source = dict(state.get("running_by_source", {}))
        running_critical = list(state.get("running_critical", []))

        if item_id in running_by_source.get(source_id, []):
            running_by_source[source_id] = [i for i in running_by_source[source_id] if i != item_id]
        if item_id in running_critical:
            running_critical = [i for i in running_critical if i != item_id]

        transaction.set(
            doc_ref,
            {"running_by_source": running_by_source, "running_critical": running_critical},
        )

    _txn(get_client().transaction())
    _wave_event("RELEASED", source_id, item_id)
