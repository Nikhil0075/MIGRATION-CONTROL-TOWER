"""Tests for tools/wave_manager.py — Day 10 Phase 4's deterministic
scheduler. evaluate_wave()/within_approval_window() are pure functions,
no live services needed. reserve_slot()/release_slot() (added in the
post-second-audit fix pass, for real transactional dispatch) are
integration-style: real Firestore, skipped automatically when it isn't
reachable, same pattern as the rest of this suite."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.wave_manager import (  # noqa: E402
    DECISION_ADMIT,
    DECISION_HOLD,
    backlog_minutes,
    evaluate_wave,
    within_approval_window,
)


def _firestore_reachable() -> bool:
    try:
        from tools.firestore_client import get_client

        get_client()
        return True
    except Exception:  # noqa: BLE001
        return False


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")

NOW = dt.datetime(2026, 8, 16, 12, 0, 0, tzinfo=dt.timezone.utc)


def _item(item_id, source_id, minutes_ago=0, risk_class=None):
    return {
        "item_id": item_id,
        "source_id": source_id,
        "risk_class": risk_class,
        "requested_at": (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(),
    }


def _decision_for(decisions, item_id):
    return next(d for d in decisions if d["item_id"] == item_id)


def test_backlog_minutes_computes_elapsed_time():
    requested_at = (NOW - dt.timedelta(minutes=45)).isoformat()
    assert backlog_minutes(requested_at, now=NOW) == 45.0


def test_admits_within_per_source_cap():
    pending = [_item("a", "wwi-sqlserver"), _item("b", "wwi-sqlserver")]
    decisions = evaluate_wave(pending, running=[], now=NOW)
    assert _decision_for(decisions, "a")["decision"] == DECISION_ADMIT
    assert _decision_for(decisions, "b")["decision"] == DECISION_ADMIT  # cap is 2 for wwi-sqlserver


def test_holds_when_per_source_cap_already_reached_by_running_items():
    running = [{"item_id": "r1", "source_id": "wwi-sqlserver", "risk_class": None}]
    pending = [_item("a", "wwi-sqlserver"), _item("b", "wwi-sqlserver")]
    decisions = evaluate_wave(pending, running=running, now=NOW)
    # cap=2, 1 already running -> exactly one more admitted, one held
    assert sorted(d["decision"] for d in decisions) == [DECISION_ADMIT, DECISION_HOLD]


def test_holds_when_per_source_cap_reached_within_the_same_wave():
    """Two pending items for a source whose cap is 1 (oracle-corpus) —
    only the first (by priority order) is admitted; the second is held
    against the FIRST's own admission, not just pre-existing running items."""
    pending = [_item("a", "oracle-corpus", minutes_ago=10), _item("b", "oracle-corpus", minutes_ago=5)]
    decisions = evaluate_wave(pending, running=[], now=NOW)
    assert _decision_for(decisions, "a")["decision"] == DECISION_ADMIT  # older -> evaluated first
    assert _decision_for(decisions, "b")["decision"] == DECISION_HOLD


def test_unlisted_source_uses_default_cap():
    pending = [_item("a", "some-new-source"), _item("b", "some-new-source")]
    decisions = evaluate_wave(pending, running=[], now=NOW)
    # default cap is 1
    assert sorted(d["decision"] for d in decisions) == [DECISION_ADMIT, DECISION_HOLD]


def test_critical_risk_concurrency_cap_applies_across_sources():
    running = [{"item_id": "r1", "source_id": "wwi-sqlserver", "risk_class": "CRITICAL"}]
    pending = [_item("a", "oracle-corpus", risk_class="CRITICAL")]  # different source, cap is source-independent
    decisions = evaluate_wave(pending, running=running, now=NOW)
    decision = _decision_for(decisions, "a")
    assert decision["decision"] == DECISION_HOLD
    assert "CRITICAL" in decision["reason"]


def test_non_critical_items_unaffected_by_critical_cap():
    running = [{"item_id": "r1", "source_id": "wwi-sqlserver", "risk_class": "CRITICAL"}]
    pending = [_item("a", "oracle-corpus", risk_class="LOW")]
    decisions = evaluate_wave(pending, running=running, now=NOW)
    assert _decision_for(decisions, "a")["decision"] == DECISION_ADMIT


