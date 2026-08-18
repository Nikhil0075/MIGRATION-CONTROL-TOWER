"""Tests for tools/plan_builder.py — pure functions, no live services needed."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.plan_builder import build_steps, compute_plan_hash  # noqa: E402

TABLES = [
    {"table_id": "sys.a.Zebra.Table"},
    {"table_id": "sys.a.Apple.Table"},
    {"table_id": "sys.a.Mango.Table"},
]


def test_critical_dependency_tables_scheduled_first():
    findings = [{"finding_type": "CRITICAL_DEPENDENCY", "table_id": "sys.a.Mango.Table"}]
    steps = build_steps(TABLES, findings, scheduled_table_ids=set(), scheduled_target_names={})
    assert steps[0]["table_id"] == "sys.a.Mango.Table"
    assert steps[0]["execution_order"] == 0


def test_non_critical_tables_alphabetical_tiebreak():
    steps = build_steps(TABLES, [], scheduled_table_ids=set(), scheduled_target_names={})
    table_ids = [s["table_id"] for s in steps]
    assert table_ids == ["sys.a.Apple.Table", "sys.a.Mango.Table", "sys.a.Zebra.Table"]


def test_scheduled_table_gets_its_real_target_name():
    steps = build_steps(
        TABLES,
        [],
        scheduled_table_ids={"sys.a.Apple.Table"},
        scheduled_target_names={"sys.a.Apple.Table": "customers_dim"},
    )
    apple_step = next(s for s in steps if s["table_id"] == "sys.a.Apple.Table")
    assert apple_step["scheduled"] is True
    assert apple_step["target_table"] == "customers_dim"


def test_unscheduled_tables_get_a_proposed_name_not_scheduled():
    steps = build_steps(TABLES, [], scheduled_table_ids=set(), scheduled_target_names={})
    assert all(s["scheduled"] is False for s in steps)
    assert all(s["target_table"] for s in steps)  # every step still gets a proposed name


def test_dialect_incompatibility_finding_adds_translation_note():
    findings = [{"finding_type": "DIALECT_INCOMPATIBILITY", "table_id": "sys.a.Apple.Table"}]
    steps = build_steps(TABLES, findings, scheduled_table_ids=set(), scheduled_target_names={})
    apple_step = next(s for s in steps if s["table_id"] == "sys.a.Apple.Table")
    zebra_step = next(s for s in steps if s["table_id"] == "sys.a.Zebra.Table")
    assert apple_step["sql_translation_notes"] is not None
    assert zebra_step["sql_translation_notes"] is None


def test_plan_hash_is_stable_for_identical_steps():
    steps_a = build_steps(TABLES, [], scheduled_table_ids=set(), scheduled_target_names={})
    steps_b = build_steps(TABLES, [], scheduled_table_ids=set(), scheduled_target_names={})
    assert compute_plan_hash(steps_a) == compute_plan_hash(steps_b)


def test_plan_hash_changes_if_steps_change():
    steps_a = build_steps(TABLES, [], scheduled_table_ids=set(), scheduled_target_names={})
    steps_b = build_steps(
        TABLES,
        [],
        scheduled_table_ids={"sys.a.Apple.Table"},
        scheduled_target_names={"sys.a.Apple.Table": "customers_dim"},
    )
    assert compute_plan_hash(steps_a) != compute_plan_hash(steps_b)


# --- Day 10 addition (master doc Appendix D, S-07) -----------------------


def test_blocked_table_is_never_scheduled_even_if_requested():
    steps = build_steps(
        TABLES,
        [],
        scheduled_table_ids={"sys.a.Apple.Table"},
        scheduled_target_names={"sys.a.Apple.Table": "customers_dim"},
        blocked_table_ids={"sys.a.Apple.Table"},
    )
    apple_step = next(s for s in steps if s["table_id"] == "sys.a.Apple.Table")
    assert apple_step["execution_blocked"] is True
    assert apple_step["scheduled"] is False
    assert apple_step["blocked_reason"] is not None


def test_unblocked_tables_have_no_blocked_reason():
    steps = build_steps(TABLES, [], scheduled_table_ids=set(), scheduled_target_names={})
    assert all(s["execution_blocked"] is False for s in steps)
    assert all(s["blocked_reason"] is None for s in steps)


# --- Day 11 Phase 3: MigrationTargets -----------------------------------
#
# The gate first. Everything below it is only safe because of it.

import json  # noqa: E402
import pytest  # noqa: E402

from tools.plan_builder import (  # noqa: E402
    BLOCKED_COMPOSITE_PRIMARY_KEY,
    BLOCKED_NO_PRIMARY_KEY,
    build_targets,
)

FIXTURE_CATALOG = REPO_ROOT / "tests" / "fixtures" / "wwi_catalog.json"

#: Computed from the real 48-table WideWorldImporters catalog with the
#: pre-refactor build_steps(), before scheduled_tables/type_rules/targets
#: existed and before the SCHEDULED_* constants were touched. This value is
#: the whole safety argument for Phase 3: the constants may only be deleted
#: if the plan they produced is reproduced byte-for-byte from configuration.
#: If this test fails, the refactor changed what gets migrated — do not
#: update the constant, find out what moved.
GOLDEN_PLAN_HASH = "e29a4c4b2cce5bd507235495bd617c9af5602ad6320108a372b60494e5521d1a"

WWI_SCHEDULED_TABLE_ID = "sqlserver-wwi.WideWorldImporters.Sales.Customers"


@pytest.fixture(scope="module")
def wwi_catalog() -> list[dict]:
    return json.loads(FIXTURE_CATALOG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def wwi_pack() -> dict:
    from tools.pack_loader import get_pack

    return get_pack("wwi_sqlserver_v1")


def test_wwi_plan_hash_is_unchanged_by_the_targets_refactor(wwi_catalog):
    """The Phase 3 gate — see GOLDEN_PLAN_HASH."""
    steps = build_steps(
        tables=wwi_catalog,
        risk_findings=[],
        scheduled_table_ids={WWI_SCHEDULED_TABLE_ID},
        scheduled_target_names={WWI_SCHEDULED_TABLE_ID: "customers_dim"},
    )
    assert compute_plan_hash(steps) == GOLDEN_PLAN_HASH


def test_pack_declaration_reproduces_the_old_constants_exactly(wwi_catalog, wwi_pack):
    """The committed pack must schedule exactly what the deleted module
    constants did: Sales.Customers -> customers_dim, keyed on CustomerID,
    aggregating CreditLimit, null-checking PhoneNumber."""
    steps = build_steps(
        tables=wwi_catalog, risk_findings=[],
        scheduled_table_ids={WWI_SCHEDULED_TABLE_ID},
        scheduled_target_names={WWI_SCHEDULED_TABLE_ID: "customers_dim"},
    )
    targets = build_targets(
        wwi_catalog, steps, pack=wwi_pack,
        estate_id="wwi-demo-estate", source_id="wwi-sqlserver",
    )
    scheduled = [t for t in targets if t["scheduled"]]
    assert len(scheduled) == 1
    target = scheduled[0]
    assert target["source_schema"] == "Sales"
    assert target["source_table"] == "Customers"
    assert target["target_table"] == "customers_dim"
    assert target["key_column"] == "CustomerID"
    assert target["order_by"] == "CustomerID"
    assert target["numeric_column"] == "CreditLimit"
    assert target["null_check_column"] == "PhoneNumber"
    assert target["aggregate_check"] == "applicable"
    assert target["table_id"] == WWI_SCHEDULED_TABLE_ID


def test_every_target_validates_against_the_contract(wwi_catalog, wwi_pack):
    import jsonschema

    from tests.test_contracts import DEFINITIONS

    steps = build_steps(wwi_catalog, [], set(), {})
    targets = build_targets(wwi_catalog, steps, pack=wwi_pack, source_id="wwi-sqlserver")
    for target in targets:
        jsonschema.validate(instance=target, schema=DEFINITIONS["MigrationTarget"])


# --- Derivation, for an estate whose pack declares nothing --------------

DERIVED_PACK = {"pack_id": "derived", "type_rules": {"numeric": ["int", "decimal"]}}


def _table(name, primary_key, columns, schema="dbo", database="db"):
    return {
        "table_id": f"src.{database}.{schema}.{name}",
        "system": "src", "database": database, "schema": schema, "table": name,
        "classification": "UNCLASSIFIED", "primary_key": primary_key, "columns": columns,
    }


def test_derives_key_numeric_and_null_columns_from_catalog_metadata():
    table = _table("orders", ["order_id"], [
        {"name": "order_id", "data_type": "int", "is_nullable": False},
        {"name": "customer_name", "data_type": "varchar", "is_nullable": False},
        {"name": "total", "data_type": "decimal", "is_nullable": False},
        {"name": "note", "data_type": "varchar", "is_nullable": True},
    ])
    steps = build_steps([table], [], set(), {})
    target = build_targets([table], steps, pack=DERIVED_PACK, source_id="src")[0]

    assert target["scheduled"] is True
    assert target["key_column"] == "order_id"
    assert target["numeric_column"] == "total"
    assert target["null_check_column"] == "note"
    assert target["aggregate_check"] == "applicable"


def test_composite_primary_key_is_blocked_not_guessed():
    table = _table("order_items", ["order_id", "line_no"], [
        {"name": "order_id", "data_type": "int", "is_nullable": False},
        {"name": "line_no", "data_type": "int", "is_nullable": False},
    ])
    steps = build_steps([table], [], set(), {})
    target = build_targets([table], steps, pack=DERIVED_PACK, source_id="src")[0]

    assert target["blocked"] is True
    assert target["scheduled"] is False
    assert target["key_column"] is None
    assert target["blocked_reason"] == BLOCKED_COMPOSITE_PRIMARY_KEY


def test_table_without_a_primary_key_is_blocked():
    table = _table("audit_log", [], [{"name": "message", "data_type": "varchar", "is_nullable": True}])
    steps = build_steps([table], [], set(), {})
    target = build_targets([table], steps, pack=DERIVED_PACK, source_id="src")[0]

    assert target["blocked"] is True
    assert target["blocked_reason"] == BLOCKED_NO_PRIMARY_KEY


def test_no_numeric_column_marks_the_aggregate_check_not_applicable():
    """The check is omitted with a recorded reason, never fabricated as a pass."""
    table = _table("tags", ["tag_id"], [
        {"name": "tag_id", "data_type": "int", "is_nullable": False},
        {"name": "label", "data_type": "varchar", "is_nullable": True},
    ])
    steps = build_steps([table], [], set(), {})
    target = build_targets([table], steps, pack=DERIVED_PACK, source_id="src")[0]

    assert target["numeric_column"] is None
    assert target["aggregate_check"] == "not_applicable"
    assert target["scheduled"] is True  # still migratable, just one fewer check


def test_unknown_nullability_is_not_treated_as_nullable():
    """is_nullable=None means the adapter couldn't tell (the static Oracle
    DDL corpus). A null check on a NOT NULL column compares 0 to 0 and
    looks like it passed while proving nothing."""
    table = _table("legacy", ["id"], [
        {"name": "id", "data_type": "int", "is_nullable": None},
        {"name": "descr", "data_type": "varchar", "is_nullable": None},
    ])
    steps = build_steps([table], [], set(), {})
    target = build_targets([table], steps, pack=DERIVED_PACK, source_id="src")[0]

    assert target["null_check_column"] is None


def test_key_column_is_never_chosen_as_the_numeric_or_null_column():
    table = _table("ids", ["id"], [
        {"name": "id", "data_type": "int", "is_nullable": True},
    ])
    steps = build_steps([table], [], set(), {})
    target = build_targets([table], steps, pack=DERIVED_PACK, source_id="src")[0]

    assert target["numeric_column"] is None
    assert target["null_check_column"] is None


def test_declared_table_missing_from_the_catalog_is_surfaced_not_dropped():
    """A renamed source table must not turn the run into a silent no-op."""
    pack = {"pack_id": "p", "scheduled_tables": [
        {"source_schema": "Sales", "source_table": "Gone", "target_table": "gone_dim"}
    ]}
    table = _table("orders", ["order_id"], [{"name": "order_id", "data_type": "int"}])
    steps = build_steps([table], [], set(), {})
    targets = build_targets([table], steps, pack=pack, source_id="src")

    assert len(targets) == 1
    assert targets[0]["blocked"] is True
    assert "does not contain" in targets[0]["blocked_reason"]


def test_blocked_table_ids_propagate_to_targets():
    table = _table("orders", ["order_id"], [{"name": "order_id", "data_type": "int"}])
    steps = build_steps([table], [], set(), {})
    targets = build_targets(
        [table], steps, pack=DERIVED_PACK, source_id="src",
        blocked_table_ids={table["table_id"]},
    )
    assert targets[0]["blocked"] is True
    assert "unresolved upstream dependency" in targets[0]["blocked_reason"]


def test_targets_are_ordered_by_execution_order():
    tables = [
        _table("b", ["id"], [{"name": "id", "data_type": "int"}]),
        _table("a", ["id"], [{"name": "id", "data_type": "int"}]),
    ]
    steps = build_steps(tables, [], set(), {})
    targets = build_targets(tables, steps, pack=DERIVED_PACK, source_id="src")
    assert [t["execution_order"] for t in targets] == sorted(t["execution_order"] for t in targets)
    assert targets[0]["source_table"] == "a"  # alphabetical tiebreak, same as steps


# --- plan_hash semantics -------------------------------------------------


def test_including_targets_changes_the_plan_hash():
    """Targets are executable content; leaving them unsigned would let an
    approved plan's real effect change without invalidating the approval."""
    steps = build_steps(TABLES, [], set(), {})
    targets = build_targets(TABLES, steps, pack=DERIVED_PACK, source_id="src")
    assert compute_plan_hash(steps) != compute_plan_hash(steps, targets)


