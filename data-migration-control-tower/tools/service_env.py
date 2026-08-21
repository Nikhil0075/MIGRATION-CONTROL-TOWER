"""Shared env-var parsing for the per-service Cloud Run entrypoints
(agents/*/service_main.py, Deploy & Harden Phase 2b/2c).

One tiny module rather than repeating this parsing in nine files. Every
entrypoint reads the same three variables so `gcloud run deploy` /
Terraform can set them uniformly across services rather than each one
inventing its own env var name.
"""

from __future__ import annotations

import os


def service_audience() -> str | None:
    """The OIDC audience this service's own deployed URL expects — set by
    the deploy step to the service's own Cloud Run URL. None (permissive,
    audience unchecked) when unset, which is correct for local dev/tests
    and WRONG for a real deployment — see
    tools/capability_http_server.py::build_capability_app()'s docstring."""
    return os.environ.get("SERVICE_AUDIENCE") or None


def allowed_caller_service_accounts() -> list[str]:
    """Comma-separated list of service-account emails permitted to call
    this service — e.g. the orchestrator's SA, or `sa-pubsub-invoker` for
    push-delivered consumers. Empty (permissive — any verified caller) when
    unset, same local-dev-only caveat as service_audience()."""
    raw = os.environ.get("ALLOWED_CALLER_SERVICE_ACCOUNTS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]
