# ADR 0003: Asynchronous job-completion-event data plane, not synchronous polling

## Status
Accepted — Deploy & Harden Phase 3.

## Context
`tools/migration_executor.py`'s only `DataPlaneExecutor` implementation, `InMemoryExecutor`,
streams rows through the orchestrator's own process — its own docstring says so plainly. A
production-oriented replacement needs to run somewhere else, genuinely separately deployed and
separately billed.

A first-draft design had the orchestrator submit a Cloud Run Jobs execution and then poll it to
completion from inside the same HTTP request/handler that triggered it. That is unsafe: it can
consume a request/handler slot for the job's full duration (potentially minutes), and it fails
outright if the orchestrator restarts mid-poll — there is nothing durable about a wait loop held
in one process's memory.

## Decision
Use the same event-driven pattern every other stage of the lifecycle already uses:

1. Orchestrator creates a `migration_runs/{run_id}/migration_executions/{execution_id}` record,
   starts the Cloud Run Job (`google.cloud.run_v2.JobsClient.run_job()`), and **returns
   immediately** — no blocking wait.
2. The job container (its own deploy, its own narrowly-scoped service account: Cloud SQL Client,
   target-BigQuery-dataset writer only, write access limited to its own execution document, Pub/Sub
   publisher for the completion topic only) reuses the existing, unmodified
   `fetch_source_rows()`/`InMemoryExecutor` logic, writes its result to that same execution
   document, and publishes `migration.completed`.
3. A new orchestrator consumer subscribes to `migration.completed`, validates the result, and
   continues the state machine — exactly like every other Pub/Sub-driven transition already does.

## Consequences
- Durable across orchestrator restarts — the job's completion event is what resumes the lifecycle,
  not a live poll loop that dies with its process.
- Fits the codebase's existing idempotency machinery (`_dedup_claim`) instead of introducing a
  second, bespoke durability mechanism just for this one executor.
- `InMemoryExecutor` remains the default everywhere else — this is additive
  (`DataPlaneExecutor.execute_remote()`, an optional second method), not a replacement of the
  existing `load()` contract or its tests.
- Terminology: this executor uses PostgreSQL's native wire protocol via `psycopg`, not JDBC — call
  it that, not "a JDBC transport," anywhere it's described.
