"""Migration Planner Agent (Day 5, master doc §4).

Responsibility: "Propose target design — create object mapping,
execution order, SQL conversion plan, transfer plan, rollback/checkpoint
strategy." Tool set (§4.2): metadata, translation tools, plan store;
denied: direct production cutover — the Planner proposes, it never
executes a cutover itself.

Scope discipline (master doc §12): only the run's canonical demo table
(Sales.Customers, feeding the wwi.sales.customers pipeline used
throughout Day 2-4) is actually `scheduled` for migration this run. The
rest of the catalog gets a real plan entry (target name, execution
order) but scheduled=False — "one complete happy path," not a fake claim
of migrating the whole estate.
"""

from __future__ import annotations

import datetime as dt
import logging

from tools.connection_context import DEFAULT_ESTATE_ID
from tools.firestore_client import get_client
from tools.lineage_graph import find_unresolved_dependencies
from tools.pack_loader import PackNotFound, dialect_note, get_pack, scheduled_tables
from tools.plan_builder import build_steps, build_targets, compute_plan_hash

logger = logging.getLogger("planner_agent")

AGENT_ID = "planner-agent"
AGENT_POLICY_KEY = "planner"
AGENT_VERSION = "0.1.0"

RUN_COLLECTION = "migration_runs"

#: Pack used when a run records none — the flagship demo path.
DEFAULT_PACK_ID = "wwi_sqlserver_v1"
DEFAULT_SOURCE_ID = "wwi-sqlserver"


def _scheduled_table_ids(pack: dict, tables: list[dict]) -> tuple[set[str], dict[str, str]]:
    """Which catalog table_ids this pack schedules, and their target names.

    This replaced the SCHEDULED_SOURCE_SCHEMA/_TABLE/_TARGET_TABLE/
    _DATABASE/_TABLE_ID module constants that six other modules imported
    (the orchestrator, pipeline_stages, run_full_migration,
    seed_long_horizon_fixture, run_cutover, evaluation/scenarios). Those
    constants compiled one estate's schema into the whole fleet; every
    consumer now reads the plan's targets instead, and this function
    resolves the pack's declaration against the run's actual catalog.

    A pack declaring nothing schedules nothing here — build_targets()
    derives the executable set from catalog metadata in that case.
    """
    declared = scheduled_tables(pack)
    if not declared:
        return set(), {}

    by_schema_table = {(t.get("schema"), t.get("table")): t for t in tables}
    table_ids: set[str] = set()
    target_names: dict[str, str] = {}
    for entry in declared:
        table = by_schema_table.get((entry["source_schema"], entry["source_table"]))
        if table is None:
            # Reported as a blocked target by build_targets(); nothing to
            # schedule here.
            continue
        table_ids.add(table["table_id"])
        target_names[table["table_id"]] = entry["target_table"]
    return table_ids, target_names

ROLLBACK_STRATEGY = (
    "On a FAILED validation, the run transitions to INVESTIGATING; the known "
    "remediation for defect_type=row_loss is a full clean reload via "
    "tools/migration_executor.py (drop_fraction=0.0), followed by re-running "
    "the same deterministic reconciliation checks. The pre-migration source "
    "row/hash snapshot recorded in the first reconciliation attempt is the "
    "checkpoint against which the reload is judged."
)


def propose_plan(run_id: str) -> dict:
    """Tool: builds and persists a MigrationPlan for the run.

    Returns the plan record (contracts/metadata_model.json's MigrationPlan).
    """
    client = get_client()
    run_ref = client.collection(RUN_COLLECTION).document(run_id)

    run = (run_ref.get().to_dict() or {}) if run_ref.get().exists else {}
    estate_id = run.get("estate_id") or DEFAULT_ESTATE_ID
    pack_id = run.get("pack_id") or DEFAULT_PACK_ID
    source_id = run.get("source_id") or DEFAULT_SOURCE_ID
    try:
        pack = get_pack(pack_id)
    except PackNotFound:
        logger.warning("run %s names unknown pack %r; planning without pack rules", run_id, pack_id)
        pack = {}

    tables = [d.to_dict() for d in run_ref.collection("catalog").stream()]
    risk_findings = [d.to_dict() for d in run_ref.collection("risk_findings").stream()]
    dependencies = [d.to_dict() for d in run_ref.collection("dependencies").stream()]

    # Appendix D S-07: a table whose declared upstream asset was never
    # discovered is blocked from scheduling rather than silently ordered.
    # Real integration, backward-compatible: today's estate has no
    # dangling dependency edge, so this is normally an empty set and
    # every table schedules exactly as before.
    table_ids = {t["table_id"] for t in tables}
    unresolved = find_unresolved_dependencies(table_ids, dependencies)
    blocked_table_ids = {edge["to_asset"] for edge in unresolved if edge["to_asset"] in table_ids}

    scheduled_ids, scheduled_names = _scheduled_table_ids(pack, tables)
    steps = build_steps(
        tables=tables,
        risk_findings=risk_findings,
        scheduled_table_ids=scheduled_ids,
        scheduled_target_names=scheduled_names,
        blocked_table_ids=blocked_table_ids,
        dialect_note=dialect_note(pack),
    )

    # The executable half of the plan (Day 11 Phase 3). Derived from the
    # catalog Discovery already wrote plus the pack's declared rules —
    # never from a model, and no longer from module constants. Phase 3a
    # writes these; the executor, Validation, recovery and cutover switch
    # onto them in 3b.
    targets = build_targets(
        tables, steps, pack=pack, estate_id=estate_id, source_id=source_id,
        blocked_table_ids=blocked_table_ids,
    )
    plan_hash = compute_plan_hash(steps, targets)

    plan = {
        "run_id": run_id,
        "estate_id": estate_id,
        "source_id": source_id,
        "pack_id": pack_id,
        "steps": steps,
        "targets": targets,
        "rollback_strategy": ROLLBACK_STRATEGY,
        "plan_hash": plan_hash,
        "created_by": AGENT_ID,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    run_ref.collection("migration_plan").document("current").set(plan)

    logger.info(
        "propose_plan: run=%s, estate=%s, pack=%s, steps=%d, scheduled=%d, "
        "targets=%d (scheduled=%d, blocked=%d), plan_hash=%s",
        run_id,
        estate_id,
        pack_id,
        len(steps),
        sum(1 for s in steps if s["scheduled"]),
        len(targets),
        sum(1 for t in targets if t["scheduled"]),
        sum(1 for t in targets if t["blocked"]),
        plan_hash[:12],
    )
    return plan


try:
    from google.adk.agents import Agent  # type: ignore

    planner_agent = Agent(
        name=AGENT_ID.replace("-", "_"),
        model="gemini-3.5-flash",
        description=(
            "Proposes a migration plan: target table names, execution "
            "order, SQL translation notes for dialect-incompatible "
            "tables, and a rollback strategy. Never performs a cutover."
        ),
        instruction=(
            "You plan a data migration from the discovered legacy "
            "estate to BigQuery. Use propose_plan to build and persist "
            "the plan for a run. Report which table(s) are actually "
            "scheduled this run versus deferred."
        ),
        tools=[propose_plan],
    )
    AGENT_FRAMEWORK = "google-adk"
except ImportError as exc:  # pragma: no cover
    logger.warning(
        "google-adk not importable (%s); using Rung-2 direct tool-call fallback.", exc
    )
    planner_agent = None
    AGENT_FRAMEWORK = "direct-fallback"
