"""DAG/scheduling-artifact SourceAdapter (Day 10 Phase 3).

Wraps tools/source_catalog.py::catalog_dag_artifacts. This "source" has
pipelines but no tables of its own — it declares upstream/downstream
table references that Lineage resolves against whatever other
adapter(s) an estate.yaml pairs it with, exactly as
agents/lineage/agent.py::build_dependency_graph already does today.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from tools.adapters.base import CAPABILITY_DISCOVER, SourceAdapter
from tools.source_catalog import catalog_dag_artifacts


class DagArtifactAdapter(SourceAdapter):
    system = "dag-artifacts"

    #: Pipeline metadata only — no tables, no rows, nothing to reconcile.
    capabilities = frozenset({CAPABILITY_DISCOVER})

    def __init__(self, dag_path: str | Path = "simulator/source_setup/dags", *, binding=None):
        self.dag_path = dag_path
        self.binding = binding

    def discover_tables(self) -> list[dict]:
        return []

    def discover_pipelines(self) -> list[dict]:
        return catalog_dag_artifacts(self.dag_path)

    def fetch_rows(self, table_id: str, order_by: str) -> Iterator[dict]:
        raise NotImplementedError(
            "DagArtifactAdapter has no tables of its own to fetch rows from "
            f"(table_id={table_id!r}) — it only declares pipeline metadata."
        )
