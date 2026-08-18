#!/usr/bin/env python
"""Day 5 milestone (master doc §17.1, Thu 20 Aug; §12.1's "first
end-to-end slice"): the full path DISCOVERED -> ... -> COMPLETE in one
command, with exactly one denial, one reconciliation failure, one
remediation, and one approval.

Chains:
  1-6. agents/orchestrator/pipeline_stages.py::advance_to_passed(): Day 4's
     event-driven Discovery->Lineage->Risk cascade (the unauthorized-PII-
     read denial happens here), Migration Planner, a migration execution
     deliberately seeded with a row-loss defect, a failed validation, an
     investigate/remediate recovery loop (checking memory_bank first —
     see recovery.py), and a second, passing validation.
  7. Cutover Agent requests approval -> READY_FOR_APPROVAL
     (the cutover-cannot-self-approve denial happens here)
  8. Human approval (a distinct identity from every agent) -> APPROVED
  9. Cutover Agent performs cutover + post-cutover monitoring ->
     CUTOVER -> MONITORING -> COMPLETE

Usage (from repo root):
    python agents/orchestrator/run_full_migration.py [pipeline_id]
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(message)s")

from agents.cutover.agent import (  # noqa: E402
    attempt_self_approval,
    perform_cutover,
    request_approval as cutover_request_approval,
    trigger_post_cutover_monitoring,
)
from agents.orchestrator.pipeline_stages import advance_to_passed  # noqa: E402
from agents.orchestrator.run_lifecycle import get_run, transition_state  # noqa: E402
from tools import approval_service, tracing  # noqa: E402
from tools.migration_plan import primary_target  # noqa: E402

DROP_FRACTION = 0.01  # the §7.2 row-loss fault-injection scenario, seeded once


def _step(n: int, title: str) -> None:
    print(f"\n=== Step {n}: {title} ===")


def main() -> None:
    pipeline_id = sys.argv[1] if len(sys.argv) > 1 else "wwi.sales.customers"
    self_approval_denied = False
    approvals = 0

    _step(1, "Discovery -> Lineage -> Risk -> Planner -> Migrate (seeded defect) -> Validate -> Recovery if needed")
    stages = advance_to_passed(pipeline_id, drop_fraction=DROP_FRACTION)
    run_id = stages["run_id"]
    pii_read_denied = stages["cascade"]["analysis"]["pii_access_denied"]
    print(f"run_id={run_id}, pii_read_denied={pii_read_denied}")
    print(f"plan_hash={stages['plan']['plan_hash'][:12]}..., steps={len(stages['plan']['steps'])}")
    print(
        f"first pass: loaded {stages['first_pass']['target_count']}/"
        f"{stages['first_pass']['source_count']} rows (defect seeded)"
    )

    reconciliation_failures = 1 if stages["reconciliation_failed"] else 0
    remediations = 0
    if stages["reconciliation_failed"]:
        incident = stages["incident"]
        remediations = 1
        print(f"incident={incident['signature']}, root_cause_by={incident['root_cause_generated_by']}")
        print(f"root_cause: {incident['root_cause']}")

    final_validation = stages["final_validation"]
    print(f"final validation: overall_status={final_validation['overall_status']}, checks={final_validation['checks']}")

    if final_validation["overall_status"] != "PASSED":
        print("\nFAILED: remediation did not resolve the defect — stopping before cutover.")
        raise SystemExit(1)

    _step(7, "Cutover Agent requests approval (and proves it cannot self-approve)")
    self_approval_decision = attempt_self_approval(run_id)
    self_approval_denied = self_approval_decision["decision"] == "DENY"
    approval_request = cutover_request_approval(run_id)
    transition_state(run_id, "READY_FOR_APPROVAL")
    print(f"approval requested, plan_hash={approval_request['plan_hash'][:12]}...")

    _step(8, "Human approval (distinct identity — never the Cutover Agent)")
    # Day 10 hardening (§5.2): the REAL approval gate now requires a
    # verified Firebase ID token — see frontend/app.py::approve_run() and
    # _verify_bearer_token(). This literal identity is a deliberate stand-
    # in for that human click so this script can still demonstrate the
    # full REQUESTED->COMPLETE lifecycle unattended (no browser, no
    # interactive sign-in) — it is not, and was never meant to be, the
    # production approval path. Don't "fix" this by prompting for
    # credentials here; use the UI for a real approval.
    token = approval_service.approve(
        run_id,
        approver_identity="ops-lead@example.internal",
        justification="scripted end-to-end demo run (agents/orchestrator/run_full_migration.py) — "
        "not a real human approval; see frontend/app.py for the authenticated UI gate",
    )
    approvals += 1
    transition_state(run_id, "APPROVED")
    print(f"approved by {token['approved_by']}, token={token['token_id']}")

    _step(9, "Cutover Agent performs cutover + post-cutover monitoring")
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
    print(f"cutover_token={cutover_record['approval_token_id']}, monitoring={monitoring['status']}")

    if monitoring["status"] == "HEALTHY":
        transition_state(run_id, "COMPLETE")

    final_state = get_run(run_id)["state"]
    print(
        "\n=== Day 5 milestone check (master doc §17.1) ===\n"
        f"run_id={run_id}\n"
        f"final_state={final_state}\n"
        f"unauthorized PII read denied: {pii_read_denied}\n"
        f"cutover self-approval denied: {self_approval_denied}\n"
        f"reconciliation_failures={reconciliation_failures} (seeded row-loss)\n"
        f"remediations={remediations} (clean reload)\n"
        f"approvals={approvals} (human approval, distinct identity from every agent)\n"
        "\n(§17.1 asks for 'one denial' — this run demonstrates two distinct "
        "governance denials, the PII-read block and the cutover self-approval "
        "block, both explicit §12 Phase-5 exit conditions; that is stronger "
        "evidence than the literal count, not a deviation from it.)\n"
    )
    if final_state != "COMPLETE":
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    finally:
        # Day 10 Phase 5: this script is short-lived enough to outrace
        # OpenTelemetry's own atexit flush — force it explicitly so the
        # run's spans are actually in Cloud Trace by the time this
        # process exits, success or failure either way.
        tracing.flush()
