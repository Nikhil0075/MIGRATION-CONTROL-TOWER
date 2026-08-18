"""Migration Planner's toolset (Day 5, master doc §4): "object mapping,
execution order, SQL conversion plan, transfer plan, rollback/checkpoint
strategy." Deterministic — the plan's *shape* is fixed arithmetic/sorting
over already-derived facts (catalog, RiskFindings), not a model's guess.

execution_order note: a true dependency-graph topological sort would be
the ideal ordering signal, but Lineage's edges today connect tables to
*pipelines* and *views* (§4's "orchestration edges"), not table-to-table
foreign-key relationships — sorting on those wouldn't actually order
anything for most of this estate. Instead, execution_order uses the
signal Risk already derived: tables feeding a CRITICAL pipeline
(RiskFinding.CRITICAL_DEPENDENCY) migrate first, alphabetical tiebreak
otherwise. A real topological sort is future work once Lineage captures
genuine table-to-table FK edges.
"""

from __future__ import annotations

import hashlib
import json

#: Used when a pack declares no dialect_notes. Previously this Oracle-specific
#: sentence was hardcoded here and applied to every source family, so a SQL
#: Server estate with a DIALECT_INCOMPATIBILITY finding was told about
#: NVL/DECODE/CONNECT BY constructs it had never contained.
_GENERIC_DIALECT_NOTE = (
    "Source dialect construct not directly supported by BigQuery; requires "
    "SQL translation before load."
)

BLOCKED_NO_PRIMARY_KEY = (
    "no primary key — the executor orders extraction by a single key column, and "
    "reconciliation's hash check compares ordered key lists; neither has a "
    "deterministic meaning without one"
)
BLOCKED_COMPOSITE_PRIMARY_KEY = (
    "composite primary key not supported by the executor — picking one column of "
    "the key arbitrarily would make both the drop-fraction row ordering and the "
    "hash check silently wrong rather than visibly unsupported"
)


def build_steps(
    tables: list[dict],
    risk_findings: list[dict],
    scheduled_table_ids: set[str],
    scheduled_target_names: dict[str, str],
    blocked_table_ids: set[str] = frozenset(),
    dialect_note: str | None = None,
) -> list[dict]:
    """Returns MigrationPlan.steps (contracts/metadata_model.json).

    scheduled_table_ids: table_ids this run will actually migrate.
    scheduled_target_names: table_id -> real target table name for
    scheduled tables (must match what tools/migration_executor.py
    actually loads into, e.g. 'customers_dim').
    blocked_table_ids (Day 10, Appendix D S-07): table_ids that have an
    unresolved upstream dependency (tools/lineage_graph.find_unresolved_
    dependencies). A blocked table is always forced to scheduled=False
    regardless of scheduled_table_ids — the Planner refuses to order a
    table whose declared source can't be resolved, rather than silently
    proceeding and letting migration_executor discover the problem.
    """
    critical_table_ids = {
        f["table_id"] for f in risk_findings if f["finding_type"] == "CRITICAL_DEPENDENCY"
    }
    note = dialect_note or _GENERIC_DIALECT_NOTE
    dialect_notes = {
        f["table_id"]: note
        for f in risk_findings
        if f["finding_type"] == "DIALECT_INCOMPATIBILITY"
    }

    ordered_tables = sorted(
        tables,
        key=lambda t: (t["table_id"] not in critical_table_ids, t["table_id"]),
    )

    steps = []
    for order, table in enumerate(ordered_tables):
        table_id = table["table_id"]
        blocked = table_id in blocked_table_ids
        scheduled = table_id in scheduled_table_ids and not blocked
        target_table = (
            scheduled_target_names[table_id]
            if scheduled
            else _proposed_target_name(table_id)
        )
        steps.append(
            {
                "table_id": table_id,
                "target_table": target_table,
                "execution_order": order,
                "scheduled": scheduled,
                "sql_translation_notes": dialect_notes.get(table_id),
                "execution_blocked": blocked,
                "blocked_reason": (
                    "unresolved upstream dependency — a lineage edge names an "
                    "upstream asset that was never discovered in this run's catalog"
                    if blocked
                    else None
                ),
            }
        )
    return steps


