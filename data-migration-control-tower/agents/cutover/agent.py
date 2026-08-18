"""Cutover Agent (Day 5, master doc §4).

Responsibility: "Execute controlled final step — verify prerequisites,
request/consume approval, perform cutover action, trigger post-cutover
monitoring." Tool set (§4.2): approved migration/cutover tools; denied:
self-approval, raw PII browsing.

Four functions map directly to those four verbs. A fifth,
attempt_self_approval, is the deliberate compliance proof from §12's
Phase-5 exit condition ("cutover cannot self-approve") — the same
pattern as Risk's verify_pii_access_boundary and Discovery's denied
PII read: exercise the real policy engine against the forbidden action
and confirm it denies.
"""

from __future__ import annotations

import datetime as dt
import logging

from tools.firestore_client import get_client
from tools.policy_engine import evaluate
from tools.reconciliation import check_hash, check_row_count
from tools import approval_service
from tools.bigquery_tools import get_key_values, get_row_count as bq_row_count
from tools.sqlserver_client import get_connection
from tools.wave_manager import within_approval_window

logger = logging.getLogger("cutover_agent")

AGENT_ID = "cutover-agent"
AGENT_POLICY_KEY = "cutover"
AGENT_VERSION = "0.1.0"

RUN_COLLECTION = "migration_runs"


def _get_run(run_id: str) -> dict:
    client = get_client()
    return client.collection(RUN_COLLECTION).document(run_id).get().to_dict()


def _get_plan_hash(run_id: str) -> str:
    client = get_client()
    plan = (
        client.collection(RUN_COLLECTION)
        .document(run_id)
        .collection("migration_plan")
        .document("current")
        .get()
        .to_dict()
    )
    if plan is None:
        raise ValueError(f"No migration plan found for run {run_id!r} — Planner must run first.")
    return plan["plan_hash"]


def attempt_self_approval(run_id: str) -> dict:
    """Tool: the §5.2/§12 proof — Cutover cannot issue its own approval token.

    Exercises the real policy engine for the forbidden action. This
    agent's code never calls tools/approval_service.approve() anywhere
    else in this file; that function is only ever invoked by
    agents/cutover/approve_cutover.py, a script standing in for a human.
    """
    decision = evaluate(
        agent_key=AGENT_POLICY_KEY,
        action="approval.self_issue",
        resource_class="PRODUCTION",
        run_id=run_id,
    )
    if decision["decision"] != "DENY":  # pragma: no cover — should never happen
        logger.error("SEPARATION-OF-DUTIES VIOLATION: %s", decision)
    else:
        logger.info("attempt_self_approval: denied as expected — %s", decision["policy_id"])
    return decision


def request_approval(run_id: str) -> dict:
    """Tool: verifies prerequisites and opens a human-approval request."""
    run = _get_run(run_id)
    if run["state"] != "PASSED":
        raise ValueError(
            f"Cannot request cutover approval: run {run_id!r} is in state "
            f"{run['state']!r}, not PASSED."
        )
    plan_hash = _get_plan_hash(run_id)
    record = approval_service.request_approval(run_id, plan_hash, requested_by=AGENT_ID)
    logger.info("request_approval: run=%s, plan_hash=%s", run_id, plan_hash[:12])
    return record


def perform_cutover(run_id: str) -> dict:
    """Tool: consumes the human-issued approval token and performs the cutover action.

    "Cutover action" for this fixture is a recorded, auditable state
    change (the migrated target table is marked authoritative for the
    pipeline) — there is no second physical system to fail traffic over
    to in this demo estate.

    Gated by tools/wave_manager.py::within_approval_window() —
    disabled by default (see policies/wave_limits.yaml) so this
    project's own demo/evaluation runs are never blocked by wall-clock
    time, but the check is real and holds if a deployment enables it.
    """
    if not within_approval_window():
        raise RuntimeError(
            f"Cannot perform cutover for run {run_id!r}: outside the declared approval "
            "window (policies/wave_limits.yaml's approval_window)."
        )
    plan_hash = _get_plan_hash(run_id)

    # Audit trail: log that this action is approval-gated (a static
    # policy declaration), separate from the real enforcement below.
    evaluate(AGENT_POLICY_KEY, "cutover.execute.approved", "PRODUCTION", run_id)

    token = approval_service.consume(run_id, expected_plan_hash=plan_hash)  # raises if invalid

    cutover_record = {
        "performed_by": AGENT_ID,
        "approval_token_id": token["token_id"],
        "approved_by": token["approved_by"],
        "plan_hash": plan_hash,
        "cutover_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    client = get_client()
    client.collection(RUN_COLLECTION).document(run_id).collection("cutover").document(
        "current"
    ).set(cutover_record)

    logger.info("perform_cutover: run=%s, token=%s", run_id, token["token_id"])
    return cutover_record


def trigger_post_cutover_monitoring(
    run_id: str, source_schema: str, source_table: str, target_table: str, key_column: str
) -> dict:
    """Tool: one-shot post-cutover reconciliation confirmation.

    §21.1 describes *periodic* re-reconciliation via Cloud Scheduler as
    the durable, weeks-long version of this; that's later-day scope. This
    is the one-shot proof the cutover data is still intact immediately
    after cutover.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {source_schema}.{source_table}")
        source_count = cursor.fetchone()[0]
        cursor.execute(
            f"SELECT CAST({key_column} AS NVARCHAR(50)) FROM {source_schema}.{source_table} "
            f"ORDER BY {key_column}"
        )
        source_keys = [r[0] for r in cursor.fetchall()]
    finally:
        conn.close()

    target_count = bq_row_count(target_table)
    target_keys = get_key_values(target_table, key_column)

    checks = [check_row_count(source_count, target_count), check_hash(source_keys, target_keys)]
    passed = all(c["status"] == "PASS" for c in checks)

    monitoring_record = {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": checks,
        "status": "HEALTHY" if passed else "DEGRADED",
    }
    client = get_client()
    client.collection(RUN_COLLECTION).document(run_id).collection("monitoring").document(
        "post_cutover"
    ).set(monitoring_record)

    logger.info("trigger_post_cutover_monitoring: run=%s, status=%s", run_id, monitoring_record["status"])
    return monitoring_record


try:
    from google.adk.agents import Agent  # type: ignore

    cutover_agent = Agent(
        name=AGENT_ID.replace("-", "_"),
        model="gemini-3.5-flash",
        description=(
            "Executes the controlled final migration step: requests "
            "human approval, consumes it, performs cutover, and confirms "
            "post-cutover health. Cannot approve its own request."
        ),
        instruction=(
            "You perform the final, human-approved cutover for a "
            "migration run. Never call approval_service.approve — that "
            "is a human action, not yours. Use request_approval, then "
            "wait for a human to approve, then perform_cutover, then "
            "trigger_post_cutover_monitoring."
        ),
        tools=[request_approval, perform_cutover, trigger_post_cutover_monitoring, attempt_self_approval],
    )
    AGENT_FRAMEWORK = "google-adk"
except ImportError as exc:  # pragma: no cover
    logger.warning(
        "google-adk not importable (%s); using Rung-2 direct tool-call fallback.", exc
    )
    cutover_agent = None
    AGENT_FRAMEWORK = "direct-fallback"
