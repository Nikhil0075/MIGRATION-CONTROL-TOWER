"""Typed HTTP dispatch envelope for distributed agent-to-agent calls
(Deploy & Harden Phase 2c, docs/adr/0002-typed-http-dispatch.md).

Shared by tools/capability_dispatch_client.py (the caller — a `cloud_run`
runtime branch of tools/registry.py::invoke_capability()) and
tools/capability_http_server.py (the receiver — one FastAPI app per
deployed agent service). Neither imports the other; both import this, so
the envelope shape can never drift between the two sides of the wire.

This replaces a first-draft design that serialized `*args, **kwargs`
directly into an HTTP body — rejected because it had no schema, no
capability allowlist, and no bound on what a payload could contain
(including, by accident, a credential value). Every field below exists
because a first-draft security/reliability gap named it specifically —
see docs/adr/0002 for which gap each one closes.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

ENVELOPE_SCHEMA_VERSION = "1.0"

#: Starlette's default has no cap; a compromised/misbehaving caller could
#: otherwise send an arbitrarily large body. 1 MiB is generous for this
#: project's payloads (capability args are ids/paths/small dicts, never
#: row data — bulk data moves through the data-plane executor, not this
#: envelope).
MAX_PAYLOAD_BYTES = 1024 * 1024


class CapabilityRequest(BaseModel):
    """The typed envelope a `cloud_run`-runtime capability call sends.

    `payload` carries the handler's actual arguments as
    {"args": [...], "kwargs": {...}} — matching invoke_capability()'s own
    `handler(*args, **kwargs)` calling convention, so a capability's
    handler function does not need two different call shapes depending
    on whether it's reached locally (dynamic import) or remotely (HTTP).
    Pydantic validates the envelope's own shape; the payload's *inner*
    shape is handler-specific and validated by the handler itself, same
    as it always was for the local dynamic-import path.
    """

    capability: str
    invocation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str | None = None
    estate_id: str | None = None
    trace_id: str | None = None
    input_schema_version: str = ENVELOPE_SCHEMA_VERSION
    payload: dict[str, Any] = Field(default_factory=dict)

    def args(self) -> list[Any]:
        return list(self.payload.get("args") or [])

    def kwargs(self) -> dict[str, Any]:
        return dict(self.payload.get("kwargs") or {})


class CapabilityResponse(BaseModel):
    """What the receiving service returns. `result` is whatever the
    handler returned, JSON-serialized — same shape a local dynamic-import
    call would have produced, so a caller that switches a card from
    `local` to `cloud_run` sees the same return value either way."""

    invocation_id: str
    output_schema_version: str = ENVELOPE_SCHEMA_VERSION
    result: Any = None


class CapabilityErrorResponse(BaseModel):
    """A structured failure — distinct from a bare HTTP 500 body, so a
    caller can distinguish "the handler raised" from "the transport
    failed" and report the actual exception type/message rather than an
    opaque request failure."""

    invocation_id: str
    error_type: str
    error_message: str


def build_payload(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """The inverse of CapabilityRequest.args()/kwargs() — used by the
    client side to build a request from the same *args, **kwargs shape
    invoke_capability() already accepts, so callers of invoke_capability()
    never need to know whether the resolved card is local or remote."""
    return {"args": list(args), "kwargs": kwargs}
