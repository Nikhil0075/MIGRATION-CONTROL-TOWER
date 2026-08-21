"""Tests for tools/data_plane_executors/cloud_run_job_executor.py
(Deploy & Harden Phase 3). Mocks google.cloud.run_v2.JobsClient — no
live Cloud Run Jobs API call needed; the Firestore writes (PENDING
manifest) are real and need live Firestore, matching this suite's usual
skip-when-unreachable pattern.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.data_plane_executors.cloud_run_job_executor import (  # noqa: E402
    CloudRunJobExecutor,
    CloudRunJobExecutorConfigError,
)


def _firestore_reachable() -> bool:
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


class _FakeBinding:
    estate_id = "retail-postgres-estate"
    source_id = "retail-postgres"


VALID_TARGET = {
    "target_id": "orders-target",
    "source_schema": "public",
    "source_table": "orders",
    "target_table": "orders_dim",
    "key_column": "order_id",
}


def test_missing_job_name_is_a_clear_config_error(monkeypatch):
    monkeypatch.delenv("DATA_PLANE_JOB_NAME", raising=False)
    executor = CloudRunJobExecutor()
    with pytest.raises(CloudRunJobExecutorConfigError, match="job_name"):
        executor.execute_remote(
            run_id="run-1", execution_id="exec-1", target=VALID_TARGET, binding=_FakeBinding()
        )


def test_a_target_missing_required_fields_is_a_clear_config_error():
    executor = CloudRunJobExecutor(job_name="projects/p/locations/r/jobs/j")
    with pytest.raises(CloudRunJobExecutorConfigError, match="source_schema|source_table|target_table"):
        executor.execute_remote(run_id="run-1", execution_id="exec-1", target={}, binding=_FakeBinding())


def test_a_missing_binding_is_a_clear_config_error():
    executor = CloudRunJobExecutor(job_name="projects/p/locations/r/jobs/j")
    with pytest.raises(CloudRunJobExecutorConfigError, match="binding"):
        executor.execute_remote(run_id="run-1", execution_id="exec-1", target=VALID_TARGET, binding=None)


@skip_if_no_firestore
def test_execute_remote_writes_a_pending_manifest_and_submits_the_job(monkeypatch):
    from tools.firestore_client import get_client
    from tools.migration_executor import RUN_COLLECTION

    fake_operation = MagicMock()
    fake_operation.operation.name = "projects/p/locations/r/operations/op-123"
    fake_client = MagicMock()
    fake_client.run_job.return_value = fake_operation

    monkeypatch.setattr(
        "google.cloud.run_v2.JobsClient", lambda: fake_client
    )

    run_id = f"test-run-{uuid.uuid4().hex[:8]}"
    execution_id = str(uuid.uuid4())
    doc_ref = (
        get_client().collection(RUN_COLLECTION).document(run_id)
        .collection("migration_executions").document(execution_id)
    )
    try:
        executor = CloudRunJobExecutor(job_name="projects/p/locations/r/jobs/migration-data-plane")
        manifest = executor.execute_remote(
            run_id=run_id, execution_id=execution_id, target=VALID_TARGET, binding=_FakeBinding()
        )

        assert manifest["status"] == "PENDING"
        assert manifest["run_id"] == run_id
        assert manifest["execution_id"] == execution_id
        assert manifest["source_table"] == "public.orders"
        assert manifest["target_table"] == "orders_dim"
        assert manifest["data_plane_job_id"] == "projects/p/locations/r/operations/op-123"
        assert manifest["target_count"] is None  # not known yet — completion arrives async

        # The Firestore doc must reflect the same PENDING state — this is
        # what handle_migration_completed later merges into, and what
        # run_job.py (the job container) will update with the real result.
        stored = doc_ref.get().to_dict()
        assert stored["status"] == "PENDING"
        assert stored["data_plane_job_id"] == "projects/p/locations/r/operations/op-123"

        fake_client.run_job.assert_called_once()
    finally:
        doc_ref.delete()


@skip_if_no_firestore
def test_execute_remote_uses_env_var_job_name_when_not_passed_explicitly(monkeypatch):
    from tools.firestore_client import get_client
    from tools.migration_executor import RUN_COLLECTION

    monkeypatch.setenv("DATA_PLANE_JOB_NAME", "projects/p/locations/r/jobs/from-env")

    fake_operation = MagicMock()
    fake_operation.operation.name = "projects/p/locations/r/operations/op-456"
    fake_client = MagicMock()
    fake_client.run_job.return_value = fake_operation
    monkeypatch.setattr("google.cloud.run_v2.JobsClient", lambda: fake_client)

    run_id = f"test-run-{uuid.uuid4().hex[:8]}"
    execution_id = str(uuid.uuid4())
    doc_ref = (
        get_client().collection(RUN_COLLECTION).document(run_id)
        .collection("migration_executions").document(execution_id)
    )
    try:
        executor = CloudRunJobExecutor()  # no job_name passed — reads env var
        executor.execute_remote(
            run_id=run_id, execution_id=execution_id, target=VALID_TARGET, binding=_FakeBinding()
        )
        request = fake_client.run_job.call_args.kwargs["request"]
        assert request.name == "projects/p/locations/r/jobs/from-env"
    finally:
        doc_ref.delete()


def test_load_delegates_to_in_memory_executor(monkeypatch):
    """CloudRunJobExecutor.load() is never actually reached in practice
    (execute_remote() always short-circuits execute_migration() first),
    but it's implemented rather than raising NotImplementedError, so the
    class remains a complete DataPlaneExecutor."""
    import tools.migration_executor as me

    calls = []
    monkeypatch.setattr(
        me.InMemoryExecutor, "load", lambda self, table, rows, batch_size: calls.append((table, batch_size)) or 7
    )
    executor = CloudRunJobExecutor(job_name="projects/p/locations/r/jobs/j")
    result = executor.load("some_table", iter([]), batch_size=500)
    assert result == 7
    assert calls == [("some_table", 500)]
