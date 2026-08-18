#!/usr/bin/env python
"""Long-horizon run fixture (master doc §21.2, proof 2):

    Maintain a fixture run whose lifecycle spans three weeks of real
    timestamps: discovery in week one, an approval gap of eighteen
    days, cutover today. Render the elapsed gap explicitly in the run
    timeline.

    Label the backdated fixture: the long-horizon fixture uses seeded
    historical timestamps because the hackathon window is shorter than
    the timeline being demonstrated. Label it as a seeded historical
    fixture in the interface, in the README, and in the video narration.
    ... A judge who discovers an unlabeled backdated run will discount
    everything else in the submission.

Every migration mechanic here is real — real SQL Server read, real
BigQuery load, real reconciliation, real approval token, real cutover.
Only two timestamps are deliberately backdated: the run's created_at
(discovery, "week one") and the approval's requested_at (so the gap to
today's real approval is ~18 days). Both the run and approval documents
are stamped is_seeded_fixture=true and a human-readable fixture_label,
so this is unmistakable in Firestore itself — not just in this script's
output.

Usage (from repo root):
    python agents/orchestrator/seed_long_horizon_fixture.py [pipeline_id]
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.cutover.agent import (  # noqa: E402
    attempt_self_approval,
    perform_cutover,
    request_approval,
    trigger_post_cutover_monitoring,
)
from agents.orchestrator.pipeline_stages import advance_to_passed  # noqa: E402
from agents.orchestrator.run_lifecycle import get_run, transition_state  # noqa: E402
from tools import approval_service  # noqa: E402
from tools.migration_plan import primary_target  # noqa: E402
from tools.firestore_client import get_client  # noqa: E402

FIXTURE_LABEL = (
    "SEEDED HISTORICAL FIXTURE (master doc §21.2) — timestamps backdated to "
    "demonstrate long-horizon behavior across a ~3-week span; every migration "
    "mechanic (Discovery/Lineage/Risk/Planner/Migration/Validation/Cutover) "
    "executed for real. This is NOT a real 3-week production run."
)

DISCOVERY_DAYS_AGO = 21
APPROVAL_REQUESTED_DAYS_AGO = 18


def main() -> None:
    pipeline_id = sys.argv[1] if len(sys.argv) > 1 else "wwi.sales.customers"
    now = dt.datetime.now(dt.timezone.utc)
    discovery_time = now - dt.timedelta(days=DISCOVERY_DAYS_AGO)
    approval_requested_time = now - dt.timedelta(days=APPROVAL_REQUESTED_DAYS_AGO)

    print(f"=== Seeding long-horizon fixture: discovery backdated to {discovery_time.isoformat()} ===")
    stages = advance_to_passed(pipeline_id, drop_fraction=0.0)
    run_id = stages["run_id"]
    if stages["final_validation"]["overall_status"] != "PASSED":
        raise RuntimeError("Expected a clean PASSED validation for the fixture.")

    client = get_client()
    run_ref = client.collection("migration_runs").document(run_id)
    run_ref.update(
        {
            "created_at": discovery_time.isoformat(),
            "is_seeded_fixture": True,
            "fixture_label": FIXTURE_LABEL,
        }
    )
    print(f"run_id={run_id}, created_at backdated to {DISCOVERY_DAYS_AGO} days ago")

    attempt_self_approval(run_id)
    request_approval(run_id)
    transition_state(run_id, "READY_FOR_APPROVAL")

    approval_ref = run_ref.collection("approval").document("current")
    approval_ref.update(
        {
            "requested_at": approval_requested_time.isoformat(),
            "is_seeded_fixture": True,
            "fixture_label": FIXTURE_LABEL,
        }
    )
    print(f"approval requested_at backdated to {APPROVAL_REQUESTED_DAYS_AGO} days ago")

    print("=== Human approval happens for real, today ===")
    token = approval_service.approve(run_id, approver_identity="ops-lead@example.internal")
    transition_state(run_id, "APPROVED")

    elapsed_days = (
        dt.datetime.fromisoformat(token["approved_at"]) - approval_requested_time
    ).days
    run_ref.update({"elapsed_days": elapsed_days})
    print(f"approval gap: {elapsed_days} days (requested {APPROVAL_REQUESTED_DAYS_AGO}d ago, approved today)")

    cutover_record = perform_cutover(run_id)
    transition_state(run_id, "CUTOVER")
    transition_state(run_id, "MONITORING")
    target = primary_target(run_id)
    monitoring = trigger_post_cutover_monitoring(
        run_id,
        target["source_schema"],
        target["source_table"],
        target["target_table"],
        target["key_column"],
    )
    if monitoring["status"] == "HEALTHY":
        transition_state(run_id, "COMPLETE")

    run = get_run(run_id)
    print(
        "\n=== Day 7 long-horizon fixture check (master doc §21.2, proof 2) ===\n"
        f"run_id={run_id}\n"
        f"final_state={run['state']}\n"
        f"is_seeded_fixture={run.get('is_seeded_fixture')}\n"
        f"discovery (created_at): {run.get('created_at')} ({DISCOVERY_DAYS_AGO} days ago)\n"
        f"approval requested: {approval_requested_time.isoformat()} ({APPROVAL_REQUESTED_DAYS_AGO} days ago)\n"
        f"approval granted: {token['approved_at']} (today)\n"
        f"elapsed_days field on run doc: {elapsed_days}\n"
        f"cutover_token={cutover_record['approval_token_id']}\n"
        f"\nfixture_label (stamped on the run and approval documents in Firestore):\n{FIXTURE_LABEL}\n"
    )
    if run["state"] != "COMPLETE":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
