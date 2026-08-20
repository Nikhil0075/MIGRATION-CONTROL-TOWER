"""Versioned API for the Oracle Redwood Migration Control Tower client."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, BeforeValidator, Field

from agents.orchestrator.run_lifecycle import RUN_COLLECTION, get_run, transition_state
from frontend.operations import get_operation, queue_operation, record_wave_override
from frontend.security import (
    UserContext,
    authorize_estate,
    get_user_context,
    require_role,
)
from tools import approval_service
from tools.connection_context import DEFAULT_ESTATE_ID
from tools.firestore_client import get_client

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS_DIR = REPO_ROOT / "packs"
FIREBASE_CONFIG_PATH = REPO_ROOT / "frontend" / "static" / "firebase-config.js"

# Removed in Day 11 Phase 4:
#   ESTATE_PATH  — estates come from the registry now (tools/estate_registry.py),
#                  with committed YAML as the fallback, so the API no longer
#                  hardcodes one file under simulator/.
#   SCALE_REPORT — dead: it pointed at evaluation/reports/scale_metrics.md but
#                  /evaluations has always read Firestore. A filesystem read
#                  here would not have survived a stateless Cloud Run deploy.

async def _scope_reads(user: UserContext = Depends(get_user_context)) -> None:
    """Pin every read on this router to the caller's estates, by default.

    `_authorize_read` sets the same scope, but only for endpoints that
    remember to call it. This is the fail-safe half: it runs for every
    route on `router`, so an endpoint added tomorrow that forgets is
    scoped anyway. Forgetting can now only make a read too NARROW, which
    is visible, rather than too wide, which is not.

    `async def` on purpose. Sync endpoints run in a worker thread with a
    COPY of this request's context, so a context variable set here is
    visible to them — one set in a sync dependency would be set in a
    different copy and lost. The registry read that resolves the grant is
    handed to a thread so it does not block the event loop; only the
    `.set()` happens here, which is where it has to happen.
    """
    from starlette.concurrency import run_in_threadpool

    _READ_SCOPE.set(await run_in_threadpool(_visible_estate_ids, user))


public_router = APIRouter(prefix="/api/v1", tags=["v1-public"])
router = APIRouter(
    prefix="/api/v1",
    tags=["v1"],
    dependencies=[Depends(require_role("viewer")), Depends(_scope_reads)],
)


class Meta(BaseModel):
    generated_at: str
    freshness: Literal["live", "cached", "stale"] = "live"
    next_cursor: str | None = None
    total: int | None = None


class Envelope(BaseModel):
    data: Any
    meta: Meta


class Availability(BaseModel):
    status: Literal["available", "not_configured", "stale"]
    reason: str | None = None
    last_observed_at: str | None = None
    value: Any = None


class ProgressSnapshot(BaseModel):
    percent: int = Field(ge=0, le=100)
    status: Literal["queued", "active", "waiting", "held", "failed", "complete"]
    label: str
    current_stage: str
    completed_units: int = Field(ge=0)
    total_units: int = Field(ge=1)
    run_id: str | None = None
    last_observed_at: str


def _reject_null_estate(value: Any) -> Any:
    """`null` is not the same as absent, and must not be treated as it.

    Pydantic applies a field default only when the KEY IS MISSING. A body
    carrying `"estate_id": null` therefore skips the default and fails
    type validation with "Input should be a valid string" — a 422 that
    tells an operator nothing about what to do. The console sends exactly
    that shape: three forms post `estate_id: activeEstateId`, and that is
    null until `/api/v1/estates` resolves, and permanently null for a
    user with no estates.

    Rejected rather than defaulted, deliberately. An explicit null means
    the caller had no estate selected; quietly applying an operator
    action — a hold, an assessment, a migration — to the demo estate
    instead is a worse outcome than refusing it. Reads may be unscoped
    and are filtered to the caller's grant; a WRITE always belongs to
    exactly one estate, so there is no unscoped default to fall back on.
    """
    if value is None:
        raise ValueError(
            "No estate is selected. This action applies to exactly one estate, "
            "so estate_id must name it."
        )
    return value


#: The compatibility default stays for callers that OMIT the field. It is
#: the explicit null that is refused.
EstateIdField = Annotated[
    str,
    BeforeValidator(_reject_null_estate),
    Field(default=DEFAULT_ESTATE_ID, min_length=2, max_length=100),
]


class StartAssessmentRequest(BaseModel):
    pack_id: str = Field(min_length=2, max_length=100)
    estate_id: EstateIdField
    justification: str = Field(min_length=8, max_length=2000)


class StartRunRequest(BaseModel):
    source_id: str | None = Field(default=None, min_length=2, max_length=100)
    pack_id: str | None = Field(default=None, min_length=2, max_length=100)
    pipeline_id: str | None = Field(default=None, min_length=2, max_length=200)
    # Deprecated compatibility alias for one release. New callers send
    # pack_id; disagreement is rejected instead of silently picking one.
    execution_profile: str | None = Field(default=None, min_length=2, max_length=100)
    estate_id: EstateIdField
    justification: str = Field(min_length=8, max_length=2000)


class RetryRequest(BaseModel):
    justification: str = Field(min_length=8, max_length=2000)


class ConnectionProfileModel(BaseModel):
    """How to reach a source, BY REFERENCE ONLY.

    There is deliberately no password field, and `model_config` forbids
    extra keys so one cannot arrive from a caller. The wizard collects a
    Secret Manager reference or the NAME of an environment variable;
    tools/secret_resolver.py resolves it at connect time. This mirrors
    contracts/metadata_model.json's ConnectionProfile, which is a closed
    schema for the same reason — tests assert both.
    """

    model_config = {"extra": "forbid"}

    host: str | None = Field(default=None, max_length=253)
    host_env: str | None = Field(default=None, max_length=100)
    port: int | None = Field(default=None, ge=1, le=65535)
    port_env: str | None = Field(default=None, max_length=100)
    user: str | None = Field(default=None, max_length=100)
    user_env: str | None = Field(default=None, max_length=100)
    password_secret_ref: str | None = Field(default=None, max_length=300)
    password_env: str | None = Field(default=None, max_length=100)


class EstateSourceModel(BaseModel):
    source_id: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    adapter: str = Field(min_length=2, max_length=100)
    config: dict = Field(default_factory=dict)
    connection_profile: ConnectionProfileModel | None = None
    pack_id: str | None = Field(default=None, max_length=100)


class EstateTargetModel(BaseModel):
    system: Literal["bigquery"] = "bigquery"
    project: str | None = None
    dataset: str | None = None
    dataset_env: str | None = None


class CreateEstateRequest(BaseModel):
    estate_id: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=2, max_length=200)
    sources: list[EstateSourceModel] = Field(min_length=1)
    target: EstateTargetModel | None = None
    owner: dict | None = None
    justification: str = Field(min_length=8, max_length=2000)


class UpdateEstateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=200)
    sources: list[EstateSourceModel] | None = None
    target: EstateTargetModel | None = None
    owner: dict | None = None
    status: Literal["ACTIVE", "DISABLED"] | None = None
    justification: str = Field(min_length=8, max_length=2000)


class DeleteEstateRequest(BaseModel):
    justification: str = Field(min_length=8, max_length=2000)


class EstateValidationRequest(BaseModel):
    estate_id: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    source: EstateSourceModel


class WaveOverrideRequest(BaseModel):
    state: Literal["HOLD", "OPEN"]
    justification: str = Field(min_length=8, max_length=2000)
    expires_at: str | None = None
    estate_id: EstateIdField


class ApproveV1Request(BaseModel):
    justification: str = Field(min_length=5, max_length=2000)


class CreateReportRequest(BaseModel):
    report_type: Literal["assessment", "run_evidence", "reconciliation", "approval_audit"]
    run_id: str = Field(min_length=2, max_length=160)
    justification: str = Field(min_length=8, max_length=2000)


class AssistantSessionRequest(BaseModel):
    estate_id: EstateIdField
    route: str = Field(default="/overview", min_length=1, max_length=300)
    run_id: str | None = Field(default=None, max_length=160)


class AssistantMessageRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _feature_enabled(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _envelope(data: Any, *, total: int | None = None, next_cursor: str | None = None) -> dict:
    return {
        "data": data,
        "meta": {"generated_at": _now(), "freshness": "live", "total": total, "next_cursor": next_cursor},
    }


# ---------------------------------------------------------------------------
# Read performance (Day 11 Phase 9)
# ---------------------------------------------------------------------------
#
# Measured before this change, against 39 runs and 312 policy decisions:
# /overview 54s, /policies 22s, /estates 8.4s. That is not data volume —
# it is round trips. Both endpoints looped every run issuing one Firestore
# query per subcollection per run, so ~78 sequential calls at roughly a
# quarter-second each. The console polls on an interval, so every operator
# sat behind that on every navigation.
#
# Two fixes, in order of effect:
#   1. One collection_group() query instead of one query per run.
#   2. A short TTL cache, surfaced honestly as meta.freshness == "cached"
#      rather than pretending the number is live.

# Sized against measured reality, not a guess. A single Firestore query
# from a developer machine costs 0.5-1.2s of pure round trip, and a page
# needs several, so a full pass over the console costs tens of seconds.
# With a 5s TTL every poll and every navigation missed — the cache never
# helped the case it exists for.
#
# 60s is chosen because migration-run state changes on the order of
# minutes, the response is explicitly labelled `freshness: "cached"`, and
# any write clears the cache so an operator always sees their own action
# immediately. Set UI_CACHE_TTL_SECONDS=0 to disable entirely.
_CACHE_TTL_SECONDS = float(os.environ.get("UI_CACHE_TTL_SECONDS", "60"))
_response_cache: dict[str, tuple[float, dict]] = {}
_cache_generation = 0


#: Estates the CURRENT request may read; `None` means unrestricted, which
#: covers both a wildcard grant and internal calls with no request behind
#: them (scripts, workers, tests). Set once per request by
#: `_authorize_read`. A context variable, not a parameter, so that a read
#: helper cannot be called from a request path without it.
_READ_SCOPE: ContextVar[frozenset[str] | None] = ContextVar("read_scope", default=None)


def _scope_key() -> str:
    """A stable cache-key fragment for the current scope.

    Without this the response cache is a cross-tenant leak: two users with
    different grants hit the same key and the second is served the first
    one's rows.
    """
    scope = _READ_SCOPE.get()
    if scope is None:
        return "*"
    return hashlib.sha256("".join(sorted(scope)).encode()).hexdigest()[:12]


def _cached(key: str, build) -> dict:
    """Serves a recent response and SAYS so.

    The envelope has always carried a `freshness` field with a "cached"
    value; until now it was hardcoded to "live". A cached read is marked,
    so the console can show it and nobody mistakes a five-second-old
    number for a live one. Set UI_CACHE_TTL_SECONDS=0 to disable.
    """
    if _CACHE_TTL_SECONDS <= 0:
        return build()
    key = f"{_scope_key()}|{key}"
    hit = _response_cache.get(key)
    if hit and time.monotonic() < hit[0]:
        payload = hit[1]
        return {**payload, "meta": {**payload["meta"], "freshness": "cached"}}
    generation = _cache_generation
    value = build()
    # Expiry is measured from when the build FINISHED, not when it started.
    # Measuring from the start meant a build slower than the TTL stored an
    # already-expired entry, so the most expensive endpoints — the only ones
    # that needed caching — never got a hit.
    # A write may clear the cache while this slow Firestore read is still
    # running. Never let that older read repopulate the cache after the
    # write; the next request must rebuild from post-write state.
    if generation == _cache_generation:
        _response_cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)
    return value


def clear_response_cache() -> None:
    """Called after a write so an operator sees their own action immediately."""
    global _cache_generation

    _cache_generation += 1
    _response_cache.clear()


def _subcollection_group(name: str, run_ids: set[str] | None = None) -> dict[str, list[dict]]:
    """Every run's `name` subcollection, in ONE round trip.

    Replaces `for run in runs: _collection_docs(run, name)` — the N+1 that
    made these endpoints take tens of seconds. Filtered in Python, never
    with .where(): a collection_group plus an equality filter needs a
    composite index this project does not create (CLAUDE.md).
    """
    grouped: dict[str, list[dict]] = {}
    for doc in get_client().collection_group(name).stream():
        parent = doc.reference.parent.parent
        if parent is None:
            continue
        run_id = parent.id
        if run_ids is not None and run_id not in run_ids:
            continue
        grouped.setdefault(run_id, []).append({"_id": doc.id, **(doc.to_dict() or {})})
    return grouped


def _collection_docs(run_id: str, name: str, limit: int | None = None) -> list[dict]:
    query = get_client().collection(RUN_COLLECTION).document(run_id).collection(name)
    if limit:
        query = query.limit(limit)
    return [{"_id": doc.id, **(doc.to_dict() or {})} for doc in query.stream()]


def _all_runs(limit: int = 500, *, estate_id: str | None = None) -> list[dict]:
    from google.cloud.firestore_v1 import Query

    docs = (
        get_client()
        .collection(RUN_COLLECTION)
        .order_by("created_at", direction=Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    runs = [{"_id": doc.id, **(doc.to_dict() or {})} for doc in docs]
    return _for_estate(runs, estate_id)


def _for_estate(records: list[dict], estate_id: str | None) -> list[dict]:
    """Filters records to one estate, in Python.

    Two decisions worth stating:

    Filtered in Python, not with `.where()` — combining an equality filter
    with the `order_by("created_at")` above needs a composite index this
    project does not create, which is the documented Firestore gotcha in
    CLAUDE.md.

    A record with **no** estate_id belongs to the default estate, not to
    nothing. Runs created before Day 11 Phase 2 carry no estate_id, and
    treating them as unmatched would empty the Control Tower dashboard
    between deploying this filter and running scripts/backfill_estate_id.py
    — a silent, confusing regression for a purely cosmetic filter.

    The estate filter is cosmetic; the scope filter applied first is not.
    `estate_id` is what the caller ASKED for, `_READ_SCOPE` is what the
    caller is ALLOWED, and the two are enforced in that order so that
    omitting the request filter can never widen the result.
    """
    scope = _READ_SCOPE.get()
    if scope is not None:
        records = [r for r in records if (r.get("estate_id") or DEFAULT_ESTATE_ID) in scope]
    if estate_id is None:
        return records
    return [r for r in records if (r.get("estate_id") or DEFAULT_ESTATE_ID) == estate_id]


def _run_subcollection(run_id: str, name: str) -> list[dict]:
    """One run's subcollection, without touching anybody else's."""
    return [
        {"_id": doc.id, **(doc.to_dict() or {})}
        for doc in get_client()
        .collection(RUN_COLLECTION)
        .document(run_id)
        .collection(name)
        .stream()
    ]


def _count_subcollection(run_id: str, name: str, *, where: tuple[str, str, Any] | None = None) -> int:
    """How many documents, WITHOUT reading them.

    Firestore's count() aggregation is answered server-side. That matters
    out of proportion to its size here: /approvals needs the NUMBER of
    risk findings per run, and reading them to find out meant streaming
    7,171 documents to produce a handful of integers.

    `where` filters on a single field, which needs no composite index —
    the automatic single-field ones cover it. Both the total and the
    critical count come back without a document leaving the server; the
    first version of this counted the total server-side and then read
    every document anyway to filter for CRITICAL, which was slower than
    the scan it replaced.
    """
    try:
        query = get_client().collection(RUN_COLLECTION).document(run_id).collection(name)
        if where:
            query = query.where(where[0], where[1], where[2])
        return int(query.count().get()[0][0].value)
    except Exception:  # noqa: BLE001 — a count is never worth failing a page for
        return 0


def _gather(work: dict[str, Any], max_workers: int = 16) -> dict[str, Any]:
    """Run independent Firestore reads concurrently.

    These are latency-bound, not CPU-bound: the win is overlapping round
    trips. Measured — sequential per-run reads for two dozen runs cost
    seconds; overlapped they cost one round trip plus change.
    """
    # Each read runs under a COPY of the calling context. A pool thread
    # starts with an empty context, so without this the reads overlapped
    # here would see `_READ_SCOPE` unset and return every estate's data —
    # the pool would quietly undo the authorization the caller just did.
    # A fresh copy per submission because one Context cannot be entered
    # twice concurrently.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {key: pool.submit(copy_context().run, fn) for key, fn in work.items()}
        return {key: future.result() for key, future in futures.items()}


def _latest_run(runs: list[dict], *, mode: str | None = None) -> dict | None:
    return next((run for run in runs if mode is None or run.get("mode") == mode), None)


EXECUTION_STAGES = [
    "REQUESTED", "DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED",
    "MIGRATING", "VALIDATING", "PASSED", "READY_FOR_APPROVAL", "APPROVED",
    "CUTOVER", "MONITORING", "COMPLETE",
]
ASSESSMENT_STAGES = EXECUTION_STAGES[:5]
VALIDATION_FAILURE_STAGES = {"FAILED", "INVESTIGATING", "REMEDIATING"}

#: A run writes its catalog when it reaches DISCOVERED. Anything earlier
#: has no tables, no pipelines and no dependencies to draw.
CATALOGUED_STATES = set(EXECUTION_STAGES[EXECUTION_STAGES.index("DISCOVERED") :]) | VALIDATION_FAILURE_STAGES


def _latest_run_with_catalog(runs: list[dict]) -> dict | None:
    """The newest run that actually got far enough to have lineage.

    `_latest_run` returns the newest run FULL STOP, which is the wrong
    thing to draw a graph from: a queued run has no catalog, so the page
    rendered an empty graph and looked broken. Observed live — the newest
    run for every estate was REQUESTED, so Lineage was blank for all of
    them unless a run_id was passed by hand, which the UI never did.

    Decided from the run document alone. `state_history` is consulted as
    well as `state` because a run that reached DISCOVERED and later failed
    still has a catalog worth showing.
    """
    for run in runs:
        if run.get("state") in CATALOGUED_STATES:
            return run
        history = {entry.get("state") for entry in (run.get("state_history") or [])}
        if "DISCOVERED" in history:
            return run
    return None
STAGE_LABELS = {
    "REQUESTED": "Request queued",
    "DISCOVERED": "Source metadata discovered",
    "ANALYZED": "Dependencies analyzed",
    "RISK_ASSESSED": "Risk assessment complete",
    "PLANNED": "Migration plan ready",
    "MIGRATING": "Migrating scheduled tables",
    "VALIDATING": "Validating migrated data",
    "FAILED": "Validation failed",
    "INVESTIGATING": "Investigating validation failure",
    "REMEDIATING": "Applying remediation",
    "PASSED": "Validation passed",
    "READY_FOR_APPROVAL": "Waiting for cutover approval",
    "APPROVED": "Cutover approved",
    "CUTOVER": "Cutover in progress",
    "MONITORING": "Post-cutover monitoring",
    "COMPLETE": "Migration complete",
}


def _run_progress(run: dict) -> dict:
    """Measured lifecycle progress derived only from durable milestones."""
    run_id = run.get("run_id") or run.get("_id")
    stage = str(run.get("state") or "REQUESTED")
    stages = ASSESSMENT_STAGES if run.get("mode") == "assessment" else EXECUTION_STAGES
    measured_stage = "VALIDATING" if stage in VALIDATION_FAILURE_STAGES else stage
    try:
        completed = stages.index(measured_stage)
    except ValueError:
        completed = 0
    percent = round(100 * completed / max(1, len(stages) - 1))

    # A table-job ratio refines only the MIGRATING milestone. It never
    # claims governance/cutover completion simply because rows were copied.
    if stage == "MIGRATING" and run_id and len(stages) > completed + 1:
        plans = _collection_docs(run_id, "migration_plan")
        jobs = _collection_docs(run_id, "migration_executions")
        targets = [
            target
            for plan in plans
            for target in (plan.get("targets") or [])
            if target.get("scheduled") and not target.get("blocked")
        ]
        target_ids = {target.get("target_id") for target in targets if target.get("target_id")}
        total_jobs = len(target_ids) or len(targets)
        completed_target_ids = {
            item.get("target_id")
            for item in jobs
            if item.get("status") in {"COMPLETED", "COMPLETE", "SUCCEEDED", "DONE"}
            and item.get("target_id")
        }
        done_jobs = len(completed_target_ids) if target_ids else sum(
            1 for item in jobs if item.get("status") in {"COMPLETED", "COMPLETE", "SUCCEEDED", "DONE"}
        )
        if total_jobs:
            segment = 100 / (len(stages) - 1)
            percent = round(min(100, percent + segment * min(done_jobs, total_jobs) / total_jobs))

    if stage == stages[-1]:
        progress_status = "complete"
    elif stage in VALIDATION_FAILURE_STAGES:
        progress_status = "failed" if stage == "FAILED" else "active"
    elif stage == "READY_FOR_APPROVAL":
        progress_status = "waiting"
    elif run.get("wave_status") == "HOLD" or run.get("status") == "held":
        progress_status = "held"
    else:
        progress_status = "active"
    observed = (
        run.get("last_transition_at")
        or ((run.get("state_history") or [{}])[-1].get("at"))
        or run.get("updated_at")
        or run.get("created_at")
        or _now()
    )
    return ProgressSnapshot(
        percent=percent,
        status=progress_status,
        label=STAGE_LABELS.get(stage, stage.replace("_", " ").title()),
        current_stage=stage,
        completed_units=completed,
        total_units=len(stages) - 1,
        run_id=run_id,
        last_observed_at=str(observed),
    ).model_dump()


def _attach_progress(run: dict) -> dict:
    return {**run, "progress": _run_progress(run)}


def _operation_progress(operation: dict) -> dict:
    run_id = operation.get("run_id") or (operation.get("result") or {}).get("run_id")
    if run_id:
        try:
            return _run_progress(get_run(run_id))
        except KeyError:
            pass
    status_value = str(operation.get("status") or "queued")
    terminal_failed = status_value in {"failed", "publish_failed"}
    return ProgressSnapshot(
        percent=0,
        status="failed" if terminal_failed else "queued",
        label=("Operation failed" if terminal_failed else "Waiting for a worker"),
        current_stage="REQUESTED",
        completed_units=0,
        total_units=4 if operation.get("kind") == "assessment.start" else 12,
        run_id=run_id,
        last_observed_at=str(operation.get("updated_at") or operation.get("created_at") or _now()),
    ).model_dump()


def _authorize_read(user: UserContext, estate_id: str | None) -> None:
    """Authorize a read AND pin the estates it is allowed to touch.

    Authorizing `estate_id` used to be all this did, which meant an
    UNSCOPED read authorized nothing: `if estate_id:` is false when the
    caller passes no filter, so every aggregate returned data from every
    estate in the project. That was not an edge case — `estatePath()` in
    the console omits the scope whenever no estate is active, which it is
    on first load, so unscoped was the default path.

    The permitted set is published on a context variable rather than
    threaded through thirteen endpoint signatures, because the point is to
    close the hole for endpoints that do NOT exist yet. Every runs-based
    read already funnels through `_for_estate`, and every estate list
    through `_all_estates`; filtering there means a new aggregate is
    scoped whether or not its author remembered to be.
    """
    if estate_id:
        authorize_estate(user, estate_id, "viewer")
    # Redundant with the `_scope_reads` router dependency for HTTP calls,
    # and deliberately so: it also covers an endpoint invoked as a plain
    # function, where no dependency has run.
    _READ_SCOPE.set(_visible_estate_ids(user))


def _visible_estate_ids(user: UserContext) -> frozenset[str] | None:
    """Every estate this user may read, or `None` for an unrestricted grant.

    `None` rather than "the set of every estate that exists right now": a
    wildcard holder must still see a run whose estate document has since
    been deleted, and enumerating estates would silently drop it. It also
    keeps the common case free of a per-request estate listing.

    Reads the registry directly rather than through `_all_estates`, which
    is itself scoped — computing the scope from a scoped read would be
    circular.
    """
    if "viewer" in user.roles_for(None):
        return None
    from tools.connection_context import list_estate_documents

    return frozenset(
        str(estate.get("estate_id"))
        for estate in list_estate_documents()
        if estate.get("estate_id") and user.has_role("viewer", str(estate.get("estate_id")))
    )


def _wave_state(estate_id: str | None = None) -> dict:
    """Concurrency state for one estate, or merged across all of them.

    Day 11 Phase 4 split `wave_state/slots` into one document per estate.
    With no estate filter the console is showing the whole fleet, so the
    per-estate documents are merged rather than one being picked
    arbitrarily — otherwise the Overview page would under-report running
    work as soon as a second estate existed.
    """
    collection = get_client().collection("wave_state")
    if estate_id is not None:
        snapshot = collection.document(estate_id).get()
        state = snapshot.to_dict() if snapshot.exists else {}
        return {
            "running_by_source": state.get("running_by_source", {}),
            "running_critical": state.get("running_critical", []),
        }

    running_by_source: dict[str, list] = {}
    running_critical: list = []
    for snapshot in collection.stream():
        state = snapshot.to_dict() or {}
        for source_id, items in (state.get("running_by_source") or {}).items():
            # Qualify with the estate so two estates using the same
            # source_id stay distinguishable in the merged view.
            running_by_source[f"{snapshot.id}:{source_id}"] = items
        running_critical.extend(state.get("running_critical") or [])
    return {"running_by_source": running_by_source, "running_critical": running_critical}


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return max(0, int(base64.urlsafe_b64decode(padded).decode()))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor.") from exc


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 2)


def _stage_metrics(runs: list[dict]) -> dict:
    by_stage: dict[str, list[float]] = {}
    for run in runs:
        history = run.get("state_history") or []
        for current, following in zip(history, history[1:]):
            try:
                start = dt.datetime.fromisoformat(current["at"])
                end = dt.datetime.fromisoformat(following["at"])
            except (KeyError, TypeError, ValueError):
                continue
            by_stage.setdefault(str(current.get("state", "UNKNOWN")), []).append(
                max(0.0, (end - start).total_seconds() * 1000)
            )
    return {
        stage: {"samples": len(values), "p50_ms": _percentile(values, 0.50), "p95_ms": _percentile(values, 0.95)}
        for stage, values in sorted(by_stage.items())
    }


def _parsed_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _fleet_health(
    runs: list[dict],
    queued_operations: list[dict],
    connection_snapshots: list[dict] | None = None,
    execution_jobs: list[dict] | None = None,
) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    for run in runs[:20]:
        if run.get("state") not in {"FAILED", "INVESTIGATING", "REMEDIATING"}:
            continue
        try:
            last = dt.datetime.fromisoformat(run.get("last_transition_at") or run["state_history"][-1]["at"])
        except (KeyError, IndexError, TypeError, ValueError):
            return "DEGRADED"
        if now - last > dt.timedelta(minutes=30):
            return "DEGRADED"
    if any(op.get("status") == "publish_failed" for op in queued_operations):
        return "DEGRADED"
    for operation in queued_operations:
        observed = _parsed_time(operation.get("updated_at") or operation.get("created_at"))
        if operation.get("status") in {"queued", "published"} and observed and now - observed > dt.timedelta(minutes=30):
            return "DEGRADED"
    for snapshot in connection_snapshots or []:
        observed = _parsed_time(snapshot.get("last_observed_at"))
        if snapshot.get("status") == "FAILED" or (observed and now - observed > dt.timedelta(hours=1)):
            return "DEGRADED"
    if any(job.get("status") == "FAILED" for job in execution_jobs or []):
        return "DEGRADED"
    return "HEALTHY" if runs else "UNKNOWN"


def _firebase_web_config() -> dict:
    env_map = {
        "apiKey": "FIREBASE_API_KEY",
        "authDomain": "FIREBASE_AUTH_DOMAIN",
        "projectId": "FIREBASE_PROJECT_ID",
        "storageBucket": "FIREBASE_STORAGE_BUCKET",
        "messagingSenderId": "FIREBASE_MESSAGING_SENDER_ID",
        "appId": "FIREBASE_APP_ID",
    }
    config = {key: os.environ.get(env_name) for key, env_name in env_map.items()}
    if all(config.get(key) for key in ("apiKey", "authDomain", "projectId", "appId")):
        return {key: value for key, value in config.items() if value}
    if FIREBASE_CONFIG_PATH.exists():
        text = FIREBASE_CONFIG_PATH.read_text(encoding="utf-8")
        for key in env_map:
            match = re.search(rf"\b{re.escape(key)}\s*:\s*[\"']([^\"']+)[\"']", text)
            if match:
                config[key] = match.group(1)
    return {key: value for key, value in config.items() if value}


def _estate(estate_id: str | None = None) -> dict:
    """One estate, from the registry, falling back to committed YAML."""
    from tools.connection_context import load_estate_document

    return load_estate_document(estate_id or DEFAULT_ESTATE_ID)


def _all_estates() -> list[dict]:
    from tools.connection_context import list_estate_documents

    estates = sorted(list_estate_documents(), key=lambda e: e.get("estate_id", ""))
    scope = _READ_SCOPE.get()
    if scope is not None:
        estates = [e for e in estates if str(e.get("estate_id")) in scope]
    return estates


def _packs() -> list[dict]:
    """Every committed Migration Pack, validated, with execution support
    derived rather than hardcoded.

    This used to glob packs/ itself and decide executability with
    `pack.get("source_id") == "wwi-sqlserver"` — a string comparison that
    would have disabled execution for every future estate. It now goes
    through the pack registry, so a malformed pack surfaces as a clear
    validation error here rather than a KeyError inside an agent later, and
    `execution_supported` follows the pack's declared mode and its
    adapter's declared capabilities.
    """
    from tools.pack_loader import PackValidationError, list_packs, supports_execution

    result = []
    try:
        packs = list_packs()
    except PackValidationError as exc:
        raise HTTPException(status_code=500, detail=f"A committed Migration Pack is invalid: {exc}") from exc

    for pack in packs:
        path = Path(pack["_path"])
        result.append(
            {
                **{k: v for k, v in pack.items() if not k.startswith("_")},
                "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
                "execution_supported": supports_execution(pack),
            }
        )
    return result


def _catalog_system_for(source: dict) -> str:
    """The `system` tag this source's catalog records carry.

    Not the same as source_id, and deliberately so: an adapter's system tag
    ("sqlserver-wwi") is the prefix of every table_id it has ever emitted,
    while the estate calls that same source "wwi-sqlserver". Rewriting one
    to match the other would change the identity of every already-
    catalogued table, so the adapter declares the mapping instead.
    """
    from tools.adapters import ADAPTER_TYPES

    adapter_cls = ADAPTER_TYPES.get(source.get("adapter"))
    return getattr(adapter_cls, "system", None) or source["source_id"]


def _sanitized_estate(estate: dict, latest: dict | None = None) -> dict:
    """One estate's operator-facing summary. Never includes a credential.

    `latest` is that estate's own most recent run — not the globally
    latest. With more than one estate the global latest is meaningless:
    an estate with no recent activity would report zero objects and look
    broken rather than idle.
    """
    catalog = _collection_docs(latest["run_id"], "catalog") if latest else []
    pipelines = _collection_docs(latest["run_id"], "pipelines") if latest else []
    sources = []
    for source in estate.get("sources", []):
        profile = source.get("connection_profile")
        catalog_system = _catalog_system_for(source)
        matching = [item for item in catalog if item.get("system") == catalog_system]
        system_count = len(matching)
        last_seen = max((item.get("discovered_at", "") for item in matching), default=None)
        # Connection health is estate-scoped since Phase 2; fall back to the
        # bare system tag for snapshots written before that.
        health_col = get_client().collection("connection_health")
        health_snapshot = health_col.document(
            f"{estate['estate_id']}__{source['source_id']}"
        ).get()
        if not health_snapshot.exists:
            health_snapshot = health_col.document(catalog_system).get()
        health = health_snapshot.to_dict() if health_snapshot.exists else None
        sources.append(
            {
                "source_id": source["source_id"],
                "adapter": source["adapter"],
                "pack_id": source.get("pack_id"),
                "execution_profiles": source.get("execution_profiles") or ([source.get("pack_id")] if source.get("pack_id") else []),
                "connection": None
                if profile is None
                else {
                    "host_from": profile.get("host_env"),
                    "port_from": profile.get("port_env"),
                    "user_from": profile.get("user_env"),
                    "credential_source": "Secret Manager reference" if profile.get("password_secret_ref") else "not configured",
                },
                "objects": system_count,
                "last_observed_at": (health or {}).get("last_observed_at") or last_seen,
                "health": (health or {}).get("status") or ("OBSERVED" if last_seen else "NOT_OBSERVED"),
                "health_detail": (health or {}).get("detail"),
            }
        )
    return {
        "estate_id": estate["estate_id"],
        "display_name": estate.get("display_name", estate["estate_id"]),
        "status": estate.get("status", "ACTIVE"),
        "origin": estate.get("origin"),
        "sources": sources,
        "target": estate.get("target"),
        "objects": len(catalog),
        "pipelines": len(pipelines),
        "pipeline_options": [
            {"pipeline_id": item.get("pipeline_id"), "name": item.get("name") or item.get("pipeline_id")}
            for item in pipelines if item.get("pipeline_id")
        ],
        "execution_readiness": _execution_readiness(estate),
        "latest_run_id": (latest or {}).get("run_id"),
        "last_run_at": (latest or {}).get("created_at"),
    }


def _source_declares_pack(source: dict, pack_id: str) -> bool:
    return source.get("pack_id") == pack_id or pack_id in (source.get("execution_profiles") or [])


def _execution_readiness(estate: dict) -> dict:
    """Authoritative, stable reasons why an estate can or cannot execute."""
    from tools.pack_loader import adapter_type_for

    packs = {pack["pack_id"]: pack for pack in _packs()}
    options: list[dict] = []
    blockers: list[dict] = []
    assigned = False
    for source in estate.get("sources") or []:
        declared = [source.get("pack_id"), *(source.get("execution_profiles") or [])]
        for pack_id in dict.fromkeys(item for item in declared if item):
            assigned = True
            pack = packs.get(pack_id)
            if pack is None:
                blockers.append({
                    "code": "UNKNOWN_PACK",
                    "message": f"Migration Pack {pack_id!r} is not registered.",
                })
                continue
            if not pack.get("execution_supported"):
                blockers.append({
                    "code": "ASSESSMENT_ONLY_PACK",
                    "message": "The selected pack supports assessment only.",
                })
                continue
            try:
                wanted_adapter = adapter_type_for(pack)
            except Exception:  # noqa: BLE001 - readiness is an operator status, not a crash
                wanted_adapter = None
            if wanted_adapter != source.get("adapter"):
                blockers.append({
                    "code": "ADAPTER_INCOMPATIBLE",
                    "message": (
                        f"Migration Pack {pack_id!r} does not support source "
                        f"{source.get('source_id')!r}."
                    ),
                })
                continue
            option = {
                "source_id": source["source_id"],
                "pack_id": pack_id,
                "label": f"{source['source_id']} · {pack.get('name') or pack_id}",
            }
            if option not in options:
                options.append(option)

    if options:
        return {
            "status": "ready" if len(options) == 1 else "selection_required",
            "options": options,
            "blockers": [],
        }
    if not assigned:
        blockers = [{
            "code": "NO_EXECUTABLE_PACK",
            "message": "No executable Migration Pack is assigned.",
        }]
    return {"status": "blocked", "options": [], "blockers": blockers}


@public_router.get("/config", response_model=Envelope)
def runtime_config() -> dict:
    firebase_config = _firebase_web_config()
    return _envelope(
        {
            "product_name": "Migration Control Tower",
            "build_version": os.environ.get("BUILD_VERSION", "development"),
            "poll_interval_ms": 10_000,
            "progress_poll_interval_ms": 2_000,
            "environment": os.environ.get("APP_ENVIRONMENT") or os.environ.get("ENVIRONMENT") or "Local",
            "firebase": firebase_config,
            "authentication_configured": bool(firebase_config),
            "features": {
                "agent_reasoning": os.environ.get("ENABLE_AGENT_REASONING_V2", "0").lower() in {"1", "true", "yes", "on"},
                "reports": os.environ.get("ENABLE_REPORTS", "0").lower() in {"1", "true", "yes", "on"},
                "assistant": os.environ.get("ENABLE_AI_ASSISTANT", "0").lower() in {"1", "true", "yes", "on"},
            },
        }
    )


@public_router.get("/session", response_model=Envelope)
def session(user: UserContext = Depends(get_user_context)) -> dict:
    grants = {estate: sorted(roles) for estate, roles in (user.estate_roles or {}).items()}
    return _envelope({
        "uid": user.uid,
        "email": user.email,
        "roles": sorted(user.roles),
        "estate_roles": grants,
        "wildcard_roles": grants.get("*", []),
        "scoped_estates": user.scoped_estates,
    })


@router.get("/overview", response_model=Envelope)
def overview(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)
    return _cached(f"overview:{estate_id}", lambda: _build_overview(estate_id))


def _estimated_cost(usage_events: list[dict]) -> dict:
    """Measured usage, priced from the committed rate card.

    Two halves that must not be confused. Usage is measured — token
    counts as the model reported them, bytes as BigQuery billed them.
    Price is declared, in contracts/price_book.json, with an effective
    date and a source. The response carries both, so the figure can be
    recomputed or the rates rejected.

    This is an estimate against published list prices. It ignores
    committed-use discounts, free tiers and anything negotiated on the
    billing account, which is precisely why Actual cost is a separate
    measurement from the billing export rather than a refinement of this
    one.
    """
    from tools.usage_meter import price_usage

    if not usage_events:
        return Availability(
            status="not_configured",
            reason=(
                "No model or BigQuery usage has been recorded for these runs. "
                "Usage is recorded from the run that causes it, so a fleet with "
                "no completed work has none."
            ),
        ).model_dump()

    priced = price_usage(usage_events)
    caveat = (
        f" {len(priced['unpriced'])} item(s) could not be priced from this card."
        if priced["unpriced"]
        else ""
    )
    return Availability(
        status="available",
        reason=(
            f"{priced['usage']['model_calls']} model call(s) and "
            f"{priced['usage']['bigquery_jobs']} BigQuery job(s), priced at "
            f"{priced['basis']} ({priced['region']}) from price book "
            f"{priced['price_book_effective_date']}.{caveat}"
        ),
        last_observed_at=max(
            (str(event.get("at")) for event in usage_events if event.get("at")), default=None
        ),
        value=priced,
    ).model_dump()


def _actual_cost() -> dict:
    """The billing export's own figure, as last snapshotted.

    Read from a durable snapshot rather than queried here. The billing
    export is a BigQuery table, and querying it on every dashboard load
    would make the cost panel a recurring cost of its own — which is a
    silly way for a cost dashboard to behave. tools/billing_export.py
    writes the snapshot; this reads it.
    """
    table = os.environ.get("CLOUD_BILLING_EXPORT_TABLE")
    if not table:
        return Availability(
            status="not_configured",
            reason=(
                "CLOUD_BILLING_EXPORT_TABLE is not configured. Enable Cloud "
                "Billing export to BigQuery, then set it to the "
                "project.dataset.table it writes to."
            ),
        ).model_dump()

    snapshot = get_client().collection("cost_snapshots").document("current").get()
    record = snapshot.to_dict() if snapshot.exists else None
    if not record:
        return Availability(
            status="not_configured",
            reason=(
                f"Billing export is configured ({table}) but no snapshot has been "
                f"taken. Run `python -m tools.billing_export`."
            ),
        ).model_dump()

    return Availability(
        status="stale" if _is_stale(record.get("observed_at")) else "available",
        reason=(
            f"{record.get('days')} day(s) of billed usage to "
            f"{record.get('period_end')}, from {record.get('source_table')}."
        ),
        last_observed_at=record.get("observed_at"),
        value=record,
    ).model_dump()


def _is_stale(observed_at: str | None, max_age_hours: int = 36) -> bool:
    """A cost snapshot older than this is reported as stale, not as current.

    The billing export itself lags by hours, so a snapshot is never
    live — but one taken last week presented as today's spend is a
    different kind of wrong.
    """
    if not observed_at:
        return True
    try:
        taken = dt.datetime.fromisoformat(str(observed_at))
    except ValueError:
        return True
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - taken).total_seconds() > max_age_hours * 3600


def _estimated_bytes(catalog: list[dict]) -> dict:
    """How many bytes the discovered estate occupies at the source.

    Summed from what each adapter read out of the source's own catalog
    during discovery — sys.allocation_units on SQL Server,
    pg_total_relation_size on Postgres — not sampled, and not inferred
    from row counts.

    Coverage is reported alongside the total, because a partial answer
    presented as a whole one is the failure this panel exists to prevent.
    An adapter with no live database to ask (a .sql corpus) records null
    rather than 0, so a total drawn from a mixed estate is a floor, and
    says so.
    """
    if not catalog:
        return Availability(
            status="not_configured",
            reason="No estate has been discovered yet, so there is nothing to measure.",
        ).model_dump()

    measured = [
        int(table["size_bytes"])
        for table in catalog
        if isinstance(table.get("size_bytes"), (int, float))
    ]
    if not measured:
        return Availability(
            status="not_configured",
            reason=(
                f"None of the {len(catalog)} discovered tables reported a size. "
                f"Re-run discovery: sources catalogued before byte measurement "
                f"existed carry no size_bytes."
            ),
        ).model_dump()

    total = sum(measured)
    complete = len(measured) == len(catalog)
    return Availability(
        status="available",
        reason=(
            f"Measured from the source catalog for all {len(catalog)} discovered tables."
            if complete
            else (
                f"Measured for {len(measured)} of {len(catalog)} discovered tables. "
                f"The rest have no live source to report a size, so this is a floor."
            )
        ),
        last_observed_at=max(
            (str(table.get("discovered_at")) for table in catalog if table.get("discovered_at")),
            default=None,
        ),
        value={
            "bytes": total,
            "tables_measured": len(measured),
            "tables_total": len(catalog),
            "largest_table": max(
                (t for t in catalog if isinstance(t.get("size_bytes"), (int, float))),
                key=lambda t: t["size_bytes"],
            ).get("table_id"),
            "complete": complete,
        },
    ).model_dump()


def _build_overview(estate_id: str | None) -> dict:
    """Overview reads ten independent things from Firestore.

    Issued sequentially this took ~21s: the data is tiny (tens of
    documents) but each round trip costs a second or more, and they were
    serialised for no reason — none of them depends on another's result
    except the four keyed on the latest run.

    Run concurrently the endpoint costs roughly its slowest single query.
    The Firestore client is safe to share across threads, and every task
    here is a read.
    """
    runs = _all_runs(200, estate_id=estate_id)
    latest = _latest_run(runs)
    latest_run_id = latest["run_id"] if latest else None
    client = get_client()

    def _operations():
        return _for_estate(
            [d.to_dict() or {} for d in client.collection("operation_requests").limit(100).stream()],
            estate_id,
        )

    def _connection_snapshots():
        snapshots = []
        for snapshot in client.collection("connection_health").stream():
            value = snapshot.to_dict() or {}
            belongs = value.get("estate_id") or (
                snapshot.id.split("__", 1)[0] if "__" in snapshot.id else DEFAULT_ESTATE_ID
            )
            if not estate_id or belongs == estate_id:
                snapshots.append(value)
        return snapshots

    def _decisions():
        return [
            item
            for items in _subcollection_group(
                "policy_decisions", {r["run_id"] for r in runs}
            ).values()
            for item in items
        ]

    def _latest_sub(name):
        return lambda: _collection_docs(latest_run_id, name) if latest_run_id else []

    # Same context-copy rule as `_gather`: a pool thread starts with an
    # empty context, so a read that consults `_READ_SCOPE` inside one of
    # these lambdas would see it unset. The run set here is already
    # scoped before the pool, so nothing is leaking today — this keeps it
    # that way for whatever gets added to the dict next.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            "operations": pool.submit(copy_context().run, _operations),
            "connections": pool.submit(copy_context().run, _connection_snapshots),
            "decisions": pool.submit(copy_context().run, _decisions),
            "findings": pool.submit(copy_context().run, _latest_sub("risk_findings")),
            "executions": pool.submit(copy_context().run, _latest_sub("migration_executions")),
            "incidents": pool.submit(copy_context().run, _latest_sub("incidents")),
            "approvals": pool.submit(copy_context().run, _latest_sub("approval_history")),
            "catalog": pool.submit(copy_context().run, _latest_sub("catalog")),
            "usage": pool.submit(copy_context().run, 
                lambda: [
                    item
                    for items in _subcollection_group(
                        "usage_events", {r["run_id"] for r in runs}
                    ).values()
                    for item in items
                ]
            ),
            "wave_state": pool.submit(copy_context().run, lambda: _wave_state(estate_id)),
        }
        resolved = {name: future.result() for name, future in futures.items()}

    operations = resolved["operations"]
    connection_snapshots = resolved["connections"]
    decisions = resolved["decisions"]
    findings = resolved["findings"]
    executions = resolved["executions"]
    incidents = resolved["incidents"]
    approvals = resolved["approvals"]
    wave_state = resolved["wave_state"]
    estimated_bytes = _estimated_bytes(resolved["catalog"])
    estimated_cost = _estimated_cost(resolved["usage"])
    latest_execution = executions[-1] if executions else None
    progress = None
    if latest_execution and latest_execution.get("source_count"):
        progress = round(100 * latest_execution.get("target_count", 0) / latest_execution["source_count"], 1)
    risk_distribution: dict[str, int] = {}
    for finding in findings:
        severity = finding.get("severity", "UNKNOWN")
        risk_distribution[severity] = risk_distribution.get(severity, 0) + 1
    failed_runs = [run for run in runs if any(item.get("state") == "FAILED" for item in run.get("state_history", []))]
    recovered = [run for run in failed_runs if run.get("state") in {"PASSED", "COMPLETE"}]
    return _envelope(
        {
            "fleet_health": _fleet_health(runs, operations, connection_snapshots, executions),
            "estate": _sanitized_estate(_estate(estate_id), latest),
            "runs": {
                "total": len(runs),
                "active": sum(1 for run in runs if run.get("state") not in {"COMPLETE", "PASSED"}),
                "complete": sum(1 for run in runs if run.get("state") == "COMPLETE"),
                "latest": _attach_progress(latest) if latest else None,
                "migrated_percent": progress,
                "row_transfer_percent": progress,
            },
            "waves": {
                "running_by_source": wave_state.get("running_by_source", {}),
                "running_critical": len(wave_state.get("running_critical", [])),
                "queued_operations": sum(1 for op in operations if op.get("status") == "queued"),
                "blocked_operations": sum(1 for op in operations if op.get("status") == "publish_failed"),
            },
            "risk_distribution": risk_distribution,
            "policy_denials": sum(1 for decision in decisions if decision.get("decision") == "DENY"),
            "latency": _stage_metrics(runs[:100]),
            "recovery_rate": round(len(recovered) / len(failed_runs), 3) if failed_runs else None,
            "human_interventions": sum(1 for item in approvals if item.get("event") == "APPROVED"),
            "incidents": {"total": len(incidents), "open": sum(1 for item in incidents if item.get("outcome") == "PENDING")},
            "estimated_cost": estimated_cost,
            "actual_cost": _actual_cost(),
            "estimated_bytes": estimated_bytes,
        }
    )


@router.get("/estates", response_model=Envelope)
def estates(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    """Every registered estate, not a hardcoded list of one.

    Each estate is summarised against its OWN most recent run. Using the
    globally latest run (as this did before Day 11 Phase 4) would report
    zero objects for every estate except whichever one happened to run
    most recently.
    """
    _authorize_read(user, estate_id)
    # Keyed by the caller's own visibility as well as the filter: two users
    # with different estate grants must never share a cached response.
    scope = ",".join(sorted(user.estate_roles or {})) or "*"
    return _cached(f"estates:{estate_id}:{scope}", lambda: _build_estates(estate_id, user))


def _build_estates(estate_id: str | None, user: UserContext) -> dict:
    runs = _all_runs(500)
    documents = [
        estate for estate in _all_estates()
        if user.has_role("viewer", str(estate.get("estate_id")))
    ]
    if estate_id is not None:
        documents = [e for e in documents if e.get("estate_id") == estate_id]
    summaries = [
        _sanitized_estate(estate, _latest_run(_for_estate(runs, estate["estate_id"])))
        for estate in documents
    ]
    return _envelope(summaries, total=len(summaries))


@router.get("/adapter-types", response_model=Envelope)
def adapter_types() -> dict:
    """Source families this deployment can talk to, and what each can do.

    The onboarding wizard renders its adapter picker from this rather than
    from a hardcoded list, so registering a new adapter (one line in
    tools/adapters/__init__.py) makes it selectable in the console with no
    frontend change. Capabilities drive which actions the wizard offers:
    an assessment-only source family is visibly disabled rather than
    accepted and then failed at run time.
    """
    from tools.adapters import describe_adapters

    types = describe_adapters()
    return _envelope(types, total=len(types))


@router.get("/estates/{estate_id}", response_model=Envelope)
def estate_detail(estate_id: str, user: UserContext = Depends(get_user_context)) -> dict:
    from tools.connection_context import EstateNotFound

    authorize_estate(user, estate_id, "viewer")
    try:
        estate = _estate(estate_id)
    except EstateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    latest = _latest_run(_all_runs(500, estate_id=estate_id))
    return _envelope(_sanitized_estate(estate, latest))


@router.post("/estates", response_model=Envelope, status_code=status.HTTP_201_CREATED)
def create_estate_endpoint(
    body: CreateEstateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    """Onboards an estate.

    Unlike the other write endpoints this is synchronous and returns 201
    rather than queueing a 202: creating an estate is a configuration
    change with no data-plane work behind it, and the wizard's next step
    (validate the connection) needs the estate to exist already. The
    operation is still recorded for audit through the same
    operation_requests path.
    """
    from tools.estate_registry import EstateConflict, EstateValidationError, create_estate

    # A user must hold operator on the estate they are creating. With only
    # a wildcard grant that is any estate — which is the correct behavior
    # for a platform admin onboarding a new customer.
    authorize_estate(user, body.estate_id, "operator")

    document = body.model_dump(exclude={"justification"}, exclude_none=True)
    try:
        record = create_estate(document, actor=user.email)
    except EstateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EstateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _record_estate_operation(
        user=user, kind="estate.create", key=idempotency_key,
        justification=body.justification, estate_id=body.estate_id,
    )
    clear_response_cache()
    return _envelope(_sanitized_estate(record, None))


@router.patch("/estates/{estate_id}", response_model=Envelope)
def update_estate_endpoint(
    estate_id: str,
    body: UpdateEstateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    from tools.estate_registry import (
        EstateNotFound,
        EstateValidationError,
        update_estate,
    )

    authorize_estate(user, estate_id, "operator")
    patch = body.model_dump(exclude={"justification"}, exclude_none=True)
    if not patch:
        raise HTTPException(status_code=422, detail="No changes supplied.")
    try:
        record = update_estate(estate_id, patch, actor=user.email, reason=body.justification)
    except EstateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EstateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _record_estate_operation(
        user=user, kind="estate.update", key=idempotency_key,
        justification=body.justification, estate_id=estate_id,
    )
    clear_response_cache()
    latest = _latest_run(_all_runs(500, estate_id=estate_id))
    return _envelope(_sanitized_estate(record, latest))


@router.delete("/estates/{estate_id}", response_model=Envelope)
def disable_estate_endpoint(
    estate_id: str,
    body: DeleteEstateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    """Soft-delete: sets status to DISABLED, never removes the document.

    Run history references estate_id, so deleting the estate a completed
    run points at makes that history uninterpretable. An estate with
    non-terminal runs is refused outright — disabling it underneath work
    that is still in flight would strand that work with no operator
    visible cause.
    """
    from tools.estate_registry import EstateNotFound, STATUS_DISABLED, set_status

    authorize_estate(user, estate_id, "operator")

    in_flight = [
        run for run in _all_runs(500, estate_id=estate_id)
        if run.get("state") not in {"COMPLETE", "FAILED"}
    ]
    if in_flight:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Estate {estate_id!r} has {len(in_flight)} run(s) still in flight "
                f"(e.g. {in_flight[0].get('run_id')} in {in_flight[0].get('state')}). "
                f"Let them finish or fail before disabling it."
            ),
        )
    try:
        record = set_status(estate_id, STATUS_DISABLED, actor=user.email, reason=body.justification)
    except EstateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _record_estate_operation(
        user=user, kind="estate.disable", key=idempotency_key,
        justification=body.justification, estate_id=estate_id,
    )
    clear_response_cache()
    return _envelope(_sanitized_estate(record, None))


@router.post("/estates/{estate_id}/sources/{source_id}/validate", response_model=Envelope)
def validate_source_connection(
    estate_id: str,
    source_id: str,
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    """The wizard's "Validate connection" step.

    Opens a real connection through the adapter and returns only
    {status, detail, object_count, latency_ms}. It never echoes a
    connection string or credential — `detail` is operator-facing text,
    and the adapter's health_check() is what enforces that. It also
    reports which backend answered for the credential, so a source
    silently running on the local-dev environment fallback instead of
    Secret Manager is visible rather than looking like success.
    """
    from tools.connection_context import (
        EstateNotFound,
        SourceNotFound,
        binding_for,
    )

    authorize_estate(user, estate_id, "operator")

    try:
        binding = binding_for(estate_id, source_id)
    except (EstateNotFound, SourceNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _envelope(_validate_binding(binding))


def _validate_binding(binding: Any) -> dict:
    """Credential-safe live probe shared by persisted and wizard validation."""
    from tools.adapters import build_adapter_for_binding
    from tools.adapters.base import AdapterCapabilityNotSupported

    if not binding.requires_connection:
        return {
            "status": "NOT_APPLICABLE",
            "detail": (
                f"Source {binding.source_id!r} is a static-file source with no live server "
                "to connect to; nothing to validate."
            ),
            "object_count": None,
            "latency_ms": 0,
        }
    try:
        result = build_adapter_for_binding(binding).health_check()
    except AdapterCapabilityNotSupported as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — an unreachable source is an expected probe result
        return {
            "status": "UNREACHABLE",
            "detail": f"{type(exc).__name__}: {exc}",
            "object_count": None,
            "latency_ms": None,
        }
    return {
        "status": result.get("status"),
        "detail": result.get("detail"),
        "object_count": result.get("object_count"),
        "latency_ms": result.get("latency_ms"),
        "last_observed_at": result.get("last_observed_at"),
    }


@router.post("/estate-validations", response_model=Envelope)
def validate_transient_estate_source(
    body: EstateValidationRequest,
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    """Validate connection references before the estate is persisted."""
    from tools.connection_context import binding_from_estate

    authorize_estate(user, body.estate_id, "operator")
    source = body.source.model_dump(exclude_none=True)
    binding = binding_from_estate(
        {"estate_id": body.estate_id, "sources": [source]}, source["source_id"]
    )
    return _envelope(_validate_binding(binding))


@router.get("/assessments", response_model=Envelope)
def assessments(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)
    runs = [run for run in _all_runs(200, estate_id=estate_id) if run.get("mode") == "assessment"]
    estate = _estate(estate_id) if estate_id else None
    allowed_pack_ids = {source.get("pack_id") for source in (estate or {}).get("sources", []) if source.get("pack_id")}
    packs = [pack for pack in _packs() if not allowed_pack_ids or pack.get("pack_id") in allowed_pack_ids]
    return _envelope({"runs": [_attach_progress(run) for run in runs], "packs": packs}, total=len(runs))


@router.get("/waves", response_model=Envelope)
def waves(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)
    return _cached(f"waves:{estate_id}", lambda: _build_waves(estate_id))


def _build_waves(estate_id: str | None) -> dict:
    client = get_client()
    wave_state = _wave_state(estate_id)
    events = [d.to_dict() or {} for d in client.collection("wave_events").limit(200).stream()]
    overrides = [d.to_dict() or {} for d in client.collection("wave_overrides").stream()]
    operations = [
        d.to_dict() or {}
        for d in client.collection("operation_requests").limit(200).stream()
        if (d.to_dict() or {}).get("status") in {"queued", "published", "publish_failed"}
    ]
    if estate_id:
        prefix = f"{estate_id}:"
        events = [item for item in events if str(item.get("source_id", "")).startswith(prefix)]
        overrides = [item for item in overrides if str(item.get("source_id", "")).startswith(prefix)]
        operations = _for_estate(operations, estate_id)
    now = dt.datetime.now(dt.timezone.utc)
    for operation in operations:
        observed = _parsed_time(operation.get("created_at"))
        operation["backlog_age_ms"] = round((now - observed).total_seconds() * 1000) if observed else None
    limits = yaml.safe_load((REPO_ROOT / "policies" / "wave_limits.yaml").read_text(encoding="utf-8"))
    backlog_ages = [item["backlog_age_ms"] for item in operations if item.get("backlog_age_ms") is not None]
    return _envelope(
        {
            "state": wave_state,
            "limits": limits,
            "events": events,
            "overrides": overrides,
            "queued": [item for item in operations if item.get("status") in {"queued", "published"}],
            "blocked": [item for item in operations if item.get("status") == "publish_failed"],
            "oldest_backlog_age_ms": max(backlog_ages, default=None),
        }
    )


@router.get("/runs", response_model=Envelope)
def runs(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
    state_filter: str | None = Query(default=None, alias="state"),
    mode: str | None = None,
    estate_id: str | None = Query(default=None),
    search: str | None = None,
    sort: Literal["created_at_desc", "created_at_asc"] = "created_at_desc",
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)
    # Only the Firestore read is cached; filtering, sorting and paging stay
    # live so a changed query never returns a stale page.
    # `_all_runs` is scoped by `_READ_SCOPE` now, so the hand-rolled
    # visible-estate filter that used to live here would be a second copy
    # of the same rule — and it was the ONLY endpoint that had one.
    items = _cached(f"runs-source:{estate_id}", lambda: _envelope(_all_runs(500, estate_id=estate_id)))["data"]
    if state_filter:
        items = [run for run in items if run.get("state") == state_filter]
    if mode:
        items = [run for run in items if run.get("mode") == mode]
    if search:
        term = search.lower()
        items = [run for run in items if term in f"{run.get('run_id', '')} {run.get('pipeline_id', '')}".lower()]
    if sort == "created_at_asc":
        items.reverse()
    offset = _decode_cursor(cursor)
    page = [_attach_progress(run) for run in items[offset : offset + limit]]
    next_cursor = _encode_cursor(offset + limit) if offset + limit < len(items) else None
    return _envelope(page, total=len(items), next_cursor=next_cursor)


@router.get("/runs/{run_id}", response_model=Envelope)
def run_detail(run_id: str, user: UserContext = Depends(get_user_context)) -> dict:
    try:
        run = get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    authorize_estate(user, run.get("estate_id") or DEFAULT_ESTATE_ID, "viewer")
    collections = (
        "stage_metrics",
        "migration_plan",
        "migration_executions",
        "reconciliation",
        "incidents",
        "policy_decisions",
        "approval_history",
        "cutover",
        "monitoring",
        "risk_findings",
        "containment_events",
        "agent_execution_events",
        "agent_artifacts",
    )
    detail = {name: _collection_docs(run_id, name) for name in collections}
    return _envelope({"run": _attach_progress(run), **detail})


@router.get("/lineage", response_model=Envelope)
def lineage(
    run_id: str | None = None,
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)
    if run_id:
        try:
            selected_run = get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        authorize_estate(user, selected_run.get("estate_id") or DEFAULT_ESTATE_ID, "viewer")
    # Widened from 50: the recency window can be filled entirely by queued
    # runs — 29 consecutive REQUESTED assessments were observed on one
    # estate — and a graph that exists but sits outside the window is
    # indistinguishable to a reader from no graph at all.
    candidates = _all_runs(200, estate_id=estate_id)
    selected = run_id or ((_latest_run_with_catalog(candidates) or {}).get("run_id"))
    # Offered so the page can say WHICH run it drew and let an operator
    # pick another, rather than silently choosing one.
    available = [
        {"run_id": run.get("run_id"), "state": run.get("state"), "created_at": run.get("created_at")}
        for run in candidates
        if run.get("state") in CATALOGUED_STATES
        or "DISCOVERED" in {entry.get("state") for entry in (run.get("state_history") or [])}
    ][:25]
    if not selected:
        return _envelope({"run_id": None, "nodes": [], "edges": [], "available_runs": available})
    tables = _collection_docs(selected, "catalog")
    pipelines = _collection_docs(selected, "pipelines")
    dependencies = _collection_docs(selected, "dependencies")
    short_ids = {f"{item.get('schema')}.{item.get('table')}": item.get("table_id") for item in tables}
    nodes = [
        {
            "id": item.get("table_id"),
            "label": f"{item.get('schema')}.{item.get('table')}",
            "type": "table",
            "classification": item.get("classification"),
            "system": item.get("system"),
        }
        for item in tables
    ] + [{"id": item.get("pipeline_id"), "label": item.get("pipeline_id"), "type": "pipeline"} for item in pipelines]
    edges = [
        {
            "from": short_ids.get(item.get("from_asset"), item.get("from_asset")),
            "to": short_ids.get(item.get("to_asset"), item.get("to_asset")),
            "relationship": item.get("relationship"),
            "confidence": item.get("confidence"),
            "source": item.get("source"),
        }
        for item in dependencies
    ]
    return _envelope(
        {"run_id": selected, "nodes": nodes, "edges": edges, "available_runs": available}
    )


@router.get("/reconciliation", response_model=Envelope)
def reconciliation(
    limit: int = Query(default=200, ge=1, le=500),
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)

    def _build() -> dict:
        runs = _all_runs(100, estate_id=estate_id)
        pipeline_by_run = {run["run_id"]: run.get("pipeline_id") for run in runs}
        by_run = _subcollection_group("reconciliation", set(pipeline_by_run))
        rows = [
            {"run_id": run_id, "pipeline_id": pipeline_by_run.get(run_id), **check}
            for run_id, checks in by_run.items()
            for check in checks
        ]
        return _envelope(rows[:limit], total=min(len(rows), limit))

    return _cached(f"reconciliation:{estate_id}:{limit}", _build)


@router.get("/policies", response_model=Envelope)
def policies(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)

    def _build() -> dict:
        runs = _all_runs(100, estate_id=estate_id)
        run_ids = {run["run_id"] for run in runs}
        # Two collection_group queries, not two per run.
        # Independent scans, overlapped — see the note in /approvals for
        # why the fix is fewer ROUND TRIPS rather than fewer documents.
        scans = _gather(
            {
                "decisions": lambda: _subcollection_group("policy_decisions", run_ids),
                "approvals": lambda: _subcollection_group("approval_history", run_ids),
            }
        )
        by_run_decisions = scans["decisions"]
        by_run_approvals = scans["approvals"]

        decisions = [
            {"run_id": run_id, **item}
            for run_id, items in by_run_decisions.items()
            for item in items
        ]
        approvals = [
            {"run_id": run_id, **item}
            for run_id, items in by_run_approvals.items()
            for item in items
        ]
        decisions.sort(key=lambda item: item.get("decided_at", ""), reverse=True)
        approvals.sort(key=lambda item: item.get("recorded_at", ""), reverse=True)
        return _envelope({"decisions": decisions[:500], "approvals": approvals[:500]})

    return _cached(f"policies:{estate_id}", _build)


@router.get("/agents", response_model=Envelope)
def agents(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)
    cards = [doc.to_dict() or {} for doc in get_client().collection_group("versions").stream()]
    cards.sort(key=lambda item: (item.get("agent_id", ""), item.get("version", "")))
    pinned: dict[str, int] = {}
    for run in _all_runs(200, estate_id=estate_id):
        for agent_id in (run.get("pinned_agents") or {}):
            pinned[agent_id] = pinned.get(agent_id, 0) + 1
    events = _subcollection_group("agent_execution_events", {run["run_id"] for run in _all_runs(200, estate_id=estate_id)})
    flat_events = [
        {"run_id": run_id, **event}
        for run_id, values in events.items()
        for event in values
    ]
    durations = sorted(int(event.get("duration_ms") or 0) for event in flat_events if event.get("duration_ms") is not None)
    # Early audit records predate the structured token object and may carry
    # a textual ``token_usage`` marker. They remain valid audit evidence but
    # must not break the whole Agents page or be mispriced as token counts.
    token_usage = [
        usage
        for event in flat_events
        if isinstance((usage := event.get("token_usage")), dict)
    ]
    completed_count = sum(1 for event in flat_events if event.get("status") == "COMPLETED")
    fallback_count = sum(1 for event in flat_events if event.get("fallback_used"))
    model_usage_events = [
        {
            "kind": "model",
            "model": event.get("model"),
            "input_tokens": event["token_usage"].get("input_tokens"),
            "output_tokens": event["token_usage"].get("output_tokens"),
            "thinking_tokens": event["token_usage"].get("thinking_tokens"),
        }
        for event in flat_events
        if event.get("model") and isinstance(event.get("token_usage"), dict)
    ]
    from tools.usage_meter import price_usage

    priced_model_usage = price_usage(model_usage_events) if model_usage_events else None
    aggregates = {
        "total_executions": len(flat_events),
        "completed": completed_count,
        "failed": sum(1 for event in flat_events if event.get("status") == "FAILED"),
        "fallbacks": fallback_count,
        "success_rate": round(completed_count / len(flat_events) * 100, 1) if flat_events else None,
        "fallback_rate": round(fallback_count / len(flat_events) * 100, 1) if flat_events else None,
        "model_executions": sum(1 for event in flat_events if event.get("model")),
        "p50_latency_ms": int(statistics.median(durations)) if durations else None,
        "p95_latency_ms": durations[min(len(durations) - 1, math.ceil(len(durations) * .95) - 1)] if durations else None,
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in token_usage),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in token_usage),
        "thinking_tokens": sum(int(item.get("thinking_tokens") or 0) for item in token_usage),
        "estimated_model_cost": priced_model_usage,
    }
    flat_events.sort(key=lambda item: item.get("completed_at") or item.get("recorded_at") or "", reverse=True)
    return _envelope({
        "cards": cards, "pinned_run_counts": pinned, "aggregates": aggregates,
        "recent_executions": flat_events[:25],
    }, total=len(cards))


def _agent_events_for_visible_runs(estate_id: str | None) -> list[dict]:
    runs = _all_runs(500, estate_id=estate_id)
    grouped = _subcollection_group("agent_execution_events", {run["run_id"] for run in runs})
    result = [{"run_id": run_id, **item} for run_id, values in grouped.items() for item in values]
    result.sort(key=lambda item: item.get("completed_at") or item.get("recorded_at") or "", reverse=True)
    return result


@router.get("/agents/{agent_id}/executions", response_model=Envelope)
def agent_executions(
    agent_id: str,
    estate_id: str | None = Query(default=None),
    run_id: str | None = None,
    event_status: str | None = Query(default=None, alias="status"),
    model: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    user: UserContext = Depends(require_role("viewer")),
) -> dict:
    _authorize_read(user, estate_id)
    items = [event for event in _agent_events_for_visible_runs(estate_id) if event.get("agent_id") == agent_id]
    if run_id:
        items = [event for event in items if event.get("run_id") == run_id]
    if event_status:
        items = [event for event in items if event.get("status") == event_status]
    if model:
        items = [event for event in items if event.get("model") == model]
    offset = _decode_cursor(cursor)
    next_cursor = _encode_cursor(offset + limit) if offset + limit < len(items) else None
    return _envelope(items[offset:offset + limit], total=len(items), next_cursor=next_cursor)


@router.get("/runs/{run_id}/agent-events", response_model=Envelope)
def run_agent_events(run_id: str, user: UserContext = Depends(get_user_context)) -> dict:
    try:
        run = get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    authorize_estate(user, run.get("estate_id") or DEFAULT_ESTATE_ID, "viewer")
    items = _collection_docs(run_id, "agent_execution_events")
    items.sort(key=lambda item: item.get("completed_at") or item.get("recorded_at") or "")
    return _envelope(items, total=len(items))


@router.get("/evaluations", response_model=Envelope)
def evaluations(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)
    client = get_client()
    evaluation_runs = [d.to_dict() or {} for d in client.collection("evaluation_runs").limit(100).stream()]
    if estate_id:
        evaluation_runs = [item for item in evaluation_runs if (item.get("estate_id") or DEFAULT_ESTATE_ID) == estate_id]
    scale_snapshot = client.collection("evaluation_scale_reports").document("current").get()
    scale_metrics = scale_snapshot.to_dict() if scale_snapshot.exists else None
    return _envelope(
        {
            "runs": sorted(evaluation_runs, key=lambda item: item.get("started_at", ""), reverse=True),
            "scale_metrics": scale_metrics,
            "scale_report_status": "available" if scale_metrics else "not_configured",
            "scale_report_reason": None if scale_metrics else "Run evaluation/scale_harness.py to persist measured scale metrics.",
        }
    )


@router.get("/system-health", response_model=Envelope)
def system_health(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    _authorize_read(user, estate_id)
    return _cached(f"system-health:{estate_id}", lambda: _build_system_health(estate_id))


def _build_system_health(estate_id: str | None) -> dict:
    client = get_client()
    latest = _latest_run(_all_runs(10, estate_id=estate_id))
    latest_agent_events = _collection_docs(latest["run_id"], "agent_execution_events") if latest else []
    model_events = [event for event in latest_agent_events if event.get("model")]
    model_last_observed = max(
        (event.get("completed_at") or event.get("recorded_at") or "" for event in model_events),
        default=None,
    )
    required_cards = [
        doc.to_dict() or {}
        for doc in client.collection_group("versions").stream()
        if (doc.to_dict() or {}).get("model_required")
    ]
    approved_required = [card for card in required_cards if card.get("status") == "APPROVED"]
    model_failures = sum(1 for event in model_events if event.get("status") == "FAILED")
    processed = [d.to_dict() or {} for d in client.collection("processed_messages").limit(100).stream()]
    connections = []
    for snapshot in client.collection("connection_health").stream():
        value = snapshot.to_dict() or {}
        belongs = value.get("estate_id") or (snapshot.id.split("__", 1)[0] if "__" in snapshot.id else DEFAULT_ESTATE_ID)
        if not estate_id or belongs == estate_id:
            connections.append(value)
    services = [
        {"service": "Firestore", "status": "HEALTHY", "last_observed_at": _now(), "detail": "Read succeeded"},
        {
            "service": "Pub/Sub",
            "status": "OBSERVED" if processed else "NOT_OBSERVED",
            "last_observed_at": max((item.get("completed_at", "") for item in processed), default=None),
            "detail": f"{len(processed)} recent processed-message records",
        },
        {
            "service": "BigQuery",
            "status": "OBSERVED" if latest and _collection_docs(latest["run_id"], "migration_executions") else "NOT_OBSERVED",
            "last_observed_at": latest.get("last_transition_at") if latest else None,
            "detail": "Derived from durable migration execution records",
        },
        {
            "service": "Cloud Trace",
            "status": "OBSERVED" if latest and latest.get("trace_id") else "NOT_INSTRUMENTED",
            "last_observed_at": latest.get("last_transition_at") if latest and latest.get("trace_id") else None,
            "detail": "Run trace_id is not persisted" if not latest or not latest.get("trace_id") else latest.get("trace_id"),
        },
        {
            "service": "Cloud Run",
            "status": "NOT_INSTRUMENTED",
            "last_observed_at": None,
            "detail": "No runtime health snapshot has been recorded.",
        },
        {
            "service": "Vertex AI reasoning",
            "status": "CONFIGURED" if _feature_enabled("ENABLE_AGENT_REASONING_V2") and os.environ.get("GCP_PROJECT_ID") else "DISABLED",
            "last_observed_at": model_last_observed,
            "detail": os.environ.get("AGENT_REASONING_MODEL", "gemini-3.7-flash"),
        },
        {
            "service": "Required-agent readiness",
            "status": (
                "READY"
                if _feature_enabled("ENABLE_AGENT_REASONING_V2") and required_cards and len(approved_required) == len(required_cards)
                else "DISABLED"
                if not _feature_enabled("ENABLE_AGENT_REASONING_V2")
                else "DEGRADED"
            ),
            "last_observed_at": model_last_observed,
            "detail": f"{len(approved_required)}/{len(required_cards)} required model registry versions approved",
        },
        {
            "service": "Model execution telemetry",
            "status": "STALE" if model_last_observed and _is_stale(model_last_observed) else "OBSERVED" if model_events else "NOT_OBSERVED",
            "last_observed_at": model_last_observed,
            "detail": (
                f"{model_failures}/{len(model_events)} model executions failed "
                f"({round(model_failures / len(model_events) * 100, 1) if model_events else 0}%)"
            ),
        },
        {
            "service": "Report storage",
            "status": "CONFIGURED" if _feature_enabled("ENABLE_REPORTS") and os.environ.get("REPORTS_BUCKET") else "DISABLED",
            "last_observed_at": None,
            "detail": "Private Cloud Storage bucket configured" if os.environ.get("REPORTS_BUCKET") else "REPORTS_BUCKET is not configured",
        },
        {
            "service": "AI assistant",
            "status": "CONFIGURED" if _feature_enabled("ENABLE_AI_ASSISTANT") and os.environ.get("GCP_PROJECT_ID") else "DISABLED",
            "last_observed_at": None,
            "detail": os.environ.get("ASSISTANT_MODEL", "gemini-3.5-flash"),
        },
    ]
    services.extend(
        {
            "service": f"Connection: {snapshot.get('source_system', 'unknown')}",
            "status": snapshot.get("status", "NOT_OBSERVED"),
            "last_observed_at": snapshot.get("last_observed_at"),
            "detail": snapshot.get("detail") or "No credential-free health detail was recorded.",
        }
        for snapshot in connections
    )
    return _envelope({"services": services, "build_version": os.environ.get("BUILD_VERSION", "development")})


@router.get("/operations/{operation_id}", response_model=Envelope)
def operation_status(
    operation_id: str,
    user: UserContext = Depends(get_user_context),
) -> dict:
    snapshot = get_client().collection("operation_requests").document(operation_id).get()
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="Operation not found.")
    operation = {"operation_id": snapshot.id, **(snapshot.to_dict() or {})}
    estate_id = operation.get("estate_id") or (operation.get("event") or {}).get("estate_id")
    if operation.get("actor") != user.email:
        if not estate_id:
            raise HTTPException(status_code=403, detail="This operation is not visible to the signed-in user.")
        authorize_estate(user, estate_id, "viewer")
    progress = _operation_progress(operation)
    return _envelope({**operation, "estate_id": estate_id, "run_id": progress.get("run_id"), "progress": progress})


@router.get("/search", response_model=Envelope)
def search_console(
    q: str = Query(min_length=1, max_length=120),
    estate_id: str = Query(min_length=2, max_length=100),
    user: UserContext = Depends(get_user_context),
) -> dict:
    authorize_estate(user, estate_id, "viewer")
    term = q.strip().lower()
    results: list[dict] = []
    estate = _estate(estate_id)
    if term in f"{estate_id} {estate.get('display_name', '')}".lower():
        results.append({
            "id": estate_id, "kind": "estate", "title": estate.get("display_name", estate_id),
            "subtitle": estate_id, "route": "/estates",
        })
    for run in _all_runs(100, estate_id=estate_id):
        haystack = f"{run.get('run_id', '')} {run.get('pipeline_id', '')} {run.get('state', '')}".lower()
        if term in haystack:
            results.append({
                "id": run.get("run_id"), "kind": "run", "title": run.get("run_id"),
                "subtitle": f"{run.get('pipeline_id', 'Assessment')} · {run.get('state', 'UNKNOWN')}",
                "route": f"/runs/{run.get('run_id')}",
            })
        if len(results) >= 20:
            break
    return _envelope(results, total=len(results))


@router.get("/notifications", response_model=Envelope)
def notifications(
    estate_id: str = Query(min_length=2, max_length=100),
    user: UserContext = Depends(get_user_context),
) -> dict:
    authorize_estate(user, estate_id, "viewer")
    now = dt.datetime.now(dt.timezone.utc)
    items: list[dict] = []
    operations = _for_estate(
        [doc.to_dict() or {} for doc in get_client().collection("operation_requests").limit(200).stream()],
        estate_id,
    )
    for operation in operations:
        status_value = operation.get("status")
        observed = _parsed_time(operation.get("updated_at") or operation.get("created_at"))
        is_stale = status_value in {"queued", "published"} and observed and now - observed > dt.timedelta(minutes=30)
        if status_value in {"failed", "publish_failed"} or is_stale:
            items.append({
                "id": operation.get("operation_id"),
                "kind": "operation",
                "severity": "critical" if status_value in {"failed", "publish_failed"} else "warning",
                "title": "Operation failed" if status_value in {"failed", "publish_failed"} else "Operation is stale",
                "detail": operation.get("error") or operation.get("kind") or "No recent worker update.",
                "status": status_value,
                "observed_at": operation.get("updated_at") or operation.get("created_at"),
                "route": "/system-health",
            })
    for run in _all_runs(100, estate_id=estate_id):
        if run.get("state") in VALIDATION_FAILURE_STAGES | {"READY_FOR_APPROVAL"}:
            pending = run.get("state") == "READY_FOR_APPROVAL"
            items.append({
                "id": run.get("run_id"),
                "kind": "approval" if pending else "run",
                "severity": "info" if pending else "critical",
                "title": "Cutover approval required" if pending else STAGE_LABELS.get(run.get("state"), "Run needs attention"),
                "detail": run.get("pipeline_id") or run.get("pack_id") or run.get("run_id"),
                "status": run.get("state"),
                "observed_at": run.get("last_transition_at") or run.get("created_at"),
                "route": f"/runs/{run.get('run_id')}",
            })
    items.sort(key=lambda item: item.get("observed_at") or "", reverse=True)
    return _envelope(items[:100], total=len(items))


def _record_estate_operation(
    *, user: UserContext, kind: str, key: str, justification: str, estate_id: str
) -> None:
    """Audit record for a configuration change.

    Estate writes are synchronous and publish no command event, so they do
    not go through _queue()/Pub/Sub — but they must still leave the same
    operator-request trail as every other console write: who, what, when,
    under which Idempotency-Key, with what justification. The durable
    estate state and its revision history live in tools/estate_registry.py;
    this is the operator-intent half of the record.
    """
    import uuid as _uuid

    from frontend.operations import _operation_id, _validated_key

    clear_response_cache()

    try:
        validated = _validated_key(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = _now()
    client = get_client()
    operation_id = _operation_id(user.email, kind, validated)
    client.collection("operation_requests").document(operation_id).set(
        {
            "operation_id": operation_id,
            "kind": kind,
            "status": "applied",
            "actor": user.email,
            "roles": sorted(user.roles),
            "justification": justification,
            "estate_id": estate_id,
            "created_at": now,
            "updated_at": now,
        }
    )
    client.collection("operation_audit").document(str(_uuid.uuid4())).set(
        {
            "operation_id": operation_id,
            "kind": kind,
            "actor": user.email,
            "estate_id": estate_id,
            "justification": justification,
            "event": "applied",
            "recorded_at": now,
        }
    )


def _queue(
    *, user: UserContext, kind: str, key: str, justification: str, topic: str, event: dict
) -> dict:
    # An operator must see the effect of their own action immediately,
    # rather than waiting out the read cache.
    clear_response_cache()
    try:
        operation = queue_operation(
            actor=user.email,
            roles=list(user.roles),
            kind=kind,
            idempotency_key=key,
            justification=justification,
            topic=topic,
            event=event,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface infrastructure publishing failures as service unavailable
        raise HTTPException(status_code=503, detail=f"The operation was recorded but could not be published: {exc}") from exc
    return _envelope(operation)


@router.post("/assessments", response_model=Envelope, status_code=status.HTTP_202_ACCEPTED)
def start_assessment(
    body: StartAssessmentRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    authorize_estate(user, body.estate_id, "operator")
    pack = next((item for item in _packs() if item.get("pack_id") == body.pack_id), None)
    if not pack:
        raise HTTPException(status_code=422, detail=f"Unknown Migration Pack: {body.pack_id!r}.")
    return _queue(
        user=user,
        kind="assessment.start",
        key=idempotency_key,
        justification=body.justification,
        topic="assessment.requested",
        event={"pack_id": body.pack_id, "pack_path": pack["path"], "estate_id": body.estate_id},
    )


def _execution_profiles_for(estate_id: str) -> list[str]:
    """Execution profiles the estate's own sources declare."""
    from tools.connection_context import EstateNotFound

    try:
        estate = _estate(estate_id)
    except EstateNotFound:
        return []
    profiles: list[str] = []
    for source in estate.get("sources", []):
        declared = source.get("execution_profiles") or (
            [source["pack_id"]] if source.get("pack_id") else []
        )
        for profile in declared:
            if profile and profile not in profiles:
                profiles.append(profile)
    return profiles


