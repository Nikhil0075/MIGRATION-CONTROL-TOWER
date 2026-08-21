"""Tests for evaluation/load_test.py (Deploy & Harden Phase 4) — the
operational-load measurement, distinct from scale_harness.py's
control-plane benchmark and data_plane_scale_test.py's real-rows-moved
measurement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evaluation.load_test import collect_live_fleet_state, run_concurrent_load, write_report  # noqa: E402


def _firestore_reachable() -> bool:
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


@skip_if_no_firestore
def test_run_concurrent_load_reports_one_latency_sample_per_call():
    result = run_concurrent_load(5)
    assert result["concurrent_runs"] == 5
    assert result["throughput_per_sec"] > 0
    assert result["latency_p50_ms"] >= 0
    assert result["latency_max_ms"] >= result["latency_p50_ms"]


def test_collect_live_fleet_state_reports_not_queryable_instance_counts(monkeypatch):
    """Never fabricate a live instance count this script cannot actually
    measure — gcloud run services list doesn't report it."""
    import evaluation.load_test as mod

    monkeypatch.setattr(
        mod,
        "_run_gcloud_json",
        lambda args: [{"metadata": {"name": "hello-agent"}}] if "services" in args else [],
    )

    state = collect_live_fleet_state()
    assert state["deployed_service_count"] == 1
    assert state["deployed_services"][0]["instance_count"] is None
    assert "not queryable" in state["note"] or "run.googleapis.com/container/instance_count" in state["note"]


def test_collect_live_fleet_state_handles_gcloud_unavailable(monkeypatch):
    import evaluation.load_test as mod

    monkeypatch.setattr(mod, "_run_gcloud_json", lambda args: None)
    state = collect_live_fleet_state()
    assert state["deployed_service_count"] == 0
    assert state["subscriptions"] == []


@skip_if_no_firestore
def test_write_report_persists_both_sections():
    from tools.firestore_client import get_client

    concurrent_load = {
        "concurrent_runs": 5, "total_wall_clock_s": 1.0, "throughput_per_sec": 5.0,
        "latency_p50_ms": 10.0, "latency_p95_ms": 20.0, "latency_max_ms": 25.0,
    }
    fleet_state = {
        "deployed_services": [{"name": "hello-agent", "instance_count": None}],
        "deployed_service_count": 1, "expected_service_count": 10,
        "subscriptions": [], "note": "test note",
    }
    real_dir = REPO_ROOT / "evaluation" / "reports"
    report_file = real_dir / "load_test_metrics.md"
    # This file has no "real" prior content — evaluation/load_test.py is
    # new this phase, so the only way it exists at all is a prior test
    # run leaving it behind (the bug this fixture-cleanup closes). Track
    # whether it pre-existed so cleanup restores exactly that state
    # rather than assuming "always delete" or "never delete".
    pre_existing_content = report_file.read_text(encoding="utf-8") if report_file.exists() else None
    try:
        write_report(concurrent_load, fleet_state, reports_dir=real_dir)

        assert report_file.exists()
        content = report_file.read_text(encoding="utf-8")
        assert "5" in content and "hello-agent" in content
    finally:
        # Cleanup: find and remove the doc(s) this test just wrote.
        docs = list(get_client().collection("evaluation_load_reports").where("concurrent_load.concurrent_runs", "==", 5).stream())
        for doc in docs:
            if doc.to_dict().get("fleet_state", {}).get("note") == "test note":
                doc.reference.delete()
        # Restore the on-disk file to what it was before this test ran —
        # a real prior report is a repo artifact, worth keeping honest;
        # a file this test itself created should not survive it.
        if pre_existing_content is None:
            report_file.unlink(missing_ok=True)
        else:
            report_file.write_text(pre_existing_content, encoding="utf-8")
