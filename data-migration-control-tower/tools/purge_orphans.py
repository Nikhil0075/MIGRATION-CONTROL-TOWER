"""Delete run subcollection documents whose parent run no longer exists.

Firestore lets a document be deleted while its subcollections survive.
`run_lifecycle.delete_run()` did exactly that, and its docstring said the
leftovers were "orphaned but harmless — test runs never populate them
anyway". Both halves of that turned out to be wrong. A survey of the live
project found 9,891 orphaned documents under 1,028 dead parents, 554 of
them with the real `run_YYYYMMDD_...` shape rather than a test id.

They are not harmless because of how the console reads. Firestore
collection-group queries here run UNFILTERED — combining `.where()` with
the `order_by("created_at")` the console needs would require a composite
index this project does not create (see CLAUDE.md) — so every aggregate
endpoint streams the whole group and filters in Python. The console pays
to read every orphan on every uncached request and then discards it.
`catalog` was the clearest case: 6,428 of 9,090 documents belonged to
runs that no longer exist.

Deletion is irreversible, so this writes every document it is about to
delete to a JSONL export first, and refuses to delete if that export
cannot be written. Restoring is reading the file back and re-setting each
path.

    python -m tools.purge_orphans              # dry run: counts only
    python -m tools.purge_orphans --apply      # export, then delete
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from agents.orchestrator.run_lifecycle import RUN_COLLECTION
from tools.firestore_client import get_client

#: Every subcollection a run can own. Named explicitly rather than
#: discovered, because a collection-group name can also exist as a
#: TOP-LEVEL collection — `policy_decisions` and `containment_events` both
#: do — and those documents have no run parent and must never be touched
#: by a tool whose whole premise is "the parent is missing".
RUN_SUBCOLLECTIONS = (
    "catalog",
    "dependencies",
    "risk_findings",
    "migration_plan",
    "approval",
    "incidents",
    "reconciliation",
    "migration_executions",
    "policy_decisions",
    "pipelines",
    "stage_metrics",
)

EXPORT_DIR = Path(__file__).resolve().parents[1] / "var" / "orphan-exports"

#: Firestore's own limit on writes per batch.
BATCH_LIMIT = 500


def live_run_ids(client) -> set[str]:
    """Ids of every run document that still exists.

    `select([])` fetches keys only. The point is to decide existence, and
    the payloads would be megabytes.
    """
    return {doc.id for doc in client.collection(RUN_COLLECTION).select([]).stream()}


def find_orphans(client, live: set[str]) -> dict[str, dict[str, list]]:
    """Maps dead run_id -> subcollection -> document references."""
    orphans: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for group in RUN_SUBCOLLECTIONS:
        for doc in client.collection_group(group).select([]).stream():
            parent = doc.reference.parent.parent
            # A top-level collection of the same name has no parent
            # document at all; one nested under something other than
            # migration_runs is not ours to judge.
            if parent is None or parent.parent.id != RUN_COLLECTION:
                continue
            if parent.id in live:
                continue
            orphans[parent.id][group].append(doc.reference)
    return orphans


def _encode(value: Any) -> Any:
    """JSON fallback for the Firestore types that are not JSON.

    Timestamps, GeoPoints, references and bytes all appear in this data.
    Rendering them as strings keeps the export readable and restorable by
    a human; it is a rescue copy, not a wire format.
    """
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def export(refs: Iterable[Any], destination: Path) -> int:
    """Writes every document to JSONL and returns how many were written.

    Reads the documents a second time, in full — the discovery pass
    fetched keys only. That is the cost of being able to undo this.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with destination.open("w", encoding="utf-8") as handle:
        for ref in refs:
            snapshot = ref.get()
            if not snapshot.exists:
                continue
            handle.write(
                json.dumps(
                    {"path": ref.path, "data": snapshot.to_dict()},
                    default=_encode,
                )
                + "\n"
            )
            written += 1
    return written


def delete(client, refs: list, batch_limit: int = BATCH_LIMIT) -> int:
    deleted = 0
    for start in range(0, len(refs), batch_limit):
        batch = client.batch()
        chunk = refs[start : start + batch_limit]
        for ref in chunk:
            batch.delete(ref)
        batch.commit()
        deleted += len(chunk)
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without it this only reports what it found.",
    )
    args = parser.parse_args(argv)

    client = get_client()
    live = live_run_ids(client)
    print(f"live run documents: {len(live)}")

    orphans = find_orphans(client, live)
    by_group: dict[str, int] = defaultdict(int)
    refs: list = []
    for groups in orphans.values():
        for group, group_refs in groups.items():
            by_group[group] += len(group_refs)
            refs.extend(group_refs)

    print(f"\ndead parents: {len(orphans)}    orphaned documents: {len(refs)}")
    for group in RUN_SUBCOLLECTIONS:
        if by_group.get(group):
            print(f"  {group:24s} {by_group[group]:6d}")

    if not refs:
        print("\nNothing to purge.")
        return 0

    if not args.apply:
        print("\nDRY RUN. Nothing was deleted. Re-run with --apply.")
        return 0

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = EXPORT_DIR / f"orphans-{stamp}.jsonl"
    print(f"\nexporting {len(refs)} documents to {destination} …")
    try:
        written = export(refs, destination)
    except OSError as exc:
        # Refuse rather than delete without a rescue copy. An
        # irreversible delete whose backup silently failed is the one
        # outcome this tool must not produce.
        print(f"export failed, nothing deleted: {exc}", file=sys.stderr)
        return 1
    print(f"exported {written} documents ({destination.stat().st_size / 1024:.0f} KB)")

    deleted = delete(client, refs)
    print(f"deleted {deleted} documents under {len(orphans)} dead parents")
    print(f"restore from: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
