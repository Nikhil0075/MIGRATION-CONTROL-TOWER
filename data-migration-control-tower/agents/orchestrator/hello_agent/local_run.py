#!/usr/bin/env python
"""Local Day-1 proof run: reads the local WideWorldImporters table list
through the metadata tool, then (if GOOGLE_APPLICATION_CREDENTIALS/ADC is
configured) writes a bootstrap-check row to Firestore.

Usage (from repo root):
    python agents/orchestrator/hello_agent/local_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable regardless of invocation cwd.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.orchestrator.hello_agent.agent import (  # noqa: E402
    AGENT_FRAMEWORK,
    list_source_tables,
    write_firestore_row,
)


def main() -> None:
    print(f"[hello-agent] framework: {AGENT_FRAMEWORK}")

    print("[hello-agent] calling list_source_tables()...")
    tables = list_source_tables()
    print(f"[hello-agent] found {len(tables)} tables in the local source:")
    for t in tables[:25]:
        print(f"  - {t}")
    if len(tables) > 25:
        print(f"  ... and {len(tables) - 25} more")

    print("[hello-agent] calling write_firestore_row()...")
    try:
        path = write_firestore_row(note=f"local_run day1 check, {len(tables)} tables discovered")
        print(f"[hello-agent] wrote Firestore document: {path}")
    except Exception as exc:  # noqa: BLE001
        print(
            "[hello-agent] Firestore write failed — this is expected if "
            "GCP auth / project is not yet configured. Error:\n  "
            f"{exc}"
        )
        raise SystemExit(1) from exc

    print("[hello-agent] Day 1 local proof complete.")


if __name__ == "__main__":
    main()
