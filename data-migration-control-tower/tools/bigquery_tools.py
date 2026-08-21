"""Thin BigQuery helper for the migration target (master doc §3.1).

Used by simulator/failure_injector/seed_row_loss.py (to create a target
table with a deliberately injected defect) and tools/reconciliation.py
(the Validation Agent's target-side queries). Deliberately minimal and
generic — full schema-mapped loading is the Migration Planner/executor's
job (later build day); this module just needs a real BigQuery table to
reconcile against.
"""

from __future__ import annotations

import os
from functools import lru_cache

from google.api_core.exceptions import Forbidden
from google.cloud import bigquery

#: Deploy & Harden Phase 1c: hard per-query cap. 1 GiB is generous for
#: this project's row-count/null-count/sum/key-hash reconciliation
#: queries (small tables, aggregate scans) while still catching a query
#: that accidentally scans something far larger than intended. Override
#: per-environment via env, not by editing code.
DEFAULT_BQ_MAX_BYTES_BILLED_PER_QUERY = 1 * 1024**3

#: Soft, best-effort cumulative cap per run — see
#: tools/usage_meter.py::reserve_bigquery_budget()'s docstring for why
#: this is a defense-in-depth control, not the real backstop.
DEFAULT_BQ_MAX_BYTES_BILLED_PER_RUN = 10 * 1024**3

#: Dry-run estimates are usually exact (BigQuery computes the same query
#: plan), but a small margin absorbs metadata/rounding differences
#: between the dry-run estimate and the real job's final accounting.
_ESTIMATE_MARGIN_FRACTION = 0.10
_ESTIMATE_MARGIN_FLOOR_BYTES = 10 * 1024**2  # BigQuery's own 10 MB minimum-billing granularity


class QueryBudgetExceeded(RuntimeError):
    """Raised when a query's dry-run estimate exceeds the per-query cap,
    or (via usage_meter.RunBudgetExceeded, re-raised here with `purpose`
    attached) the run's cumulative cap. Distinct from BigQuery's own
    Forbidden/BadRequest so callers can catch a budget refusal
    specifically rather than any API error."""


@lru_cache(maxsize=1)
def get_client() -> bigquery.Client:
    project_id = os.environ.get("GCP_PROJECT_ID")
    return bigquery.Client(project=project_id) if project_id else bigquery.Client()


def _dataset(dataset: str | None = None) -> str:
    return dataset or os.environ.get("BQ_DATASET", "migration_target")


def _max_bytes_billed_per_query() -> int:
    try:
        return int(os.environ.get("BQ_MAX_BYTES_BILLED_PER_QUERY", DEFAULT_BQ_MAX_BYTES_BILLED_PER_QUERY))
    except ValueError:
        return DEFAULT_BQ_MAX_BYTES_BILLED_PER_QUERY


def _max_bytes_billed_per_run() -> int:
    try:
        return int(os.environ.get("BQ_MAX_BYTES_BILLED_PER_RUN", DEFAULT_BQ_MAX_BYTES_BILLED_PER_RUN))
    except ValueError:
        return DEFAULT_BQ_MAX_BYTES_BILLED_PER_RUN


def _estimate_query_bytes(query: str) -> int:
    """A dry run costs nothing and executes nothing — BigQuery validates
    the query and returns its planned bytes-processed without running it,
    which is what makes "check before you spend" possible at all."""
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    job = get_client().query(query, job_config=job_config)
    return int(job.total_bytes_processed or 0)


def load_json_rows(
    table: str, rows: list[dict], truncate: bool = True, schema: list[bigquery.SchemaField] | None = None
) -> int:
    """Loads rows into {dataset}.{table}.

    Returns the number of rows loaded. truncate=True (the default) makes
    a single call idempotent — re-running the failure injector replaces
    the fixture rather than accumulating duplicates.

    truncate=False (Day 10 Phase 4): appends instead, so
    tools/migration_executor.py::execute_migration can stream a table in
    batches — call once with truncate=True (autodetects and establishes
    the target schema) then truncate=False for every subsequent batch.

    schema (Day 10 Phase 4): when given, disables autodetect and uses
    this explicit schema instead. Re-autodetecting per batch is NOT
    safe for a multi-batch load — a batch whose slice of some nullable
    column happens to be all-NULL can get inferred as a different type
    than an earlier batch's (a real `400 Provided Schema does not match
    Table` error reproduced during this refactor), silently or loudly
    breaking the append. get_table_schema() below fetches the schema
    BigQuery settled on after the first (autodetect) batch so every
    later batch reuses it explicitly rather than guessing again.
    """
    client = get_client()
    table_ref = f"{client.project}.{_dataset()}.{table}"
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=(
            bigquery.WriteDisposition.WRITE_TRUNCATE if truncate else bigquery.WriteDisposition.WRITE_APPEND
        ),
    )
    if schema is not None:
        job_config.schema = schema
        job_config.autodetect = False
    else:
        job_config.autodetect = True
    job = client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()  # wait for completion, raises on failure
    # Batch loads are not charged for the load itself. Recorded anyway,
    # at a zero rate declared in the price book: a reader can then tell a
    # free operation from one nobody thought to measure.
    from tools.usage_meter import current_run_id, record_bigquery_usage

    record_bigquery_usage(
        current_run_id(),
        job_kind="load",
        bytes_billed=getattr(job, "output_bytes", None) or 0,
        purpose="data_plane.load",
    )
    return len(rows)