def test_plan_hash_with_targets_is_stable():
    steps = build_steps(TABLES, [], set(), {})
    targets = build_targets(TABLES, steps, pack=DERIVED_PACK, source_id="src")
    assert compute_plan_hash(steps, targets) == compute_plan_hash(steps, targets)


# --- dialect notes now come from the pack --------------------------------


def test_dialect_note_comes_from_the_pack_not_a_hardcoded_oracle_sentence():
    findings = [{"finding_type": "DIALECT_INCOMPATIBILITY", "table_id": "sys.a.Apple.Table"}]
    steps = build_steps(TABLES, findings, set(), {}, dialect_note="Teradata QUALIFY clause.")
    apple = next(s for s in steps if s["table_id"] == "sys.a.Apple.Table")
    assert apple["sql_translation_notes"] == "Teradata QUALIFY clause."


def test_dialect_note_falls_back_to_a_source_agnostic_sentence():
    findings = [{"finding_type": "DIALECT_INCOMPATIBILITY", "table_id": "sys.a.Apple.Table"}]
    steps = build_steps(TABLES, findings, set(), {})
    apple = next(s for s in steps if s["table_id"] == "sys.a.Apple.Table")
    assert "NVL" not in apple["sql_translation_notes"]
    assert "SQL translation" in apple["sql_translation_notes"]
