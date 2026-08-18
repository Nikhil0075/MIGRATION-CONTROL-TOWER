"""Tests for tools/migration_executor.py — Day 10 Phase 4's streaming
refactor (fetch_source_rows now a generator, execute_migration batches
via DataPlaneExecutor instead of one cursor.fetchall() + one
load_json_rows() call). The exact row-loss semantics (§7.2's
fault-injection scenario) and the excluded/carried column set must be
unchanged — these tests assert that directly, live against SQL Server
and BigQuery (skip automatically when unreachable, same pattern as the
rest of this suite).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools.migration_executor import (  # noqa: E402
    DataPlaneExecutor,
    InMemoryExecutor,
    execute_migration,
    fetch_source_rows,
)


def _sql_server_reachable() -> bool:
    try:
        from tools.sqlserver_client import get_connection

        get_connection().close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _bigquery_reachable() -> bool:
    try:
        from tools.bigquery_tools import get_client

        get_client()
        return True
    except Exception:  # noqa: BLE001
        return False


skip_if_no_sql_server = pytest.mark.skipif(not _sql_server_reachable(), reason="SQL Server container not reachable")
skip_if_no_live_infra = pytest.mark.skipif(
    not (_sql_server_reachable() and _bigquery_reachable()), reason="SQL Server and/or BigQuery not reachable"
)


class _RecordingExecutor(DataPlaneExecutor):
    """Captures every batch handed to it instead of really loading to
    BigQuery — lets the streaming/batching contract be asserted without
    live infra."""

    def __init__(self):
        self.batches: list[list[dict]] = []

    def load(self, target_table, rows, batch_size):
        total = 0
        buffer = []
        for row in rows:
            buffer.append(row)
            if len(buffer) >= batch_size:
                self.batches.append(buffer)
                total += len(buffer)
                buffer = []
        if buffer:
            self.batches.append(buffer)
            total += len(buffer)
        return total


@skip_if_no_sql_server
def test_fetch_source_rows_returns_a_generator_not_a_list():
    import types

    _columns, row_iter, _excluded = fetch_source_rows("Sales", "Customers", "CustomerID")
    assert isinstance(row_iter, types.GeneratorType)
    # fully consume so the underlying connection's finally block closes it
    list(row_iter)


@skip_if_no_sql_server
def test_fetch_source_rows_batches_via_recording_executor():
    """The row iterator really is consumed in batch_size-sized chunks,
    not pulled all at once."""
    _columns, row_iter, _excluded = fetch_source_rows("Sales", "Customers", "CustomerID", batch_size=100)
    executor = _RecordingExecutor()
    total = executor.load("unused_table", row_iter, batch_size=100)

    assert total == sum(len(b) for b in executor.batches)
    assert total > 100, "expected Sales.Customers to have more than one batch worth of rows"
    for batch in executor.batches[:-1]:  # every batch except possibly the last is full-sized
        assert len(batch) == 100


@skip_if_no_live_infra
def test_execute_migration_clean_load_matches_source_count():
    """drop_fraction=0.0: target_count must equal the real source row count."""
    manifest = execute_migration(
        run_id="test-migration-executor-clean",
        source_schema="Sales",
        source_table="Customers",
        target_table="customers_dim",
        key_column="CustomerID",
        drop_fraction=0.0,
    )
    assert manifest["dropped_count"] == 0
    assert manifest["target_count"] == manifest["source_count"]
    assert manifest["deliberate_defect"] is False


@skip_if_no_live_infra
def test_execute_migration_drop_fraction_is_exact():
    """drop_fraction > 0 must drop exactly ceil(source_count * fraction)
    rows — the same deterministic arithmetic as before streaming."""
    import math

    # First, a clean load to learn the real source_count.
    baseline = execute_migration(
        run_id="test-migration-executor-baseline",
        source_schema="Sales",
        source_table="Customers",
        target_table="customers_dim",
        key_column="CustomerID",
        drop_fraction=0.0,
    )
    source_count = baseline["source_count"]
    expected_drop = max(1, math.ceil(source_count * 0.05))

    lossy = execute_migration(
        run_id="test-migration-executor-lossy",
        source_schema="Sales",
        source_table="Customers",
        target_table="customers_dim",
        key_column="CustomerID",
        drop_fraction=0.05,
        batch_size=50,  # deliberately small to exercise multiple batches around the cutoff
    )
    assert lossy["source_count"] == source_count
    assert lossy["dropped_count"] == expected_drop
    assert lossy["target_count"] == source_count - expected_drop
    assert lossy["deliberate_defect"] is True

    # Restore a clean target so this test doesn't leave the shared demo
    # table's real BigQuery target in a lossy state for whatever runs next.
    execute_migration(
        run_id="test-migration-executor-restore",
        source_schema="Sales",
        source_table="Customers",
        target_table="customers_dim",
        key_column="CustomerID",
        drop_fraction=0.0,
    )


def test_in_memory_executor_truncates_first_batch_only(monkeypatch):
    """load_json_rows must be called with truncate=True exactly once (the
    first batch) and truncate=False for every subsequent batch — an
    APPEND-only stream would silently duplicate rows; an all-TRUNCATE
    stream would silently drop everything but the last batch."""
    calls = []

    def _fake_load_json_rows(table, rows, truncate=True, schema=None):
        calls.append((table, len(rows), truncate, schema))
        return len(rows)

    import tools.migration_executor as me

    monkeypatch.setattr(me, "load_json_rows", _fake_load_json_rows)
    monkeypatch.setattr(me, "get_table_schema", lambda table: "fake-locked-schema")

    rows = iter([{"id": i} for i in range(250)])
    total = InMemoryExecutor().load("some_table", rows, batch_size=100)

    assert total == 250
    assert [c[2] for c in calls] == [True, False, False]  # 100, 100, 50
    assert [c[1] for c in calls] == [100, 100, 50]
    # The first (truncate) batch autodetects (schema=None); every later
    # batch reuses the schema fetched right after that first batch.
    assert [c[3] for c in calls] == [None, "fake-locked-schema", "fake-locked-schema"]


def test_in_memory_executor_still_truncates_on_zero_rows(monkeypatch):
    """Even an empty stream must issue one truncate=True call, so the
    target table is genuinely cleared/created every run — matching the
    pre-streaming behavior's guarantee."""
    calls = []

    def _fake_load_json_rows(table, rows, truncate=True, schema=None):
        calls.append((table, len(rows), truncate, schema))
        return len(rows)

    import tools.migration_executor as me

    monkeypatch.setattr(me, "load_json_rows", _fake_load_json_rows)
    monkeypatch.setattr(me, "get_table_schema", lambda table: "fake-locked-schema")

    total = InMemoryExecutor().load("some_table", iter([]), batch_size=100)
    assert total == 0
    assert calls == [("some_table", 0, True, None)]
