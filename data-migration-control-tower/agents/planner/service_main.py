"""Cloud Run entrypoint for the Planner agent service (Deploy & Harden
Phase 2b). Serves only the `planner.plan.propose` capability over typed
HTTP — see agents/lineage/service_main.py's docstring for why Planner
has no Pub/Sub consumer of its own.

Run locally: uvicorn agents.planner.service_main:app --port 8080
Deploy: gcloud run deploy planner-agent --source . \
    --dockerfile agents/planner/Dockerfile --region us-central1 \
    --service-account sa-planner@PROJECT_ID.iam.gserviceaccount.com \
    --no-allow-unauthenticated
"""

from __future__ import annotations

from agents.planner.agent import propose_plan
from tools.capability_http_server import build_capability_app
from tools.service_env import allowed_caller_service_accounts, service_audience

app = build_capability_app(
    service_name="planner-agent",
    handlers={"planner.plan.propose": propose_plan},
    audience=service_audience(),
    allowed_caller_service_accounts=allowed_caller_service_accounts(),
)
