"""Cloud Run entrypoint for the Validation agent service (Deploy &
Harden Phase 2b). Serves only the `validation.reconcile.source_target`
capability over typed HTTP. Note: the "validation" name in
tools/worker_supervisor.py::default_specs() is the ORCHESTRATOR's own
`handle_validation_requested` consumer (which itself calls this
capability), not a consumer this service owns — see
agents/orchestrator/service_main.py.

Run locally: uvicorn agents.validation.service_main:app --port 8080
Deploy: gcloud run deploy validation-agent --source . \
    --dockerfile agents/validation/Dockerfile --region us-central1 \
    --service-account sa-validation@PROJECT_ID.iam.gserviceaccount.com \
    --no-allow-unauthenticated
"""

from __future__ import annotations

from agents.validation.agent import run_reconciliation
from tools.capability_http_server import build_capability_app
from tools.service_env import allowed_caller_service_accounts, service_audience

app = build_capability_app(
    service_name="validation-agent",
    handlers={"validation.reconcile.source_target": run_reconciliation},
    audience=service_audience(),
    allowed_caller_service_accounts=allowed_caller_service_accounts(),
)