def _proposed_target_name(table_id: str) -> str:
    """Generic proposed BigQuery table name for a not-yet-scheduled table."""
    parts = table_id.split(".")
    schema, table = parts[-2], parts[-1]
    return f"{schema}_{table}".lower()


# ---------------------------------------------------------------------------
# MigrationTargets — the executable half of the plan (Day 11 Phase 3)
# ---------------------------------------------------------------------------


def _columns(table: dict) -> list[dict]:
    return [c for c in (table.get("columns") or []) if c.get("name")]


def _pick_numeric_column(table: dict, key_column: str, numeric_types: set[str]) -> str | None:
    for column in _columns(table):
        if column["name"] == key_column:
            continue
        if str(column.get("data_type", "")).lower() in numeric_types:
            return column["name"]
    return None


def _pick_null_check_column(table: dict, key_column: str) -> str | None:
    """First genuinely nullable non-key column.

    `is_nullable is None` means the adapter could not determine nullability
    (the static Oracle DDL corpus) — deliberately not treated as nullable,
    because a null-profile check on a NOT NULL column only ever compares
    0 against 0 and looks like a passing check while proving nothing.
    """
    for column in _columns(table):
        if column["name"] == key_column:
            continue
        if column.get("is_nullable") is True:
            return column["name"]
    return None


def _blocked_target(table: dict, reason: str, order: int, source_id: str) -> dict:
    schema, table_name = table.get("schema", ""), table.get("table", "")
    return {
        "target_id": f"{source_id}:{schema}.{table_name}",
        "table_id": table["table_id"],
        "source_database": table.get("database", ""),
        "source_schema": schema,
        "source_table": table_name,
        "target_table": _proposed_target_name(table["table_id"]),
        "key_column": None,
        "order_by": None,
        "numeric_column": None,
        "null_check_column": None,
        "aggregate_check": "not_applicable",
        "execution_order": order,
        "scheduled": False,
        "blocked": True,
        "blocked_reason": reason,
        "sql_translation_notes": None,
    }


