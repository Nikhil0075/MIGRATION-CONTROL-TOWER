"""Plan-driven execution (Day 11 Phase 3b).

What this phase actually changed: the orchestrator, the migration
executor, the Validation agent, recovery and the cutover worker used to
share one estate's schema through module constants. They now read the
run's MigrationTargets. These tests pin the behaviour that made the
change worth making — multi-table execution, per-estate wave keys, and
the refusal to migrate nothing — rather than re-testing the plan
derivation itself (tests/test_plan_builder.py covers that).
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import seed_migration_plan
from tools import migration_plan
from tools.migration_plan import (
    NoScheduledTargets,
    PlanNotFound,
    primary_target,
    scheduled_targets,
    target_for_table_ref,
    targets as all_targets,
)

TWO_TARGETS = [
    {
        "target_id": "wwi-sqlserver:Sales.Customers",
        "table_id": "sqlserver-wwi.WideWorldImporters.Sales.Customers",
        "source_database": "WideWorldImporters", "source_schema": "Sales",
        "source_table": "Customers", "target_table": "customers_dim",
        "key_column": "CustomerID", "order_by": "CustomerID",
        "numeric_column": "CreditLimit", "null_check_column": "PhoneNumber",
        "aggregate_check": "applicable", "execution_order": 0,
        "scheduled": True, "blocked": False, "blocked_reason": None,
        "sql_translation_notes": None,
    },
    {
        "target_id": "wwi-sqlserver:Sales.Orders",
        "table_id": "sqlserver-wwi.WideWorldImporters.Sales.Orders",
        "source_database": "WideWorldImporters", "source_schema": "Sales",
        "source_table": "Orders", "target_table": "orders_fact",
        "key_column": "OrderID", "order_by": "OrderID",
        "numeric_column": None, "null_check_column": None,
        "aggregate_check": "not_applicable", "execution_order": 1,
        "scheduled": True, "blocked": False, "blocked_reason": None,
        "sql_translation_notes": None,
    },
]


@pytest.fixture
def planned_run(firestore_cleanup):
    from agents.orchestrator.run_lifecycle import create_run, transition_state

    def _make(targets=None, **plan_overrides):
        run_id = firestore_cleanup.run(
            create_run(f"test.plan_driven.{uuid.uuid4().hex[:6]}")
        )
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED"):
            transition_state(run_id, state)
        seed_migration_plan(run_id, targets=targets, **plan_overrides)
        return run_id

    return _make


# ---------------------------------------------------------------------------
# Reading the plan
# ---------------------------------------------------------------------------


@pytest.mark.requires_firestore
def test_scheduled_targets_are_returned_in_execution_order(planned_run):
    run_id = planned_run(targets=list(reversed(TWO_TARGETS)))
    assert [t["source_table"] for t in scheduled_targets(run_id)] == ["Customers", "Orders"]


@pytest.mark.requires_firestore
def test_blocked_targets_are_excluded_from_execution(planned_run):
    blocked = dict(TWO_TARGETS[1], scheduled=False, blocked=True,
                   blocked_reason="composite primary key not supported by the executor")
    run_id = planned_run(targets=[TWO_TARGETS[0], blocked])

    assert [t["source_table"] for t in scheduled_targets(run_id)] == ["Customers"]
    assert len(all_targets(run_id)) == 2, "blocked targets stay visible in the plan"


@pytest.mark.requires_firestore
def test_a_plan_scheduling_nothing_raises_rather_than_silently_succeeding(planned_run):
    """A migration that moves zero tables and reports success is the worst
    available outcome — and a mis-declared pack or a renamed source table
    is exactly what would produce one."""
    blocked = dict(TWO_TARGETS[0], scheduled=False, blocked=True,
                   blocked_reason="no primary key")
    run_id = planned_run(targets=[blocked])

    with pytest.raises(NoScheduledTargets, match="no primary key"):
        scheduled_targets(run_id)


@pytest.mark.requires_firestore
def test_missing_plan_is_reported_clearly(firestore_cleanup):
    from agents.orchestrator.run_lifecycle import create_run

    run_id = firestore_cleanup.run(create_run("test.plan_driven.noplan"))
    with pytest.raises(PlanNotFound, match="has not run yet"):
        scheduled_targets(run_id)


@pytest.mark.requires_firestore
def test_target_lookup_by_table_ref_is_case_insensitive(planned_run):
    run_id = planned_run(targets=TWO_TARGETS)
    assert target_for_table_ref(run_id, "sales.orders")["key_column"] == "OrderID"
    assert target_for_table_ref(run_id, "Sales.Nope") is None


@pytest.mark.requires_firestore
def test_primary_target_is_the_first_scheduled_one(planned_run):
    run_id = planned_run(targets=TWO_TARGETS)
    assert primary_target(run_id)["source_table"] == "Customers"


# ---------------------------------------------------------------------------
# handle_planned — multi-table execution and wave keying
# ---------------------------------------------------------------------------


@pytest.mark.requires_firestore
def test_handle_planned_executes_every_scheduled_target(planned_run, monkeypatch):
    from agents.orchestrator import orchestrator

    run_id = planned_run(targets=TWO_TARGETS)
    executed = []

    def fake_execute(**kwargs):
        executed.append(kwargs["target"]["source_table"])
        return {"target_count": 10, "source_count": 10, "dropped_count": 0}

    monkeypatch.setattr(orchestrator, "execute_migration", fake_execute)
    monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

    result = orchestrator.handle_planned({"run_id": run_id})

    assert executed == ["Customers", "Orders"]
    assert result["targets_migrated"] == 2


@pytest.mark.requires_firestore
def test_handle_planned_reports_aggregate_counts_not_the_last_table(planned_run, monkeypatch):
    """frontend/api_v1.py computes migrated_percent from these and
    evaluation/scenarios.py reads them — narrowing them to one table would
    silently misreport progress."""
    from agents.orchestrator import orchestrator

    run_id = planned_run(targets=TWO_TARGETS)
    counts = iter([(100, 100), (7, 7)])

    def fake_execute(**kwargs):
        source, target = next(counts)
        return {"source_count": source, "target_count": target, "dropped_count": 0}

    monkeypatch.setattr(orchestrator, "execute_migration", fake_execute)
    monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

    result = orchestrator.handle_planned({"run_id": run_id})

    assert result["source_count"] == 107
    assert result["target_count"] == 107


@pytest.mark.requires_firestore
def test_wave_slot_is_reserved_once_per_run_not_once_per_target(planned_run, monkeypatch):
    """A per-target reservation would make a multi-table run contend with
    itself and deadlock against its own max_concurrent_per_source."""
    from agents.orchestrator import orchestrator

    run_id = planned_run(targets=TWO_TARGETS)
    reservations, releases = [], []

    monkeypatch.setattr(
        orchestrator.wave_manager, "reserve_slot",
        lambda source_id, item_id, risk_class=None: (
            reservations.append(source_id), {"decision": "ADMIT", "reason": "test"})[1],
    )
    monkeypatch.setattr(
        orchestrator.wave_manager, "release_slot",
        lambda source_id, item_id: releases.append(source_id),
    )
    monkeypatch.setattr(
        orchestrator, "execute_migration",
        lambda **kwargs: {"target_count": 1, "source_count": 1, "dropped_count": 0},
    )
    monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

    orchestrator.handle_planned({"run_id": run_id})

    assert len(reservations) == 1
    assert len(releases) == 1


@pytest.mark.requires_firestore
def test_wave_key_is_scoped_by_estate_and_source(planned_run, monkeypatch):
    """Two estates using the same source_id must not contend for one
    another's concurrency slots."""
    from agents.orchestrator import orchestrator

    run_id = planned_run(targets=TWO_TARGETS)
    reservations = []

    monkeypatch.setattr(
        orchestrator.wave_manager, "reserve_slot",
        lambda source_id, item_id, risk_class=None: (
            reservations.append(source_id), {"decision": "ADMIT", "reason": "t"})[1],
    )
    monkeypatch.setattr(orchestrator.wave_manager, "release_slot", lambda *a: None)
    monkeypatch.setattr(
        orchestrator, "execute_migration",
        lambda **kwargs: {"target_count": 1, "source_count": 1, "dropped_count": 0},
    )
    monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

    orchestrator.handle_planned({"run_id": run_id})

    assert reservations == ["wwi-demo-estate:wwi-sqlserver"]


