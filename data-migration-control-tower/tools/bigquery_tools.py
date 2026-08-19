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

from google.cloud import bigquery


@lru_cache(maxsize=1)
def get_client() -> bigquery.Client:
    project_id = os.environ.get("GCP_PROJECT_ID")
    return bigquery.Client(project=project_id) if project_id else bigquery.Client()


def _dataset(dataset: str | None = None) -> str:
    return dataset or os.environ.get("BQ_DATASET", "migration_target")


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
    return len(rows)


def get_table_schema(table: str) -> list[bigquery.SchemaField]:
    client = get_client()
    table_ref = f"{client.project}.{_dataset()}.{table}"
    return client.get_table(table_ref).schema


def get_row_count(table: str, *, dataset: str | None = None) -> int:
    client = get_client()
    query = f"SELECT COUNT(*) AS n FROM `{client.project}.{_dataset(dataset)}.{table}`"
    return int(next(iter(client.query(query).result()))["n"])


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
    return int(next(iter(client.query(query).result()))["n"])


def get_numeric_sum(table: str, column: str) -> float:
    client = get_client()
    query = f"SELECT SUM(`{column}`) AS s FROM `{client.project}.{_dataset()}.{table}`"
    value = next(iter(client.query(query).result()))["s"]
    return float(value) if value is not None else 0.0


def get_key_values(table: str, column: str, *, dataset: str | None = None) -> list[str]:
    """Returns every value of `column` as a string, for a keyed hash check."""
    client = get_client()
    query = (
        f"SELECT CAST(`{column}` AS STRING) AS k "
        f"FROM `{client.project}.{_dataset(dataset)}.{table}` ORDER BY k"
    )
    return [row["k"] for row in client.query(query).result()]
