"""Calling side of typed HTTP capability dispatch (Deploy & Harden
Phase 2c, docs/adr/0002-typed-http-dispatch.md).

Used by tools/registry.py::invoke_capability()'s `cloud_run` runtime
branch — a resolved AgentCard whose `runtime.type == "cloud_run"` calls
through here instead of dynamically importing the handler.
"""

from __future__ import annotations

import logging

from tools.capability_dispatch import CapabilityRequest, CapabilityResponse, build_payload

logger = logging.getLogger("capability_dispatch_client")

#: Cloud Run request timeouts can be long (this project's SLA declares
#: p95_latency_ms up to 45s per AgentCard, seed_registry.py), but a
#: dispatch call must not hang forever if a service is genuinely stuck.
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2


class RemoteCapabilityError(RuntimeError):
    """The remote service ran the handler and it raised — carries the
    structured error type/message from CapabilityErrorResponse rather
    than just an HTTP status code."""

    def __init__(self, error_type: str, error_message: str):
        self.error_type = error_type
        self.error_message = error_message
        super().__init__(f"remote capability call failed: {error_type}: {error_message}")


class RemoteCapabilityUnreachable(RuntimeError):
    """Transport-level failure (timeout, connection refused, non-2xx with
    no structured body, auth failure) — distinct from RemoteCapabilityError
    so a caller can tell "the handler failed" from "we couldn't even
    reach it," which usually call for different responses (retry vs.
    escalate to on-call)."""


def _fetch_identity_token(audience: str) -> str:
    """Fetches an OIDC identity token for `audience` using the runtime's
    own credentials (Cloud Run's attached service account, or local ADC
    in dev). Lazy import — same Rung-2 pattern as every other optional
    GCP dependency in this codebase."""
    import google.auth.transport.requests
    from google.oauth2 import id_token

    request = google.auth.transport.requests.Request()
    return id_token.fetch_id_token(request, audience)


def invoke_remote_capability(
    *,
    service_url: str,
    capability: str,
    args: list,
    kwargs: dict,
    run_id: str | None = None,
    estate_id: str | None = None,
    trace_id: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
):
    """POSTs a typed CapabilityRequest to `service_url`/invoke and returns
    the handler's result (unwrapped — the caller sees the same value a
    local dynamic-import call would have returned).

    Retries transport-level failures (timeout, connection error) up to
    `max_retries` times with the SAME invocation_id, so a retried call is
    naturally deduped server-side (tools/idempotency.py) rather than
    risking a double side effect. Does NOT retry a RemoteCapabilityError
    (the handler ran and raised) — retrying that would just re-run
    something that already failed for a reason unrelated to the network.
    """
    import requests as http_requests

    request = CapabilityRequest(
        capability=capability,
        run_id=run_id,
        estate_id=estate_id,
        trace_id=trace_id,
        payload=build_payload(*args, **kwargs),
    )

    token = _fetch_identity_token(service_url)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = http_requests.post(
                f"{service_url.rstrip('/')}/invoke",
                data=request.model_dump_json(),
                headers=headers,
                timeout=timeout_seconds,
            )
        except http_requests.RequestException as exc:
            last_exc = exc
            logger.warning(
                "capability %s: transport failure on attempt %d/%d: %s",
                capability, attempt + 1, max_retries + 1, exc,
            )
            continue

        if response.status_code == 200:
            parsed = CapabilityResponse.model_validate(response.json())
            return parsed.result

        if response.status_code == 500:
            body = response.json().get("detail", {})
            raise RemoteCapabilityError(
                body.get("error_type", "UnknownError"), body.get("error_message", response.text)
            )

        # 401/403/404/413/etc — a transport/protocol-level refusal, not
        # something a retry with the same body would fix.
        raise RemoteCapabilityUnreachable(
            f"{service_url}/invoke returned {response.status_code}: {response.text[:500]}"
        )

    raise RemoteCapabilityUnreachable(
        f"{service_url}/invoke unreachable after {max_retries + 1} attempts: {last_exc}"
    )
