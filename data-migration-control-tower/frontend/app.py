#!/usr/bin/env python
"""Control Tower UI backend (Day 9, master doc §11).

    The UI should visualize what the agents are doing and why. Avoid a
    chat-first interface. A judge should understand the system by
    watching state and evidence change.

A read-only API over the exact same Firestore data every other script
in this repo reads and writes — no separate datastore, no duplicated
business logic. `tools/registry.py`, `agents/orchestrator/run_lifecycle.py`,
and friends are imported directly, never reimplemented here. The only
write path is POST /api/runs/{run_id}/approve, which wraps
tools/approval_service.approve() exactly the way
agents/cutover/approve_cutover.py does — the UI's "Approve" button IS
the human action, still a distinct identity from every agent, never the
Cutover Agent approving itself.

Usage (from repo root):
    uvicorn frontend.app:app --reload --port 8080
Then open http://localhost:8080/
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from contextlib import asynccontextmanager  # noqa: E402

from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from agents.orchestrator.run_lifecycle import RUN_COLLECTION, get_run  # noqa: E402
from tools import approval_service, registry  # noqa: E402
from tools.firestore_client import get_client  # noqa: E402
from frontend.security import (  # noqa: E402
    _is_allowlisted,
    _load_approver_allowlist,
    get_approver_identity,
)

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Starts the in-process event consumers alongside the API.

    This is what makes the console the only thing an operator runs. Every
    console action already published a durable Pub/Sub command; until now
    a human had to run a one-shot script per click to consume it, and
    GET /api/v1/operations/{id} reported the gap in words as "Waiting for
    a worker".

    Two supervisors in one deployment is normal rather than exceptional —
    `uvicorn --reload` runs a reloader parent and a child, and Cloud Run
    autoscales — so consumption is gated on a Firestore lease and the
    loser idles in standby. See tools/instance_lock.py.
    """
    from frontend.worker_runtime import start_supervisor_background, stop_supervisor

    start_supervisor_background()
    try:
        yield
    finally:
        stop_supervisor()


app = FastAPI(title="Autonomous Data Migration Control Tower", lifespan=_lifespan)

# Day 10 hardening: was allow_origins=["*"] — any origin could call this
# API from a signed-in browser. Restricted to explicitly configured UI
# origins (env-configured for whatever the UI is actually hosted at;
# localhost:8080 covers `uvicorn frontend.app:app --port 8080` local dev).
_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("UI_ALLOWED_ORIGINS", "http://localhost:8080").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    # PATCH/DELETE added in Day 11 Phase 6 for the estate onboarding
    # endpoints (PATCH /estates/{id}, DELETE /estates/{id}).
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
)


@app.exception_handler(RequestValidationError)
async def _validation_error_without_echoing_input(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 responses that say what was wrong without repeating what was sent.

    FastAPI's default handler includes an `input` field carrying the
    rejected value. That is helpful for a malformed page number and
    actively harmful for a credential: the estate endpoints deliberately
    reject any connection profile carrying a password value
    (ConnectionProfileModel forbids extra keys), and the default handler
    would then reflect that password back in the response body — into the
    caller's console, and into any access log that records response
    bodies. The rejection was already correct; this stops the rejection
    itself from becoming the disclosure.

    `loc` and `msg` are preserved, so the client still learns exactly
    which field failed and why.
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {k: v for k, v in error.items() if k not in {"input", "ctx", "url"}}
                for error in exc.errors()
            ]
        },
    )


