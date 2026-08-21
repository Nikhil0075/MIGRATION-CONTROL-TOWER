"""CloudRunJobExecutor — a DataPlaneExecutor that runs elsewhere entirely
(Deploy & Harden Phase 3, docs/adr/0003-async-data-plane-job.md).

Durable, asynchronous flow — NOT synchronous polling from an HTTP
request (a first draft did that; rejected as unsafe, see the ADR):

  1. execute_remote() writes a PENDING execution manifest, submits a
     Cloud Run Jobs execution (google.cloud.run_v2.JobsClient.run_job()),
     and returns IMMEDIATELY — no blocking wait.
  2. The job container (tools/data_plane_job/run_job.py, its own
     deployed image, its own narrowly-scoped service account) reuses the
     EXISTING, UNMODIFIED tools/adapters/*.py fetch_rows() +
     tools/migration_executor.py::InMemoryExecutor().load() — genuinely
     running in a separately-deployed, separately-billed unit, not this
     process. It writes the final manifest to the SAME Firestore
     document this class created, then publishes `migration.completed`.
  3. agents/orchestrator/orchestrator.py::handle_migration_completed
     consumes that event and continues the lifecycle — the same
     event-driven pattern every other stage already uses.

Terminology: PostgreSQL via `psycopg` (what the job container actually
uses, through PostgresAdapter) is NOT JDBC — never call this a "JDBC
transport" anywhere it's described. It's a direct wire-protocol driver
connection running in its own deployed container instead of the
orchestrator's process; that's the real, honest improvement over
InMemoryExecutor, not something bigger than it is.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Iterator

from tools.firestore_client import get_client
from tools.migration_executor import RUN_COLLECTION, DataPlaneExecutor

logger = logging.getLogger("cloud_run_job_executor")

#: Env var read by infrastructure/terraform's data-plane-job resource
#: (Deploy & Harden Phase 3) and by this class — the deployed Cloud Run
#: Job's fully-qualified name, e.g.
#: projects/P/locations/R/jobs/migration-data-plane. No default: a
#: caller that constructs this executor without setting it gets a clear
#: error at execute_remote() time, not a confusing API failure.
JOB_NAME_ENV_VAR = "DATA_PLANE_JOB_NAME"


class CloudRunJobExecutorConfigError(RuntimeError):
    """Raised when this executor is used without the configuration it
    needs — a missing job name, or a target that doesn't carry what the
    job container needs to run (e.g. built from positional args with no
    plan `target` dict, which the local InMemoryExecutor path tolerates
    but a remote submission cannot)."""


class CloudRunJobExecutor(DataPlaneExecutor):
    """Submit-and-return; the actual row movement happens in a separate
    Cloud Run Job execution, not this process. load() is never called on
    this class in practice — execute_remote() always returns non-None,
    so tools/migration_executor.py::execute_migration() short-circuits
    before load() would be reached — but it's implemented (delegating to
    InMemoryExecutor) so this class remains a complete DataPlaneExecutor
    if a caller ever constructs one directly against local rows."""

    def __init__(self, *, job_name: str | None = None, region: str | None = None):
        self.job_name = job_name or os.environ.get(JOB_NAME_ENV_VAR)
        self.region = region or os.environ.get("GCP_REGION", "us-central1")

    def load(self, target_table: str, rows: Iterator[dict], batch_size: int) -> int:
        from tools.migration_executor import InMemoryExecutor

        return InMemoryExecutor().load(target_table, rows, batch_size)

    def execute_remote(
        self,
        *,
        run_id: str,
        execution_id: str,
        target: dict,
        binding,
    ) -> dict:
        if not self.job_name:
            raise CloudRunJobExecutorConfigError(
                f"CloudRunJobExecutor has no job_name — set {JOB_NAME_ENV_VAR} or pass job_name= "
                "explicitly. This is a required deployment-time configuration error, not a "
                "transient one."
            )
        required = ("source_schema", "source_table", "target_table")
        missing = [f for f in required if not target.get(f)]
        if missing:
            raise CloudRunJobExecutorConfigError(
                f"CloudRunJobExecutor needs a plan target dict with {required} — got missing "
                f"{missing}. The positional-args calling convention (no `target=`) is not "
                f"supported for remote execution; it only tells InMemoryExecutor's local path "
                f"what to do."
            )
        if binding is None:
            raise CloudRunJobExecutorConfigError(
                "CloudRunJobExecutor needs a resolved SourceBinding (binding=) so the job "
                "container knows which estate/source/credentials to use — got None."
            )

        started_at = dt.datetime.now(dt.timezone.utc)
        pending_manifest = {
            "execution_id": execution_id,
            "data_plane_job_id": None,  # filled in once run_job() returns its execution name
            "executor": type(self).__name__,
            "status": "PENDING",
            "run_id": run_id,
            "target_id": target.get("target_id"),
            "estate_id": getattr(binding, "estate_id", None),
            "source_id": getattr(binding, "source_id", None),
            "source_table": f"{target['source_schema']}.{target['source_table']}",
            "target_table": target["target_table"],
            "key_column": target.get("order_by") or target.get("key_column"),
            "numeric_column": target.get("numeric_column"),
            "null_check_column": target.get("null_check_column"),
            "source_count": None,
            "target_count": None,
            "started_at": started_at.isoformat(),
            "completed_at": None,
            "duration_ms": None,
        }
        doc_ref = (
            get_client()
            .collection(RUN_COLLECTION)
            .document(run_id)
            .collection("migration_executions")
            .document(execution_id)
        )
        doc_ref.set(pending_manifest)

        job_execution_name = self._submit_job(run_id=run_id, execution_id=execution_id, target=target, binding=binding)
        doc_ref.set({"data_plane_job_id": job_execution_name}, merge=True)
        pending_manifest["data_plane_job_id"] = job_execution_name

        logger.info(
            "execute_remote: submitted %s for run=%s execution=%s (target_table=%s) — "
            "completion arrives via migration.completed, not this call",
            job_execution_name, run_id, execution_id, target["target_table"],
        )
        return pending_manifest

    def _submit_job(self, *, run_id: str, execution_id: str, target: dict, binding) -> str:
        """Submits the Cloud Run Jobs execution with this run's parameters
        as env var overrides. Lazy import — same Rung-2 pattern as every
        other optional GCP dependency in this codebase; a checkout that
        never uses CloudRunJobExecutor doesn't need google-cloud-run
        installed."""
        from google.cloud import run_v2

        client = run_v2.JobsClient()
        env_overrides = [
            run_v2.EnvVar(name="RUN_ID", value=run_id),
            run_v2.EnvVar(name="EXECUTION_ID", value=execution_id),
            run_v2.EnvVar(name="ESTATE_ID", value=str(getattr(binding, "estate_id", "") or "")),
            run_v2.EnvVar(name="SOURCE_ID", value=str(getattr(binding, "source_id", "") or "")),
            run_v2.EnvVar(name="SOURCE_SCHEMA", value=target["source_schema"]),
            run_v2.EnvVar(name="SOURCE_TABLE", value=target["source_table"]),
            run_v2.EnvVar(name="TARGET_TABLE", value=target["target_table"]),
            run_v2.EnvVar(name="KEY_COLUMN", value=str(target.get("order_by") or target.get("key_column") or "")),
        ]
        request = run_v2.RunJobRequest(
            name=self.job_name,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(env=env_overrides)
                ]
            ),
        )
        operation = client.run_job(request=request)
        # Not awaited (operation.result()) — that would block on the
        # job's full runtime, exactly the synchronous-polling design
        # this class exists to avoid. run_job() returns a long-running-
        # operation object (google.api_core.operation.Operation) whose
        # own resource name is available immediately, without waiting
        # for the job to finish — that's the only thing recorded here,
        # purely for traceability (evaluation/collect_deployment_evidence.py,
        # manual debugging), never polled by this class itself.
        return operation.operation.name
