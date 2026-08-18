"""Validation & Reconciliation Agent (Day 3, master doc §4).

Responsibility: "Run deterministic row/schema/hash/aggregate/null/
uniqueness checks; explain failures." The checks themselves
(tools/reconciliation.py) are pure arithmetic/set comparison — this
agent's job is fetching the source/target values to feed them and
recording the results, never deciding what counts as a pass (§9).

Tool set (§4.2): source/target query execution with masked output, hash
compute — read-only aggregate/count/hash queries against each table the
run's plan schedules; no raw row-level PII is ever pulled back to the
caller, only counts, sums, and hashed/opaque key lists.

Day 11 Phase 3b removed this agent's six module constants
(SOURCE_SCHEMA/SOURCE_TABLE/TARGET_TABLE/KEY_COLUMN/NUMERIC_COLUMN/
NULL_CHECK_COLUMN) and the raw pyodbc queries that used them. Which
tables to reconcile, and on which columns, now comes from the run's
MigrationTargets; the source-side queries moved behind
SourceAdapter.source_facts(), so reconciling a different source family is
an adapter implementation rather than an edit to this file.

Two consequences worth stating, because both are visible in output:
  - A run reconciles every scheduled target, so `checks` may contain
    several tables' results. overall_status is PASSED only if every check
    of every target passed.
  - A target whose table has no numeric or no nullable column omits that
    check and records why, rather than comparing against a fabricated
    zero. tools/reconciliation.py only returns PASS/FAIL, so a check it
    cannot meaningfully run must not be run at all.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from tools.bigquery_tools import (
    get_column_names as bq_columns,
    get_key_values as bq_keys,
    get_null_count as bq_nulls,
    get_numeric_sum as bq_sum,
    get_row_count as bq_row_count,
)
from tools.firestore_client import get_client
from tools.reconciliation import (
    check_aggregate,
    check_hash,
    check_null_profile,
    check_row_count,
    check_schema,
)
from tools.adapters import build_adapter_for_binding
from tools.migration_plan import plan_binding, scheduled_targets

logger = logging.getLogger("validation_agent")

AGENT_ID = "validation-agent"
AGENT_POLICY_KEY = "validation"
AGENT_VERSION = "0.1.0"

RUN_COLLECTION = "migration_runs"

def _check_one_target(adapter, target: dict) -> list[dict]:
    """The five deterministic checks for one scheduled table.

    Source facts come from the adapter (one round trip); target facts from
    BigQuery. Both are keyed on column names carried by `target`, so this
    function names no table and no column of its own.
    """
    source = adapter.source_facts(target)
    target_table = target["target_table"]

    results = [
        check_schema(source["columns"], bq_columns(target_table)),
        check_row_count(source["row_count"], bq_row_count(target_table)),
    ]

    numeric_column = target.get("numeric_column")
    if numeric_column and source.get("numeric_sum") is not None:
        results.append(
            check_aggregate(source["numeric_sum"], bq_sum(target_table, numeric_column))
        )
    else:
        logger.info(
            "run_reconciliation: skipping aggregate check for %s — no numeric column "
            "(aggregate_check=%s)", target["target_id"], target.get("aggregate_check"),
        )

    null_check_column = target.get("null_check_column")
    if null_check_column and source.get("null_count") is not None:
        results.append(
            check_null_profile(source["null_count"], bq_nulls(target_table, null_check_column))
        )
    else:
        logger.info(
            "run_reconciliation: skipping null-profile check for %s — no nullable column",
            target["target_id"],
        )

    results.append(check_hash(source["keys"], bq_keys(target_table, target["key_column"])))
    return results


def run_reconciliation(run_id: str) -> dict:
    """Tool: runs the deterministic checks for every scheduled target.

    Signature unchanged — the orchestrator resolves this through the Agent
    Registry by capability, so its arity is a wire contract shared with
    every APPROVED card, including versions pinned into completed runs.

    Returns {"overall_status": "PASSED"|"FAILED", "checks": [...]}.
    """
    targets = scheduled_targets(run_id)
    adapter = build_adapter_for_binding(plan_binding(run_id))

    client = get_client()
    run_ref = client.collection(RUN_COLLECTION).document(run_id)
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    all_results: list[dict] = []
    checks_summary: list[dict] = []

    for target in targets:
        table_ref = f"{target['source_schema']}.{target['source_table']}"
        results = _check_one_target(adapter, target)
        all_results.extend(results)

        for result in results:
            record = {
                **result,
                "run_id": run_id,
                # Unchanged shape: the Control Tower's reconciliation view
                # and recovery's incident signatures both key on this.
                "table": table_ref,
                "target_id": target["target_id"],
                "checked_at": now,
            }
            run_ref.collection("reconciliation").document(
                f"{result['check_type']}_{uuid.uuid4().hex[:8]}"
            ).set(record)

        checks_summary.extend(
            {"check_type": r["check_type"], "status": r["status"], "table": table_ref}
            for r in results
        )

    overall_status = (
        "PASSED" if all_results and all(r["status"] == "PASS" for r in all_results) else "FAILED"
    )

    summary = {
        "run_id": run_id,
        "overall_status": overall_status,
        "targets": len(targets),
        "checks": checks_summary,
    }
    logger.info("run_reconciliation: %s", summary)
    return summary


try:
    from google.adk.agents import Agent  # type: ignore

    validation_agent = Agent(
        name=AGENT_ID.replace("-", "_"),
        model="gemini-3.5-flash",
        description=(
            "Runs deterministic schema/row-count/aggregate/null/hash "
            "checks between the legacy source and the BigQuery target. "
            "Pass/fail thresholds are fixed arithmetic, never a model "
            "judgment call."
        ),
        instruction=(
            "You validate a migration run's source-to-target data. Use "
            "run_reconciliation to execute all deterministic checks and "
            "report which, if any, failed and why."
        ),
        tools=[run_reconciliation],
    )
    AGENT_FRAMEWORK = "google-adk"
except ImportError as exc:  # pragma: no cover
    logger.warning(
        "google-adk not importable (%s); using Rung-2 direct tool-call fallback.", exc
    )
    validation_agent = None
    AGENT_FRAMEWORK = "direct-fallback"
