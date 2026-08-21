"""Cloud Run entrypoint for the Orchestrator service (Deploy & Harden
Phase 2b) — the state-machine driver. Owns 7 of
tools/worker_supervisor.py::default_specs()'s 9 consumer subscriptions
(everything except `assessment`, owned by the Discovery service, and
`cutover`, owned by the Cutover service — see docs/ARCHITECTURE.md's
"corrected service topology"), each mounted at its own push path so
Pub/Sub subscriptions can target them independently:

    /push/migration    -> handle_migration_requested
    /push/discovery     -> handle_discovery_completed
    /push/risk          -> handle_risk_assessed
    /push/plan          -> handle_planned
    /push/validation    -> handle_validation_requested
    /push/approval      -> handle_validation_passed
    /push/recovery      -> handle_validation_failed

Every handler here still dispatches to the other agent services via
tools/registry.py::invoke_capability() — under `cloud_run`-runtime
AgentCards (Deploy & Harden Phase 2c), that's now a typed HTTP call
under the orchestrator's OWN service account, not a dynamic import
running as if it were the target agent. `local`-runtime cards (not yet
migrated) keep working exactly as before.

Run locally: uvicorn agents.orchestrator.service_main:app --port 8080
Deploy: gcloud run deploy orchestrator --source . \
    --dockerfile agents/orchestrator/Dockerfile --region us-central1 \
    --service-account sa-orchestrator@PROJECT_ID.iam.gserviceaccount.com \
    --no-allow-unauthenticated
"""

from __future__ import annotations

from fastapi import FastAPI

from agents.orchestrator import orchestrator as orch
from tools.pubsub_push_server import build_push_app
from tools.service_env import allowed_caller_service_accounts, service_audience

app = FastAPI(title="orchestrator")

_CONSUMERS = {
    "migration": orch.handle_migration_requested,
    "discovery": orch.handle_discovery_completed,
    "risk": orch.handle_risk_assessed,
    "plan": orch.handle_planned,
    "validation": orch.handle_validation_requested,
    "approval": orch.handle_validation_passed,
    "recovery": orch.handle_validation_failed,
}

for _name, _handler in _CONSUMERS.items():
    _sub_app = build_push_app(
        consumer_name=_name,
        handler=_handler,
        audience=service_audience(),
        allowed_caller_service_accounts=allowed_caller_service_accounts(),
    )
    app.mount(f"/push/{_name}", _sub_app)


@app.get("/status")
def status() -> dict:
    return {"service": "orchestrator", "consumers": sorted(_CONSUMERS)}
