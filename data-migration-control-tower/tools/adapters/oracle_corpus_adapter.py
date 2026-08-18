"""Oracle-dialect corpus SourceAdapter (Day 10 Phase 3).

This project's second, structurally distinct source family — a
different dialect (NVL/DECODE/CONNECT BY), no live database connection
at all (master doc §18.3's explicit "no live Oracle dependency" rule).
Used for Phase 3's second-estate onboarding proof
(packs/oracle_corpus_v1/) — the same SourceAdapter interface, the same
Discovery -> Lineage -> Risk -> Planner assessment flow
(agents/discovery/run_assessment.py), a real second source proven
pluggable without needing brand-new live infrastructure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from tools.adapters.base import CAPABILITY_DISCOVER, SourceAdapter
from tools.source_catalog import catalog_oracle_corpus


class OracleCorpusAdapter(SourceAdapter):
    system = "oracle-corpus"

    #: Discovery only, and that is the honest declaration — there is no
    #: live Oracle instance behind this corpus (§18.3), so it can neither
    #: transfer rows nor be reconciled against a target. Declaring it here
    #: means the console disables execution for this source rather than
    #: offering an action that would fail at run time.
    capabilities = frozenset({CAPABILITY_DISCOVER})

    def __init__(
        self,
        corpus_path: str | Path = "simulator/source_setup/oracle_dialect_corpus",
        *,
        binding=None,
    ):
        self.corpus_path = corpus_path
        self.binding = binding

    def discover_tables(self) -> list[dict]:
        return catalog_oracle_corpus(self.corpus_path)

    def discover_pipelines(self) -> list[dict]:
        # The corpus has no DAG/scheduling metadata of its own.
        return []

    def fetch_rows(self, table_id: str, order_by: str) -> Iterator[dict]:
        raise NotImplementedError(
            f"OracleCorpusAdapter has no live database to fetch rows from "
            f"(table_id={table_id!r}) — this corpus is discovery/assessment-only "
            "per master doc §18.3. A future OracleAdapter backed by a real "
            "connection would implement this the same way SqlServerAdapter does."
        )
