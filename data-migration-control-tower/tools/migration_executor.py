"""Migration executor (Day 5, master doc §3.1's "Migration Tools" box).

Copies a table from the legacy SQL Server source into the BigQuery
target. This is deterministic data-movement code — no model call, no
judgment about whether a row "looks right" (that is Validation's job).
Used two ways in the same run:

  1. The Migration Planner's plan is executed for the first time
     (PLANNED -> MIGRATING) — optionally with drop_fraction > 0 to
     exercise the §7.2 row-loss fault-injection scenario as part of the
     one-shot Day 5 milestone demo (never a hidden default; the caller
     must ask for it explicitly).
  2. The recovery loop's remediation step (agents/orchestrator/recovery.py)
     calls it again with drop_fraction=0.0 to reload clean data.

Column-type handling: most SQL Server scalar types convert cleanly to
BigQuery-autodetected JSON. Spatial types (geography/geometry) and
hierarchyid do NOT — rather than silently excluding them (which used to
make the post-remediation schema check permanently fail, since the
target could never match the source's full column set), they are
selected via SQL Server's own `.ToString()` method and loaded as text.
This is an honest, complete migration of every column, not a redacted
one. Truly binary/XML types (varbinary, image, xml) are still excluded
today — real type mapping for those is a documented gap, not a bug.
"""

from __future__ import annotations

import abc
import datetime as dt
import math
import uuid
from typing import Iterator

from tools.usage_meter import attributes_usage
from tools.bigquery_tools import get_table_schema, load_json_rows
from tools.firestore_client import get_client
from tools.sqlserver_client import get_connection

RUN_COLLECTION = "migration_runs"

#: fetch_source_rows()/execute_migration() batch size (Day 10 Phase 4).
DEFAULT_BATCH_SIZE = 500

# Converts cleanly to BigQuery-autodetected JSON as-is.
_SCALAR_SAFE_TYPES = {
    "int", "bigint", "smallint", "tinyint", "bit",
    "nvarchar", "varchar", "char", "nchar",
    "decimal", "numeric", "float", "real",
    "date", "datetime", "datetime2", "uniqueidentifier",
}
# Selected via col.ToString() instead — a real value, not a dropped column.
_STRINGIFY_VIA_METHOD_TYPES = {"geography", "geometry", "hierarchyid"}
# Still excluded — no honest cheap conversion; a real gap, not hidden.
_STILL_EXCLUDED_TYPES = {"xml", "varbinary", "image"}


def _column_plan(cursor, schema: str, table: str) -> tuple[list[str], list[str]]:
    """Returns (select_exprs, output_column_names) for every column this
    executor can carry, in a single SELECT-ready form."""
    cursor.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        schema,
        table,
    )
    select_exprs, names = [], []
    for name, data_type in cursor.fetchall():
        if data_type in _SCALAR_SAFE_TYPES:
            select_exprs.append(f"[{name}]")
            names.append(name)
        elif data_type in _STRINGIFY_VIA_METHOD_TYPES:
            select_exprs.append(f"[{name}].ToString() AS [{name}]")
            names.append(name)
        # else: _STILL_EXCLUDED_TYPES or unrecognized — skipped.
    return select_exprs, names


def _excluded_columns(cursor, schema: str, table: str, carried: list[str]) -> list[str]:
    cursor.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        schema,
        table,
    )
    all_columns = [r[0] for r in cursor.fetchall()]
    return [c for c in all_columns if c not in carried]


