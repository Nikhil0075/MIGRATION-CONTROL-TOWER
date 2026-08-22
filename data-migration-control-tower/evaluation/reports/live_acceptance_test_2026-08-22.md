# Live Acceptance Test — 2026-08-22

Deploy & Harden Phase 5 close-out. This is what actually happened publishing a real
`migration.requested` Pub/Sub message against the live, fully-deployed 9-service Cloud Run fleet —
not a simulation, not a local test run. Every finding below came from real Cloud Logging output and
real Firestore state, captured as it happened.

## What this proves

The distributed deployment genuinely works end to end at the transport and dispatch layer: a real
Pub/Sub message reaches the deployed orchestrator's push endpoint, passes real OIDC caller
verification, and the orchestrator's `handle_migration_requested` actually executes — creates a
real Firestore run document, calls `registry.invoke_capability()` for the Discovery capability, and
that capability handler actually runs. Three real, previously-invisible bugs surfaced along the way
and are now fixed; one real architectural constraint was found and is documented honestly below
rather than papered over.

## Bugs found live, fixed, and now covered by a regression test

### 1. Every push endpoint 307-redirected — no delivery ever succeeded

`agents/orchestrator/service_main.py` (and the discovery/cutover equivalents) each
`app.mount(f"/push/{name}", sub_app)` a sub-app whose own route is registered at `"/"`. Starlette's
default `redirect_slashes=True` means a request to the bare mount path (no trailing slash) — which
is exactly what `infrastructure/terraform/pubsub.tf`'s `push_targets` map declared for all 10 push
subscriptions — 307-redirects before any handler or auth code ever runs. Pub/Sub push delivery does
not follow redirects. Every single delivery attempt failed silently; Pub/Sub kept redelivering
(visible as a new Firestore run created every ~15 seconds) until `max_delivery_attempts` was
exhausted and the message landed in the dead-letter topic.

**Fixed**: added the trailing slash to all 10 paths in `pubsub.tf`'s `push_targets` map.
**Regression test**: `tests/test_service_entrypoints.py::test_push_mount_bare_path_redirects_but_trailing_slash_does_not`,
parametrized over all 10 mounts — asserts the bare path still redirects (documents the trap) and the
trailing-slash path does not (proves the fix), so this can't silently regress.

### 2. OIDC audience mismatch — every push request 401'd once routing was fixed

Left unset, GCP defaults a push subscription's OIDC token `aud` claim to the full push endpoint URL
(including that route's own path). `tools/capability_http_server.py::verify_caller_identity()`
checks the token against `SERVICE_AUDIENCE`, one env var per *service*. The orchestrator owns 8
different push routes — one `SERVICE_AUDIENCE` value cannot equal 8 different per-route URLs
simultaneously, so every route but whichever one happened to match failed verification with 401.

**Fixed**: `pubsub.tf`'s `oidc_token` block now sets `audience` explicitly to the owning service's
own base `.uri` — the same value used to construct `push_endpoint` — so every route on one service
shares one consistent audience. **Also fixed a second, compounding form of the same bug**:
`SERVICE_AUDIENCE` had been set (in the post-deploy manual step) from
`gcloud run services describe --format='value(status.url)'`, which returned a *different, equally
valid* Cloud Run hostname alias than Terraform's own `.uri` output for the identical service —
confirmed live, the two are genuinely different strings. `infrastructure/terraform/README.md`'s
post-deploy checklist now says explicitly: use `terraform output cloud_run_service_urls`, not
`gcloud run services describe`, for this step.

### 3. No agent service account had Cloud Trace write permission at all

Not a `traceparent`-propagation gap (that one was already known and documented in
`docs/EVALUATION.md`) — a total absence of `roles/cloudtrace.agent` on every one of the 8 agent
service accounts, so `tools/tracing.py`'s span export failed with a 403 on every single request,
regardless of propagation.

**Fixed**: `infrastructure/terraform/iam.tf` now grants `roles/cloudtrace.agent` as a baseline role
to every agent SA, same pattern as the existing `datastore.user`/`pubsub.publisher` grants.

## Known, real constraint — stated honestly, not fixed here

The test run did not reach `DISCOVERED`. The actual failure, once the three bugs above were fixed,
was:

```
tools.secret_resolver.SecretResolutionError: Could not resolve secret 'sqlserver-wwi-password'
from Secret Manager (403 Permission 'secretmanager.versions.access' denied ...)
```

This is not a new bug — it is the visible consequence of a design decision already stated honestly
in `docs/compliance_matrix.md` and `docs/GOVERNANCE.md`: **no real AgentCard has been flipped from
`runtime.type=local` to `cloud_run` yet.** Discovery's capability still resolves via in-process
dynamic import *inside the orchestrator's own container*, running under `sa-orchestrator`'s
identity — not a real HTTP call to the independently-deployed `discovery-agent` service under its
own `sa-discovery` identity. `sa-orchestrator` was never granted access to Discovery's SQL Server
secret (correctly, by least-privilege design — that secret belongs to Discovery's own scope), so
in-process dispatch breaks the moment each agent gets its own narrowly-scoped SA, which is exactly
what this deployment now has.

Separately and independently, `wwi-sqlserver`'s actual target — a local Docker container
(`simulator/source_setup/docker-compose.yml`) — has no network path from a deployed Cloud Run
service at all, with or without the secret. Fixing the IAM gap alone would only trade a permission
error for a connection timeout.

**What closing this gap for real would need** (not attempted here — a materially larger change than
this session's live-testing scope, and one that should be reviewed on its own before applying):
flipping Discovery's (and every other capability-serving agent's) AgentCard to `runtime.type=cloud_run`
so dispatch genuinely goes over the typed HTTP path built in Phase 2c, each agent's own SA granted
access to only the secrets its own bindings need, and a data source actually reachable from Cloud
Run — either the already-provisioned `postgres_retail_exec_v1` Cloud SQL instance (network path
already wired, schema not yet loaded) with Direct VPC egress added to Discovery's own service, or a
genuinely public-reachable demo SQL Server.

## Evidence

- Real Firestore run documents created and observed stuck at `REQUESTED`, `state_history: ["REQUESTED"]`,
  across three separate publish attempts (one per bug, as each was fixed) — all cleaned up after
  capturing the evidence (`agents/orchestrator/run_lifecycle.py::delete_run`, matching this repo's
  test-hygiene convention for anything created against the shared project).
- Real Cloud Logging tracebacks (`gcloud logging read`) for every failure mode: `307 Temporary Redirect`,
  `401 Unauthorized`, `500 Internal Server Error` with a full Python traceback ending in
  `SecretResolutionError`.
- Dead-letter topic backlog observed and drained (`gcloud pubsub subscriptions pull dead-letter-sub`)
  after each redelivery storm.
- `evaluation/reports/cloud_deployment_evidence.md` (regenerated the same day) — static service/
  subscription/IAM evidence from `evaluation/collect_deployment_evidence.py`.

## What this session did not attempt

- Getting a run all the way to `COMPLETE` against a real data source — blocked on the architectural
  gap above, which is a real scoping decision, not a quick fix.
- Multi-service Cloud Trace *span propagation* (as opposed to the write-permission fix above) —
  `traceparent`/`X-Cloud-Trace-Context` propagation through Pub/Sub message attributes and HTTP
  headers is still unwired, per `evaluation/collect_deployment_evidence.py`'s own "known gaps"
  section.
- A live failure/recovery demonstration and a deployed-build browser E2E pass — both depend on a run
  reaching further than `REQUESTED`, so they're blocked on the same gap.
