#!/usr/bin/env python
"""Day 2 exit-condition run: creates a migration run, inventories the
estate (SQL Server + Oracle corpus + DAG artifacts), and persists the
structured catalog under that run ID in Firestore.

Usage (from repo root):
    python agents/discovery/run_discovery.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.discovery.agent import AGENT_FRAMEWORK, discover_estate  # noqa: E402
from agents.orchestrator.run_lifecycle import create_run, write_catalog  # noqa: E402

PIPELINE_ID = "wwi.sales.customers"  # matches the demo walkthrough in master doc §2.3


def main() -> None:
    print(f"[discovery-agent] framework: {AGENT_FRAMEWORK}")

    oracle_corpus_path = os.environ.get(
        "ORACLE_CORPUS_PATH", "simulator/source_setup/oracle_dialect_corpus"
    )
    dag_artifacts_path = os.environ.get("DAG_ARTIFACTS_PATH", "simulator/source_setup/dags")

    run_id = create_run(pipeline_id=PIPELINE_ID)
    print(f"[discovery-agent] created run: {run_id}")

    table_records, pipeline_records = discover_estate(oracle_corpus_path, dag_artifacts_path)
    print(
        f"[discovery-agent] discovered {len(table_records)} tables, "
        f"{len(pipeline_records)} pipelines"
    )

    summary = write_catalog(run_id, table_records, pipeline_records)
    print(f"[discovery-agent] catalog persisted: {summary}")

    if summary["tables_written"] < 12:
        print(
            "[discovery-agent] WARNING: fewer than 12 tables discovered — "
            "check that the SQL Server container is running and "
            "WideWorldImporters is restored (docker compose + restore_wwi.sh)."
        )

    print("[discovery-agent] Day 2 exit condition check: "
          f"run_id={run_id}, tables={summary['tables_written']}, "
          f"pipelines={summary['pipelines_written']}, state={summary['state']}")


if __name__ == "__main__":
    main()
