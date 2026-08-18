#!/usr/bin/env python
"""Day 10 evaluation harness entrypoint (master doc §17.2, Fri 28 Aug;
Appendix D).

Usage (from repo root):
    python evaluation/run_harness.py                  # all 14 scenarios
    python evaluation/run_harness.py --skip-expensive  # skip S-11 (a second full migration run)

Runs every scenario in evaluation/scenarios.py against real
infrastructure, records each outcome to Firestore, and writes a
generated JSON + Markdown report to evaluation/reports/ — never
hand-typed, matching the Fri 28 Aug definition of done: "All scenarios
in Appendix D pass or fail reproducibly and metrics are generated,
never hand-entered."
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from evaluation.scenarios import build_scenarios  # noqa: E402
from tools.evaluation_harness import run_all, write_metrics_report  # noqa: E402


def main() -> None:
    include_expensive = "--skip-expensive" not in sys.argv
    scenarios = build_scenarios(include_expensive=include_expensive)

    print(f"=== Day 10 evaluation harness: {len(scenarios)} scenarios ===")
    if not include_expensive:
        print("(--skip-expensive: S-11 skipped)")
    print()

    output = run_all(scenarios)
    json_path, md_path = write_metrics_report(output)

    summary = output["summary"]
    print()
    print(f"=== {summary['passed']}/{summary['total']} passed in {summary['total_duration_seconds']}s ===")
    print(f"harness_run_id={summary['harness_run_id']}")
    print(f"report: {json_path.relative_to(REPO_ROOT)}")
    print(f"report: {md_path.relative_to(REPO_ROOT)}")

    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
