"""Tests for tools/memory_bank.py.

Needs live Firestore (facts are persisted globally there), so these skip
automatically when it isn't reachable — same pattern as the other
live-service tests in this suite. Every fact a test writes is deleted in
teardown so it can't pollute a real recall (memory_bank has no
"deprecate" concept like the registry — a fixture's facts must be
removed outright, or a real future recall could cite fabricated evidence).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools import memory_bank  # noqa: E402


def _firestore_reachable() -> bool:
    try:
        from tools.firestore_client import get_client

        get_client()
        return True
    except Exception:  # noqa: BLE001
        return False


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


@pytest.fixture()
def signature():
    sig = f"test_defect:{uuid.uuid4().hex[:8]}"
    yield sig
    from tools.firestore_client import get_client

    get_client().collection(memory_bank.COLLECTION).document(memory_bank._safe_doc_id(sig)).delete()


@skip_if_no_firestore
def test_recall_returns_none_when_nothing_recorded(signature):
    assert memory_bank.recall(signature) is None


@skip_if_no_firestore
def test_record_then_recall_round_trip(signature):
    fact = memory_bank.record(signature, root_cause="rows dropped", fix="reload cleanly", source_run_id="run_1")
    assert fact["signature"] == signature
    assert fact["source_run_ids"] == ["run_1"]
    assert fact["reuse_count"] == 0

    recalled = memory_bank.recall(signature)
    assert recalled is not None
    assert recalled["fix"] == "reload cleanly"


@skip_if_no_firestore
def test_recording_the_same_signature_twice_increments_reuse_and_appends_run(signature):
    memory_bank.record(signature, root_cause="rows dropped", fix="reload cleanly", source_run_id="run_1")
    updated = memory_bank.record(signature, root_cause="rows dropped again", fix="reload cleanly", source_run_id="run_2")

    assert updated["reuse_count"] == 1
    assert updated["source_run_ids"] == ["run_1", "run_2"]
    assert updated["root_cause"] == "rows dropped again"  # latest confirmation wins


@skip_if_no_firestore
def test_recording_same_run_id_twice_does_not_duplicate(signature):
    memory_bank.record(signature, root_cause="x", fix="y", source_run_id="run_1")
    updated = memory_bank.record(signature, root_cause="x", fix="y", source_run_id="run_1")
    assert updated["source_run_ids"] == ["run_1"]


@skip_if_no_firestore
def test_mark_recalled_tracks_which_runs_reused_the_fact(signature):
    memory_bank.record(signature, root_cause="x", fix="y", source_run_id="run_1")
    memory_bank.mark_recalled(signature, "run_2")

    fact = memory_bank.recall(signature)
    assert fact["recalled_by_run_ids"] == ["run_2"]


@skip_if_no_firestore
def test_mark_recalled_on_unknown_signature_is_a_noop():
    memory_bank.mark_recalled(f"no-such-signature-{uuid.uuid4().hex}", "run_1")  # must not raise
