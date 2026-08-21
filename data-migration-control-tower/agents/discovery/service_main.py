"""Cloud Run entrypoint for the Discovery agent service (Deploy & Harden
Phase 2b). Serves the `discovery.catalog.estate` capability over typed
HTTP (tools/capability_http_server.py) and the `assessment` Pub/Sub push
consumer (tools/pubsub_push_server.py, mounted under /push/assessment) —
the same combination `tools/worker_supervisor.py::default_specs()`'s
"assessment" entry and the registry's "discovery.catalog.estate"
capability represent today, just as two routes on one deployed service
instead of two threads in the shared supervisor process.

Run locally: uvicorn agents.discovery.service_main:app --port 8080
Deploy: gcloud run deploy discovery-agent --source . \
    --dockerfile agents/discovery/Dockerfile --region us-central1 \
    --service-account sa-discovery@PROJECT_ID.iam.gserviceaccount.com \
    --no-allow-unauthenticated
"""

from __future__ import annotations

from agents.discovery.agent import discover_estate
from agents.discovery.run_assessment_worker import handle_assessment_requested
from tools.capability_http_server import build_capability_app
from tools.pubsub_push_server import build_push_app
from tools.service_env import allowed_caller_service_accounts, service_audience

app = build_capability_app(
    service_name="discovery-agent",
    handlers={"discovery.catalog.estate": discover_estate},
    audience=service_audience(),
    allowed_caller_service_accounts=allowed_caller_service_accounts(),
)

_assessment_push_app = build_push_app(
    consumer_name="assessment",
    handler=handle_assessment_requested,
    audience=service_audience(),
    allowed_caller_service_accounts=allowed_caller_service_accounts(),
)
app.mount("/push/assessment", _assessment_push_app)