def test_backlog_aged_item_is_escalated_ahead_of_fresher_peers():
    """oracle-corpus cap is 1: an old, escalated item must be admitted
    over a fresher one requested first in wall-clock terms but not
    escalated — i.e. priority is backlog-driven, not FIFO-by-default."""
    pending = [
        _item("fresh", "oracle-corpus", minutes_ago=1),
        _item("stale", "oracle-corpus", minutes_ago=45),  # > 30-minute escalation threshold
    ]
    decisions = evaluate_wave(pending, running=[], now=NOW)
    assert _decision_for(decisions, "stale")["decision"] == DECISION_ADMIT
    assert _decision_for(decisions, "stale")["escalated"] is True
    assert _decision_for(decisions, "fresh")["decision"] == DECISION_HOLD


def test_within_approval_window_always_true_when_disabled():
    assert within_approval_window(NOW) is True  # policies/wave_limits.yaml ships with enabled: false


def test_within_approval_window_respects_configured_hours(monkeypatch):
    import tools.wave_manager as wm

    monkeypatch.setattr(
        wm,
        "_load_limits",
        lambda: {"approval_window": {"enabled": True, "start_hour_utc": 13, "end_hour_utc": 21}},
    )
    inside = NOW.replace(hour=15)
    outside = NOW.replace(hour=3)
    assert within_approval_window(inside) is True
    assert within_approval_window(outside) is False


def test_within_approval_window_handles_midnight_wraparound(monkeypatch):
    import tools.wave_manager as wm

    monkeypatch.setattr(
        wm,
        "_load_limits",
        lambda: {"approval_window": {"enabled": True, "start_hour_utc": 22, "end_hour_utc": 4}},
    )
    late_night = NOW.replace(hour=23)
    early_morning = NOW.replace(hour=2)
    midday = NOW.replace(hour=12)
    assert within_approval_window(late_night) is True
    assert within_approval_window(early_morning) is True
    assert within_approval_window(midday) is False


# --- reserve_slot / release_slot: real transactional dispatch (fix pass) --


@pytest.fixture()
def clean_wave_state():
    """Clears the wave-state documents these tests touch, before and after.

    Since Day 11 Phase 4 there is one document per estate rather than a
    single global `wave_state/slots`, so the fixture clears each estate key
    used below. This is still shared state that mirrors real dispatch — a
    leaked reservation would hold a real run — so it is cleared on both
    sides, not just teardown.
    """
    from tools.wave_manager import _slot_doc_ref

    keys = ["wwi-sqlserver", "estate-a:wwi-sqlserver", "estate-b:wwi-sqlserver"]
    refs = {_slot_doc_ref(k).path: _slot_doc_ref(k) for k in keys}
    for ref in refs.values():
        ref.set({"running_by_source": {}, "running_critical": []})
    yield
    for ref in refs.values():
        ref.delete()


@skip_if_no_firestore
def test_reserve_slot_admits_within_cap(clean_wave_state):
    from tools.wave_manager import reserve_slot

    decision = reserve_slot("wwi-sqlserver", "test-run-a")
    assert decision["decision"] == DECISION_ADMIT


@skip_if_no_firestore
def test_reserve_slot_holds_once_cap_reached(clean_wave_state):
    from tools.wave_manager import reserve_slot

    assert reserve_slot("wwi-sqlserver", "test-run-a")["decision"] == DECISION_ADMIT
    assert reserve_slot("wwi-sqlserver", "test-run-b")["decision"] == DECISION_ADMIT  # cap is 2
    held = reserve_slot("wwi-sqlserver", "test-run-c")
    assert held["decision"] == DECISION_HOLD


@skip_if_no_firestore
def test_reserve_slot_is_idempotent_for_the_same_item(clean_wave_state):
    """A redelivered message calling reserve_slot again for a run_id it
    already reserved must not double-book a slot (which would then wrongly
    HOLD a different, unrelated run)."""
    from tools.wave_manager import reserve_slot

    assert reserve_slot("wwi-sqlserver", "test-run-a")["decision"] == DECISION_ADMIT
    assert reserve_slot("wwi-sqlserver", "test-run-a")["decision"] == DECISION_ADMIT  # re-check, not a 2nd slot
    assert reserve_slot("wwi-sqlserver", "test-run-b")["decision"] == DECISION_ADMIT  # still room for 1 more


