#!/usr/bin/env python
"""Safely bind the registered demo SQL Server source to its execution pack.

Dry-run is the default.  ``--apply`` changes only a missing ``pack_id`` on
the one named source, records the normal estate revision, and refuses to
replace a different value.
"""

from __future__ import annotations

import argparse
import copy
import json

from tools.connection_context import DEFAULT_ESTATE_ID
from tools.estate_registry import EstateConflict, get_estate, update_estate

SOURCE_ID = "wwi-sqlserver"
PACK_ID = "wwi_sqlserver_v1"
ACTOR = "demo-pack-targeted-backfill"


def backfill_demo_pack(*, apply: bool = False) -> dict:
    estate = get_estate(DEFAULT_ESTATE_ID)
    sources = copy.deepcopy(estate.get("sources") or [])
    source = next((item for item in sources if item.get("source_id") == SOURCE_ID), None)
    if source is None:
        raise EstateConflict(
            f"Estate {DEFAULT_ESTATE_ID!r} has no source {SOURCE_ID!r}; refusing to patch."
        )
    existing = source.get("pack_id")
    if existing and existing != PACK_ID:
        raise EstateConflict(
            f"Source {SOURCE_ID!r} already declares conflicting pack_id {existing!r}; "
            f"refusing to replace it with {PACK_ID!r}."
        )
    if existing == PACK_ID:
        return {"status": "unchanged", "estate_id": DEFAULT_ESTATE_ID, "pack_id": PACK_ID}
    if not apply:
        return {"status": "dry_run", "estate_id": DEFAULT_ESTATE_ID, "pack_id": PACK_ID}

    source["pack_id"] = PACK_ID
    update_estate(
        DEFAULT_ESTATE_ID,
        {"sources": sources},
        actor=ACTOR,
        reason="targeted_backfill:missing_demo_pack_id",
    )
    return {"status": "applied", "estate_id": DEFAULT_ESTATE_ID, "pack_id": PACK_ID}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the targeted patch.")
    args = parser.parse_args()
    print(json.dumps(backfill_demo_pack(apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
