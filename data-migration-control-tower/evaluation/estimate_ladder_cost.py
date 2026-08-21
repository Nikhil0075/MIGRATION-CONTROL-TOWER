#!/usr/bin/env python
"""Full-stack cost estimate before a scale run (Deploy & Harden
Phase 4c). Prints a breakdown and requires explicit confirmation before
the plan's own condition — "checked against remaining trial credit,
never auto-run" — is satisfied. This script never triggers a scale run
itself; it only estimates and asks.

Three scenarios, matching the three distinct measurements
docs/EVALUATION.md describes — each has a genuinely different cost
profile, so folding them into one number would hide which dimension
actually costs money:

  control-plane   evaluation/scale_harness.py's tiers (1k/5k/20k
                   metadata object definitions). No BigQuery, no
                   Vertex AI, no live data reads at ANY tier (see that
                   module's own docstring) — the only real cost driver
                   is a small, FIXED number of Firestore writes
                   (POLICY_SAMPLE_SIZE regardless of --count, plus
                   write_report()'s own two writes), so this estimate
                   does not scale with tier size and is near-zero at
                   every approved tier.

  data-plane      evaluation/data_plane_scale_test.py's real row/byte
                   movement. Extrapolated from a real MEASURED sample
                   (tools/usage_meter.py's recorded BigQuery
                   bytes/model tokens for one small run) using
                   contracts/price_book.json's measured-usage rates —
                   genuinely scales with row count.

  operational-load evaluation/load_test.py's concurrent-run simulation
                   against a deployed fleet. Uses
                   evaluation/infra_price_book.json's Cloud Run
                   CPU/memory-second assumptions (no measured usage
                   exists for this yet) — genuinely scales with
                   concurrency x duration.

Usage:
    python evaluation/estimate_ladder_cost.py --scenario control-plane --tier 20000
    python evaluation/estimate_ladder_cost.py --scenario data-plane --rows 1000000 --sample-run-id <run_id>
    python evaluation/estimate_ladder_cost.py --scenario operational-load --concurrent-runs 10 --duration-minutes 15
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

INFRA_PRICE_BOOK_PATH = REPO_ROOT / "evaluation" / "infra_price_book.json"

# Fixed regardless of --count — see evaluation/scale_harness.py's own
# docstring: POLICY_SAMPLE_SIZE policy_evaluate() calls (bounded at 100)
# plus write_report()'s 2 document writes (tier + "current"), which is
# ALL the Firestore activity the harness performs at any tier.
CONTROL_PLANE_FIXED_FIRESTORE_WRITES = 102


def load_infra_price_book() -> dict:
    return json.loads(INFRA_PRICE_BOOK_PATH.read_text(encoding="utf-8"))


def estimate_control_plane(tier: int, *, book: dict | None = None) -> dict:
    book = book or load_infra_price_book()
    rate = book["rates"]["firestore_write"]["price"]
    writes = CONTROL_PLANE_FIXED_FIRESTORE_WRITES
    firestore_cost = (writes / 100_000) * rate

    return {
        "scenario": "control-plane",
        "tier": tier,
        "line_items": {
            "bigquery": {"amount": 0.0, "note": "not touched at any tier — scale_harness.py never queries BigQuery"},
            "vertex_ai": {"amount": 0.0, "note": "model_calls=0 at any tier — control-plane-only by design"},
            "firestore": {
                "amount": round(firestore_cost, 6),
                "note": f"{writes} writes (FIXED regardless of tier — bounded policy-decision sample + 2 report writes)",
            },
        },
        "total": round(firestore_cost, 6),
        "currency": book["currency"],
        "scales_with_tier": False,
        "headline": (
            f"Estimated cost for the {tier:,}-object control-plane tier: "
            f"${round(firestore_cost, 6)} — near-zero and CONSTANT across tiers, "
            "since this harness touches no BigQuery/Vertex AI at any scale."
        ),
    }


def estimate_data_plane(
    rows: int, *, sample_bytes_per_row: float | None = None, price_book: dict | None = None
) -> dict:
    """Extrapolates from a per-row byte estimate. `sample_bytes_per_row`
    should come from a real measured sample (a small real
    execute_migration() run's usage_events, divided by its row count) —
    passing None makes this an explicitly LABELED rough estimate using a
    generic assumption instead, never silently treated as measured."""
    from tools.usage_meter import load_price_book

    price_book = price_book or load_price_book()
    bq_rate = price_book["rates"]["bigquery_query"]["price"]  # per TiB billed
    tib = 1024**4

    measured = sample_bytes_per_row is not None
    bytes_per_row = sample_bytes_per_row if measured else 200.0  # generic assumption, flagged below
    estimated_bytes = rows * bytes_per_row
    bigquery_cost = (estimated_bytes / tib) * bq_rate

    return {
        "scenario": "data-plane",
        "rows": rows,
        "line_items": {
            "bigquery_load": {"amount": 0.0, "note": "batch loads are not charged for the load itself (price_book.json)"},
            "bigquery_query": {
                "amount": round(bigquery_cost, 4),
                "note": f"{estimated_bytes:,.0f} bytes estimated ({'measured sample' if measured else 'GENERIC 200 bytes/row assumption — pass --sample-run-id for a real figure'})",
            },
        },
        "total": round(bigquery_cost, 4),
        "currency": price_book["currency"],
        "scales_with_tier": True,
        "measured_basis": measured,
        "headline": (
            f"Estimated cost for moving {rows:,} rows: ${round(bigquery_cost, 4)} "
            f"({'from a real measured sample' if measured else 'from a generic per-row assumption — treat as rough'})."
        ),
    }


def estimate_operational_load(
    concurrent_runs: int, duration_minutes: float, *, services: int = 9, book: dict | None = None
) -> dict:
    book = book or load_infra_price_book()
    cpu_rate = book["rates"]["cloud_run_cpu_second"]["price"]
    mem_rate = book["rates"]["cloud_run_memory_gib_second"]["price"]
    duration_seconds = duration_minutes * 60

    # Assumption: each concurrent run touches every one of the 9
    # services for the full duration at 1 vCPU / 1 GiB — a deliberately
    # conservative (likely overestimating) assumption, stated as such.
    vcpu_seconds = concurrent_runs * services * duration_seconds
    gib_seconds = vcpu_seconds  # same duration, 1 GiB assumption
    cpu_cost = vcpu_seconds * cpu_rate
    mem_cost = gib_seconds * mem_rate
    total = cpu_cost + mem_cost

    return {
        "scenario": "operational-load",
        "concurrent_runs": concurrent_runs,
        "duration_minutes": duration_minutes,
        "line_items": {
            "cloud_run_cpu": {"amount": round(cpu_cost, 4), "note": f"{vcpu_seconds:,.0f} vCPU-seconds assumed (1 vCPU x {services} services x {concurrent_runs} runs x {duration_minutes}min)"},
            "cloud_run_memory": {"amount": round(mem_cost, 4), "note": f"{gib_seconds:,.0f} GiB-seconds assumed (1 GiB, same basis)"},
        },
        "total": round(total, 4),
        "currency": book["currency"],
        "scales_with_tier": True,
        "measured_basis": False,
        "headline": (
            f"Estimated cost for {concurrent_runs} concurrent runs x {duration_minutes} min against "
            f"all {services} services: ${round(total, 4)} — a conservative (likely overestimating) "
            "assumption-based figure, not a measurement."
        ),
    }


def print_estimate(estimate: dict) -> None:
    print(f"\n[cost-estimate] scenario: {estimate['scenario']}")
    for name, item in estimate["line_items"].items():
        print(f"[cost-estimate]   {name}: ${item['amount']} — {item['note']}")
    print(f"[cost-estimate] TOTAL: ${estimate['total']} {estimate['currency']}")
    print(f"[cost-estimate] {estimate['headline']}")
    if not estimate.get("measured_basis", True):
        print(
            "[cost-estimate] NOTE: this scenario's cost is an ASSUMPTION-based estimate "
            "(evaluation/infra_price_book.json), not a measurement — no usage-tracking "
            "instrumentation exists yet for this infrastructure."
        )
    print(
        "[cost-estimate] Check this against your ACTUAL remaining trial credit "
        "(Cloud Console -> Billing -> Credits) before confirming — do not trust an assumed balance."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True, choices=["control-plane", "data-plane", "operational-load"])
    parser.add_argument("--tier", type=int, help="control-plane: object count (e.g. 1000, 5000, 20000)")
    parser.add_argument("--rows", type=int, help="data-plane: rows to be moved")
    parser.add_argument("--sample-run-id", help="data-plane: a real run_id to derive bytes/row from usage_events")
    parser.add_argument("--concurrent-runs", type=int, help="operational-load: simultaneous migration runs")
    parser.add_argument("--duration-minutes", type=float, help="operational-load: how long the load runs")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt (non-interactive use)")
    args = parser.parse_args(argv)

    if args.scenario == "control-plane":
        if not args.tier:
            print("[cost-estimate] ERROR: --tier is required for --scenario control-plane", file=sys.stderr)
            return 1
        estimate = estimate_control_plane(args.tier)
    elif args.scenario == "data-plane":
        if not args.rows:
            print("[cost-estimate] ERROR: --rows is required for --scenario data-plane", file=sys.stderr)
            return 1
        sample_bytes_per_row = None
        if args.sample_run_id:
            sample_bytes_per_row = _bytes_per_row_from_run(args.sample_run_id)
        estimate = estimate_data_plane(args.rows, sample_bytes_per_row=sample_bytes_per_row)
    else:
        if not args.concurrent_runs or not args.duration_minutes:
            print(
                "[cost-estimate] ERROR: --concurrent-runs and --duration-minutes are required for "
                "--scenario operational-load", file=sys.stderr,
            )
            return 1
        estimate = estimate_operational_load(args.concurrent_runs, args.duration_minutes)

    print_estimate(estimate)

    if not args.yes:
        confirm = input("\n[cost-estimate] Proceed with the actual run at this estimated cost? [y/N] ").strip().lower()
        if confirm != "y":
            print("[cost-estimate] Not confirmed — no run should proceed.")
            return 1
    return 0


def _bytes_per_row_from_run(run_id: str) -> float | None:
    from tools.firestore_client import get_client

    events = [
        d.to_dict()
        for d in get_client().collection("migration_runs").document(run_id).collection("usage_events").stream()
        if (d.to_dict() or {}).get("kind") == "bigquery"
    ]
    executions = [
        d.to_dict()
        for d in get_client().collection("migration_runs").document(run_id).collection("migration_executions").stream()
    ]
    total_bytes = sum(int(e.get("bytes_billed") or 0) for e in events)
    total_rows = sum(int(e.get("target_count") or 0) for e in executions)
    if not total_rows or not total_bytes:
        print(
            f"[cost-estimate] WARNING: run {run_id!r} has no usable bytes_billed/target_count — "
            "falling back to the generic per-row assumption.",
            file=sys.stderr,
        )
        return None
    return total_bytes / total_rows


if __name__ == "__main__":
    raise SystemExit(main())
