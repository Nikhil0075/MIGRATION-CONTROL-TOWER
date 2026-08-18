"""Finance Reporting Impact Agent (Block C, master doc §20.3).

The cross-department discovery proof: an agent owned by Finance Systems
— a department distinct from Technology, which owns the migration fleet
— published and approved by a distinct identity, discovered and invoked
by the orchestrator purely by capability query
(agents/orchestrator/orchestrator.py's trigger_finance_impact_check),
which has no import of, or hardcoded knowledge about, this module.

assess_impact() uses real Lineage data, not a canned answer: it finds
which discovered views look like finance-reporting views (name contains
'REVENUE' — the only real candidate in this estate today is
SH.V_QUARTERLY_REVENUE_BY_CHANNEL, derived by
tools/lineage_graph.py::parse_sql_view_dependencies from the actual
corpus SQL) and reports which upstream tables feed them — the tables
whose migration risk directly affects financial reporting.
"""

from __future__ import annotations

import datetime as dt
import logging

from tools.firestore_client import get_client

logger = logging.getLogger("finance_impact_agent")

AGENT_ID = "finance-impact-agent"
AGENT_VERSION = "1.0.0"
RUN_COLLECTION = "migration_runs"

_FINANCE_REPORT_MARKERS = ("REVENUE", "FINANCE", "P&L", "LEDGER")


def assess_impact(run_id: str) -> dict:
    """Tool: identifies finance-report views and their upstream tables for a run."""
    client = get_client()
    run_ref = client.collection(RUN_COLLECTION).document(run_id)
    edges = [d.to_dict() for d in run_ref.collection("dependencies").stream()]

    finance_views = sorted(
        {
            e["to_asset"]
            for e in edges
            if e["relationship"] == "reads"
            and any(marker in e["to_asset"].upper() for marker in _FINANCE_REPORT_MARKERS)
        }
    )

    impacted_tables = sorted(
        {e["from_asset"] for e in edges if e["relationship"] == "reads" and e["to_asset"] in finance_views}
    )

    assessment = {
        "assessed_by": AGENT_ID,
        "run_id": run_id,
        "finance_report_views": finance_views,
        "impacted_source_tables": impacted_tables,
        "assessed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    run_ref.collection("finance_impact_assessment").document("current").set(assessment)

    logger.info("assess_impact: %s", assessment)
    return assessment
