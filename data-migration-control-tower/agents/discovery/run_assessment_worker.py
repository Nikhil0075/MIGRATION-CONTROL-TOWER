#!/usr/bin/env python
"""Consumes one operator-console assessment.requested command.

This mirrors the repository's finite pull-worker shape.  A Cloud Run/Eventarc
push worker can replace it without changing the UI operation contract.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.discovery.run_assessment import run_assessment  # noqa: E402
from tools.events import ack, nack, pull  # noqa: E402
from tools.firestore_client import get_client  # noqa: E402

SUBSCRIPTION = "assessment-requested-sub"


def main() -> None:
    messages = pull(SUBSCRIPTION, max_messages=1, timeout=30)
    if not messages:
        raise SystemExit("No assessment.requested message was available.")
    payload = messages[0]
    operation_id = payload.get("operation_id")
    operation_ref = (
        get_client().collection("operation_requests").document(operation_id) if operation_id else None
    )
    try:
        def _attach_run(run_id: str) -> None:
            if operation_ref:
                operation_ref.set(
                    {
                        "status": "active",
                        "run_id": run_id,
                        "estate_id": payload.get("estate_id"),
                        "result": {"run_id": run_id},
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                    merge=True,
                )

        report = run_assessment(
            str(REPO_ROOT / payload["pack_path"]),
            estate_id=payload.get("estate_id"),
            on_run_created=_attach_run,
        )
        if operation_ref:
            operation_ref.update(
                {
                    "status": "done",
                    "result": {"run_id": report["run_id"], "pack_id": report["pack_id"]},
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
        ack(SUBSCRIPTION, payload["_pubsub_ack_id"])
        print(f"assessment complete: run_id={report['run_id']}, pack={report['pack_id']}")
    except Exception as exc:
        if operation_ref:
            operation_ref.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
        nack(SUBSCRIPTION, payload["_pubsub_ack_id"])
        raise


if __name__ == "__main__":
    main()
