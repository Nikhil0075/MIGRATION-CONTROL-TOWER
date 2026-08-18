"""Tests for tools/lineage_graph.py — pure functions, no live services needed."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.lineage_graph import (  # noqa: E402
    find_unresolved_dependencies,
    parse_pipeline_dependencies,
    parse_sql_view_dependencies,
)

ORACLE_CORPUS_PATH = REPO_ROOT / "simulator" / "source_setup" / "oracle_dialect_corpus"


def test_pipeline_dependencies_derive_reads_and_writes_edges():
    pipelines = [
        {
            "pipeline_id": "wwi.sales.customers",
            "upstream_tables": ["Sales.Customers", "Application.People"],
            "downstream_tables": ["bigquery.migration_target.customers_dim"],
        }
    ]
    edges = parse_pipeline_dependencies(pipelines)
    assert len(edges) == 3

    reads = [e for e in edges if e["relationship"] == "reads"]
    writes = [e for e in edges if e["relationship"] == "writes"]
    assert {e["from_asset"] for e in reads} == {"Sales.Customers", "Application.People"}
    assert all(e["to_asset"] == "wwi.sales.customers" for e in reads)
    assert writes[0] == {
        "from_asset": "wwi.sales.customers",
        "to_asset": "bigquery.migration_target.customers_dim",
        "relationship": "writes",
        "discovered_by": "lineage-agent",
        "confidence": 1.0,
        "source": "dag_reference",
    }


def test_pipeline_dependencies_high_confidence():
    edges = parse_pipeline_dependencies(
        [{"pipeline_id": "p", "upstream_tables": ["T"], "downstream_tables": []}]
    )
    assert edges[0]["confidence"] == 1.0
    assert edges[0]["source"] == "dag_reference"


def test_sql_view_dependencies_derived_from_real_corpus():
    edges = parse_sql_view_dependencies(ORACLE_CORPUS_PATH)
    assert len(edges) >= 6  # 2 views, 3 tables referenced each, at time of writing

    view_targets = {e["to_asset"] for e in edges}
    assert "CO.V_CUSTOMER_ACCOUNT_SUMMARY" in view_targets
    assert "SH.V_QUARTERLY_REVENUE_BY_CHANNEL" in view_targets

    customer_summary_sources = {
        e["from_asset"] for e in edges if e["to_asset"] == "CO.V_CUSTOMER_ACCOUNT_SUMMARY"
    }
    assert customer_summary_sources == {"CO.CUSTOMERS", "CO.ORDERS", "HR.EMPLOYEES"}


def test_sql_view_dependencies_lower_confidence_than_dag_edges():
    edges = parse_sql_view_dependencies(ORACLE_CORPUS_PATH)
    assert all(e["confidence"] < 1.0 for e in edges)
    assert all(e["source"] == "sql_view_parse" for e in edges)


def test_sql_view_dependencies_never_self_references():
    edges = parse_sql_view_dependencies(ORACLE_CORPUS_PATH)
    assert all(e["from_asset"] != e["to_asset"] for e in edges)


# --- Day 10 addition (master doc Appendix D, S-07) -----------------------


def test_find_unresolved_dependencies_flags_missing_upstream_table():
    table_ids = {"Sales.Customers", "Sales.Orders"}
    dependencies = [
        {"from_asset": "Sales.NeverDiscovered", "to_asset": "Sales.Orders", "relationship": "reads"},
        {"from_asset": "Sales.Customers", "to_asset": "Sales.Orders", "relationship": "reads"},
    ]
    unresolved = find_unresolved_dependencies(table_ids, dependencies)
    assert len(unresolved) == 1
    assert unresolved[0]["from_asset"] == "Sales.NeverDiscovered"


def test_find_unresolved_dependencies_ignores_write_edges():
    table_ids = {"Sales.Customers"}
    dependencies = [
        {"from_asset": "wwi.sales.customers", "to_asset": "bq.customers_dim", "relationship": "writes"},
    ]
    assert find_unresolved_dependencies(table_ids, dependencies) == []


def test_find_unresolved_dependencies_empty_when_all_resolved():
    table_ids = {"Sales.Customers", "Sales.Orders"}
    dependencies = [
        {"from_asset": "Sales.Customers", "to_asset": "Sales.Orders", "relationship": "reads"},
    ]
    assert find_unresolved_dependencies(table_ids, dependencies) == []