def _to_json_safe(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if hasattr(value, "__float__") and not isinstance(value, (int, float, bool)):
        return float(value)  # decimal.Decimal
    return value


def _connectable(binding) -> bool:
    return binding is not None and getattr(binding, "requires_connection", False)


def _count_rows(schema: str, table: str, *, binding=None) -> int:
    conn = get_connection(profile=binding.resolve()) if _connectable(binding) else get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {schema}.{table}")
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def fetch_source_rows(
    schema: str,
    table: str,
    order_by: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    *,
    binding=None,
) -> tuple[list[str], Iterator[dict], list[str]]:
    """Returns (columns, row_iterator, excluded_columns) for schema.table.

    row_iterator yields dicts in batches via cursor.fetchmany(batch_size)
    (Day 10 Phase 4 hardening) rather than one cursor.fetchall() that
    buffers the entire table in memory before returning — the connection
    stays open for the lifetime of the generator and closes when it's
    exhausted or explicitly .close()'d (see execute_migration below).

    `binding` (Day 11) selects the estate whose credentials to use. None
    keeps the process-global environment path, which is what the local CLI
    scripts and the existing tests rely on.
    """
    conn = get_connection(profile=binding.resolve()) if _connectable(binding) else get_connection()
    cursor = conn.cursor()
    select_exprs, columns = _column_plan(cursor, schema, table)
    excluded = _excluded_columns(cursor, schema, table, columns)

    col_list = ", ".join(select_exprs)
    cursor.execute(f"SELECT {col_list} FROM {schema}.{table} ORDER BY {order_by}")

    def _rows() -> Iterator[dict]:
        try:
            while True:
                batch = cursor.fetchmany(batch_size)
                if not batch:
                    break
                for row in batch:
                    yield dict(zip(columns, (_to_json_safe(v) for v in row)))
        finally:
            conn.close()

    return columns, _rows(), excluded


class DataPlaneExecutor(abc.ABC):
    """Boundary between "decide what to move" (deterministic, this
    module's own code) and "actually move the bytes" (Day 10 Phase 4,
    master doc §32.12's control-plane/data-plane split). InMemoryExecutor
    below streams rows through THIS process's memory — genuinely batched,
    but not a separately-deployed service. tools/data_plane_executors/
    cloud_run_job_executor.py::CloudRunJobExecutor (Deploy & Harden
    Phase 3, docs/adr/0003-async-data-plane-job.md) is the first
    implementation that genuinely runs elsewhere.
    """

    @abc.abstractmethod
    def load(self, target_table: str, rows: Iterator[dict], batch_size: int) -> int:
        """Loads rows into target_table, streaming in batches. Returns
        the total row count loaded."""

    def execute_remote(
        self,
        *,
        run_id: str,
        execution_id: str,
        target: dict,
        binding,
    ) -> dict | None:
        """Optional: submits this execution to run somewhere else
        entirely and returns immediately with a PENDING manifest,
        WITHOUT fetching/counting rows in this process — the whole
        point being that this process (the orchestrator) may have no
        network path to the source at all (see docs/ARCHITECTURE.md's
        "on-prem network reachability" note).

        Returns None (the default, inherited by InMemoryExecutor and
        every executor that doesn't override this) to mean "not
        supported — use load() via the normal synchronous path
        instead." execute_migration() below checks this FIRST, before
        doing any of its own row-fetching/counting work, so an executor
        that returns non-None here never has fetch_source_rows() or
        _count_rows() called against it at all — those are SQL-Server-
        specific today and would be actively wrong to call for, say, a
        Postgres-sourced remote execution.

        Completion is NOT synchronous: the caller must not assume
        target_count/status in the returned manifest are final — see
        CloudRunJobExecutor's docstring for the actual completion event
        flow (a migration.completed Pub/Sub event, handled by
        agents/orchestrator/orchestrator.py::handle_migration_completed).
        """
        return None


class InMemoryExecutor(DataPlaneExecutor):
    """Streams rows through this process's own memory in bounded batches
    (tools/bigquery_tools.py::load_json_rows) — genuinely batched (Phase
    4's fix for the old single-fetchall()-then-single-load shape), but
    still this process doing the work, not a managed service. A real
    DataflowExecutor would submit a Dataflow job and return; that's not
    attempted here without a second live estate/real scale to justify
    the GCP cost (see the Day 10 hardening plan's Phase 4 scope note).
    """

    def load(self, target_table: str, rows: Iterator[dict], batch_size: int) -> int:
        total = 0
        buffer: list[dict] = []
        first_batch = True
        # Locked in right after the first (autodetect) batch and reused
        # explicitly for every later one — see load_json_rows()'s
        # docstring for why per-batch autodetect isn't safe.
        schema = None
        for row in rows:
            buffer.append(row)
            if len(buffer) >= batch_size:
                load_json_rows(target_table, buffer, truncate=first_batch, schema=schema)
                if first_batch:
                    schema = get_table_schema(target_table)
                total += len(buffer)
                buffer = []
                first_batch = False
        # Always issue at least one load call, even for zero/short rows —
        # matches the prior single-call behavior's guarantee that the
        # target table is truncated/created every run, not left stale
        # from a previous one.
        if buffer or first_batch:
            load_json_rows(target_table, buffer, truncate=first_batch, schema=schema)
            total += len(buffer)
        return total


@attributes_usage
def execute_migration(
    run_id: str,
    source_schema: str | None = None,
    source_table: str | None = None,
    target_table: str | None = None,
    key_column: str | None = None,
    drop_fraction: float = 0.0,
    batch_size: int = DEFAULT_BATCH_SIZE,
    data_plane: DataPlaneExecutor | None = None,
    *,
    target: dict | None = None,
    binding=None,
) -> dict:
    """Copies source_schema.source_table into BigQuery {dataset}.target_table.

    Two ways to say what to migrate. `target` is a MigrationTarget from the
    run's plan (contracts/metadata_model.json) and is what the orchestrator
    passes — it carries the source schema/table, target table and key
    column that used to be module constants shared by four modules. The
    positional form is retained because several standalone scripts and the
    existing tests construct a migration directly, and requiring them to
    build a plan first would be a worse interface for no safety gain.

    drop_fraction > 0 deliberately drops that fraction of rows (deterministic:
    the last N by key_column order) — the §7.2 row-loss fault-injection
    scenario. 0.0 (the default) is a clean, complete copy. Row loss is
    still exact and deterministic under streaming: source_count comes
    from a real COUNT(*) query, keep_count = source_count - drop_count is
    computed up front, and the row stream is cut off after keep_count
    rows rather than buffering everything to slice a Python list.

    Writes an execution manifest to
    migration_runs/{run_id}/migration_executions/{execution_id} and
    returns it. Does not itself change run state — the caller (Planner's
    first-pass script or the recovery loop) owns state transitions, since
    the two callers need different transition semantics (see
    agents/orchestrator/recovery.py's docstring for why).
    """
    if target is not None:
        source_schema = target["source_schema"]
        source_table = target["source_table"]
        target_table = target["target_table"]
        key_column = target.get("order_by") or target.get("key_column")
    missing = [
        name for name, value in (
            ("source_schema", source_schema), ("source_table", source_table),
            ("target_table", target_table), ("key_column", key_column),
        ) if not value
    ]
    if missing:
        raise ValueError(
            f"execute_migration needs {missing} — pass a plan target, or all four "
            f"positional arguments."
        )

    data_plane = data_plane or InMemoryExecutor()
    execution_id = str(uuid.uuid4())

    # Deploy & Harden Phase 3 (docs/adr/0003): an executor that can run
    # this migration somewhere else entirely gets first refusal, BEFORE
    # any of this function's own SQL-Server-specific row-fetching/
    # counting runs — _count_rows()/fetch_source_rows() below open a
    # pyodbc connection unconditionally, which is actively wrong to do
    # for, say, a Postgres-sourced remote execution the orchestrator may
    # have no network path to anyway. See execute_remote()'s own
    # docstring for what "PENDING manifest, completion via a
    # migration.completed event" means for the caller.
    remote_manifest = data_plane.execute_remote(
        run_id=run_id, execution_id=execution_id, target=target or {}, binding=binding
    )
    if remote_manifest is not None:
        return remote_manifest

    started_at = dt.datetime.now(dt.timezone.utc)
    source_count = _count_rows(source_schema, source_table, binding=binding)

    drop_count = 0
    keep_count = source_count
    if drop_fraction > 0:
        drop_count = max(1, math.ceil(source_count * drop_fraction))
        keep_count = source_count - drop_count

    _columns, row_iter, excluded_columns = fetch_source_rows(
        source_schema, source_table, key_column, batch_size=batch_size, binding=binding
    )

    def _kept_rows() -> Iterator[dict]:
        for i, row in enumerate(row_iter):
            if i >= keep_count:
                break
            yield row

    try:
        target_count = data_plane.load(target_table, _kept_rows(), batch_size)
    except Exception as exc:
        failed_at = dt.datetime.now(dt.timezone.utc)
        failure_manifest = {
            "execution_id": execution_id,
            "data_plane_job_id": f"inline-{execution_id}",
            "executor": type(data_plane).__name__,
            "status": "FAILED",
            "run_id": run_id,
            # Which plan target produced this manifest. A multi-table run
            # writes one manifest per target, so without it the executions
            # subcollection cannot be attributed back to the plan.
            "target_id": (target or {}).get("target_id"),
            "estate_id": getattr(binding, "estate_id", None),
            "source_id": getattr(binding, "source_id", None),
            "source_table": f"{source_schema}.{source_table}",
            "target_table": target_table,
            "source_count": source_count,
            "target_count": None,
            "bytes_read": None,
            "bytes_written": None,
            "started_at": started_at.isoformat(),
            "completed_at": failed_at.isoformat(),
            "duration_ms": round((failed_at - started_at).total_seconds() * 1000),
            "error": str(exc),
        }
        get_client().collection(RUN_COLLECTION).document(run_id).collection(
            "migration_executions"
        ).document(execution_id).set(failure_manifest)
        raise
    finally:
        row_iter.close()  # ensure the SQL Server connection releases promptly, even on early break/error

    completed_at = dt.datetime.now(dt.timezone.utc)

    manifest = {
        "execution_id": execution_id,
        "data_plane_job_id": f"inline-{execution_id}",
        "executor": type(data_plane).__name__,
        "status": "COMPLETED",
        "run_id": run_id,
        "source_table": f"{source_schema}.{source_table}",
        "target_table": target_table,
        "source_count": source_count,
        "target_count": target_count,
        "dropped_count": drop_count,
        "excluded_columns": excluded_columns,
        "deliberate_defect": drop_fraction > 0,
        "bytes_read": None,
        "bytes_written": None,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": round((completed_at - started_at).total_seconds() * 1000),
        "executed_at": completed_at.isoformat(),
    }

    client = get_client()
    client.collection(RUN_COLLECTION).document(run_id).collection(
        "migration_executions"
    ).document(manifest["execution_id"]).set(manifest)

    return manifest
