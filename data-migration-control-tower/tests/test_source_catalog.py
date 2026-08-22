"""Tests for tools/source_catalog.py.

The SQL Server tests are skipped automatically when the local/simulated
on-prem container isn't reachable (e.g. CI without Docker) — the Oracle
corpus and DAG artifact tests never need a live database and always run,
so `pytest tests/` is meaningful even without `docker compose up`.

Day 2 exit condition (master doc §17.1, Mon 17 Aug): a run inventories
12-20 tables plus SQL and DAG artifacts. This file asserts the pieces
that make that condition true; agents/discovery/run_discovery.py is the
end-to-end script that also persists them to Firestore.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools.source_catalog import (  # noqa: E402
    catalog_dag_artifacts,
    catalog_oracle_corpus,
    catalog_postgres_tables,
    catalog_sql_server_tables,
)
from tools.sqlserver_client import get_connection  # noqa: E402

ORACLE_CORPUS_PATH = REPO_ROOT / "simulator" / "source_setup" / "oracle_dialect_corpus"
DAG_ARTIFACTS_PATH = REPO_ROOT / "simulator" / "source_setup" / "dags"


def _sql_server_reachable() -> bool:
    try:
        conn = get_connection()
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


SQL_SERVER_AVAILABLE = _sql_server_reachable()
skip_if_no_sql_server = pytest.mark.skipif(
    not SQL_SERVER_AVAILABLE,
    reason="SQL Server container not reachable — run `docker compose up -d` "
    "and `./restore_wwi.sh` in simulator/source_setup/ first.",
)


# --------------------------------------------------------------------------
# Oracle-dialect corpus (always runs, no live DB required)
# --------------------------------------------------------------------------


def test_oracle_corpus_returns_well_formed_records():
    records = catalog_oracle_corpus(ORACLE_CORPUS_PATH)
    assert len(records) >= 8  # 10 at time of writing across CO/SH/HR

    required_fields = {"table_id", "system", "database", "schema", "table", "classification"}
    for record in records:
        assert required_fields.issubset(record.keys())
        assert record["classification"] == "UNCLASSIFIED"
        assert record["system"] == "oracle-corpus"


def test_oracle_corpus_captures_expected_tables():
    records = catalog_oracle_corpus(ORACLE_CORPUS_PATH)
    table_names = {r["table"] for r in records}
    for expected in {"CUSTOMERS", "ORDERS", "EMPLOYEES", "SALES", "PRODUCTS"}:
        assert expected in table_names


def test_oracle_corpus_extracts_primary_keys():
    records = catalog_oracle_corpus(ORACLE_CORPUS_PATH)
    by_table = {r["table"]: r for r in records}
    assert by_table["CUSTOMERS"]["primary_key"] == ["CUSTOMER_ID"]
    assert by_table["EMPLOYEES"]["primary_key"] == ["EMPLOYEE_ID"]


# --------------------------------------------------------------------------
# DAG artifacts (always runs, no live DB required)
# --------------------------------------------------------------------------


def test_dag_artifacts_returns_well_formed_pipelines():
    records = catalog_dag_artifacts(DAG_ARTIFACTS_PATH)
    assert 3 <= len(records) <= 6

    required_fields = {
        "pipeline_id",
        "source_system",
        "target_system",
        "schedule",
        "owner",
        "criticality",
        "code_path",
        "status",
    }
    for record in records:
        assert required_fields.issubset(record.keys())
        assert record["status"] == "ACTIVE"


def test_dag_artifacts_include_upstream_tables_for_lineage():
    records = catalog_dag_artifacts(DAG_ARTIFACTS_PATH)
    for record in records:
        assert len(record["upstream_tables"]) > 0


# --------------------------------------------------------------------------
# SQL Server (skipped unless the container is running)
# --------------------------------------------------------------------------


@skip_if_no_sql_server
def test_sql_server_tables_well_formed():
    conn = get_connection()
    try:
        records = catalog_sql_server_tables(conn)
    finally:
        conn.close()

    assert len(records) > 0
    for record in records:
        assert record["system"] == "sqlserver-wwi"
        assert record["classification"] == "UNCLASSIFIED"


@skip_if_no_sql_server
def test_estate_meets_day2_exit_condition_table_count():
    """12-20+ tables across SQL Server + Oracle corpus (master doc §17.1)."""
    conn = get_connection()
    try:
        sql_server_records = catalog_sql_server_tables(conn)
    finally:
        conn.close()
    oracle_records = catalog_oracle_corpus(ORACLE_CORPUS_PATH)

    total = len(sql_server_records) + len(oracle_records)
    assert total >= 12, f"expected >= 12 tables for the Day 2 exit condition, got {total}"


# --------------------------------------------------------------------------
# Postgres (fake cursor — no live DB required)
#
# Regression coverage for a real bug found against a live Cloud SQL
# instance (Deploy & Harden, 2026-08-22): the original primary-key query
# read information_schema.table_constraints / key_column_usage, both of
# which the SQL standard gates on has_table_privilege(oid, 'INSERT,
# UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') — SELECT is deliberately
# not in that list, so a genuinely least-privilege read-only role (exactly
# what migration_readonly is) saw zero primary-key columns on every table
# it could otherwise query fine. Every local/manual run before this had
# connected as the `postgres` superuser, which trivially passes that
# check, so the bug was never observable until a real least-privilege
# role hit it live. The fix reads pg_constraint/pg_class/pg_attribute
# directly, which carry no such privilege gate.
# --------------------------------------------------------------------------


class _FakeCursor:
    """Replays canned rows keyed by a substring of the executed SQL, so a
    test can drive catalog_postgres_tables() without a real connection."""

    def __init__(self, responses: dict[str, list[tuple]]):
        self._responses = responses
        self._last_key: str | None = None
        self.executed_sql: list[str] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed_sql.append(sql)
        for key in self._responses:
            if key in sql:
                self._last_key = key
                return
        raise AssertionError(f"no fake response registered for SQL: {sql!r}")

    def fetchall(self) -> list[tuple]:
        return self._responses[self._last_key]

    def fetchone(self) -> tuple | None:
        rows = self._responses[self._last_key]
        return rows[0] if rows else None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _make_fake_postgres_conn(*, primary_key_rows: list[tuple]) -> tuple[_FakeConn, _FakeCursor]:
    cursor = _FakeCursor({
        "FROM information_schema.tables": [("retail", "orders")],
        "FROM information_schema.columns": [
            ("order_id", "integer", "NO"),
            ("line_no", "integer", "NO"),
        ],
        "FROM pg_constraint": primary_key_rows,
        "reltuples": [(100,)],
        "pg_total_relation_size": [(4096,)],
    })
    return _FakeConn(cursor), cursor


def test_postgres_primary_key_query_reads_pg_catalog_not_information_schema():
    """The fix: no query anywhere in catalog_postgres_tables() should touch
    information_schema.table_constraints or key_column_usage — those are
    exactly the views a least-privilege SELECT-only role can't see rows in."""
    conn, cursor = _make_fake_postgres_conn(primary_key_rows=[("order_id",)])
    catalog_postgres_tables(conn, database="retail")
    for sql in cursor.executed_sql:
        assert "information_schema.table_constraints" not in sql
        assert "information_schema.key_column_usage" not in sql


def test_postgres_composite_primary_key_preserves_declared_column_order():
    """order_items-style composite key: (order_id, line_no) must come back
    in that order, not alphabetical or arbitrary — the Planner blocks
    composite keys, but only after seeing the real column list."""
    conn, _ = _make_fake_postgres_conn(
        primary_key_rows=[("order_id",), ("line_no",)]
    )
    records = catalog_postgres_tables(conn, database="retail")
    assert records[0]["primary_key"] == ["order_id", "line_no"]


def test_postgres_table_with_no_primary_key_reports_empty_list():
    conn, _ = _make_fake_postgres_conn(primary_key_rows=[])
    records = catalog_postgres_tables(conn, database="retail")
    assert records[0]["primary_key"] == []
