"""Versioned API for the Oracle Redwood Migration Control Tower client."""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import re
import statistics
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from agents.orchestrator.run_lifecycle import RUN_COLLECTION, get_run, transition_state
from frontend.operations import get_operation, queue_operation, record_wave_override
from frontend.security import UserContext, authorize_estate, get_user_context, require_role
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

public_router = APIRouter(prefix="/api/v1", tags=["v1-public"])
router = APIRouter(prefix="/api/v1", tags=["v1"], dependencies=[Depends(require_role("viewer"))])


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


class StartAssessmentRequest(BaseModel):
    pack_id: str = Field(min_length=2, max_length=100)
    estate_id: str = Field(default="wwi-demo-estate", min_length=2, max_length=100)
    justification: str = Field(min_length=8, max_length=2000)


class StartRunRequest(BaseModel):
    pipeline_id: str = Field(default="wwi.sales.customers", min_length=2, max_length=200)
    execution_profile: Literal["wwi-default"] = "wwi-default"
    estate_id: str = Field(default=DEFAULT_ESTATE_ID, min_length=2, max_length=100)
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


class WaveOverrideRequest(BaseModel):
    state: Literal["HOLD", "OPEN"]
    justification: str = Field(min_length=8, max_length=2000)
    expires_at: str | None = None