def _metered_query(query: str, *, purpose: str):
    """Runs a query and records what BigQuery says it billed.

    One helper rather than five instrumented call sites, so a query added
    later is metered — and cost-controlled — by construction instead of
    by remembering. The job object carries the actual figures recorded
    afterward; nothing here estimates the FINAL cost. The dry-run
    estimate below exists only to decide, before spending anything,
    whether this query is allowed to run at all (Deploy & Harden
    Phase 1c, dry-run -> reserve -> cap):

      1. Dry-run the query (free, doesn't execute) to estimate its bytes.
      2. Reserve that estimate against the run's cumulative soft budget
         (tools/usage_meter.py::reserve_bigquery_budget) — refuses before
         any real query runs if the run's already near its cap.
      3. Run the real query with `maximum_bytes_billed` set from the
         estimate (BigQuery itself refuses to run past that, as a hard
         per-query backstop independent of step 2's soft, run-scoped one).

    `total_bytes_billed`, not `total_bytes_processed`, is what's recorded
    afterward: BigQuery bills a 10 MB minimum per query, so this
    project's many tiny reconciliation counts cost far more than the
    bytes they touch. Both are recorded, because the gap between them is
    itself the interesting part.
    """
    from tools.usage_meter import RunBudgetExceeded, current_run_id, record_bigquery_usage, reserve_bigquery_budget

    run_id = current_run_id()
    per_query_cap = _max_bytes_billed_per_query()

    estimated_bytes = _estimate_query_bytes(query)
    if estimated_bytes > per_query_cap:
        raise QueryBudgetExceeded(
            f"{purpose}: dry-run estimate {estimated_bytes:,} bytes exceeds the "
            f"{per_query_cap:,}-byte per-query cap (BQ_MAX_BYTES_BILLED_PER_QUERY) — refused "
            f"before running, not after."
        )

    if run_id:
        try:
            reserve_bigquery_budget(run_id, estimated_bytes, _max_bytes_billed_per_run())
        except RunBudgetExceeded as exc:
            raise QueryBudgetExceeded(f"{purpose}: {exc}") from exc

    margin = max(int(estimated_bytes * _ESTIMATE_MARGIN_FRACTION), _ESTIMATE_MARGIN_FLOOR_BYTES)
    job_config = bigquery.QueryJobConfig(maximum_bytes_billed=min(estimated_bytes + margin, per_query_cap))
    try:
        job = get_client().query(query, job_config=job_config)
        result = job.result()  # wait, so the job carries its final statistics
    except Forbidden as exc:
        raise QueryBudgetExceeded(
            f"{purpose}: BigQuery refused to run past its own maximum_bytes_billed cap "
            f"({job_config.maximum_bytes_billed:,} bytes) — the dry-run estimate undershot the "
            f"actual cost. Original error: {exc}"
        ) from exc
    record_bigquery_usage(
        run_id,
        job_kind="query",
        bytes_billed=getattr(job, "total_bytes_billed", None),
        bytes_processed=getattr(job, "total_bytes_processed", None),
        purpose=purpose,
    )
    return result


def get_table_schema(table: str) -> list[bigquery.SchemaField]:
    client = get_client()
    table_ref = f"{client.project}.{_dataset()}.{table}"
    return client.get_table(table_ref).schema


def get_row_count(table: str, *, dataset: str | None = None) -> int:
    client = get_client()
    query = f"SELECT COUNT(*) AS n FROM `{client.project}.{_dataset(dataset)}.{table}`"
    return int(next(iter(_metered_query(query, purpose="validation.row_count")))["n"])


def get_column_names(table: str) -> list[str]:
    client = get_client()
    table_ref = f"{client.project}.{_dataset()}.{table}"
    return [field.name for field in client.get_table(table_ref).schema]


def get_null_count(table: str, column: str) -> int:
    client = get_client()
    query = (
        f"SELECT COUNTIF(`{column}` IS NULL) AS n "
        f"FROM `{client.project}.{_dataset()}.{table}`"
    )
    return int(next(iter(_metered_query(query, purpose="validation.null_count")))["n"])


def get_numeric_sum(table: str, column: str) -> float:
    client = get_client()
    query = f"SELECT SUM(`{column}`) AS s FROM `{client.project}.{_dataset()}.{table}`"
    value = next(iter(_metered_query(query, purpose="validation.numeric_sum")))["s"]
    return float(value) if value is not None else 0.0


def get_key_values(table: str, column: str, *, dataset: str | None = None) -> list[str]:
    """Returns every value of `column` as a string, for a keyed hash check."""
    client = get_client()
    query = (
        f"SELECT CAST(`{column}` AS STRING) AS k "
        f"FROM `{client.project}.{_dataset(dataset)}.{table}` ORDER BY k"
    )
    return [row["k"] for row in _metered_query(query, purpose="validation.key_values")]