@skip_if_no_firestore
def test_release_slot_frees_capacity_for_the_next_reservation(clean_wave_state):
    from tools.wave_manager import reserve_slot, release_slot

    reserve_slot("wwi-sqlserver", "test-run-a")
    reserve_slot("wwi-sqlserver", "test-run-b")
    assert reserve_slot("wwi-sqlserver", "test-run-c")["decision"] == DECISION_HOLD

    release_slot("wwi-sqlserver", "test-run-a")
    assert reserve_slot("wwi-sqlserver", "test-run-c")["decision"] == DECISION_ADMIT


@skip_if_no_firestore
def test_release_slot_is_a_safe_no_op_for_an_unreserved_item(clean_wave_state):
    from tools.wave_manager import release_slot

    release_slot("wwi-sqlserver", "never-reserved")  # must not raise


@skip_if_no_firestore
def test_reserve_slot_critical_cap_applies_across_sources(clean_wave_state):
    from tools.wave_manager import reserve_slot

    admitted = reserve_slot("wwi-sqlserver", "test-run-a", risk_class="CRITICAL")
    assert admitted["decision"] == DECISION_ADMIT

    held = reserve_slot("oracle-corpus", "test-run-b", risk_class="CRITICAL")  # different source, same global cap
    assert held["decision"] == DECISION_HOLD
    assert "CRITICAL" in held["reason"]


# --- Per-estate isolation (Day 11 Phase 4) -------------------------------


@skip_if_no_firestore
def test_two_estates_do_not_contend_for_the_same_slots(clean_wave_state):
    """The property the per-estate split exists for.

    Before Phase 4 every estate's running items counted against one global
    `wave_state/slots` document, so onboarding a second customer would have
    made their runs queue behind — and be held by — the first customer's
    load. wwi-sqlserver's cap is 2, so filling estate A must leave estate B
    completely unaffected.
    """
    from tools.wave_manager import reserve_slot

    assert reserve_slot("estate-a:wwi-sqlserver", "a-1")["decision"] == DECISION_ADMIT
    assert reserve_slot("estate-a:wwi-sqlserver", "a-2")["decision"] == DECISION_ADMIT
    assert reserve_slot("estate-a:wwi-sqlserver", "a-3")["decision"] == DECISION_HOLD

    # Same source_id, different estate: unaffected by A being at its cap.
    assert reserve_slot("estate-b:wwi-sqlserver", "b-1")["decision"] == DECISION_ADMIT
    assert reserve_slot("estate-b:wwi-sqlserver", "b-2")["decision"] == DECISION_ADMIT


@skip_if_no_firestore
def test_releasing_one_estates_slot_does_not_free_anothers(clean_wave_state):
    from tools.wave_manager import release_slot, reserve_slot

    reserve_slot("estate-a:wwi-sqlserver", "a-1")
    reserve_slot("estate-a:wwi-sqlserver", "a-2")
    reserve_slot("estate-b:wwi-sqlserver", "b-1")
    reserve_slot("estate-b:wwi-sqlserver", "b-2")

    release_slot("estate-b:wwi-sqlserver", "b-1")

    assert reserve_slot("estate-a:wwi-sqlserver", "a-3")["decision"] == DECISION_HOLD
    assert reserve_slot("estate-b:wwi-sqlserver", "b-3")["decision"] == DECISION_ADMIT


def test_estate_of_reads_the_estate_from_a_qualified_wave_key():
    from tools.wave_manager import estate_of

    assert estate_of("acme-legacy:acme-sqlserver") == "acme-legacy"


def test_estate_of_defaults_an_unqualified_key_to_the_demo_estate():
    """Bare source ids come from the standalone scripts and from callers
    predating estate scoping — same "missing means default" rule applied
    to run documents, so they keep working rather than erroring."""
    from tools.connection_context import DEFAULT_ESTATE_ID
    from tools.wave_manager import estate_of

    assert estate_of("wwi-sqlserver") == DEFAULT_ESTATE_ID