class ApproveV1Request(BaseModel):
    justification: str = Field(min_length=5, max_length=2000)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _envelope(data: Any, *, total: int | None = None, next_cursor: str | None = None) -> dict:
    return {
        "data": data,
        "meta": {"generated_at": _now(), "freshness": "live", "total": total, "next_cursor": next_cursor},
    }


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
    """
    if estate_id is None:
        return records
    return [r for r in records if (r.get("estate_id") or DEFAULT_ESTATE_ID) == estate_id]


def _latest_run(runs: list[dict], *, mode: str | None = None) -> dict | None:
    return next((run for run in runs if mode is None or run.get("mode") == mode), None)


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

    return sorted(list_estate_documents(), key=lambda e: e.get("estate_id", ""))


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
        "latest_run_id": (latest or {}).get("run_id"),
        "last_run_at": (latest or {}).get("created_at"),
    }


@public_router.get("/config", response_model=Envelope)
def runtime_config() -> dict:
    firebase_config = _firebase_web_config()
    return _envelope(
        {
            "product_name": "Migration Control Tower",
            "build_version": os.environ.get("BUILD_VERSION", "development"),
            "poll_interval_ms": 10_000,
            "firebase": firebase_config,
            "authentication_configured": bool(firebase_config),
        }
    )


@public_router.get("/session", response_model=Envelope)
def session(user: UserContext = Depends(get_user_context)) -> dict:
    return _envelope({"uid": user.uid, "email": user.email, "roles": sorted(user.roles)})


@router.get("/overview", response_model=Envelope)
def overview(estate_id: str | None = Query(default=None)) -> dict:
    runs = _all_runs(200, estate_id=estate_id)
    latest = _latest_run(runs)
    client = get_client()
    operations = [d.to_dict() or {} for d in client.collection("operation_requests").limit(100).stream()]
    connection_snapshots = [d.to_dict() or {} for d in client.collection("connection_health").stream()]
    decisions = [d.to_dict() or {} for d in client.collection_group("policy_decisions").stream()]
    findings = _collection_docs(latest["run_id"], "risk_findings") if latest else []
    executions = _collection_docs(latest["run_id"], "migration_executions") if latest else []
    incidents = _collection_docs(latest["run_id"], "incidents") if latest else []
    approvals = _collection_docs(latest["run_id"], "approval_history") if latest else []
    wave_state = _wave_state(estate_id)
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
                "latest": latest,
                "migrated_percent": progress,
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
            "estimated_cost": Availability(status="not_configured", reason="Token and priced job usage are not yet recorded.").model_dump(),
            "actual_cost": Availability(
                status="stale" if os.environ.get("CLOUD_BILLING_EXPORT_TABLE") else "not_configured",
                reason=(
                    "Billing export is configured but no durable cost snapshot has been observed."
                    if os.environ.get("CLOUD_BILLING_EXPORT_TABLE")
                    else "CLOUD_BILLING_EXPORT_TABLE is not configured."
                ),
            ).model_dump(),
            "estimated_bytes": Availability(status="not_configured", reason="Source byte estimates are not recorded.").model_dump(),
        }
    )


@router.get("/estates", response_model=Envelope)
def estates(estate_id: str | None = Query(default=None)) -> dict:
    """Every registered estate, not a hardcoded list of one.

    Each estate is summarised against its OWN most recent run. Using the
    globally latest run (as this did before Day 11 Phase 4) would report
    zero objects for every estate except whichever one happened to run
    most recently.
    """
    runs = _all_runs(500)
    documents = _all_estates()
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
def estate_detail(estate_id: str) -> dict:
    from tools.connection_context import EstateNotFound

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
    from tools.adapters import build_adapter_for_binding
    from tools.adapters.base import AdapterCapabilityNotSupported
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

    if not binding.requires_connection:
        return _envelope({
            "status": "NOT_APPLICABLE",
            "detail": (
                f"Source {source_id!r} is a static-file source with no live server "
                f"to connect to; nothing to validate."
            ),
            "object_count": None,
            "latency_ms": 0,
        })

    try:
        adapter = build_adapter_for_binding(binding)
        result = adapter.health_check()
    except AdapterCapabilityNotSupported as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — a failed probe is a result, not a 500
        return _envelope({
            "status": "UNREACHABLE",
            "detail": f"{type(exc).__name__}: {exc}",
            "object_count": None,
            "latency_ms": None,
        })

    return _envelope({
        "status": result.get("status"),
        "detail": result.get("detail"),
        "object_count": result.get("object_count"),
        "latency_ms": result.get("latency_ms"),
        "last_observed_at": result.get("last_observed_at"),
    })


@router.get("/assessments", response_model=Envelope)
def assessments(estate_id: str | None = Query(default=None)) -> dict:
    runs = [run for run in _all_runs(200, estate_id=estate_id) if run.get("mode") == "assessment"]
    return _envelope({"runs": runs, "packs": _packs()}, total=len(runs))


@router.get("/waves", response_model=Envelope)
def waves(estate_id: str | None = Query(default=None)) -> dict:
    client = get_client()
    wave_state = _wave_state(estate_id)
    events = [d.to_dict() or {} for d in client.collection("wave_events").limit(200).stream()]
    overrides = [d.to_dict() or {} for d in client.collection("wave_overrides").stream()]
    operations = [
        d.to_dict() or {}
        for d in client.collection("operation_requests").limit(200).stream()
        if (d.to_dict() or {}).get("status") in {"queued", "published", "publish_failed"}
    ]
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
) -> dict:
    items = _all_runs(500, estate_id=estate_id)
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
    page = items[offset : offset + limit]
    next_cursor = _encode_cursor(offset + limit) if offset + limit < len(items) else None
    return _envelope(page, total=len(items), next_cursor=next_cursor)


@router.get("/runs/{run_id}", response_model=Envelope)
def run_detail(run_id: str) -> dict:
    try:
        run = get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
    )
    detail = {name: _collection_docs(run_id, name) for name in collections}
    return _envelope({"run": run, **detail})


@router.get("/lineage", response_model=Envelope)
def lineage(run_id: str | None = None, estate_id: str | None = Query(default=None)) -> dict:
    selected = run_id or ((_latest_run(_all_runs(50, estate_id=estate_id)) or {}).get("run_id"))
    if not selected:
        return _envelope({"run_id": None, "nodes": [], "edges": []})
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
    return _envelope({"run_id": selected, "nodes": nodes, "edges": edges})


@router.get("/reconciliation", response_model=Envelope)
def reconciliation(
    limit: int = Query(default=200, ge=1, le=500),
    estate_id: str | None = Query(default=None),
) -> dict:
    rows = []
    for run in _all_runs(100, estate_id=estate_id):
        for check in _collection_docs(run["run_id"], "reconciliation"):
            rows.append({"run_id": run["run_id"], "pipeline_id": run.get("pipeline_id"), **check})
            if len(rows) >= limit:
                return _envelope(rows, total=len(rows))
    return _envelope(rows, total=len(rows))


@router.get("/policies", response_model=Envelope)
def policies(estate_id: str | None = Query(default=None)) -> dict:
    client = get_client()
    decisions = [d.to_dict() or {} for d in client.collection_group("policy_decisions").stream()]
    approvals = []
    for run in _all_runs(100, estate_id=estate_id):
        approvals.extend({"run_id": run["run_id"], **item} for item in _collection_docs(run["run_id"], "approval_history"))
    decisions.sort(key=lambda item: item.get("decided_at", ""), reverse=True)
    approvals.sort(key=lambda item: item.get("recorded_at", ""), reverse=True)
    return _envelope({"decisions": decisions[:500], "approvals": approvals[:500]})


@router.get("/agents", response_model=Envelope)
def agents() -> dict:
    cards = [doc.to_dict() or {} for doc in get_client().collection_group("versions").stream()]
    cards.sort(key=lambda item: (item.get("agent_id", ""), item.get("version", "")))
    pinned: dict[str, int] = {}
    for run in _all_runs(200):
        for agent_id in (run.get("pinned_agents") or {}):
            pinned[agent_id] = pinned.get(agent_id, 0) + 1
    return _envelope({"cards": cards, "pinned_run_counts": pinned}, total=len(cards))


@router.get("/evaluations", response_model=Envelope)
def evaluations() -> dict:
    client = get_client()
    evaluation_runs = [d.to_dict() or {} for d in client.collection("evaluation_runs").limit(100).stream()]
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
def system_health() -> dict:
    client = get_client()
    latest = _latest_run(_all_runs(10))
    processed = [d.to_dict() or {} for d in client.collection("processed_messages").limit(100).stream()]
    connections = [d.to_dict() or {} for d in client.collection("connection_health").stream()]
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


@router.post("/runs", response_model=Envelope, status_code=status.HTTP_202_ACCEPTED)
def start_run(
    body: StartRunRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    user: UserContext = Depends(require_role("operator")),
) -> dict:
    authorize_estate(user, body.estate_id, "operator")
    return _queue(
        user=user,
        kind="migration.start",
        key=idempotency_key,
        justification=body.justification,
        topic="migration.requested",
        event={
            "pipeline_id": body.pipeline_id,
            "execution_profile": body.execution_profile,
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
    from tools.wave_manager import estate_of

    authorize_estate(user, estate_of(source_id), "operator")
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
        event={"source_id": source_id, "state": body.state, "expires_at": body.expires_at},
    )
    if operation["data"].get("idempotent_replay"):
        return operation
    record = record_wave_override(
        source_id=source_id,
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
