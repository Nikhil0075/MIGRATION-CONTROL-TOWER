"""Cloud Run entrypoint for the Finance Reporting Impact agent service
(Deploy & Harden Phase 2b). Serves only the
`impact.assessment.finance_reporting` capability over typed HTTP — the
cross-department agent (master doc §26.2), discovered purely via
capability wildcard, gets exactly the same independent deployment as
every other agent rather than a special case.

Run locally: uvicorn agents.finance.service_main:app --port 8080
Deploy: gcloud run deploy finance-impact-agent --source . \
    --dockerfile agents/finance/Dockerfile --region us-central1 \
    --service-account sa-finance-impact@PROJECT_ID.iam.gserviceaccount.com \
    --no-allow-unauthenticated
"""

from __future__ import annotations

from agents.finance.impact_agent import assess_impact
from tools.capability_http_server import build_capability_app
from tools.service_env import allowed_caller_service_accounts, service_audience

app = build_capability_app(
    service_name="finance-impact-agent",
    handlers={"impact.assessment.finance_reporting": assess_impact},
    audience=service_audience(),
    allowed_caller_service_accounts=allowed_caller_service_accounts(),
)
