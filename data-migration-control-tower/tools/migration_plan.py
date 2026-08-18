"""Reading a run's MigrationPlan (Day 11 Phase 3).

One read point, deliberately. The orchestrator, the migration executor,
the Validation agent, recovery and the cutover worker all need to know
"which tables is this run migrating, keyed on what?" — and before Phase 3
they each answered it by importing the same module constants
(agents/planner/agent.py's SCHEDULED_*, orchestrator's KEY_COLUMN,
validation's own six). Five consumers, five copies of one assumption, and
no way to change it without editing all five.

They now all read the plan the Planner wrote. This module is that read,
so the shape of `migration_plan/current` is known in exactly one place.
"""

from __future__ import annotations

from tools.firestore_client import get_client

RUN_COLLECTION = "migration_runs"
PLAN_COLLECTION = "migration_plan"
PLAN_DOC = "current"


class PlanNotFound(LookupError):
    pass


class NoScheduledTargets(LookupError):
    pass


def get_plan(run_id: str) -> dict:
    snapshot = (
        get_client()
        .collection(RUN_COLLECTION)
        .document(run_id)
        .collection(PLAN_COLLECTION)
        .document(PLAN_DOC)
        .get()
    )
    if not snapshot.exists:
        raise PlanNotFound(
            f"Run {run_id!r} has no migration plan — the Planner has not run yet "
            f"(the run should be in PLANNED or later)."
        )
    return snapshot.to_dict() or {}


def targets(run_id: str) -> list[dict]:
    """Every target, blocked ones included, in execution order."""
    plan = get_plan(run_id)
    return sorted(plan.get("targets") or [], key=lambda t: t.get("execution_order", 0))


def scheduled_targets(run_id: str) -> list[dict]:
    """The targets this run actually migrates, in execution order.

    Raises rather than returning [] when a plan schedules nothing: a
    migration that silently moves zero tables and reports success is the
    worst available outcome, and it is exactly what a mis-declared pack or
    a renamed source table would produce.
    """
    plan = get_plan(run_id)
    all_targets = plan.get("targets") or []
    scheduled = [t for t in all_targets if t.get("scheduled") and not t.get("blocked")]
    if not scheduled:
        blocked = [
            f"{t.get('source_schema')}.{t.get('source_table')}: {t.get('blocked_reason')}"
            for t in all_targets if t.get("blocked")
        ]
        raise NoScheduledTargets(
            f"Run {run_id!r} has no scheduled migration targets. "
            + (f"Blocked: {blocked}" if blocked else
               "The plan declares no targets at all — check the pack's scheduled_tables.")
        )
    return sorted(scheduled, key=lambda t: t.get("execution_order", 0))


def primary_target(run_id: str) -> dict:
    """The first scheduled target, for callers that assume one table.

    Several standalone scripts and the evaluation harness were written
    around a single-table run and used the SCHEDULED_* constants directly.
    They now ask the plan instead. This is a deliberate simplification, not
    a claim that runs are single-table: a genuinely multi-table caller
    should iterate scheduled_targets() rather than call this.
    """
    return scheduled_targets(run_id)[0]


def target_for_table_ref(run_id: str, table_ref: str) -> dict | None:
    """Finds a target by 'Schema.Table', the form reconciliation records and
    incident signatures use."""
    wanted = table_ref.strip().lower()
    for target in targets(run_id):
        candidate = f"{target.get('source_schema')}.{target.get('source_table')}".lower()
        if candidate == wanted:
            return target
    return None


def plan_binding(run_id: str):
    """The SourceBinding this run's plan executes against."""
    from tools.connection_context import binding_for_run

    plan = get_plan(run_id)
    return binding_for_run(run_id, source_id=plan.get("source_id"))


def pack_id_for(run_id: str) -> str | None:
    return get_plan(run_id).get("pack_id")
