"""Tests for the deterministic state machine in run_lifecycle.py.

_allowed_next_states() is pure (no Firestore) and is tested exhaustively
here. transition_state() additionally reads/writes Firestore; the live
round-trip tests are skipped automatically when Firestore isn't
reachable (same pattern as tests/test_policy_engine.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agents.orchestrator.run_lifecycle import (  # noqa: E402
    STATES,
    _allowed_next_states,
    create_run,
    delete_run,
    transition_state,
)


def test_canonical_forward_path_is_fully_connected():
    """Every state in the §8 diagram must be reachable from REQUESTED."""
    reachable = {"REQUESTED"}
    frontier = ["REQUESTED"]
    while frontier:
        state = frontier.pop()
        for nxt in _allowed_next_states(state):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable == set(STATES)


def test_discovered_only_advances_to_analyzed():
    assert _allowed_next_states("DISCOVERED") == {"ANALYZED"}


def test_analyzed_only_advances_to_risk_assessed():
    assert _allowed_next_states("ANALYZED") == {"RISK_ASSESSED"}


def test_validating_can_pass_or_fail():
    assert _allowed_next_states("VALIDATING") == {"PASSED", "FAILED"}


def test_complete_is_terminal():
    assert _allowed_next_states("COMPLETE") == set()


def test_risk_assessed_only_advances_to_planned():
    # As of Day 5 the Migration Planner and migration executor are real
    # agents, so the RISK_ASSESSED -> VALIDATING provisional shortcut
    # (used during Day 3-4) has been removed — this is the only edge.
    assert _allowed_next_states("RISK_ASSESSED") == {"PLANNED"}


def test_no_provisional_transitions_remain():
    from agents.orchestrator.run_lifecycle import _PROVISIONAL_TRANSITIONS

    assert _PROVISIONAL_TRANSITIONS == {}


def _firestore_reachable() -> bool:
    # Delegates to the shared probe, which performs a real round trip.
    # This used to call `get_client()` and return True — but the Firestore
    # client is lazy and does no I/O when constructed, so it answered True
    # whenever the import worked, and the skipif below never skipped.
    from tests.probes import firestore_reachable

    return firestore_reachable()


@pytest.fixture()
def temp_run():
    """A throwaway run, deleted in teardown.

    Without this, tests/*.py's create_run() calls accumulate forever —
    and frontend/app.py's dashboard picks the single most-recently-
    created run as the "active run" to display, so a leftover test run
    doesn't just clutter Firestore, it can silently become what a judge
    sees on the dashboard (confirmed during Day 9 UI testing: a stray
    'test.state_machine.force' run showed up as the active run with 0
    discovered pipelines)."""
    run_id = create_run("test.state_machine.fixture")
    yield run_id
    delete_run(run_id)


@pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")
def test_illegal_transition_is_rejected(temp_run):
    with pytest.raises(ValueError, match="Illegal transition"):
        transition_state(temp_run, "RISK_ASSESSED")  # REQUESTED -> RISK_ASSESSED is not legal


@pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")
def test_legal_transition_succeeds(temp_run):
    transition_state(temp_run, "DISCOVERED")  # REQUESTED -> DISCOVERED is legal, should not raise


@pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")
def test_force_bypasses_legality_check(temp_run):
    transition_state(temp_run, "COMPLETE", force=True)  # would be illegal without force