def _resolve_execution_profile(estate_id: str, requested: str | None) -> str | None:
    """Resolves — and validates — the execution profile for one estate.

    Previously this defaulted to "wwi-default" for every estate, so a run
    started against a newly onboarded estate carried a profile belonging
    to the demo estate. Nothing rejected it; the mismatch would only
    surface later, in the run.

    Omitted with exactly one profile available: that one is used.
    Omitted with several: the caller must choose, rather than the server
    picking arbitrarily.
    """
    available = _execution_profiles_for(estate_id)

    if requested is None:
        if len(available) == 1:
            return available[0]
        if not available:
            return None
        raise HTTPException(
            status_code=422,
            detail=(
                f"Estate {estate_id!r} offers several execution profiles "
                f"({sorted(available)}); name the one to use."
            ),
        )

    if available and requested not in available:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Execution profile {requested!r} is not offered by estate "
                f"{estate_id!r}. Available: {sorted(available)}."
            ),
        )
    return requested


def _resolve_execution_binding(body: StartRunRequest) -> tuple[str, str, str]:
    """Resolve and validate the canonical estate/source/pack contract."""
    from tools.adapters import ADAPTER_TYPES
    from tools.connection_context import EstateNotFound
    from tools.pack_loader import adapter_type_for

    if body.pack_id and body.execution_profile and body.pack_id != body.execution_profile:
        raise HTTPException(
            status_code=422,
            detail="pack_id and deprecated execution_profile must match when both are supplied.",
        )
    if body.execution_profile and not body.pack_id:
        # Compatibility validation keeps the former API error contract,
        # while the value is immediately normalized to canonical pack_id.
        pack_id = _resolve_execution_profile(body.estate_id, body.execution_profile)
    elif body.pack_id:
        pack_id = body.pack_id
    else:
        pack_id = _resolve_execution_profile(body.estate_id, None)
    if not pack_id:
        raise HTTPException(status_code=422, detail="Select a Migration Pack using pack_id.")

    pack = next((item for item in _packs() if item.get("pack_id") == pack_id), None)
    if pack is None:
        raise HTTPException(status_code=422, detail=f"Unknown Migration Pack: {pack_id!r}.")
    if not pack.get("execution_supported"):
        raise HTTPException(
            status_code=422,
            detail=f"Migration Pack {pack_id!r} supports assessment only.",
        )
    try:
        estate = _estate(body.estate_id)
    except EstateNotFound as exc:
        raise HTTPException(status_code=422, detail=f"Unknown estate: {body.estate_id!r}.") from exc

    sources = estate.get("sources") or []
    if body.source_id:
        candidates = [source for source in sources if source.get("source_id") == body.source_id]
        if not candidates:
            raise HTTPException(
                status_code=422,
                detail=f"Estate {body.estate_id!r} has no source {body.source_id!r}.",
            )
        if not _source_declares_pack(candidates[0], pack_id):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Source {body.source_id!r} is not bound to Migration Pack {pack_id!r}."
                ),
            )
    else:
        candidates = [source for source in sources if _source_declares_pack(source, pack_id)]
        if len(candidates) != 1:
            detail = (
                f"No source in estate {body.estate_id!r} declares Migration Pack {pack_id!r}."
                if not candidates
                else f"Several sources declare Migration Pack {pack_id!r}; select source_id."
            )
            raise HTTPException(status_code=422, detail=detail)

    source = candidates[0]
    wanted_adapter = adapter_type_for(pack)
    if source.get("adapter") != wanted_adapter or wanted_adapter not in ADAPTER_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Migration Pack {pack_id!r} is incompatible with source "
                f"{source.get('source_id')!r}."
            ),
        )
    return source["source_id"], pack_id, body.pipeline_id or pack_id


