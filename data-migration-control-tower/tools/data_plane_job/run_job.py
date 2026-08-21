#!/usr/bin/env python
"""Cloud Run Job entrypoint for the async data-plane executor (Deploy &
Harden Phase 3, docs/adr/0003-async-data-plane-job.md).

Submitted by tools/data_plane_executors/cloud_run_job_executor.py, runs
in its own deployed container under its own narrowly-scoped service
account (Cloud SQL Client, target-BigQuery-dataset writer, write access
limited to its own execution document, Pub/Sub publisher for
migration.completed only, and only the Secret Manager references it
needs — infrastructure/terraform/README.md's Cloud SQL setup checklist).

Reuses, UNMODIFIED: the adapter's own fetch_rows() (whichever adapter
the resolved binding names — PostgresAdapter for Phase 3's Cloud SQL
demo source, though nothing here is Postgres-specific) and
tools/migration_executor.py::InMemoryExecutor for the actual BigQuery
load. This container's own code is entirely param plumbing + the
completion event — no data-movement logic is reimplemented here.

Config: entirely via env vars (Cloud Run Jobs passes parameters this
way, not argv) — see
tools/data_plane_executors/cloud_run_job_executor.py::_submit_job()
for exactly which ones it sets.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("data_plane_job")


def _env(name: str, *, required: bool = True) -> str | None:
    value = os.environ.get(name)
    if required and not value:
        raise RuntimeError(f"data-plane job: required env var {name!r} is unset or empty.")
    return value or None


def main() -> int:
    from tools.connection_context import binding_from_estate, load_estate_document
    from tools.adapters import build_adapter_for_binding
    from tools.events import publish
    from tools.firestore_client import get_client
    from tools.migration_executor import RUN_COLLECTION, InMemoryExecutor

    run_id = _env("RUN_ID")
    execution_id = _env("EXECUTION_ID")
    estate_id = _env("ESTATE_ID")
    source_id = _env("SOURCE_ID")
    source_schema = _env("SOURCE_SCHEMA")
    source_table = _env("SOURCE_TABLE")
    target_table = _env("TARGET_TABLE")
    key_column = _env("KEY_COLUMN")

    doc_ref = (
        get_client()
        .collection(RUN_COLLECTION)
        .document(run_id)
        .collection("migration_executions")
        .document(execution_id)
    )

    started_at = dt.datetime.now(dt.timezone.utc)
    table_id = f"{source_schema}.{source_table}"
    logger.info("data-plane job starting: run=%s execution=%s table=%s -> %s", run_id, execution_id, table_id, target_table)

    try:
        estate = load_estate_document(estate_id)
        binding = binding_from_estate(estate, source_id)
        adapter = build_adapter_for_binding(binding)

        source_count = adapter.count_rows(table_id)
        row_iter = adapter.fetch_rows(table_id, key_column)
        target_count = InMemoryExecutor().load(target_table, row_iter, batch_size=500)

        completed_at = dt.datetime.now(dt.timezone.utc)
        manifest_update = {
            "status": "COMPLETED",
            "source_count": source_count,
            "target_count": target_count,
            "completed_at": completed_at.isoformat(),
            "duration_ms": round((completed_at - started_at).total_seconds() * 1000),
        }
        doc_ref.set(manifest_update, merge=True)
        logger.info("data-plane job COMPLETED: run=%s execution=%s %d/%d rows", run_id, execution_id, target_count, source_count)

        publish("migration.completed", {
            "run_id": run_id,
            "execution_id": execution_id,
            "status": "COMPLETED",
            "target_table": target_table,
        })
        return 0

    except Exception as exc:  # noqa: BLE001 — this is the job's own top-level handler; it must
        # record failure and publish completion (with status=FAILED) rather than just crashing
        # silently, since nothing else is watching this process directly.
        failed_at = dt.datetime.now(dt.timezone.utc)
        logger.exception("data-plane job FAILED: run=%s execution=%s", run_id, execution_id)
        doc_ref.set(
            {
                "status": "FAILED",
                "error": str(exc)[:2000],
                "completed_at": failed_at.isoformat(),
                "duration_ms": round((failed_at - started_at).total_seconds() * 1000),
            },
            merge=True,
        )
        publish("migration.completed", {
            "run_id": run_id,
            "execution_id": execution_id,
            "status": "FAILED",
            "target_table": target_table,
            "error": str(exc)[:500],
        })
        return 1


if __name__ == "__main__":
    sys.exit(main())
