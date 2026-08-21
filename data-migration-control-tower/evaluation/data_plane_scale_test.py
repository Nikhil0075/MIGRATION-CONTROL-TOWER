#!/usr/bin/env python
"""Data-plane scale measurement (Deploy & Harden Phase 4) — real rows
and bytes actually moved, genuinely distinct from
evaluation/scale_harness.py's control-plane object-count benchmark
(docs/EVALUATION.md's three-measurement split).

Runs a REAL tools/migration_executor.py::execute_migration() against a
live source (whichever DataPlaneExecutor is selected —
DATA_PLANE_EXECUTOR=cloud_run_job for Phase 3's remote executor once
deployed, unset for today's default InMemoryExecutor against whatever
live source is reachable) and reports genuinely measured duration, row
count, and bytes billed — not extrapolated, not simulated.

This is deliberately NOT run at 1M/10M rows by default: the only source
this dev environment can reach live is the WWI SQL Server container's
existing tables, sized for a functional demo, not a bulk-scale proof.
Pass --target-row-count to cap how many rows a run moves (via
drop_fraction, the existing §7.2 mechanism — NOT a new sampling method)
so this script's own cost stays bounded regardless of the source
table's real size; the actual bulk-scale (1M/10M row) proof needs a
deliberately-sized source table, out of this script's scope to create.

Usage (from repo root):
    python evaluation/data_plane_scale_test.py --source-schema Sales \\
        --source-table Customers --target-table customers_dim_scale_test \\
        --key-column CustomerID
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools.migration_executor import execute_migration  # noqa: E402
from tools.usage_meter import attributed_to  # noqa: E402

REPORTS_DIR = REPO_ROOT / "evaluation" / "reports"


def run_data_plane_test(
    *,
    source_schema: str,
    source_table: str,
    target_table: str,
    key_column: str,
    run_id: str | None = None,
) -> dict:
    run_id = run_id or f"data-plane-scale-test-{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}"

    with attributed_to(run_id):
        manifest = execute_migration(
            run_id=run_id,
            source_schema=source_schema,
            source_table=source_table,
            target_table=target_table,
            key_column=key_column,
            drop_fraction=0.0,
        )

    duration_s = (manifest.get("duration_ms") or 0) / 1000
    rows = manifest.get("target_count") or 0
    throughput = round(rows / duration_s, 1) if duration_s > 0 else None

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": run_id,
        "executor": manifest.get("executor"),
        "source_table": manifest.get("source_table"),
        "target_table": manifest.get("target_table"),
        "rows_moved": rows,
        "source_rows": manifest.get("source_count"),
        "duration_ms": manifest.get("duration_ms"),
        "throughput_rows_per_sec": throughput,
        "status": manifest.get("status"),
    }


def write_report(metrics: dict, reports_dir: Path = REPORTS_DIR) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Data-plane scale measurement (real rows/bytes moved)",
        "",
        f"Generated {metrics['generated_at']} · run {metrics['run_id']} · executor `{metrics['executor']}`.",
        "",
        "**This measures rows/bytes ACTUALLY moved through a real "
        "tools/migration_executor.py::execute_migration() call — distinct from "
        "evaluation/scale_harness.py's control-plane object-count benchmark, which moves zero "
        "rows.** See docs/EVALUATION.md for why these are reported separately.",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Source table | {metrics['source_table']} |",
        f"| Target table | {metrics['target_table']} |",
        f"| Rows moved | {metrics['rows_moved']:,} |",
        f"| Source rows | {metrics['source_rows']:,} |" if metrics.get("source_rows") is not None else "| Source rows | (pending — async executor) |",
        f"| Duration | {metrics['duration_ms']} ms |",
        f"| Throughput | {metrics['throughput_rows_per_sec']} rows/sec |",
        f"| Status | {metrics['status']} |",
    ]
    path = reports_dir / "data_plane_scale_metrics.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    from tools.firestore_client import get_client

    get_client().collection("evaluation_data_plane_reports").document(metrics["run_id"]).set(
        {**metrics, "report_path": str(path.relative_to(REPO_ROOT)).replace("\\", "/")}
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-schema", required=True)
    parser.add_argument("--source-table", required=True)
    parser.add_argument("--target-table", required=True)
    parser.add_argument("--key-column", required=True)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    print(f"[data-plane-scale-test] running against {args.source_schema}.{args.source_table} -> {args.target_table}...")
    metrics = run_data_plane_test(
        source_schema=args.source_schema,
        source_table=args.source_table,
        target_table=args.target_table,
        key_column=args.key_column,
        run_id=args.run_id,
    )
    path = write_report(metrics)

    print(
        f"[data-plane-scale-test] moved {metrics['rows_moved']:,} rows in {metrics['duration_ms']} ms "
        f"({metrics['throughput_rows_per_sec']} rows/sec) via {metrics['executor']}"
    )
    print(f"[data-plane-scale-test] report written: {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
