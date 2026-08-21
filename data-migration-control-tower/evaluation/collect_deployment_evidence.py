#!/usr/bin/env python
"""Collects live Cloud Run/Pub-Sub/Cloud Trace evidence for whatever is
actually deployed (Deploy & Harden Phase 2g) — replaces the earlier,
hello-agent-only `evaluation/reports/cloud_deployment_evidence.md`,
which was written by hand from a one-off query session and never
regenerated as the fleet grew.

Every figure here comes from a real `gcloud`/API call, captured with a
timestamp — nothing is typed in by hand, and a service that isn't
deployed yet shows up as "not found," not silently omitted. Run this
after any deploy (`infrastructure/terraform`'s `deploy_cloud_run_services`
step) to refresh the evidence file, not once at the start of the
project.

Usage (from repo root):
    python evaluation/collect_deployment_evidence.py [--out PATH]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

#: On Windows, `gcloud` is a `.cmd` wrapper — subprocess.run(["gcloud", ...])
#: with shell=False (the safe default) fails to find it with WinError 2,
#: since Windows only resolves PATHEXT-suffixed executables through a
#: shell. shutil.which() resolves the real, full path (including the
#: .cmd extension where relevant) up front so no shell=True is needed on
#: any platform — avoids the shell-injection surface that would come
#: with quoting a full command string instead.
_GCLOUD = shutil.which("gcloud") or "gcloud"

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "evaluation" / "reports" / "cloud_deployment_evidence.md"

#: The 9-service topology (docs/ARCHITECTURE.md) — checked even for
#: services not deployed yet, so the report is honest about what's
#: missing, not just about what exists.
EXPECTED_SERVICES = [
    "control-tower-ui", "orchestrator", "discovery-agent", "lineage-agent",
    "risk-agent", "planner-agent", "validation-agent", "cutover-agent",
    "finance-impact-agent", "hello-agent",
]

EXPECTED_PUSH_SUBSCRIPTIONS = [
    "migration-requested-push", "discovery-completed-push", "risk-assessed-push",
    "plan-created-push", "validation-requested-push", "validation-passed-push",
    "validation-failed-push", "assessment-requested-push", "cutover-approved-push",
]


def _run_gcloud_json(args: list[str]) -> list | dict | None:
    """Runs a `gcloud ... --format=json` command; returns None (not an
    exception) on failure, so one missing/undeployed resource doesn't
    abort collection of everything else."""
    try:
        result = subprocess.run(
            [_GCLOUD, *args, "--format=json"], capture_output=True, text=True, timeout=60, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  gcloud call failed to run: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def collect_cloud_run_services() -> dict:
    services = _run_gcloud_json(["run", "services", "list"]) or []
    by_name = {s["metadata"]["name"]: s for s in services}
    rows = []
    for name in EXPECTED_SERVICES:
        service = by_name.get(name)
        if service is None:
            rows.append({"name": name, "deployed": False})
            continue
        rows.append(
            {
                "name": name,
                "deployed": True,
                "url": service.get("status", {}).get("url"),
                "service_account": service.get("spec", {}).get("template", {}).get("spec", {}).get("serviceAccountName"),
                "latest_ready_revision": service.get("status", {}).get("latestReadyRevisionName"),
                "region": service.get("metadata", {}).get("labels", {}).get("cloud.googleapis.com/location"),
            }
        )
    return {"services": rows}


def collect_push_subscriptions() -> dict:
    subs = _run_gcloud_json(["pubsub", "subscriptions", "list"]) or []
    by_name = {s["name"].rsplit("/", 1)[-1]: s for s in subs}
    rows = []
    for name in EXPECTED_PUSH_SUBSCRIPTIONS:
        sub = by_name.get(name)
        if sub is None:
            rows.append({"name": name, "exists": False})
            continue
        push_config = sub.get("pushConfig", {})
        rows.append(
            {
                "name": name,
                "exists": True,
                "push_endpoint": push_config.get("pushEndpoint"),
                "oidc_service_account": push_config.get("oidcToken", {}).get("serviceAccountEmail"),
                "topic": sub.get("topic", "").rsplit("/", 1)[-1],
            }
        )
    return {"push_subscriptions": rows}


def collect_dead_letter_backlog() -> dict:
    sub = _run_gcloud_json(["pubsub", "subscriptions", "describe", "dead-letter-sub"])
    if sub is None:
        return {"dead_letter_sub": None}
    return {"dead_letter_sub": {"name": sub.get("name"), "topic": sub.get("topic")}}


def render_markdown(evidence: dict) -> str:
    lines = [
        "# Cloud Deployment Evidence",
        "",
        f"Generated by `evaluation/collect_deployment_evidence.py` at "
        f"{dt.datetime.now(dt.timezone.utc).isoformat()} — every figure below came from a live "
        "`gcloud ... --format=json` call at that moment, not typed in by hand. Re-run this after "
        "any deploy; do not hand-edit this file.",
        "",
        "## Cloud Run services (9-service topology, docs/ARCHITECTURE.md)",
        "",
        "| Service | Deployed | URL | Service Account | Latest Revision |",
        "|---|---|---|---|---|",
    ]
    for row in evidence["services"]:
        if row["deployed"]:
            lines.append(
                f"| {row['name']} | ✅ | {row.get('url', '')} | {row.get('service_account', '')} | "
                f"{row.get('latest_ready_revision', '')} |"
            )
        else:
            lines.append(f"| {row['name']} | ❌ not found | — | — | — |")

    lines += [
        "",
        "## Push subscriptions (Deploy & Harden Phase 2b)",
        "",
        "| Subscription | Exists | Push endpoint | OIDC service account |",
        "|---|---|---|---|",
    ]
    for row in evidence["push_subscriptions"]:
        if row["exists"]:
            lines.append(
                f"| {row['name']} | ✅ | {row.get('push_endpoint', '')} | {row.get('oidc_service_account', '')} |"
            )
        else:
            lines.append(f"| {row['name']} | ❌ not found | — | — |")

    lines += [
        "",
        "## Dead-letter subscription",
        "",
        f"`dead-letter-sub`: {'found — ' + str(evidence['dead_letter_sub']) if evidence['dead_letter_sub'] else 'not found'}",
        "",
        "## Known gaps in this evidence (state honestly, not silently)",
        "",
        "- Multi-service Cloud Trace spanning more than one deployed service requires "
        "`traceparent`/`X-Cloud-Trace-Context` propagation through Pub/Sub message attributes and "
        "HTTP headers respectively (tools/pubsub_push_server.py, "
        "tools/capability_dispatch_client.py) — not yet wired into either as of this script's "
        "writing; a query against Cloud Trace for a real multi-service trace tree is a manual "
        "follow-up once that lands, not automated here.",
        "- This script reports what `gcloud` can see; it does not itself exercise the deployed "
        "system (no live request is made against any service here) — pair this with a real "
        "end-to-end `run_full_migration.py` run against the deployed URLs for behavioral proof, "
        "not just existence proof.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    print("==> Collecting Cloud Run service state...")
    evidence = collect_cloud_run_services()
    print("==> Collecting push subscription state...")
    evidence.update(collect_push_subscriptions())
    print("==> Collecting dead-letter subscription state...")
    evidence.update(collect_dead_letter_backlog())

    markdown = render_markdown(evidence)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(markdown, encoding="utf-8")
    print(f"==> Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