@router.post("/runs", response_model=Envelope, status_code=status.HTTP_202_ACCEPTED)
def start_run(
    body: StartRunRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    authorize_estate(user, body.estate_id, "operator")
    source_id, pack_id, pipeline_id = _resolve_execution_binding(body)
    return _queue(
        user=user,
        kind="migration.start",
        key=idempotency_key,
        justification=body.justification,
        topic="migration.requested",
        event={
            "pipeline_id": pipeline_id,
            "source_id": source_id,
            "pack_id": pack_id,
            "estate_id": body.estate_id,
            "drop_fraction": 0.0,
        },
    )


@router.post("/runs/{run_id}/retry", response_model=Envelope, status_code=status.HTTP_202_ACCEPTED)
def retry_run(
    run_id: str,
    body: RetryRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    try:
        run = get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    authorize_estate(user, run.get("estate_id") or DEFAULT_ESTATE_ID, "operator")
    topic_by_state = {"PLANNED": "plan.created", "FAILED": "validation.failed"}
    topic = topic_by_state.get(run.get("state"))
    if not topic:
        raise HTTPException(status_code=409, detail="Retry is allowed only for PLANNED and FAILED runs.")
    return _queue(
        user=user,
        kind="migration.retry",
        key=idempotency_key,
        justification=body.justification,
        topic=topic,
        event={"run_id": run_id, "retry_reason": body.justification},
    )


@router.put("/waves/{source_id}/override", response_model=Envelope, status_code=status.HTTP_202_ACCEPTED)
def wave_override(
    source_id: str,
    body: WaveOverrideRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    authorize_estate(user, body.estate_id, "operator")
    wave_key = f"{body.estate_id}:{source_id}"
    if body.expires_at:
        try:
            expires = dt.datetime.fromisoformat(body.expires_at)
            if expires.tzinfo is None:
                raise ValueError
            if expires <= dt.datetime.now(dt.timezone.utc):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="expires_at must be a future timezone-aware RFC 3339 timestamp.") from exc
    operation = _queue(
        user=user,
        kind="wave.override",
        key=idempotency_key,
        justification=body.justification,
        topic="wave.override.requested",
        event={"source_id": wave_key, "estate_id": body.estate_id, "state": body.state, "expires_at": body.expires_at},
    )
    if operation["data"].get("idempotent_replay"):
        return operation
    record = record_wave_override(
        source_id=wave_key,
        state=body.state,
        actor=user.email,
        justification=body.justification,
        expires_at=body.expires_at,
    )
    operation["data"]["override"] = record
    return operation


@router.post("/runs/{run_id}/approve", response_model=Envelope, status_code=status.HTTP_202_ACCEPTED)
def approve(
    run_id: str,
    body: ApproveV1Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("approver")),
) -> dict:
    try:
        existing = get_operation(
            actor=user.email,
            kind="cutover.approve",
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if existing:
        existing["approval"] = approval_service.get_approval(run_id)
        return _envelope(existing)
    try:
        run = get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    authorize_estate(user, run.get("estate_id") or DEFAULT_ESTATE_ID, "approver")
    if run.get("state") != "READY_FOR_APPROVAL":
        raise HTTPException(status_code=409, detail="The run is not ready for approval.")
    try:
        token = approval_service.approve(run_id, approver_identity=user.email, justification=body.justification)
        transition_state(run_id, "APPROVED")
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = _queue(
        user=user,
        kind="cutover.approve",
        key=idempotency_key,
        justification=body.justification,
        topic="cutover.approved",
        event={"run_id": run_id, "token_id": token["token_id"]},
    )
    result["data"]["approval"] = {"status": "APPROVED", "approved_by": user.email, "token_id": token["token_id"]}
    return result


# ---------------------------------------------------------------------------
# Workers (Day 11 Phase 10)
# ---------------------------------------------------------------------------
#
# The console publishes a durable command for every operator action, and
# tools/worker_supervisor.py is what finally consumes them in-process. That
# makes "queued but nothing is happening" a state an operator can now
# cause — by pausing a consumer, or by running an instance that does not
# hold the worker lease — so it has to be a state they can also SEE. This
# is the endpoint behind the System Health page's Workers panel.


class WorkerControlRequest(BaseModel):
    justification: str = Field(min_length=5, max_length=2000)


@router.get("/workers", response_model=Envelope)
def workers(user: UserContext = Depends(get_user_context)) -> dict:
    """Per-consumer state plus who holds the worker lease.

    Deliberately not estate-scoped, and readable by any viewer: the
    consumer fleet is a property of the process, not of an estate, and it
    carries no estate data — only subscription names and counters.
    """
    from frontend import worker_runtime

    return _envelope(worker_runtime.status())


@router.post("/workers/{name}/{action}", response_model=Envelope)
def set_worker_paused(
    name: str,
    action: str,
    body: WorkerControlRequest,
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    """Pauses or resumes one consumer, or "all".

    NOT estate-scoped, and one of the few mutating routes that does not
    call authorize_estate — see NON_ESTATE_MUTATING_ROUTES in
    tests/test_estate_rbac.py. A consumer serves every estate on the
    subscription, so there is no estate to authorize against; picking one
    would misrepresent the blast radius rather than contain it. The
    coarse `operator` role plus a recorded justification is the control.

    Pause is durable (Firestore worker_controls/{name}), not a local flag:
    on Cloud Run this request lands on an arbitrary instance, very likely
    not the lease holder, and an in-process flag would silently do
    nothing. It also has to survive a restart — a consumer paused for a
    reason must not resume itself on the next deploy.

    No Idempotency-Key: pausing a paused consumer is a no-op with no
    data-plane effect. It is still attributed, through operation_audit.
    """
    import uuid as _uuid

    from frontend import worker_runtime

    if action not in {"pause", "resume"}:
        raise HTTPException(status_code=404, detail="Unknown worker action.")

    supervisor = worker_runtime.get_supervisor()
    if supervisor is None:
        raise HTTPException(
            status_code=503,
            detail="No worker supervisor is running in this process, so there is nothing to pause.",
        )

    try:
        result = supervisor.set_paused(
            name, action == "pause", actor=user.email, justification=body.justification
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No such consumer: {name!r}") from exc

    get_client().collection("operation_audit").document(str(_uuid.uuid4())).set(
        {
            "kind": f"worker.{action}",
            "actor": user.email,
            "roles": sorted(user.roles),
            "consumers": result["consumers"],
            "justification": body.justification,
            "event": action,
            "recorded_at": _now(),
        }
    )
    clear_response_cache()
    return _envelope({**result, "action": action})


# ---------------------------------------------------------------------------
# Dead letters and incidents (Day 11 Phase 13)
# ---------------------------------------------------------------------------
#
# Both of these existed as data and neither was reachable. The dead-letter
# subscription was provisioned and forwarding, but nothing could read it —
# a message that defeated a consumer showed up only as `error` on the
# workers panel, with the payload visible solely by running gcloud. The
# `incidents` subcollection has been written by recovery.py since Day 8 and
# has never had a screen.


class DeadLetterActionRequest(BaseModel):
    justification: str = Field(min_length=5, max_length=2000)


@router.get("/dead-letters", response_model=Envelope)
def dead_letters(
    limit: int = Query(default=25, ge=1, le=100),
    user: UserContext = Depends(get_user_context),
) -> dict:
    """What the fleet gave up on, and which consumer gave up.

    Reading is non-destructive: tools/dead_letters.py returns each lease
    immediately, so refreshing this page does not hide the queue from the
    next reader for a minute.
    """
    from tools import dead_letters as dlq

    try:
        pending = dlq.list_dead_letters(limit=limit)
    except Exception as exc:  # noqa: BLE001 — an unreachable DLQ must not 500 the page
        raise HTTPException(
            status_code=503, detail=f"The dead-letter queue could not be read: {exc}"
        ) from exc
    return _envelope({"pending": pending, "archive": dlq.list_archive(limit=limit)},
                     total=len(pending))


@router.post("/dead-letters/{message_id}/{action}", response_model=Envelope)
def act_on_dead_letter(
    message_id: str,
    action: str,
    body: DeadLetterActionRequest,
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    """Replay a dead letter onto its original topic, or archive it.

    NOT estate-scoped, and one of the few mutating routes that does not
    call authorize_estate — see NON_ESTATE_MUTATING_ROUTES in
    tests/test_estate_rbac.py. A dead letter is a message on a fleet-wide
    subscription; its payload may name an estate, but the queue does not
    belong to one, and authorizing against an estate parsed out of an
    untrusted payload would be worse than not authorizing at all.

    No Idempotency-Key: the message id IS the idempotency key. Replaying a
    message that has already been replayed fails with 404 because it is no
    longer in the queue.
    """
    import uuid as _uuid

    from tools import dead_letters as dlq

    if action not in {"replay", "archive"}:
        raise HTTPException(status_code=404, detail="Unknown dead-letter action.")

    handler = dlq.replay if action == "replay" else dlq.archive
    try:
        result = handler(message_id, actor=user.email, justification=body.justification)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    get_client().collection("operation_audit").document(str(_uuid.uuid4())).set(
        {
            "kind": f"dead_letter.{action}",
            "actor": user.email,
            "roles": sorted(user.roles),
            "message_id": message_id,
            "justification": body.justification,
            "event": action,
            "recorded_at": _now(),
            **result,
        }
    )
    clear_response_cache()
    return _envelope(result)


@router.get("/incidents", response_model=Envelope)
def incidents(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    """Everything that went wrong on a run, in one place.

    Assembled from collections that already exist rather than from a new
    incident store: recovery.py's per-run `incidents` subcollection, the
    reconciliation checks that failed, and the policy decisions that
    denied. `canonical_root_cause` is surfaced rather than `root_cause` —
    the latter is display-wrapped narrative ("recalled from memory…") and
    conflating the two was a real defect that nested wrapper text across
    generations.
    """
    _authorize_read(user, estate_id)
    runs = _all_runs(60, estate_id=estate_id)
    by_run = {run.get("run_id"): run for run in runs}

    client = get_client()
    # Both collection groups are read up front and together; the loops
    # below are pure Python over what came back.
    scanned = _gather(
        {
            "incidents": lambda: [d.to_dict() or {} for d in client.collection_group("incidents").stream()],
            "decisions": lambda: [
                d.to_dict() or {} for d in client.collection_group("policy_decisions").stream()
            ],
        }
    )

    records: list[dict] = []
    for incident in scanned["incidents"]:
        run = by_run.get(incident.get("run_id"))
        if run is None:
            continue  # another estate's run, or older than the window
        records.append(
            {
                "incident_id": incident.get("incident_id"),
                "run_id": incident.get("run_id"),
                "estate_id": run.get("estate_id"),
                "signature": incident.get("signature"),
                "table_ref": incident.get("table_ref"),
                "root_cause": incident.get("canonical_root_cause") or incident.get("root_cause"),
                "explained_by": incident.get("root_cause_generated_by"),
                "fix": incident.get("fix") or None,
                "outcome": incident.get("outcome"),
                "opened_at": incident.get("created_at"),
                "run_state": run.get("state"),
                "memory_refs": run.get("memory_refs") or [],
                "route": f"/runs/{incident.get('run_id')}",
            }
        )
    records.sort(key=lambda item: item.get("opened_at") or "", reverse=True)

    denials = [
        {
            "run_id": decision.get("run_id"),
            "agent_id": decision.get("agent_id"),
            "action": decision.get("action"),
            "resource_class": decision.get("resource_class"),
            "decided_at": decision.get("decided_at"),
            "reason": decision.get("reason"),
        }
        for decision in scanned["decisions"]
        if decision.get("decision") == "DENY" and decision.get("run_id") in by_run
    ]
    denials.sort(key=lambda item: item.get("decided_at") or "", reverse=True)

    return _envelope(
        {
            "incidents": records,
            "policy_denials": denials[:50],
            "open_count": sum(1 for item in records if item.get("outcome") == "PENDING"),
        },
        total=len(records),
    )


@router.get("/memory-bank", response_model=Envelope)
def memory_bank_facts(user: UserContext = Depends(get_user_context)) -> dict:
    """Durable remediation facts learned across runs.

    Deliberately NOT estate-scoped, and not filterable by one: the whole
    value of this collection is that a fact confirmed on one estate is
    available to a later run on another. That is also its honest cost —
    a signature carries a source table name, so this is the one place
    where metadata legitimately crosses an estate boundary. Read access
    is therefore the same coarse role as the rest of the console rather
    than an estate grant, and the UI says so on the page.

    `root_cause` here is already the canonical fact: recovery.py writes
    `canonical_root_cause` into memory precisely so that re-confirming an
    already-recalled fact does not nest another "Recalled from memory…"
    wrapper on every generation. Nothing further needs unwrapping.
    """
    from tools import memory_bank

    facts = []
    for fact in memory_bank.list_facts():
        recalled_by = fact.get("recalled_by_run_ids") or []
        source_runs = fact.get("source_run_ids") or []
        facts.append(
            {
                "signature": fact.get("signature"),
                "root_cause": fact.get("root_cause"),
                "fix": fact.get("fix"),
                # Two different things, kept apart on purpose.
                # `recalled_by` is the number of LATER runs that cited this
                # fact as evidence — the only figure that demonstrates
                # cross-run learning. `confirmations` counts how often the
                # fact was re-confirmed after a successful remediation.
                "recalled_by_count": len(recalled_by),
                "recalled_by_run_ids": recalled_by,
                "confirmations": fact.get("reuse_count", 0),
                "source_run_ids": source_runs,
                "first_learned_at": fact.get("created_at"),
                "last_confirmed_at": fact.get("last_confirmed_at"),
            }
        )
    facts.sort(key=lambda item: (item["recalled_by_count"], item.get("last_confirmed_at") or ""), reverse=True)
    return _envelope(
        {
            "facts": facts,
            "reused_facts": sum(1 for fact in facts if fact["recalled_by_count"]),
        },
        total=len(facts),
    )


@router.get("/approvals", response_model=Envelope)
def approvals(
    estate_id: str | None = Query(default=None),
    user: UserContext = Depends(get_user_context),
) -> dict:
    """The cutover approval inbox, with the evidence behind each decision.

    The point of this endpoint is one fact that was previously invisible
    until it was too late to matter: an approval token is bound to the
    plan hash it was issued against, and `approval_service.consume()`
    refuses the cutover if the plan has changed since. That refusal
    happened at cutover time, long after the human clicked approve. Here
    the binding is compared up front, so a stale approval is visible
    BEFORE anyone relies on it.

    Nothing here can approve anything. The only path from
    READY_FOR_APPROVAL to APPROVED remains the authenticated approver
    endpoint, which is a separate identity from every agent.
    """
    _authorize_read(user, estate_id)

    def _build() -> dict:
        runs = _all_runs(100, estate_id=estate_id)
        by_id = {run["run_id"]: run for run in runs}
        run_ids = set(by_id)

        # Four independent collection-group scans, overlapped.
        #
        # This is the opposite of the obvious fix, and the measurements are
        # why. Reading fewer DOCUMENTS made it slower twice: per-run reads
        # for 84 runs took 7.7s against a 3.3s scan, and replacing document
        # reads with server-side count() aggregations took 11.7s, because
        # each aggregation is its own round trip and costs ~2.2s against
        # this off-region project.
        #
        # The bottleneck is latency, not volume. A collection-group scan is
        # ONE round trip that streams a lot; two dozen per-run queries are
        # two dozen round trips that stream almost nothing. So the scans
        # stay, and the win comes from running them at the same time
        # instead of one after another.
        scans = _gather(
            {
                name: (lambda n=name: _subcollection_group(n, run_ids))
                for name in ("approval", "migration_plan", "reconciliation", "risk_findings")
            }
        )
        approval_docs = scans["approval"]
        plan_docs = scans["migration_plan"]
        reconciliation = scans["reconciliation"]
        risk_findings = scans["risk_findings"]

        items = []
        for run_id, run in by_id.items():
            approval = next(
                (doc for doc in approval_docs.get(run_id, []) if doc.get("_id") == "current"), None
            )
            if approval is None:
                continue

            plan = next(
                (doc for doc in plan_docs.get(run_id, []) if doc.get("_id") == "current"), None
            ) or {}
            current_plan_hash = plan.get("plan_hash")
            approved_plan_hash = approval.get("plan_hash")

            checks = reconciliation.get(run_id, [])
            failed_checks = [c for c in checks if str(c.get("status", "")).upper() not in {"PASSED", "OK"}]
            findings = risk_findings.get(run_id, [])
            findings_total = len(findings)
            critical_total = sum(
                1 for item in findings if str(item.get("severity", "")).upper() == "CRITICAL"
            )

            approved_at = approval.get("approved_at")
            expires_after_days = approval.get("expires_after_days")
            expires_at = None
            expired = False
            if approved_at and expires_after_days:
                try:
                    expires = dt.datetime.fromisoformat(approved_at) + dt.timedelta(
                        days=int(expires_after_days)
                    )
                    expires_at = expires.isoformat()
                    expired = expires < dt.datetime.now(dt.timezone.utc)
                except (TypeError, ValueError):
                    expires_at = None

            items.append(
                {
                    "run_id": run_id,
                    "estate_id": run.get("estate_id"),
                    "run_state": run.get("state"),
                    "status": approval.get("status"),
                    "requested_by": approval.get("requested_by"),
                    "requested_at": approval.get("requested_at"),
                    "approved_by": approval.get("approved_by"),
                    "approved_at": approval.get("approved_at"),
                    "justification": approval.get("justification"),
                    "token_id": approval.get("token_id"),
                    # The binding, stated rather than implied. None for
                    # current_plan_hash means no plan is recorded yet, which
                    # is different from a mismatch and must not read as one.
                    "approved_plan_hash": approved_plan_hash,
                    "current_plan_hash": current_plan_hash,
                    "binding": (
                        "intact"
                        if approved_plan_hash and approved_plan_hash == current_plan_hash
                        else "no_plan"
                        if not current_plan_hash
                        else "stale"
                    ),
                    "expires_at": expires_at,
                    "expired": expired,
                    # Evidence an approver should see before deciding.
                    "checks_total": len(checks),
                    "checks_failed": len(failed_checks),
                    "risk_findings": findings_total,
                    "critical_findings": critical_total,
                    "route": f"/runs/{run_id}",
                }
            )

        items.sort(key=lambda item: item.get("requested_at") or "", reverse=True)
        awaiting = [item for item in items if item["status"] == "PENDING"]
        return _envelope(
            {
                "awaiting": awaiting,
                "decided": [item for item in items if item["status"] != "PENDING"],
                "stale_bindings": sum(1 for item in items if item["binding"] == "stale"),
            },
            total=len(items),
        )

    return _cached(f"approvals:{estate_id}", _build)


# ---------------------------------------------------------------------------
# Immutable reports
# ---------------------------------------------------------------------------


@router.post("/reports", response_model=Envelope, status_code=status.HTTP_202_ACCEPTED)
def create_report(
    body: CreateReportRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("viewer")),
) -> dict:
    if not _feature_enabled("ENABLE_REPORTS"):
        raise HTTPException(status_code=503, detail="Report generation is not enabled on this deployment.")
    try:
        run = get_run(body.run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    estate_id = run.get("estate_id") or DEFAULT_ESTATE_ID
    authorize_estate(user, estate_id, "viewer")

    from frontend.operations import _operation_id, _validated_key
    from frontend.report_service import generate_background

    try:
        key = _validated_key(idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report_id = _operation_id(user.email, f"report:{body.report_type}:{body.run_id}", key)
    client = get_client()
    ref = client.collection("report_artifacts").document(report_id)
    existing = ref.get()
    if existing.exists:
        return _envelope({"report_id": report_id, **(existing.to_dict() or {})})
    now = _now()
    record = {
        "report_id": report_id, "report_type": body.report_type, "run_id": body.run_id,
        "estate_id": estate_id, "status": "queued", "requested_by": user.email,
        "justification": body.justification, "created_at": now, "updated_at": now,
        "progress": {"percent": 0, "stage": "queued"},
    }
    try:
        ref.create(record)
    except Exception as exc:
        if type(exc).__name__ != "AlreadyExists":
            raise
        raced = ref.get()
        return _envelope({"report_id": report_id, **(raced.to_dict() or record)})
    client.collection("operation_audit").document(str(__import__("uuid").uuid4())).set({
        "operation_id": report_id, "kind": "report.generate", "actor": user.email,
        "estate_id": estate_id, "run_id": body.run_id, "report_type": body.report_type,
        "justification": body.justification, "event": "queued", "recorded_at": now,
    })
    background_tasks.add_task(generate_background, report_id)
    return _envelope(record)


@router.get("/reports/latest", response_model=Envelope)
def latest_report(
    run_id: str = Query(min_length=2, max_length=200),
    report_type: Literal["assessment", "run_evidence", "reconciliation", "approval_audit"] = Query(),
    user: UserContext = Depends(get_user_context),
) -> dict:
    """Restore the most recent authorized artifact after page navigation.

    Report generation is asynchronous and the client may be closed or moved
    to another route while it runs. Keeping the report id only in component
    state made completed artifacts effectively disappear from the console.
    """
    try:
        run = get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    estate_id = run.get("estate_id") or DEFAULT_ESTATE_ID
    authorize_estate(user, estate_id, "viewer")
    reports = []
    for snapshot in get_client().collection("report_artifacts").limit(250).stream():
        record = snapshot.to_dict() or {}
        if record.get("run_id") == run_id and record.get("report_type") == report_type:
            reports.append({"report_id": snapshot.id, **record})
    reports.sort(
        key=lambda item: item.get("completed_at") or item.get("created_at") or "",
        reverse=True,
    )
    return _envelope(reports[0] if reports else None)


@router.get("/reports/{report_id}", response_model=Envelope)
def report_status(report_id: str, user: UserContext = Depends(get_user_context)) -> dict:
    snapshot = get_client().collection("report_artifacts").document(report_id).get()
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="Report not found.")
    report = {"report_id": snapshot.id, **(snapshot.to_dict() or {})}
    authorize_estate(user, report.get("estate_id") or DEFAULT_ESTATE_ID, "viewer")
    # The complete snapshot is available through the authenticated JSON
    # download; status polling stays compact.
    report.pop("snapshot", None)
    return _envelope(report)


@router.get("/reports/{report_id}/download")
def report_download(
    report_id: str,
    format: Literal["pdf", "json"] = Query(default="pdf"),
    user: UserContext = Depends(get_user_context),
) -> Response:
    snapshot = get_client().collection("report_artifacts").document(report_id).get()
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="Report not found.")
    report = {"report_id": snapshot.id, **(snapshot.to_dict() or {})}
    authorize_estate(user, report.get("estate_id") or DEFAULT_ESTATE_ID, "viewer")
    if report.get("status") != "ready":
        raise HTTPException(status_code=409, detail=f"Report is {report.get('status', 'not ready')}.")
    from frontend.report_service import download

    try:
        data, content_type, filename = download(report, format)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Report artifact is unavailable: {exc}") from exc
    return Response(
        content=data,
        media_type=content_type,
        headers={
            # The page fetches this with a Firebase Authorization header and
            # downloads the resulting Blob. Chromium may perform a separate
            # security recheck without custom headers; that request must stay
            # unauthorized rather than weakening artifact RBAC.
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-SHA256": report.get("pdf_sha256") if format == "pdf" else report.get("json_sha256", report.get("evidence_hash", "")),
        },
    )


# ---------------------------------------------------------------------------
# Read-only Gemini assistant
# ---------------------------------------------------------------------------


@router.post("/assistant/sessions", response_model=Envelope, status_code=status.HTTP_201_CREATED)
def assistant_create_session(
    body: AssistantSessionRequest,
    user: UserContext = Depends(require_role("viewer")),
) -> dict:
    if not _feature_enabled("ENABLE_AI_ASSISTANT"):
        raise HTTPException(status_code=503, detail="The AI assistant is not enabled on this deployment.")
    authorize_estate(user, body.estate_id, "viewer")
    if body.run_id:
        try:
            run = get_run(body.run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if (run.get("estate_id") or DEFAULT_ESTATE_ID) != body.estate_id:
            raise HTTPException(status_code=422, detail="The selected run does not belong to the active estate.")
    from frontend.assistant_service import create_session

    return _envelope(create_session(
        uid=user.uid, email=user.email, estate_id=body.estate_id, route=body.route, run_id=body.run_id,
    ))


def _owned_assistant_session(session_id: str, user: UserContext) -> dict:
    from frontend.assistant_service import get_session

    session_record = get_session(session_id)
    if not session_record:
        raise HTTPException(status_code=404, detail="Assistant session not found.")
    if session_record.get("uid") != user.uid:
        raise HTTPException(status_code=404, detail="Assistant session not found.")
    authorize_estate(user, session_record.get("estate_id") or DEFAULT_ESTATE_ID, "viewer")
    return session_record


@router.get("/assistant/sessions/{session_id}", response_model=Envelope)
def assistant_get_session(session_id: str, user: UserContext = Depends(get_user_context)) -> dict:
    session_record = _owned_assistant_session(session_id, user)
    from frontend.assistant_service import MESSAGE_COLLECTION
    messages = [
        {"id": snapshot.id, **(snapshot.to_dict() or {})}
        for snapshot in get_client().collection("assistant_sessions").document(session_id).collection(MESSAGE_COLLECTION).stream()
    ]
    messages.sort(key=lambda item: item.get("created_at") or "")
    return _envelope({**session_record, "messages": messages})


@router.post("/assistant/sessions/{session_id}/messages")
def assistant_message(
    session_id: str,
    body: AssistantMessageRequest,
    user: UserContext = Depends(require_role("viewer")),
) -> StreamingResponse:
    if not _feature_enabled("ENABLE_AI_ASSISTANT"):
        raise HTTPException(status_code=503, detail="The AI assistant is not enabled on this deployment.")
    session_record = _owned_assistant_session(session_id, user)
    authorize_estate(user, session_record.get("estate_id") or DEFAULT_ESTATE_ID, "viewer")
    from frontend.assistant_service import stream_answer

    return StreamingResponse(
        stream_answer(session=session_record, question=body.question),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.delete("/assistant/sessions/{session_id}", response_model=Envelope)
def assistant_delete_session(
    session_id: str,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("viewer")),
) -> dict:
    session_record = _owned_assistant_session(session_id, user)
    authorize_estate(user, session_record.get("estate_id") or DEFAULT_ESTATE_ID, "viewer")
    from frontend.operations import _validated_key
    from frontend.assistant_service import delete_session

    try:
        _validated_key(idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    delete_session(session_id)
    get_client().collection("operation_audit").document(str(__import__("uuid").uuid4())).set({
        "kind": "assistant.session.delete", "actor": user.email,
        "estate_id": session_record.get("estate_id"), "session_id": session_id,
        "event": "applied", "recorded_at": _now(),
    })
    return _envelope({"session_id": session_id, "status": "deleted"})