def build_targets(
    tables: list[dict],
    steps: list[dict],
    *,
    pack: dict | None = None,
    estate_id: str = "",
    source_id: str = "",
    blocked_table_ids: set[str] = frozenset(),
) -> list[dict]:
    """Resolves which tables this run migrates, and with which columns.

    Two derivation paths, explicit first:

    **Declared** — when the pack lists `scheduled_tables`, those entries win
    verbatim. This is how the committed WWI pack reproduces the previous
    SCHEDULED_*/KEY_COLUMN/NUMERIC_COLUMN/NULL_CHECK_COLUMN constants
    exactly, which is what makes deleting them provably behavior-preserving.

    **Derived** — for a newly onboarded estate with no declared tables,
    every catalogued table with a usable single-column primary key is
    scheduled, and its columns come from discovered metadata plus the
    pack's numeric type rules. Nothing here is a model judgment; all of it
    is arithmetic over facts Discovery already wrote (CLAUDE.md's core
    rule).

    Tables that cannot be executed honestly are blocked with a stated
    reason rather than scheduled on a guess — no primary key, a composite
    primary key, or an unresolved upstream dependency.
    """
    from tools.pack_loader import scheduled_tables as declared_tables
    from tools.pack_loader import type_rules as pack_type_rules

    pack = pack or {}
    numeric_types = {t.lower() for t in pack_type_rules(pack)["numeric"]}
    declared = declared_tables(pack)

    order_by_table_id = {s["table_id"]: s["execution_order"] for s in steps}
    notes_by_table_id = {s["table_id"]: s.get("sql_translation_notes") for s in steps}
    by_schema_table = {
        (t.get("schema"), t.get("table")): t for t in tables if t.get("table_id")
    }

    targets: list[dict] = []

    if declared:
        for entry in declared:
            key = (entry["source_schema"], entry["source_table"])
            table = by_schema_table.get(key)
            if table is None:
                # Declared but absent from the catalog: surface it as a
                # blocked target rather than dropping it silently, or a
                # renamed source table becomes an invisible no-op run.
                targets.append({
                    "target_id": f"{source_id}:{key[0]}.{key[1]}",
                    "table_id": "",
                    "source_database": "",
                    "source_schema": key[0],
                    "source_table": key[1],
                    "target_table": entry["target_table"],
                    "key_column": entry.get("key_column"),
                    "order_by": entry.get("key_column"),
                    "numeric_column": entry.get("numeric_column"),
                    "null_check_column": entry.get("null_check_column"),
                    "aggregate_check": "not_applicable",
                    "execution_order": len(targets),
                    "scheduled": False,
                    "blocked": True,
                    "blocked_reason": (
                        f"pack schedules {key[0]}.{key[1]}, which this run's catalog does "
                        f"not contain — the source table may have been renamed or dropped"
                    ),
                    "sql_translation_notes": None,
                })
                continue

            table_id = table["table_id"]
            blocked = table_id in blocked_table_ids
            key_column = entry.get("key_column") or (
                table.get("primary_key")[0] if len(table.get("primary_key") or []) == 1 else None
            )
            numeric_column = entry.get("numeric_column")
            targets.append({
                "target_id": f"{source_id}:{key[0]}.{key[1]}",
                "table_id": table_id,
                "source_database": table.get("database", ""),
                "source_schema": key[0],
                "source_table": key[1],
                "target_table": entry["target_table"],
                "key_column": key_column,
                "order_by": entry.get("order_by") or key_column,
                "numeric_column": numeric_column,
                "null_check_column": entry.get("null_check_column"),
                "aggregate_check": "applicable" if numeric_column else "not_applicable",
                "execution_order": order_by_table_id.get(table_id, len(targets)),
                "scheduled": not blocked,
                "blocked": blocked,
                "blocked_reason": (
                    "unresolved upstream dependency — a lineage edge names an upstream "
                    "asset that was never discovered in this run's catalog"
                    if blocked else None
                ),
                "sql_translation_notes": notes_by_table_id.get(table_id),
            })
        return sorted(targets, key=lambda t: t["execution_order"])

    for table in tables:
        table_id = table.get("table_id")
        if not table_id:
            continue
        order = order_by_table_id.get(table_id, len(targets))
        primary_key = table.get("primary_key") or []

        if table_id in blocked_table_ids:
            targets.append(_blocked_target(
                table,
                "unresolved upstream dependency — a lineage edge names an upstream asset "
                "that was never discovered in this run's catalog",
                order, source_id))
            continue
        if not primary_key:
            targets.append(_blocked_target(table, BLOCKED_NO_PRIMARY_KEY, order, source_id))
            continue
        if len(primary_key) > 1:
            targets.append(_blocked_target(
                table, BLOCKED_COMPOSITE_PRIMARY_KEY, order, source_id))
            continue

        key_column = primary_key[0]
        numeric_column = _pick_numeric_column(table, key_column, numeric_types)
        targets.append({
            "target_id": f"{source_id}:{table.get('schema')}.{table.get('table')}",
            "table_id": table_id,
            "source_database": table.get("database", ""),
            "source_schema": table.get("schema", ""),
            "source_table": table.get("table", ""),
            "target_table": _proposed_target_name(table_id),
            "key_column": key_column,
            "order_by": key_column,
            "numeric_column": numeric_column,
            "null_check_column": _pick_null_check_column(table, key_column),
            "aggregate_check": "applicable" if numeric_column else "not_applicable",
            "execution_order": order,
            "scheduled": True,
            "blocked": False,
            "blocked_reason": None,
            "sql_translation_notes": notes_by_table_id.get(table_id),
        })

    return sorted(targets, key=lambda t: t["execution_order"])


def compute_plan_hash(steps: list[dict], targets: list[dict] | None = None) -> str:
    """Stable hash binding an approval token to this exact plan (§21.1) —
    a changed plan invalidates any outstanding approval.

    `targets` is included when present, deliberately. Targets name the
    tables and columns that will actually be read and written; leaving them
    unsigned would let the executable content of an approved plan change
    without invalidating the approval, which is precisely what
    tools/approval_service.py::consume() exists to prevent.

    The consequence is intended and worth stating plainly: plans that carry
    targets hash differently from the pre-Phase-3 plans they replace, so any
    approval outstanding across that upgrade must be re-issued. Callers
    passing steps alone still get the original hash — which is what
    tests/test_plan_builder.py's golden-hash gate relies on to prove step
    generation itself did not change.
    """
    canonical = json.dumps(steps, sort_keys=True)
    if targets is not None:
        canonical = json.dumps({"steps": steps, "targets": targets}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
