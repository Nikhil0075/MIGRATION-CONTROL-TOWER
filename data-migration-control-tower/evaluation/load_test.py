#!/usr/bin/env python
"""Operational load measurement (Deploy & Harden Phase 4) — concurrent
runs, Pub/Sub backlog, and Cloud Run instance count under simultaneous
load. The third of three distinct scale measurements (docs/EVALUATION.md)
— genuinely different from evaluation/scale_harness.py (control-plane
object count, single-threaded) and evaluation/data_plane_scale_test.py
(one migration's real rows/bytes).

Two parts, run independently so this is useful before a full fleet is
deployed:

1. **Concurrent policy-decision throughput** — real, runnable today: N
   threads hitting tools/policy_engine.py::evaluate() (a real Firestore
   write) simultaneously, measuring wall-clock throughput and whether
   latency degrades under concurrency. This exercises real Firestore
   contention regardless of how many Cloud Run services are deployed.

2. **Live Cloud Run/Pub-Sub state** — queries whatever's actually
   deployed (today: hello-agent only; the full 9-service topology once
   Deploy & Harden Phase 2's `deploy_cloud_run_services` is applied) via
   `gcloud`, the same honesty pattern
   evaluation/collect_deployment_evidence.py uses: a service/metric
   that isn't deployed/available yet is reported as such, not omitted.

Usage (from repo root):
    python evaluation/load_test.py --concurrent-runs 10
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools.policy_engine import evaluate as policy_evaluate  # noqa: E402

REPORTS_DIR = REPO_ROOT / "evaluation" / "reports"

#: Same Windows subprocess.run() fix as
#: evaluation/collect_deployment_evidence.py — gcloud is a .cmd wrapper
#: on Windows and subprocess.run(["gcloud", ...]) with shell=False
#: cannot find it without resolving the full path first.
_GCLOUD = shutil.which("gcloud") or "gcloud"


def _one_policy_call(i: int) -> float:
    start = time.perf_counter()
    policy_evaluate(agent_key="discovery", action="source.catalog.sql_server", resource_class="METADATA")
    return (time.perf_counter() - start) * 1000


def run_concurrent_load(concurrent_runs: int) -> dict:
    """Fires `concurrent_runs` real policy-engine calls simultaneously
    via a thread pool and measures both individual latency (does it
    degrade under concurrency?) and aggregate throughput."""
    start = time.perf_counter()
    latencies_ms: list[float] = []
    with ThreadPoolExecutor(max_workers=concurrent_runs) as pool:
        futures = [pool.submit(_one_policy_call, i) for i in range(concurrent_runs)]
        for future in as_completed(futures):
            latencies_ms.append(future.result())
    total_s = time.perf_counter() - start

    latencies_ms.sort()
    p50 = latencies_ms[len(latencies_ms) // 2] if latencies_ms else 0.0
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)] if latencies_ms else 0.0

    return {
        "concurrent_runs": concurrent_runs,
        "total_wall_clock_s": round(total_s, 3),
        "throughput_per_sec": round(concurrent_runs / total_s, 2) if total_s > 0 else None,
        "latency_p50_ms": round(p50, 2),
        "latency_p95_ms": round(p95, 2),
        "latency_max_ms": round(max(latencies_ms), 2) if latencies_ms else 0.0,
    }


def _run_gcloud_json(args: list[str]):
    try:
        result = subprocess.run(
            [_GCLOUD, *args, "--format=json"], capture_output=True, text=True, timeout=60, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def collect_live_fleet_state() -> dict:
    services = _run_gcloud_json(["run", "services", "list"]) or []
    subs = _run_gcloud_json(["pubsub", "subscriptions", "list"]) or []

    service_rows = [
        {
            "name": s["metadata"]["name"],
            "instance_count": None,  # gcloud run services list doesn't report live instance count;
            # Cloud Monitoring's run.googleapis.com/container/instance_count metric would, once
            # a service is deployed to query it against — not queryable for a service that doesn't
            # exist, so left explicitly None rather than guessed.
        }
        for s in services
    ]
    sub_rows = [{"name": s["name"].rsplit("/", 1)[-1], "topic": s.get("topic", "").rsplit("/", 1)[-1]} for s in subs]

    return {
        "deployed_services": service_rows,
        "deployed_service_count": len(service_rows),
        "expected_service_count": 10,  # the 9-service topology + hello-agent, docs/ARCHITECTURE.md
        "subscriptions": sub_rows,
        "note": (
            "instance_count is None for every service — gcloud run services list doesn't report "
            "live autoscaled instance count; querying it needs Cloud Monitoring's "
            "run.googleapis.com/container/instance_count metric against a real load window, a "
            "live follow-up once the fleet is deployed and under real traffic, not something this "
            "script fabricates."
        ),
    }


def write_report(concurrent_load: dict, fleet_state: dict, reports_dir: Path = REPORTS_DIR) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    lines = [
        "# Operational load measurement",
        "",
        f"Generated {generated_at}.",
        "",
        "**Measures concurrent-request behavior and live fleet state — distinct from "
        "evaluation/scale_harness.py (control-plane object count) and "
        "evaluation/data_plane_scale_test.py (one migration's real rows/bytes).** "
        "See docs/EVALUATION.md.",
        "",
        "## Concurrent policy-decision throughput",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Concurrent calls | {concurrent_load['concurrent_runs']} |",
        f"| Total wall-clock | {concurrent_load['total_wall_clock_s']} s |",
        f"| Throughput | {concurrent_load['throughput_per_sec']} calls/sec |",
        f"| Latency p50 | {concurrent_load['latency_p50_ms']} ms |",
        f"| Latency p95 | {concurrent_load['latency_p95_ms']} ms |",
        f"| Latency max | {concurrent_load['latency_max_ms']} ms |",
        "",
        "## Live fleet state",
        "",
        f"Deployed services: {fleet_state['deployed_service_count']} / {fleet_state['expected_service_count']} expected.",
        "",
        "| Service | Instance count |",
        "|---|---|",
    ]
    for row in fleet_state["deployed_services"]:
        lines.append(f"| {row['name']} | {row['instance_count'] if row['instance_count'] is not None else 'not queryable — see note'} |")
    lines += ["", fleet_state["note"]]

    path = reports_dir / "load_test_metrics.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    from tools.firestore_client import get_client

    get_client().collection("evaluation_load_reports").document(
        f"{concurrent_load['concurrent_runs']}-{generated_at}"
    ).set({"generated_at": generated_at, "concurrent_load": concurrent_load, "fleet_state": fleet_state})
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--concurrent-runs", type=int, default=10)
    parser.add_argument("--skip-fleet-query", action="store_true", help="skip the live gcloud queries")
    args = parser.parse_args()

    print(f"[load-test] firing {args.concurrent_runs} concurrent policy-decision calls...")
    concurrent_load = run_concurrent_load(args.concurrent_runs)
    print(
        f"[load-test] {concurrent_load['throughput_per_sec']} calls/sec, "
        f"p50={concurrent_load['latency_p50_ms']}ms, p95={concurrent_load['latency_p95_ms']}ms"
    )

    if args.skip_fleet_query:
        fleet_state = {
            "deployed_services": [], "deployed_service_count": 0, "expected_service_count": 10,
            "subscriptions": [], "note": "--skip-fleet-query passed — live gcloud queries not run.",
        }
    else:
        print("[load-test] querying live fleet state...")
        fleet_state = collect_live_fleet_state()
        print(f"[load-test] {fleet_state['deployed_service_count']}/{fleet_state['expected_service_count']} services deployed")

    path = write_report(concurrent_load, fleet_state)
    print(f"[load-test] report written: {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
