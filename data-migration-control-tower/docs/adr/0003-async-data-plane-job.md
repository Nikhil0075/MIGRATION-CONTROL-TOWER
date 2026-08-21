# ADR 0003: Asynchronous job-completion-event data plane, not synchronous polling

## Status
Implemented — Deploy & Harden Phase 3. Code complete and tested
(`tools/data_plane_executors/cloud_run_job_executor.py`,
`tools/data_plane_job/run_job.py`, `agents/orchestrator/orchestrator.py::
handle_migration_completed`); not yet deployed live (needs
`infrastructure/terraform`'s `enable_data_plane_job`/`enable_cloud_sql`,
both still `false` by default).

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
   publisher for the completion topic only) reuses the existing, unmodified adapter's own
   `fetch_rows()`/`count_rows()` (`tools/adapters/postgres_adapter.py` for Phase 3's Cloud SQL demo
   source) plus `tools/migration_executor.py::InMemoryExecutor` for the actual BigQuery load, writes
   its result to that same execution document, and publishes `migration.completed`.

   **Correction from the original design**: `tools/migration_executor.py::fetch_source_rows()` is
   SQL-Server-specific (hardcoded to `tools/sqlserver_client.py`), not source-agnostic — the job
   container cannot reuse it unmodified for a Postgres source. It uses the adapter's own
   `fetch_rows()` instead, which already existed for exactly this purpose
   (`tools/adapters/base.py`'s `CAPABILITY_TRANSFER` contract). `InMemoryExecutor.load()` — the
   part that's genuinely source-agnostic — is what's actually reused unchanged.
3. A new orchestrator consumer, `handle_migration_completed`, subscribes to `migration.completed`,
   decrements a per-run `pending_remote_executions` counter (a Firestore transaction — two
   completions arriving close together must not both read the same starting count), and once every
   submitted execution has reported in, aggregates their manifests and continues the state machine
   exactly like `handle_planned()`'s own synchronous aggregation does.
4. `handle_planned()` itself gained a conditional branch (`DATA_PLANE_EXECUTOR=cloud_run_job`, unset
   by default): when set, it submits every target's execution and returns immediately WITHOUT
   transitioning to `VALIDATING` or publishing `validation.requested` — that's left entirely to
   `handle_migration_completed`. Unset (every deployment today), `handle_planned()`'s behavior is
   byte-for-byte what it always was.
5. A `FAILED` async execution still lands the run in `VALIDATING`, not a direct `FAILED` transition
   — `run_lifecycle.py`'s canonical graph has no `MIGRATING -> FAILED` edge, and inventing one would
   duplicate what Validation's own reconciliation already does correctly: a missing/incomplete
   target table fails reconciliation through the existing, tested `VALIDATING -> FAILED` path.

## Consequences
- Durable across orchestrator restarts — the job's completion event is what resumes the lifecycle,
  not a live poll loop that dies with its process.
- Fits the codebase's existing idempotency machinery — `tools/idempotency.py` (a new module,
  generalizing `orchestrator.py::_dedup_claim`'s pattern for `handle_migration_completed`'s own
  message dedup) rather than introducing a second, bespoke durability mechanism just for this one
  executor.
- `InMemoryExecutor` remains the default everywhere else — this is additive
  (`DataPlaneExecutor.execute_remote()`, an optional second method returning `None` by default), not
  a replacement of the existing `load()` contract or its tests.
- Terminology: this executor uses PostgreSQL's native wire protocol via `psycopg`, not JDBC — call
  it that, not "a JDBC transport," anywhere it's described.

## Known limitation, stated rather than hidden
`handle_planned()`'s Wave Manager slot release (`wave_manager.release_slot()`) still happens in that
function's own `finally` block regardless of sync/async — for the async path, the concurrency slot
frees up as soon as jobs are **submitted**, not once they actually **complete**. Wave Manager's
admission control therefore does not yet account for in-flight remote work. Fixing this needs the
slot release to move into `handle_migration_completed` too, with its own careful design (a slot
released twice, or never, on a redelivered/duplicate event is worse than today's honestly-limited
behavior) — left as explicit follow-up work, not silently shipped as if solved.
