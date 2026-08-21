"""Cloud Run entrypoint for the Lineage agent service (Deploy & Harden
Phase 2b). Serves only the `lineage.graph.build` capability over typed
HTTP — Lineage has no Pub/Sub consumer of its own (it's invoked directly
by the orchestrator via tools/registry.py::invoke_capability(), not a
subscription in tools/worker_supervisor.py::default_specs()).

Run locally: uvicorn agents.lineage.service_main:app --port 8080
Deploy: gcloud run deploy lineage-agent --source . \
    --dockerfile agents/lineage/Dockerfile --region us-central1 \
    --service-account sa-lineage@PROJECT_ID.iam.gserviceaccount.com \
    --no-allow-unauthenticated
"""

from __future__ import annotations

from agents.lineage.agent import build_dependency_graph
from tools.capability_http_server import build_capability_app
from tools.service_env import allowed_caller_service_accounts, service_audience

app = build_capability_app(
    service_name="lineage-agent",
    handlers={"lineage.graph.build": build_dependency_graph},
    audience=service_audience(),
    allowed_caller_service_accounts=allowed_caller_service_accounts(),
)
