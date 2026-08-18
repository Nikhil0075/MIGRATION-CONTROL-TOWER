"""Thin wrapper over agents.orchestrator.orchestrator.advance_through_
validation() (Day 5, extended Day 10 Phase 2).

Before Phase 2's hardening, this module called every stage function
directly (Planner, migration execution, Validation, Recovery) in a
straight line — convenient, but it meant the project's own claim ("the
run advances between states on events without manual invocation of each
agent") was only true for Discovery -> Lineage -> Risk; everything after
that was ordinary Python function calls with no Pub/Sub round trip at
all. That direct-call chain has moved into
agents/orchestrator/orchestrator.py's handle_risk_assessed(),
handle_planned(), handle_validation_requested(), and
handle_validation_failed() — each triggered by consuming a real message,
not by this module calling the next stage. See that module's docstring
for the full event chain and why Cutover stays outside it.

This module is kept only so existing callers
(run_full_migration.py, durability_demo.py, evaluation/scenarios.py)
don't need to change their imports.
"""

from __future__ import annotations

from agents.orchestrator.orchestrator import advance_through_validation

__all__ = ["advance_to_passed", "key_column_for"]


def key_column_for(run_id: str) -> str:
    """The key column of a run's first scheduled target.

    Replaces the re-exported `KEY_COLUMN` constant, which this module
    published and two callers imported (evaluation/scenarios.py,
    seed_long_horizon_fixture.py). That constant hardcoded one estate's
    primary key into anything that touched this module; the value now
    comes from the run's own plan.

    "First scheduled target" is a deliberate simplification for callers
    that assume a single-table run, which both of them do — a genuinely
    multi-table caller should read tools/migration_plan.py directly rather
    than ask for one key column.
    """
    from tools.migration_plan import primary_target

    return primary_target(run_id)["key_column"]


def advance_to_passed(pipeline_id: str, drop_fraction: float = 0.0, poll_timeout: float = 30.0) -> dict:
    """Runs migration.requested -> ... -> PASSED, including the recovery
    loop if drop_fraction seeds a row-loss defect. Returns a summary dict
    with the same shape this function has always returned — see
    orchestrator.py::advance_through_validation()'s docstring for how
    each stage is actually reached.
    """
    return advance_through_validation(pipeline_id, drop_fraction=drop_fraction, poll_timeout=poll_timeout)
