#!/usr/bin/env python
"""Consume an authenticated console ``cutover.approved`` operation."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.cutover.agent import perform_cutover, trigger_post_cutover_monitoring  # noqa: E402
from agents.orchestrator.run_lifecycle import get_run, transition_state  # noqa: E402
from tools.events import ack, nack, publish, pull  # noqa: E402
from tools.firestore_client import get_client  # noqa: E402
from tools.migration_plan import scheduled_targets  # noqa: E402

SUBSCRIPTION = "cutover-approved-sub"


def _finish(run_id: str) -> dict:
    state = get_run(run_id)["state"]
    if state == "APPROVED":
        perform_cutover(run_id)
        transition_state(run_id, "CUTOVER")
        state = "CUTOVER"
    if state == "CUTOVER":
        transition_state(run_id, "MONITORING")
        state = "MONITORING"
    if state == "MONITORING":
        # Every scheduled target is monitored, and the run only completes
        # if all of them are healthy. This used to be a single hardcoded
        # ("Sales", "Customers", "customers_dim", "CustomerID") call, which
        # in a multi-table run would have declared the whole cutover
        # healthy on the evidence of one table.
        monitoring = None
        for target in scheduled_targets(run_id):
            monitoring = trigger_post_cutover_monitoring(
                run_id,
                target["source_schema"],
                target["source_table"],
                target["target_table"],
                target["key_column"],
            )
            if monitoring["status"] != "HEALTHY":
                return {"run_id": run_id, "state": "MONITORING", "monitoring": monitoring}
        transition_state(run_id, "COMPLETE")
        publish("cutover.completed", {"run_id": run_id})
    final = get_run(run_id)["state"]
    if final != "COMPLETE":
        raise RuntimeError(f"Run {run_id!r} cannot consume cutover.approved from state {final!r}.")
    return {"run_id": run_id, "state": final}


def handle_cutover_approved(payload: dict) -> dict:
    """Runs one cutover.approved command. Returns the final run state.

    Pure function of one payload — no pull, ack or nack; the caller owns
    the message. Shared by the CLI wrapper below and the in-process
    supervisor so the operation-record lifecycle exists once.

    Note the unhealthy branch. `_finish` RETURNS (rather than raising)
    when a post-cutover monitoring check is not HEALTHY, leaving the run
    at MONITORING. Recording that as "done" would report a cutover that
    did not complete as successful, so it is recorded as failed with the
    monitoring evidence attached. `_finish` itself is left alone — four
    tests pin its behaviour, and the honest fix belongs at the boundary
    that interprets the result.
    """
    operation_id = payload.get("operation_id")
    operation_ref = (
        get_client().collection("operation_requests").document(operation_id) if operation_id else None
    )
    try:
        result = _finish(payload["run_id"])
    except Exception as exc:
        if operation_ref:
            operation_ref.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
        raise

    completed = result.get("state") == "COMPLETE"
    if operation_ref:
        operation_ref.update(
            {
                "status": "done" if completed else "failed",
                "result": result,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                **(
                    # Cleared on success for the same reason as the
                    # assessment handler: a redelivered operation that
                    # failed once and later completed would otherwise stay
                    # "done" with the earlier attempt's error attached.
                    {"error": None}
                    if completed
                    else {
                        "error": (
                            f"cutover halted at {result.get('state')}: post-cutover monitoring "
                            f"reported {(result.get('monitoring') or {}).get('status', 'an unhealthy target')}"
                        )
                    }
                ),
            }
        )
    return result


def main() -> None:
    """CLI wrapper: one message, then exit. The supervisor calls
    handle_cutover_approved directly."""
    from tools.worker_supervisor import warn_if_a_supervisor_is_running

    warn_if_a_supervisor_is_running()
    messages = pull(SUBSCRIPTION, max_messages=1, timeout=30)
    if not messages:
        raise SystemExit("No cutover.approved message was available.")
    payload = messages[0]
    try:
        result = handle_cutover_approved(payload)
    except Exception:
        nack(SUBSCRIPTION, payload["_pubsub_ack_id"])
        raise
    ack(SUBSCRIPTION, payload["_pubsub_ack_id"])
    print(f"cutover finished: run_id={result['run_id']} state={result['state']}")


if __name__ == "__main__":
    main()
