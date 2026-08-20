"""Tests for Day 10's evaluation harness core (tools/evaluation_harness.py)
and the one new deterministic behavior in agents/orchestrator/orchestrator.py
that Appendix D's S-12 depends on: Pub/Sub duplicate-delivery dedup.

evaluation/scenarios.py itself is exercised end-to-end by
evaluation/run_harness.py against real infrastructure (SQL Server,
BigQuery, Vertex AI) — that's cost-bearing and deliberately not part of
the fast `pytest tests/` loop, the same reasoning tests/test_source_catalog.py
documents for skipping when SQL Server isn't reachable.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools.evaluation_harness import run_scenario, write_metrics_report  # noqa: E402


def _firestore_reachable() -> bool:
    # Delegates to the shared probe, which performs a real round trip.
    # This used to call `get_client()` and return True — but the Firestore
    # client is lazy and does no I/O when constructed, so it answered True
    # whenever the import worked, and the skipif below never skipped.
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


def test_run_scenario_records_pass_with_evidence():
    result = run_scenario("T-01", "always passes", lambda: {"ok": True})
    assert result["status"] == "PASS"
    assert result["evidence"] == {"ok": True}
    assert result["error"] is None
    assert result["duration_seconds"] >= 0


def test_run_scenario_catches_assertion_as_fail_not_a_crash():
    def _fails():
        assert False, "deliberate failure"

    result = run_scenario("T-02", "always fails", _fails)
    assert result["status"] == "FAIL"
    assert "deliberate failure" in result["error"]
    assert result["evidence"] is None


def test_run_scenario_catches_any_exception_as_fail():
    def _crashes():
        raise RuntimeError("boom")

    result = run_scenario("T-03", "always crashes", _crashes)
    assert result["status"] == "FAIL"
    assert "RuntimeError" in result["error"]


def test_write_metrics_report_generates_json_and_markdown(tmp_path):
    run_output = {
        "summary": {
            "harness_run_id": "eval_test_fixture",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "total": 1,
            "passed": 1,
            "failed": 0,
            "total_duration_seconds": 1.0,
        },
        "results": [
            {
                "scenario_id": "S-99",
                "description": "fixture scenario",
                "status": "PASS",
                "duration_seconds": 1.0,
                "evidence": {"ok": True},
                "error": None,
                "ran_at": "2026-01-01T00:00:01+00:00",
            }
        ],
    }
    json_path, md_path = write_metrics_report(run_output, reports_dir=tmp_path)
    assert json_path.exists()
    assert md_path.exists()
    assert "S-99" in md_path.read_text(encoding="utf-8")
    assert "eval_test_fixture" in json_path.read_text(encoding="utf-8")


def _drain_discovery_completed() -> None:
    """handle_migration_requested() really publishes discovery.completed
    on every non-deduped call, even when called directly (bypassing
    orchestrator.run_once()'s own pull of that topic). Left undrained,
    that message sits in discovery-completed-sub and gets picked up by
    the NEXT unrelated pull() — e.g. a later, real advance_to_passed()
    call — handing it a stale run_id and crashing on
    pin_agent_version()'s .update() against a run that (if the stale
    message's run was since deleted, as tests do) no longer exists. Same
    "clean up what you create" discipline as delete_run()/delete_card();
    this is the Pub/Sub-flavored version of it.
    """
    from tools.events import ack, pull

    # pull() no longer auto-acks (fix pass: a real defect where a handler
    # failure after pull() silently lost the message with no redelivery
    # path) — a drain must now ack what it drains, or the "drained"
    # messages just resurface once their ack deadline expires.
    for message in pull("discovery-completed-sub", max_messages=10, timeout=3.0):
        ack("discovery-completed-sub", message["_pubsub_ack_id"])


@skip_if_no_firestore
@pytest.mark.requires_sqlserver
def test_duplicate_message_delivery_produces_exactly_one_run():
    """Appendix D S-12, exercised directly against handle_migration_requested
    (no live Pub/Sub round-trip needed — the dedup key is carried on the
    payload dict exactly as tools/events.py::pull() attaches it).

    Needs SQL Server as well as Firestore: handle_migration_requested runs
    the real Discovery capability, which opens a live source connection.
    Without the marker this failed rather than skipped whenever the WWI
    container was down, which read as a broken dedup guarantee.
    """
    from agents.orchestrator.orchestrator import handle_migration_requested
    from agents.orchestrator.run_lifecycle import delete_run
    from tools.firestore_client import get_client

    message_id = f"test-{uuid.uuid4().hex}"
    payload = {"pipeline_id": "wwi.sales.customers", "_pubsub_message_id": message_id}

    first = handle_migration_requested(payload)
    try:
        second = handle_migration_requested(payload)
        assert not first.get("deduped")
        assert second.get("deduped") is True
        assert second["run_id"] == first["run_id"]
    finally:
        delete_run(first["run_id"])
        get_client().collection("processed_messages").document(f"handle_migration_requested:{message_id}").delete()
        _drain_discovery_completed()


@skip_if_no_firestore
@pytest.mark.requires_sqlserver
def test_message_without_id_is_never_deduped():
    """Backward compatibility: a payload with no _pubsub_message_id (e.g.
    a direct call from agents/orchestrator/run_full_migration.py, which
    never goes through tools/events.py::pull()) always creates a real run.
    """
    from agents.orchestrator.orchestrator import handle_migration_requested
    from agents.orchestrator.run_lifecycle import delete_run

    payload = {"pipeline_id": "wwi.sales.customers"}
    result = handle_migration_requested(payload)
    try:
        assert not result.get("deduped")
    finally:
        delete_run(result["run_id"])
        _drain_discovery_completed()
