"""Backfills estate_id onto migration runs created before it existed.

Runs created before Day 11 Phase 2 carry no estate_id, source_id or
pack_id. Every reader already treats a missing estate_id as the demo
estate — that fallback is deliberate and is what keeps the Control Tower
dashboard populated between deploying the estate filter and running this
script. So this backfill is a tidy-up, not a prerequisite: nothing breaks
if it is never run.

    python scripts/backfill_estate_id.py --dry-run
    python scripts/backfill_estate_id.py
    python scripts/backfill_estate_id.py --estate-id acme-legacy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools.connection_context import DEFAULT_ESTATE_ID  # noqa: E402
from tools.firestore_client import get_client  # noqa: E402

RUN_COLLECTION = "migration_runs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--estate-id", default=DEFAULT_ESTATE_ID)
    parser.add_argument("--source-id", default=None, help="also set source_id where absent")
    parser.add_argument("--pack-id", default=None, help="also set pack_id where absent")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client = get_client()
    scanned = 0
    updated = 0

    for snapshot in client.collection(RUN_COLLECTION).stream():
        scanned += 1
        run = snapshot.to_dict() or {}
        patch = {}
        if not run.get("estate_id"):
            patch["estate_id"] = args.estate_id
        if args.source_id and not run.get("source_id"):
            patch["source_id"] = args.source_id
        if args.pack_id and not run.get("pack_id"):
            patch["pack_id"] = args.pack_id
        if not patch:
            continue
        updated += 1
        print(f"{'would update' if args.dry_run else 'updating'} {snapshot.id}: {patch}")
        if not args.dry_run:
            snapshot.reference.update(patch)

    verb = "would update" if args.dry_run else "updated"
    print(f"\nscanned {scanned} run(s), {verb} {updated}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
