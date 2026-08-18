# Cloud deployment + observability evidence (Day 10, master doc §17.2 Fri 28 Aug)

## Deployment

`hello-agent` — deployed Cloud Run service, `Ready` condition confirmed
live via `gcloud run services describe`:

```
url: https://hello-agent-z5ab2ay6rq-uc.a.run.app
status.conditions: Ready=True, ConfigurationsReady=True, RoutesReady=True
resources.limits: cpu=1000m, memory=1Gi
```

A second image, `control-tower-ui` (the FastAPI Control Tower UI —
`frontend/Dockerfile`), was built and pushed to
`us-central1-docker.pkg.dev/autonomous-data-migration/control-tower/`
this build day but deliberately **not** deployed publicly — the owner
plans a full UI redesign and separate branded hosting rather than
shipping this bare-bones version under a `*.run.app` URL. The image
stays available in Artifact Registry for that later deploy.

## Observability

Structured Cloud Logging output for `hello-agent`, captured via
`gcloud logging read 'resource.type="cloud_run_revision" AND
resource.labels.service_name="hello-agent"'`:

```
2026-08-16T05:44:52.992835Z  GET /status HTTP/1.1" 200 OK
2026-08-16T05:44:53.749559Z  INFO:hello_agent:wrote bootstrap-check row at _bootstrap_check/fd70afbd-3d72-4508-8a41-bcbca8900d9c
2026-08-16T05:44:53.751134Z  POST /bootstrap-check HTTP/1.1" 200 OK
```

### Cloud Trace (Day 10 hardening, Phase 5)

`tools/tracing.py` wraps the OpenTelemetry SDK + `opentelemetry-
exporter-gcp-trace`, exporting a real span for every orchestrator event
handler (`handle_migration_requested`, `handle_discovery_completed`,
`handle_risk_assessed`, `handle_planned`, `handle_validation_requested`,
`handle_validation_failed`), each tagged with `run_id` and, once
resolved, the registry-resolved `agent_id`/`version`. Best-effort and
never fatal: a missing `GCP_PROJECT_ID` or exporter failure degrades to
"no traces recorded," never a broken migration run.

Queried live (`google.cloud.trace_v1.TraceServiceClient.list_traces`)
immediately after a real `agents/orchestrator/run_full_migration.py`
run — trace `5b4b3b280138a716f616e0895ba941dc` contains all 10 spans for
that run's `run_id` (`run_20260816_180252_6672a4ab`), showing the real
sequence: `advance_through_validation` → `handle_migration_requested` →
`handle_discovery_completed` → `lineage.graph.build` →
`risk.assess.estate` (`agent_id=risk-agent`) →
`handle_risk_assessed` (`agent_id=planner-agent`, real `plan_hash`) →
`handle_planned` (`drop_fraction=0.01`) → `handle_validation_requested`
(`overall_status=FAILED`) → `handle_validation_failed`
(`incident_signature=row_loss:Sales.Customers`,
`root_cause_generated_by=recalled_memory`) → `handle_validation_requested`
(`overall_status=PASSED`). Filtering Cloud Trace's UI to that `run_id`
attribute shows exactly this tree — satisfying Appendix E's "Traces
carry run ID and agent identity and are viewable in Cloud Trace" as a
real, queried artifact, not a claim.

### What's still true about the deployment topology

This project runs the fleet as one orchestrator process today (see
`infrastructure/README.md`'s Rung-2 substitution note), not six
independent Cloud Run services — so these spans don't cross a real
network/service boundary yet, only Python function-call boundaries
within one process. That's a real, honestly-stated limit on what the
trace tree proves: it shows genuine causality, timing, and per-stage
agent identity, not inter-service network latency or independent
failure domains. Per-agent IAM service accounts already exist
(`infrastructure/gcp_setup.sh`) and Cloud Run's SA-attachment model —
confirmed live for `hello-agent` — is the real workload-identity
mechanism for any agent that IS deployed; the audit's "mostly
declarative rather than workload identities" phrasing slightly
overstates the gap. The actual gap is deployment count (1 of 7
services), not the identity mechanism itself.