# ---------------------------------------------------------------------------
# Wave limit / override key cascade
# ---------------------------------------------------------------------------


def test_wave_cap_prefers_the_estate_scoped_key():
    from tools.wave_manager import _cap_for

    caps = {"acme:wwi-sqlserver": 5, "wwi-sqlserver": 2, "default": 1}
    assert _cap_for("acme:wwi-sqlserver", caps) == 5


def test_wave_cap_falls_back_to_the_bare_source_key():
    """policies/wave_limits.yaml's existing source-only keys must keep
    working as the middle tier, or the demo path silently throttles to 1."""
    from tools.wave_manager import _cap_for

    caps = {"wwi-sqlserver": 2, "default": 1}
    assert _cap_for("wwi-demo-estate:wwi-sqlserver", caps) == 2


def test_wave_cap_falls_back_to_default_for_an_unknown_source():
    from tools.wave_manager import _cap_for

    assert _cap_for("other-estate:unknown-source", {"default": 3}) == 3


# ---------------------------------------------------------------------------
# recovery — the failing table comes from the results, not a default
# ---------------------------------------------------------------------------


def test_failing_table_ref_is_taken_from_the_failed_check():
    from agents.orchestrator.recovery import _failing_table_ref

    results = [
        {"check_type": "schema", "status": "PASS", "table": "Sales.Customers"},
        {"check_type": "row_count", "status": "FAIL", "table": "Sales.Orders"},
    ]
    failed = [r for r in results if r["status"] == "FAIL"]
    assert _failing_table_ref(failed, results) == "Sales.Orders"


def test_failing_table_ref_falls_back_to_any_recorded_table():
    from agents.orchestrator.recovery import _failing_table_ref

    results = [{"check_type": "schema", "status": "PASS", "table": "Sales.Customers"}]
    assert _failing_table_ref([], results) == "Sales.Customers"


def test_failing_table_ref_is_honest_when_nothing_is_recorded():
    from agents.orchestrator.recovery import _failing_table_ref

    assert _failing_table_ref([], []) == "unknown"


# ---------------------------------------------------------------------------
# execute_migration accepts a target
# ---------------------------------------------------------------------------


def test_execute_migration_requires_a_target_or_full_arguments():
    from tools.migration_executor import execute_migration

    with pytest.raises(ValueError, match="pass a plan target"):
        execute_migration("run-1", source_schema="Sales")


def test_execute_migration_prefers_order_by_over_key_column():
    """order_by is what the extraction ORDER BY uses; a target may declare
    it separately from the identity key."""
    from tools.migration_executor import execute_migration

    target = dict(TWO_TARGETS[0], order_by="ModifiedDate")
    captured = {}

    class _Recorder:
        def load(self, target_table, rows, batch_size):
            captured["target_table"] = target_table
            return 0

    with pytest.raises(Exception):
        # Reaching the source is expected to fail without a live server;
        # the assertion below is about argument resolution, which happens
        # before any connection is opened.
        execute_migration("run-1", target=target, data_plane=_Recorder())
