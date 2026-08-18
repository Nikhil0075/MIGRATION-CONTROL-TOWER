"""Cloud Run entrypoint for hello-agent.

Deliberately does not attempt to reach the on-prem SQL Server from Cloud
Run (see root README / plan for why: Cloud Run has no network path to a
laptop's local Docker network without a tunnel). This service proves the
other half of the Day 1 exit condition: the agent code runs on Cloud Run
and can write to the Firestore state plane.

/list-source-tables is included and works when SOURCE_TUNNEL_HOST is set
to a reachable tunnel endpoint (optional stretch, not required for Day 1).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

from agents.orchestrator.hello_agent.agent import (
    AGENT_FRAMEWORK,
    AGENT_ID,
    AGENT_VERSION,
    list_source_tables,
    write_firestore_row,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hello_agent.main")

app = FastAPI(title="hello-agent", version=AGENT_VERSION)


@app.get("/status")
def status() -> dict:
    # NOT named /healthz: Cloud Run's default *.run.app domain intercepts
    # that exact path at the infrastructure level and never forwards it
    # to the container (reproduced identically on Google's own stock
    # Cloud Run quickstart image — confirmed 16 Aug 2026, not app-specific).
    return {"status": "ok", "agent_id": AGENT_ID, "framework": AGENT_FRAMEWORK}


@app.post("/bootstrap-check")
def bootstrap_check() -> dict:
    """Day 1 exit condition (cloud half): write a row to Firestore from Cloud Run."""
    try:
        path = write_firestore_row(note="cloud_run bootstrap-check")
    except Exception as exc:  # noqa: BLE001
        logger.exception("bootstrap-check failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "firestore_path": path}


@app.get("/list-source-tables")
def list_source_tables_endpoint() -> dict:
    """Optional: only works if SOURCE_TUNNEL_HOST exposes the local DB (see README)."""
    try:
        tables = list_source_tables()
    except Exception as exc:  # noqa: BLE001
        logger.exception("list-source-tables failed (expected without a tunnel)")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", "table_count": len(tables), "tables": tables}
