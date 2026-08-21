"""Tests for evaluation/data_plane_scale_test.py (Deploy & Harden
Phase 4) — the real-rows-moved measurement, distinct from
scale_harness.py's control-plane object-count benchmark.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evaluation.data_plane_scale_test import run_data_plane_test, write_report  # noqa: E402


def _firestore_reachable() -> bool:
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


@skip_if_no_firestore
def test_run_data_plane_test_computes_throughput_from_a_real_manifest(monkeypatch):
    import evaluation.data_plane_scale_test as mod

    fake_manifest = {
        "executor": "InMemoryExecutor", "source_table": "Sales.Customers",
        "target_table": "customers_dim_test", "target_count": 1000, "source_count": 1000,
        "duration_ms": 2000, "status": "COMPLETED",
    }
    monkeypatch.setattr(mod, "execute_migration", lambda **kwargs: fake_manifest)

    result = run_data_plane_test(
        source_schema="Sales", source_table="Customers",
        target_table="customers_dim_test", key_column="CustomerID",
        run_id=f"test-{uuid.uuid4().hex[:8]}",
    )
    assert result["rows_moved"] == 1000
    assert result["throughput_rows_per_sec"] == 500.0  # 1000 rows / 2 seconds


@skip_if_no_firestore
def test_run_data_plane_test_handles_a_pending_async_manifest(monkeypatch):
    """An async CloudRunJobExecutor manifest has target_count=None and no
    duration yet — must not crash computing throughput."""
    import evaluation.data_plane_scale_test as mod

    pending_manifest = {
        "executor": "CloudRunJobExecutor", "source_table": "public.orders",
        "target_table": "orders_dim", "target_count": None, "source_count": None,
        "duration_ms": None, "status": "PENDING",
    }
    monkeypatch.setattr(mod, "execute_migration", lambda **kwargs: pending_manifest)

    result = run_data_plane_test(
        source_schema="public", source_table="orders",
        target_table="orders_dim", key_column="id",
        run_id=f"test-{uuid.uuid4().hex[:8]}",
    )
    assert result["rows_moved"] == 0
    assert result["throughput_rows_per_sec"] is None
    assert result["status"] == "PENDING"


@skip_if_no_firestore
def test_write_report_persists_to_firestore_and_disk(tmp_path):
    from tools.firestore_client import get_client

    run_id = f"test-report-{uuid.uuid4().hex[:8]}"
    metrics = {
        "generated_at": "2026-01-01T00:00:00+00:00", "run_id": run_id,
        "executor": "InMemoryExecutor", "source_table": "Sales.Customers",
        "target_table": "customers_dim_test", "rows_moved": 500, "source_rows": 500,
        "duration_ms": 1000, "throughput_rows_per_sec": 500.0, "status": "COMPLETED",
    }
    collection = get_client().collection("evaluation_data_plane_reports")
    try:
        # write_report() calls path.relative_to(REPO_ROOT), so reports_dir
        # must be inside the repo tree — same constraint as scale_harness.py.
        real_dir = REPO_ROOT / "evaluation" / "reports"
        write_report(metrics, reports_dir=real_dir)

        doc = collection.document(run_id).get()
        assert doc.exists
        assert doc.to_dict()["rows_moved"] == 500
        assert (real_dir / "data_plane_scale_metrics.md").exists()
    finally:
        collection.document(run_id).delete()
