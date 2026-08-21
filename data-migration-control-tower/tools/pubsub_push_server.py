"""Pub/Sub push wrapper for event consumers (Deploy & Harden Phase 2b,
docs/adr/0002-typed-http-dispatch.md's companion piece for event-driven
dispatch rather than direct agent-to-agent calls).

Wraps an existing, UNMODIFIED tools/worker_supervisor.py::ConsumerSpec
handler as a Cloud Run HTTP endpoint that Pub/Sub can push to, per the
standard Pub/Sub-push-to-Cloud-Run pattern:
`--push-auth-service-account` on the subscription, `--no-allow-unauthenticated`
on the service, an OIDC token Pub/Sub attaches to every push request.

Replaces tools/worker_supervisor.py's manual ack()/nack()/_LeaseHeartbeat
machinery for any consumer migrated to push delivery — a 200 response IS
the ack, a non-2xx IS the nack-and-redeliver, and Cloud Run's own request
timeout (up to 3600s) replaces the 60s-ack-deadline-plus-heartbeat problem
that machinery exists to solve. WorkerSupervisor itself stays in the
codebase unchanged for any consumer not yet split onto its own service —
see docs/ARCHITECTURE.md.

Idempotency (`_dedup_claim`-equivalent) still applies: Pub/Sub push
delivery is at-least-once, exactly like pull — the SAME payload shape
tools/events.py::pull() already produces (including `_pubsub_message_id`)
is reconstructed here so the handler's own dedup logic
(agents/orchestrator/orchestrator.py::_dedup_claim, keyed on that field)
works completely unchanged.
"""

from __future__ import annotations

import base64
import json
import logging

from fastapi import Depends, FastAPI, HTTPException

from tools.capability_dispatch import MAX_PAYLOAD_BYTES
from tools.capability_http_server import _enforce_body_size_limit, verify_caller_identity

logger = logging.getLogger("pubsub_push_server")


def _decode_push_envelope(body: dict) -> dict:
    """Turns a Pub/Sub push HTTP body into the same payload shape
    tools/events.py::pull() already returns from a pull request — so a
    handler function needs no changes to run under either transport.

    Push envelope shape (Pub/Sub's own, not this project's design):
    {"message": {"data": "<base64>", "messageId": "...", "attributes": {}},
     "subscription": "projects/.../subscriptions/..."}
    """
    message = body.get("message") or {}
    data = message.get("data")
    if not data:
        raise HTTPException(status_code=400, detail="push envelope has no message.data")
    try:
        decoded_bytes = base64.b64decode(data)
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — a malformed push body is a 400, not a 500
        raise HTTPException(status_code=400, detail=f"could not decode push message data: {exc}") from exc

    message_id = message.get("messageId") or message.get("message_id")
    if message_id:
        # Same field name tools/events.py::pull() attaches — this is what
        # makes _dedup_claim()/_dedup_complete() work unchanged regardless
        # of which transport delivered the message.
        payload["_pubsub_message_id"] = message_id
    return payload


def build_push_app(
    *,
    consumer_name: str,
    handler,
    audience: str | None = None,
    allowed_caller_service_accounts: list[str] | None = None,
) -> FastAPI:
    """Builds the Cloud Run push endpoint for one consumer.

    `handler` is the exact, unmodified function from
    tools/worker_supervisor.py::default_specs() (e.g.
    `orchestrator.handle_migration_requested`) — reused, not reimplemented.

    audience/allowed_caller_service_accounts default permissive (None)
    only for local testing/dev, same caveat as
    tools/capability_http_server.py::build_capability_app() — a real
    deployment MUST set both.
    """
    app = FastAPI(title=f"{consumer_name} push consumer")
    app.state.audience = audience
    app.state.allowed_caller_service_accounts = set(allowed_caller_service_accounts or [])
    app.middleware("http")(_enforce_body_size_limit)

    @app.get("/status")
    def status() -> dict:
        return {"consumer": consumer_name}

    @app.post("/")
    def push(body: dict, caller: str = Depends(verify_caller_identity)) -> dict:
        payload = _decode_push_envelope(body)
        try:
            result = handler(payload)
        except Exception as exc:  # noqa: BLE001 — non-2xx is what triggers Pub/Sub redelivery
            logger.exception(
                "%s failed handling message %s", consumer_name, payload.get("_pubsub_message_id")
            )
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
        return {"status": "ok", "consumer": consumer_name, "result": result if isinstance(result, dict) else None}

    return app


__all__ = ["build_push_app", "MAX_PAYLOAD_BYTES"]
