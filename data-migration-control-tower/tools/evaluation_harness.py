"""Deterministic evaluation harness (Day 10, master doc §17.2 Fri 28 Aug;
Appendix D's fourteen-scenario catalog).

    Do not hand-type success metrics into the UI. Generate them from an
    evaluation harness. Store each test run with an input scenario,
    expected outcome, actual tool trajectory, policy result, and final
    state. [§10]
    All scenarios in Appendix D pass or fail reproducibly and metrics
    are generated, never hand-entered. [Fri 28 Aug definition of done]

A "scenario" here is a zero-argument callable that runs real code
against real infrastructure (Firestore, SQL Server, BigQuery, Pub/Sub —
whatever the scenario needs) and either returns an evidence dict (PASS)
or raises (FAIL). Per Appendix D's "Assertion discipline": every
scenario function below must actually assert something concrete
(status codes, exception types, record contents) — a scenario that
returns without checking anything would be worse than no scenario.

Each outcome is written to Firestore (evaluation_runs/{harness_run_id}/
scenarios/{scenario_id}) so a run is inspectable after the fact, and a
Markdown+JSON report is generated to evaluation/reports/ — never
hand-typed, matching the DoD above.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from pathlib import Path
from typing import Callable

from tools.firestore_client import get_client

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "evaluation" / "reports"

Scenario = tuple[str, str, Callable[[], dict]]


def new_harness_run_id() -> str:
    return f"eval_{dt.datetime.now(dt.timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"


def run_scenario(scenario_id: str, description: str, fn: Callable[[], dict]) -> dict:
    """Runs one scenario, catching any exception as a FAIL (not a harness
    crash) so one broken scenario never stops the rest of the catalog
    from reporting. AssertionError (the scenario's own `assert` calls)
    and any other exception are both failures — the harness doesn't
    distinguish "the code broke" from "the assertion failed", because
    both mean the scenario didn't demonstrate what it claims to.
    """
    start = time.monotonic()
    evidence: dict | None = None
    error: str | None = None
    try:
        evidence = fn()
        status = "PASS"
    except Exception as exc:  # noqa: BLE001 - deliberate: any failure mode is a FAIL
        status = "FAIL"
        error = f"{type(exc).__name__}: {exc}"
    duration = round(time.monotonic() - start, 3)
    return {
        "scenario_id": scenario_id,
        "description": description,
        "status": status,
        "duration_seconds": duration,
        "evidence": evidence,
        "error": error,
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def run_all(scenarios: list[Scenario], harness_run_id: str | None = None, verbose: bool = True) -> dict:
    """Runs every scenario in order, records each to Firestore, and
    returns {"summary": ..., "results": [...]}. Scenario order matters
    only for console readability — each scenario is independently
    reproducible (see evaluation/scenarios.py's docstring on shared
    fixtures for the one deliberate exception: the expensive live
    migration run S-02/S-04/S-13/S-14 all read from, to keep cloud cost
    bounded rather than re-running a full migration four times).
    """
    harness_run_id = harness_run_id or new_harness_run_id()
    client = get_client()
    harness_ref = client.collection("evaluation_runs").document(harness_run_id)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()

    results = []
    for scenario_id, description, fn in scenarios:
        result = run_scenario(scenario_id, description, fn)
        results.append(result)
        harness_ref.collection("scenarios").document(scenario_id).set(result)
        if verbose:
            marker = "PASS" if result["status"] == "PASS" else "FAIL"
            print(f"[{marker}] {scenario_id} ({result['duration_seconds']}s) — {description}")
            if result["error"]:
                print(f"       {result['error']}")

    summary = {
        "harness_run_id": harness_run_id,
        "started_at": started_at,
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "total": len(results),
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "failed": sum(1 for r in results if r["status"] == "FAIL"),
        "total_duration_seconds": round(sum(r["duration_seconds"] for r in results), 3),
    }
    harness_ref.set(summary)
    return {"summary": summary, "results": results}


def write_metrics_report(run_output: dict, reports_dir: Path = REPORTS_DIR) -> tuple[Path, Path]:
    """Writes the run's summary+results as JSON (machine-readable) and a
    Markdown table (drop-in for the write-up / README) — both generated
    directly from run_all()'s return value, never hand-transcribed.
    Returns (json_path, md_path).
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    harness_run_id = run_output["summary"]["harness_run_id"]

    json_path = reports_dir / f"{harness_run_id}.json"
    json_path.write_text(json.dumps(run_output, indent=2, default=str), encoding="utf-8")

    summary = run_output["summary"]
    lines = [
        f"# Evaluation harness report — `{harness_run_id}`",
        "",
        f"Generated {summary['finished_at']} · {summary['passed']}/{summary['total']} scenarios passed · "
        f"{summary['total_duration_seconds']}s total",
        "",
        "| ID | Scenario | Status | Duration (s) |",
        "|---|---|---|---|",
    ]
    for r in run_output["results"]:
        status_marker = "✅ PASS" if r["status"] == "PASS" else "❌ FAIL"
        lines.append(f"| {r['scenario_id']} | {r['description']} | {status_marker} | {r['duration_seconds']} |")

    failed = [r for r in run_output["results"] if r["status"] == "FAIL"]
    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            lines.append(f"- **{r['scenario_id']}** — {r['error']}")

    md_path = reports_dir / f"{harness_run_id}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
