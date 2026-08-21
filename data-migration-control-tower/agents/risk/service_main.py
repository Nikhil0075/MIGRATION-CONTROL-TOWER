"""Cloud Run entrypoint for the Risk agent service (Deploy & Harden
Phase 2b). Serves only the `risk.assess.estate` capability over typed
HTTP — see agents/lineage/service_main.py's docstring for why Risk has
no Pub/Sub consumer of its own.

Run locally: uvicorn agents.risk.service_main:app --port 8080
Deploy: gcloud run deploy risk-agent --source . \
    --dockerfile agents/risk/Dockerfile --region us-central1 \
    --service-account sa-risk@PROJECT_ID.iam.gserviceaccount.com \
    --no-allow-unauthenticated
"""

from __future__ import annotations

from agents.risk.agent import classify_estate
from tools.capability_http_server import build_capability_app
from tools.service_env import allowed_caller_service_accounts, service_audience

app = build_capability_app(
    service_name="risk-agent",
    handlers={"risk.assess.estate": classify_estate},
    audience=service_audience(),
    allowed_caller_service_accounts=allowed_caller_service_accounts(),
)
