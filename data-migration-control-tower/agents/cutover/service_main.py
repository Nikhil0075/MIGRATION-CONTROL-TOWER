"""Cloud Run entrypoint for the Cutover agent service (Deploy & Harden
Phase 2b). Serves the `cutover.request_approval` capability over typed
HTTP and the `cutover` Pub/Sub push consumer (mounted under
/push/cutover) — see agents/discovery/service_main.py's docstring for
the pattern this follows.

Run locally: uvicorn agents.cutover.service_main:app --port 8080
Deploy: gcloud run deploy cutover-agent --source . \
    --dockerfile agents/cutover/Dockerfile --region us-central1 \
    --service-account sa-cutover@PROJECT_ID.iam.gserviceaccount.com \
    --no-allow-unauthenticated
"""

from __future__ import annotations

from agents.cutover.agent import request_approval
from agents.cutover.run_cutover_worker import handle_cutover_approved
from tools.capability_http_server import build_capability_app
from tools.pubsub_push_server import build_push_app
from tools.service_env import allowed_caller_service_accounts, service_audience

app = build_capability_app(
    service_name="cutover-agent",
    handlers={"cutover.request_approval": request_approval},
    audience=service_audience(),
    allowed_caller_service_accounts=allowed_caller_service_accounts(),
)

_cutover_push_app = build_push_app(
    consumer_name="cutover",
    handler=handle_cutover_approved,
    audience=service_audience(),
    allowed_caller_service_accounts=allowed_caller_service_accounts(),
)
app.mount("/push/cutover", _cutover_push_app)
