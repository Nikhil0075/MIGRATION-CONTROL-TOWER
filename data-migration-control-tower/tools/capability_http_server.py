"""Receiving side of typed HTTP capability dispatch (Deploy & Harden
Phase 2c, docs/adr/0002-typed-http-dispatch.md).

One FastAPI app per deployed agent service, built by build_capability_app()
and exposing exactly the capabilities that service's AgentCard(s)
advertise. Every request is checked, in order:

  1. Request body size (MAX_PAYLOAD_BYTES, tools/capability_dispatch.py)
  2. OIDC token: audience + issuer verified, caller's service-account
     email checked against an explicit allowlist — verify_caller_identity()
     is a MODULE-LEVEL dependency (not a closure) specifically so tests
     can override it via `app.dependency_overrides[verify_caller_identity]`,
     the same pattern frontend/app.py's own auth already uses.
  3. Capability allowlist — this service only serves what it was built
     to serve; an unknown capability is a 404, not a dynamic-import
     attempt (there is no dynamic import on this path at all).
  4. Pydantic request/response schema validation (automatic via FastAPI).
  5. Idempotency (tools/idempotency.py, keyed by invocation_id) — a
     redelivered/retried call with the same invocation_id returns the
     cached result rather than redoing the work.

No credential values are ever expected in the payload — secrets are
resolved server-side (tools/secret_resolver.py) by reference, never
passed over this wire.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from tools.capability_dispatch import (
    MAX_PAYLOAD_BYTES,
    CapabilityErrorResponse,
    CapabilityRequest,
    CapabilityResponse,
)

logger = logging.getLogger("capability_http_server")

IDEMPOTENCY_COLLECTION = "capability_invocations"


def verify_caller_identity(request: Request, authorization: str | None = Header(default=None)) -> str:
    """Verifies the incoming OIDC token and returns the caller's
    service-account email, or raises 401/403.

    Reads its configuration off `request.app.state` (set by
    build_capability_app()) rather than closing over it, so this exact
    function object is importable and override-able in tests —
    `app.dependency_overrides[verify_caller_identity] = lambda: "..."` —
    without needing access to a per-app closure.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing Bearer token")
    token = authorization[len("Bearer "):]

    audience = request.app.state.audience
    allowed_callers = request.app.state.allowed_caller_service_accounts

    # Lazy import: this module is imported by tools/registry.py indirectly
    # via card resolution paths that don't always need OIDC verification
    # (e.g. local-only test runs) — same Rung-2 pattern as
    # tools/secret_resolver.py's Secret Manager import.
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)
    except Exception as exc:  # noqa: BLE001 — any verification failure is a 401, not a 500
        raise HTTPException(status_code=401, detail=f"invalid OIDC token: {exc}") from exc

    caller_email = claims.get("email")
    if allowed_callers and caller_email not in allowed_callers:
        raise HTTPException(status_code=403, detail=f"caller {caller_email!r} is not on the allowlist for this service")
    return caller_email


async def _enforce_body_size_limit(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > MAX_PAYLOAD_BYTES:
        from starlette.responses import JSONResponse

        return JSONResponse(
            status_code=413,
            content={"detail": f"request body exceeds {MAX_PAYLOAD_BYTES} byte limit"},
        )
    return await call_next(request)


def build_capability_app(
    *,
    service_name: str,
    handlers: dict[str, Callable[..., Any]],
    audience: str | None = None,
    allowed_caller_service_accounts: list[str] | None = None,
) -> FastAPI:
    """Builds the FastAPI app for one deployed agent service.

    handlers: {capability_string: function} — the exact same handler
    functions the `local` dynamic-import path already calls (e.g.
    `{"discovery.catalog.estate": agents.discovery.agent.discover_estate}`)
    — reused unchanged, not reimplemented for the HTTP path.

    audience/allowed_caller_service_accounts default to permissive when
    None (audience unchecked, any caller allowed) ONLY so this can be
    unit-tested and run locally without live OIDC infrastructure —
    a REAL deployment must set both via env vars at startup (see the
    per-agent push_main.py entrypoints) or every caller is trusted,
    which defeats the point of this file.
    """
    app = FastAPI(title=f"{service_name} capability service")
    app.state.audience = audience
    app.state.allowed_caller_service_accounts = set(allowed_caller_service_accounts or [])
    app.middleware("http")(_enforce_body_size_limit)

    @app.get("/status")
    def status() -> dict:
        return {"service": service_name, "capabilities": sorted(handlers)}

    @app.post("/invoke", response_model=CapabilityResponse)
    def invoke(req: CapabilityRequest, caller: str = Depends(verify_caller_identity)) -> CapabilityResponse:
        if req.capability not in handlers:
            raise HTTPException(
                status_code=404,
                detail=f"{service_name!r} does not serve capability {req.capability!r} (has: {sorted(handlers)})",
            )

        from tools import idempotency

        status_, cached = idempotency.claim(IDEMPOTENCY_COLLECTION, req.invocation_id, actor=service_name)
        if status_ == "done":
            # Unwrap the same way the fresh path wraps below, so a
            # deduped replay returns the identical shape the original
            # call did — a caller must not be able to tell, from the
            # response shape alone, whether this was a cache hit.
            logger.info("invocation %s already completed — returning cached result", req.invocation_id)
            return CapabilityResponse(invocation_id=req.invocation_id, result=cached.get("value"))

        handler = handlers[req.capability]
        try:
            result = handler(*req.args(), **req.kwargs())
        except Exception as exc:  # noqa: BLE001 — turned into a structured error response, not a bare 500
            logger.exception("capability %s failed for invocation %s", req.capability, req.invocation_id)
            raise HTTPException(
                status_code=500,
                detail=CapabilityErrorResponse(
                    invocation_id=req.invocation_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:2000],
                ).model_dump(),
            ) from exc

        idempotency.complete(IDEMPOTENCY_COLLECTION, req.invocation_id, actor=service_name, result={"value": result})
        return CapabilityResponse(invocation_id=req.invocation_id, result=result)

    return app
