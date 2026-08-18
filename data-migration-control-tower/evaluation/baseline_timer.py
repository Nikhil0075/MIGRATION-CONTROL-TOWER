#!/usr/bin/env python
"""Manual-baseline stopwatch (Day 10, master doc §25.1: "Measure a human
performing the same assessment on the same estate ... This does not
require a real analyst — perform the assessment manually, honestly, and
time it. Record the method so the number is defensible.")

This is a genuine stopwatch, not a fabricated number: `start` records a
real wall-clock timestamp to Firestore; `stop` reads it back and records
the elapsed seconds. The person timed is whoever runs these commands
while actually doing the six §25.1 activities by hand against this
project's real estate (the WideWorldImporters SQL Server database + the
Oracle-dialect corpus + the DAG stubs) — the same estate the fleet
assesses, per §25.1's "same estate, same inputs, timed" requirement.

Usage (from repo root):
    python evaluation/baseline_timer.py start asset_inventory
    ... (go enumerate tables/columns/jobs by hand) ...
    python evaluation/baseline_timer.py stop asset_inventory --method \
        "Enumerated INFORMATION_SCHEMA.TABLES/COLUMNS by hand via SSMS-equivalent \
        query, cross-referenced simulator/source_setup/oracle_dialect_corpus/*.sql \
        and simulator/source_setup/dags/*.py by reading each file."

    python evaluation/baseline_timer.py report   # prints what's recorded so far
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools.firestore_client import get_client  # noqa: E402

COLLECTION = "operational_baseline"

# The six activities from master doc §25.1's table, in order, with the
# fleet-equivalent name shown in the report for traceability.
ACTIVITIES = {
    "asset_inventory": "Discovery Agent run",
    "dependency_mapping": "Lineage Agent graph construction",
    "sensitivity_classification": "Gemma screen plus Risk Agent",
    "dialect_review": "Risk Agent plus Planner translation plan",
    "reconciliation_design": "Validation Agent deterministic check suite",
    "documentation_reconciliation": "Multimodal drift detection",
}


def _doc(activity: str):
    if activity not in ACTIVITIES:
        raise SystemExit(f"Unknown activity {activity!r}. Choices: {sorted(ACTIVITIES)}")
    return get_client().collection(COLLECTION).document(activity)


def start(activity: str) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    _doc(activity).set({"activity": activity, "fleet_equivalent": ACTIVITIES[activity], "started_at": now}, merge=True)
    print(f"started {activity!r} at {now}")


def stop(activity: str, method: str) -> None:
    doc = _doc(activity).get()
    record = doc.to_dict() or {}
    started_at = record.get("started_at")
    if not started_at:
        raise SystemExit(f"No open 'start' found for {activity!r} — call start first.")
    started = dt.datetime.fromisoformat(started_at)
    finished = dt.datetime.now(dt.timezone.utc)
    elapsed = (finished - started).total_seconds()
    _doc(activity).set(
        {
            "activity": activity,
            "fleet_equivalent": ACTIVITIES[activity],
            "started_at": started_at,
            "finished_at": finished.isoformat(),
            "manual_seconds": elapsed,
            "method": method,
        },
        merge=True,
    )
    print(f"stopped {activity!r}: {elapsed:.1f}s elapsed")


def report() -> None:
    client = get_client()
    print(f"{'activity':<30} {'seconds':>10}  method")
    for activity in ACTIVITIES:
        doc = client.collection(COLLECTION).document(activity).get()
        record = doc.to_dict() or {}
        seconds = record.get("manual_seconds")
        method = record.get("method", "(not yet timed)")
        seconds_str = f"{seconds:.1f}" if seconds is not None else "—"
        print(f"{activity:<30} {seconds_str:>10}  {method}")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd == "start":
        start(sys.argv[2])
    elif cmd == "stop":
        method = ""
        args = sys.argv[3:]
        if "--method" in args:
            method = args[args.index("--method") + 1]
        stop(sys.argv[2], method)
    elif cmd == "report":
        report()
    else:
        raise SystemExit(f"Unknown command {cmd!r}. Use start|stop|report.")


if __name__ == "__main__":
    main()
