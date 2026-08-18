#!/usr/bin/env python
"""Seeds the row-loss defect from master doc §7.2's failure-injection matrix:

    Injected condition: Row loss — drop/filter 0.2%-1% of target rows
    Expected autonomous action: Row-count/hash checks fail; lineage is
    traversed to identify upstream filter.

Thin wrapper over tools/migration_executor.py's drop_fraction option —
that module is the single real data-movement path (used identically by
the Migration Planner's first-pass execution and by this standalone
fixture script), so the row-fetch/type-conversion logic isn't duplicated
in two places that could quietly drift apart.

Usage (from repo root):
    python simulator/failure_injector/seed_row_loss.py [run_id]

Note: as of Day 5, agents/orchestrator/run_full_migration.py seeds this
same defect as part of the full milestone chain automatically. This
script remains useful for seeding a defect on an already-existing run
when debugging Validation in isolation (see root README's step-by-step
section).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.orchestrator.run_lifecycle import get_latest_run_id, transition_state  # noqa: E402
from tools.migration_executor import execute_migration  # noqa: E402

SOURCE_SCHEMA = "Sales"
SOURCE_TABLE = "Customers"
TARGET_TABLE = "customers_dim"  # matches dag_wwi_customers_full_load.py's declared downstream table
KEY_COLUMN = "CustomerID"
DROP_FRACTION = 0.01  # ~1% row loss, top of the §7.2-specified 0.2%-1% range


def seed_row_loss(run_id: str) -> dict:
    return execute_migration(
        run_id=run_id,
        source_schema=SOURCE_SCHEMA,
        source_table=SOURCE_TABLE,
        target_table=TARGET_TABLE,
        key_column=KEY_COLUMN,
        drop_fraction=DROP_FRACTION,
    )


def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else get_latest_run_id()
    print(f"[failure-injector] seeding row-loss defect for run: {run_id}")

    # If the run is freshly PLANNED (the standalone step-by-step flow),
    # this script also stands in for "perform the migration" and moves
    # PLANNED -> MIGRATING. If it's already past that (e.g. re-seeding
    # for a repeat debugging session), leave state alone rather than error.
    try:
        transition_state(run_id, "MIGRATING")
    except ValueError as exc:
        print(f"[failure-injector] state transition skipped ({exc})")

    manifest = seed_row_loss(run_id)
    print(f"[failure-injector] {manifest}")
    dropped_fraction = manifest["dropped_count"] / manifest["source_count"]
    print(
        f"[failure-injector] loaded {manifest['target_count']} of "
        f"{manifest['source_count']} source rows into "
        f"{{dataset}}.{TARGET_TABLE} — {manifest['dropped_count']} rows "
        f"({dropped_fraction:.2%}) deliberately dropped."
    )
    if manifest["excluded_columns"]:
        print(f"[failure-injector] note: columns excluded from load: {manifest['excluded_columns']}")


if __name__ == "__main__":
    main()
