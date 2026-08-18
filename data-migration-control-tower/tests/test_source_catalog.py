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
