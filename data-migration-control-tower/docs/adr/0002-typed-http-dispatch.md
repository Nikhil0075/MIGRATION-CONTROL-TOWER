# ADR 0002: Typed HTTP dispatch for distributed agents

## Status
Accepted — Deploy & Harden Phase 2.

## Context
Today `tools/registry.py::invoke_capability()` resolves a capability to an APPROVED AgentCard and
dynamically imports and calls its `handler` string (`module:function`) in-process. This is how
every agent currently runs — inside one orchestrator process, under one identity, regardless of
which of the 7 per-agent IAM service accounts `infrastructure/gcp_setup.sh` declared for it.

Making the fleet genuinely distributed (separate Cloud Run services, separate failure domains,
real per-agent workload identity) means `invoke_capability()` needs a second dispatch path that
calls out over the network instead of importing code. The naive version of that — serializing
`*args, **kwargs` into an HTTP body and calling `handler(*args, **kwargs)` on the receiving end —
was rejected: it has no schema, no capability allowlist, no size/deadline bounds, and risks
credential values leaking into a payload never designed to carry them.

## Decision
Extend `AgentCard.runtime` (`contracts/metadata_model.json`) with a `cloud_run` variant alongside
today's `local` variant. A `cloud_run` card's `invoke_capability()` path sends a versioned,
schema-checked envelope:

```json
{"capability": "discovery.catalog.estate", "invocation_id": "...", "run_id": "...",
 "estate_id": "...", "trace_id": "...", "input_schema_version": "1.0", "payload": {}}
```

The receiving service (`tools/capability_http_server.py` for direct calls,
`tools/pubsub_push_server.py` for event-consumer calls) enforces, on every request: a capability
allowlist, Pydantic request/result schema validation, OIDC audience+issuer verification, a
caller-service-account allowlist, a request size limit, a deadline, an idempotency key, trace
propagation, and output-schema validation. Secrets are never passed in the payload — they're
resolved server-side from Secret Manager by name.

`local` cards keep the existing dynamic-import path unchanged, so nothing not yet migrated to
Cloud Run breaks.

## Consequences
- Real per-agent identity and failure isolation — an agent's own service account is what actually
  executes its code, not the orchestrator's.
- New failure modes that didn't exist before: network timeouts, partial failures, auth token
  expiry — must be handled explicitly (retry/backoff, circuit breaking), not ignored.
- Bigger surface to secure: every service is now a network-reachable endpoint, which is why the
  allowlist/schema/OIDC checks are mandatory on day one, not deferred.
- `AgentCard`/registry seeding changes are a contract change — existing `local`-runtime cards and
  tests must keep passing unmodified.
