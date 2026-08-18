"""Seeds the committed estate YAML into the Firestore estate registry.

Run after infrastructure/gcp_setup.sh and alongside seed_registry.py —
the orchestrator can resolve capabilities without this, but nothing can
resolve a *source connection* until an estate exists in the registry or
on disk.

Idempotent. Safe to re-run: an estate the console has since edited
(origin=wizard) is skipped with a warning rather than reverted, unless
--force is passed. See tools/estate_registry.import_from_yaml.

    python infrastructure/seed_estates.py
    python infrastructure/seed_estates.py --force
    python infrastructure/seed_estates.py --list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools.estate_registry import (  # noqa: E402
    export_to_yaml,
    list_estates,
    seed_committed_estates,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite estates that were authored or edited in the console",
    )
    parser.add_argument("--list", action="store_true", help="list registered estates and exit")
    parser.add_argument("--export", metavar="ESTATE_ID", help="print an estate as YAML and exit")
    args = parser.parse_args()

    if args.export:
        print(export_to_yaml(args.export))
        return 0

    if args.list:
        estates = list_estates()
        if not estates:
            print("No estates registered. Run this script without --list to seed them.")
            return 0
        for estate in estates:
            sources = ", ".join(s["source_id"] for s in estate.get("sources", []))
            print(
                f"{estate['estate_id']:<24} {estate.get('status', 'ACTIVE'):<9} "
                f"origin={estate.get('origin', '?'):<12} sources=[{sources}]"
            )
        return 0

    imported = seed_committed_estates(actor="seed_estates.py", force=args.force)
    if not imported:
        print(
            "No estates imported. Either none are committed, or every committed estate "
            "has since been edited in the console — re-run with --force to replace those."
        )
        return 0

    for estate in imported:
        sources = ", ".join(s["source_id"] for s in estate.get("sources", []))
        print(f"seeded {estate['estate_id']} (sources: {sources})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