@app.middleware("http")
async def _add_security_headers(request: Request, call_next):
    """Day 10 hardening: a Content-Security-Policy alongside the app.js
    XSS fixes — belt and suspenders against the same "estate content is
    untrusted" gap the audit flagged. script-src is 'self' plus the two
    CDN hosts this static UI currently loads from (vis-network, Firebase
    Auth's client SDK); connect-src/frame-src add every host Firebase
    Auth's signInWithPopup() flow actually touches — found live, by
    reproducing a real auth/internal-error: the SDK's popup flow uses a
    gapi-based CORS-relay iframe served from apis.google.com (NOT
    covered by the `*.googleapis.com` wildcard — apis.google.com and
    googleapis.com are unrelated domains, a real and easy-to-miss CSP
    gotcha) plus accounts.google.com for the OAuth handshake itself.
    Without both allowlisted here, the SDK's own network calls (not the
    popup's, which runs under its own document's CSP) get silently
    blocked and surface only as an opaque auth/internal-error.
    """
    response = await call_next(request)
    legacy = request.url.path == "/legacy" or request.url.path.startswith("/static/")
    # Firebase is bundled in the release, but signInWithPopup() dynamically
    # loads Google's gapi relay from apis.google.com. Keep that single runtime
    # origin in the production shell; the legacy-only unpkg/gstatic hosts do
    # not carry over to the new application.
    script_sources = (
        "'self' https://unpkg.com https://www.gstatic.com https://apis.google.com"
        if legacy
        else "'self' https://apis.google.com"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src {script_sources}; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://*.googleapis.com https://identitytoolkit.googleapis.com "
        "https://apis.google.com https://accounts.google.com; "
        "frame-src https://*.firebaseapp.com https://accounts.google.com https://apis.google.com; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "worker-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


STATIC_DIR = Path(__file__).resolve().parent / "static"
DIST_DIR = Path(__file__).resolve().parent / "client" / "web"

_AT_RISK_STATES = {"FAILED", "INVESTIGATING", "REMEDIATING"}
_SEVERITY_WEIGHT = {"CRITICAL": 25, "HIGH": 10, "MEDIUM": 4, "LOW": 1}
_STALE_AFTER = dt.timedelta(minutes=30)


def _compute_fleet_health(runs: list[dict]) -> str:
    """Day 10 hardening: was a literal "HEALTHY" string. Real, simple,
    inspectable signal instead of a scoring model: DEGRADED if any of the
    most recent runs is sitting in FAILED/INVESTIGATING with no further
    state transition in the last _STALE_AFTER window (a run stuck mid-
    recovery, not one still actively working the loop). HEALTHY
    otherwise; UNKNOWN if there's no run history at all yet.
    """
    if not runs:
        return "UNKNOWN"
    now = dt.datetime.now(dt.timezone.utc)
    for run in runs[:10]:
        if run.get("state") not in ("FAILED", "INVESTIGATING"):
            continue
        history = run.get("state_history") or []
        if not history:
            continue
        last_at = dt.datetime.fromisoformat(history[-1]["at"])
        if now - last_at > _STALE_AFTER:
            return "DEGRADED"
    return "HEALTHY"


def _run_ref(run_id: str):
    client = get_client()
    ref = client.collection(RUN_COLLECTION).document(run_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id!r}")
    return ref


def _collection_docs(run_id: str, name: str) -> list[dict]:
    return [d.to_dict() for d in _run_ref(run_id).collection(name).stream()]


@app.get("/api/runs")
def list_runs(limit: int = 50) -> list[dict]:
    from google.cloud.firestore_v1 import Query

    client = get_client()
    docs = (
        client.collection(RUN_COLLECTION)
        .order_by("created_at", direction=Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [d.to_dict() for d in docs]


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    try:
        return get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}/lineage")
def run_lineage(run_id: str) -> dict:
    tables = _collection_docs(run_id, "catalog")
    pipelines = _collection_docs(run_id, "pipelines")
    edges = _collection_docs(run_id, "dependencies")

    nodes = [
        {"id": t["table_id"], "label": f"{t['schema']}.{t['table']}", "type": "table", "classification": t.get("classification")}
        for t in tables
    ] + [{"id": p["pipeline_id"], "label": p["pipeline_id"], "type": "pipeline"} for p in pipelines]

    # Dependency edges reference short "Schema.Table" names and pipeline
    # ids, not the full source-qualified table_id — resolve short names
    # to their table_id so the graph doesn't silently drop nodes.
    short_to_table_id = {f"{t['schema']}.{t['table']}": t["table_id"] for t in tables}

    def _resolve(ref: str) -> str:
        return short_to_table_id.get(ref, ref)

    edge_list = [
        {
            "from": _resolve(e["from_asset"]),
            "to": _resolve(e["to_asset"]),
            "relationship": e["relationship"],
            "confidence": e["confidence"],
            "source": e.get("source"),
        }
        for e in edges
    ]
    return {"nodes": nodes, "edges": edge_list}


@app.get("/api/runs/{run_id}/reconciliation")
def run_reconciliation(run_id: str) -> list[dict]:
    checks = _collection_docs(run_id, "reconciliation")
    return sorted(checks, key=lambda c: c.get("checked_at", ""))


@app.get("/api/runs/{run_id}/audit")
def run_audit(run_id: str) -> list[dict]:
    """Chronological, distinctly-typed audit trail (§23.2's "make
    containment visible" requirement, generalized to every recorded
    decision — policy, containment, and risk findings all carry actor,
    origin/subject, and outcome)."""
    events: list[dict] = []
    for decision in _collection_docs(run_id, "policy_decisions"):
        events.append(
            {
                "kind": "policy_decision",
                "at": decision.get("decided_at"),
                "actor": decision.get("agent_id"),
                "subject": decision.get("action"),
                "outcome": decision.get("decision"),
                "detail": decision,
            }
        )
    for event in _collection_docs(run_id, "containment_events"):
        events.append(
            {
                "kind": "containment_event",
                "at": event.get("recorded_at"),
                "actor": event.get("acting_agent"),
                "subject": event.get("origin"),
                "outcome": event.get("outcome"),
                "detail": event,
            }
        )
    for finding in _collection_docs(run_id, "risk_findings"):
        events.append(
            {
                "kind": "risk_finding",
                "at": finding.get("discovered_at"),
                "actor": finding.get("discovered_by"),
                "subject": finding.get("table_id"),
                "outcome": finding.get("finding_type"),
                "detail": finding,
            }
        )
    return sorted((e for e in events if e["at"]), key=lambda e: e["at"])


@app.get("/api/registry")
def list_registry() -> dict:
    client = get_client()
    all_cards = [doc.to_dict() for doc in client.collection_group("versions").stream()]
    by_agent: dict[str, list[dict]] = {}
    for card in all_cards:
        by_agent.setdefault(card["agent_id"], []).append(card)
    for cards in by_agent.values():
        cards.sort(key=lambda c: c["version"])
    return by_agent


@app.get("/api/dashboard")
def dashboard() -> dict:
    runs = list_runs(limit=200)
    completed = sum(1 for r in runs if r.get("state") == "COMPLETE")
    at_risk = sum(1 for r in runs if r.get("state") in _AT_RISK_STATES)

    client = get_client()
    # Filtered client-side, not via .where() — a collection_group()
    # equality filter needs a composite index that doesn't exist in this
    # project (confirmed empirically); registry.py made the same call
    # for the same reason. The collection is small enough that this is
    # not a real cost.
    all_decisions = [d.to_dict() for d in client.collection_group("policy_decisions").stream()]
    blocked_run_ids = {d.get("run_id") for d in all_decisions if d.get("decision") == "DENY" and d.get("run_id")}

    active_run = runs[0] if runs else None
    risk_score = 0
    active_findings: list[dict] = []
    discovered_pipeline_count = 0
    if active_run:
        active_findings = _collection_docs(active_run["run_id"], "risk_findings")
        risk_score = min(100, sum(_SEVERITY_WEIGHT.get(f.get("severity"), 0) for f in active_findings))
        discovered_pipeline_count = len(_collection_docs(active_run["run_id"], "pipelines"))

    return {
        "fleet_health": _compute_fleet_health(runs),
        "total_runs": len(runs),
        "completed_runs": completed,
        "at_risk_runs": at_risk,
        "blocked_runs": len(blocked_run_ids),
        "discovered_pipelines": discovered_pipeline_count,
        "active_run": active_run,
        "active_run_risk_score": risk_score,
        "active_run_findings_sample": active_findings[:5],
    }


@app.get("/api/runs/{run_id}/plan")
def run_plan(run_id: str) -> dict:
    """Plan hash + scope, shown in the approval confirmation dialog
    before a human commits to approving (Day 10 hardening — §5.2 asks
    the approver to see the exact scope being approved, not a blind
    click)."""
    doc = _run_ref(run_id).collection("migration_plan").document("current").get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail=f"No migration plan recorded yet for run {run_id!r}.")
    return doc.to_dict()


class ApproveRequest(BaseModel):
    # Day 10 hardening: approver_identity used to be a client-supplied
    # string here (defaulting to a literal "control-tower-ui@example.
    # internal") — that's not authentication, it's a label anyone calling
    # this endpoint could set to anything. Identity now comes ONLY from
    # _verify_bearer_token()'s verified Firebase ID token; the field is
    # gone entirely. justification is the one thing the client still
    # supplies, and it's mandatory.
    justification: str = Field(min_length=5, max_length=2000)


@app.post("/api/runs/{run_id}/approve")
def approve_run(
    run_id: str, body: ApproveRequest, approver_identity: str = Depends(get_approver_identity)
) -> dict:
    """The UI's one write path — the human approval action (§11.1's
    dashboard, §5.2's separation of duties). Identical to what
    agents/cutover/approve_cutover.py does; never callable by agent code.

    Day 10 hardening: now requires a verified Firebase ID token (401 if
    missing/invalid, enforced by the get_approver_identity dependency
    above) and a justification string; both are recorded on the
    approval_history trail via tools/approval_service.py.
    """
    from agents.orchestrator.run_lifecycle import transition_state

    try:
        run = get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if run["state"] != "READY_FOR_APPROVAL":
        raise HTTPException(
            status_code=409,
            detail=f"Run is in state {run['state']!r}, not READY_FOR_APPROVAL — nothing to approve.",
        )
    try:
        token = approval_service.approve(
            run_id, approver_identity=approver_identity, justification=body.justification
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    transition_state(run_id, "APPROVED")
    return {"status": "APPROVED", "token_id": token["token_id"], "approved_by": token["approved_by"]}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="legacy-static")

# The new versioned API is intentionally added without disturbing the legacy
# /api routes above.  /api/v1/config remains public so the bundled Firebase SDK
# can initialize; every other v1 data route verifies a Firebase token.
from frontend.api_v1 import public_router as api_v1_public_router  # noqa: E402
from frontend.api_v1 import router as api_v1_router  # noqa: E402

app.include_router(api_v1_public_router)
app.include_router(api_v1_router)


@app.get("/legacy")
def legacy_index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/{full_path:path}")
def application_shell(full_path: str) -> FileResponse:
    """Serves the JET release build and provides SPA deep-link fallback.

    During source-only development (before ``ojet build``) the legacy UI is
    retained as a safe fallback. Unknown API paths remain real 404s rather
    than being disguised as an HTML page.
    """
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="No such API endpoint.")

    if DIST_DIR.exists():
        requested = (DIST_DIR / full_path).resolve()
        dist_root = DIST_DIR.resolve()
        if requested.is_relative_to(dist_root) and requested.is_file():
            # Only fingerprinted release assets are immutable. Oracle JET also
            # emits a few stable loader names, which must be revalidated.
            fingerprinted = bool(re.search(r"[.-][0-9a-f]{8,}[.-]", requested.name, re.IGNORECASE))
            versioned_jet_theme = full_path.startswith("styles/redwood/20.1.3/")
            # Brand art carries no content hash, so it would otherwise be
            # revalidated on every page load. The directory itself is
            # versioned instead: publishing new artwork means /v2/, which
            # is a new URL, so a stale cache is impossible by construction.
            versioned_brand = full_path.startswith("assets/brand/v1/")
            cache = (
                "public, max-age=31536000, immutable"
                if fingerprinted or versioned_jet_theme or versioned_brand
                else "no-cache"
            )
            return FileResponse(str(requested), headers={"Cache-Control": cache})
        index_path = DIST_DIR / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path), headers={"Cache-Control": "no-cache"})
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-cache"})
