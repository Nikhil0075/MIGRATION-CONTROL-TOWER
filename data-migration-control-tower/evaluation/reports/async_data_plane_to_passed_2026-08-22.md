# Async Data-Plane Job: PLANNED → VALIDATING → PASSED, Live-Verified

Deploy & Harden Phase 5 close-out, continuation of the same day's Cloud SQL
discovery-path wiring. Scope: get a real run through the async
`CloudRunJobExecutor` path for the first time, all the way to a genuine
`PASSED` reconciliation verdict — not just `PLANNED`.

## Starting point

The prior investigation (`cloud_sql_discovery_wiring_2026-08-22.md`) left a
run stuck at `PLANNED` with `NoScheduledTargets: no primary key` on all four
`retail.*` tables, despite the live schema genuinely declaring correct
primary keys. Root cause and seven more real bugs, found and fixed in
sequence — each one only became visible once the bug before it stopped
masking it, live testing's usual pattern in this project.

## Bug 1 — primary-key discovery invisible to a least-privilege role

`tools/source_catalog.py::catalog_postgres_tables()` read primary keys from
`information_schema.table_constraints` / `key_column_usage`. Both views are
SQL-standard-gated on `has_table_privilege(oid, 'INSERT, UPDATE, DELETE,
TRUNCATE, REFERENCES, TRIGGER')` — SELECT is deliberately not in that list
(confirmed by pulling the live view definition via `pg_get_viewdef`, not
assumed). `migration_readonly` has only SELECT, so every primary key on
every table came back empty. Never caught locally: the dev/test Postgres
estate always connects as the `postgres` superuser, which trivially passes
the check regardless of grants.

Fixed by reading `pg_constraint`/`pg_class`/`pg_attribute` directly — plain
system catalogs, no such gate, confirmed live readable by `migration_readonly`
(`pg_constraint` returned all 118 rows in the database, including all 6
belonging to `retail`). Regression coverage:
`tests/test_source_catalog.py` (fake-cursor harness — this function had zero
unit coverage before, only ever exercised live).

## Bug 2 — async executor never actually selected

`infrastructure/terraform/cloud_run.tf` never set `DATA_PLANE_EXECUTOR` on
the orchestrator. `enable_data_plane_job=true` created the Cloud Run Job
resource and its IAM/VPC wiring, but nothing ever told
`_select_data_plane_executor()` to use it — every run silently took the
synchronous `InMemoryExecutor` path instead, meaning this path had never
once actually run. Fixed by wiring `DATA_PLANE_EXECUTOR=cloud_run_job` and
`DATA_PLANE_JOB_NAME` (the job's `.id`, not `.name` — see bug 3).

## Bug 3 — bare job name instead of the fully-qualified resource path

`CloudRunJobExecutor`'s own `JOB_NAME_ENV_VAR` docstring already documented
the required form (`projects/P/locations/R/jobs/NAME`); the first live wiring
pass set the bare name anyway. `RunJobRequest.name` rejected it outright.
Fixed by referencing the Terraform resource's `.id` (fully-qualified)
instead of `.name` (bare).

## Bug 4 — `handle_planned()` redelivery livelock

The genuinely slow part of `handle_planned()` (Wave Manager's bounded-retry
admission, then a real Cloud Run Jobs API call per target) outlasted the
Pub/Sub push subscription's ack deadline. A redelivery arriving after the
`PLANNED -> MIGRATING` transition already succeeded, but before
`_dedup_complete()` ran, hit a hard-coded `transition_state(run_id,
"MIGRATING")` on every single retry — an illegal `MIGRATING -> MIGRATING`
transition, raised every time. Since a raised exception is never acked,
Pub/Sub redelivered the same message every few seconds, forever: a live,
self-sustaining crash loop, confirmed via Cloud Logging, with a run stuck at
`MIGRATING` and zero executions ever submitted.

Fixed by treating an already-`MIGRATING` run as a stale-claim redo instead
of an error, using a migration_executions doc's real `data_plane_job_id`
(not the `pending_remote_executions` counter alone, which is written before
submission even starts) to tell "safe to redo" apart from "already
genuinely submitted, refuse to double-submit." Three new regression tests in
`tests/test_orchestrator.py` cover: resuming with nothing yet submitted,
refusing to resubmit once something genuinely was, and redoing when only the
counter (not a real submission) was recorded — the exact three states this
investigation actually hit live, in that order.

## Bug 5 — orchestrator had no IAM permission to invoke the job at all

`sa-orchestrator` had no `google_cloud_run_v2_job_iam_member` binding on the
data-plane job resource. First attempt: `roles/run.invoker` — insufficient,
since `CloudRunJobExecutor._submit_job()`'s `RunJobRequest` carries
per-target container overrides (`RUN_ID`, `EXECUTION_ID`, `SOURCE_TABLE`,
etc.), which needs the more sensitive `run.jobs.runWithOverrides`
permission, only present in `roles/run.developer`. Both gaps caught live,
one after the other.

## Bug 6 — the job container never had `GCP_PROJECT_ID`

`google_cloud_run_v2_job.data_plane` set no env vars on its container at
all. The job's own entrypoint (`tools/data_plane_job/run_job.py`) needs
`GCP_PROJECT_ID` to resolve its source secret
(`tools/secret_resolver.py`) and to publish `migration.completed`
(`tools/events.py`) — without it, every real execution crashed inside the
container before reading a row, and the first one crashed before it could
even publish its own `migration.completed(status=FAILED)`.

## Bug 7 — `bigquery.dataEditor` is not `bigquery.jobUser`

Once bugs 2–6 were fixed, the job genuinely connected to Cloud SQL, read
real rows, and tried to load them into BigQuery — and failed with `403:
User does not have bigquery.jobs.create permission`. `dataEditor` grants
write access to a dataset once a job exists; `bigquery.jobs.create` is a
separate permission needed to start one, by BigQuery's own design. Fixed
for `sa-data-plane-job`. The exact same gap then reappeared one stage later
for `sa-orchestrator`, which runs Validation reconciliation in-process
(`agents/validation/agent.py` → `tools/bigquery_tools.py::get_row_count()`)
— every reconciliation query failed with the identical `Forbidden` until
`sa-orchestrator` (and, for future-proofing once real `cloud_run` dispatch
is ever turned on, `sa-validation`/`sa-cutover`) also got `bigquery.jobUser`.

## An eighth, unrelated but blocking problem: a stray day-old process

Independently of the above, a Python process had been running continuously
since **2026-08-21 02:41 AM** — over a day — repeatedly publishing duplicate
`migration.requested` events (`pipeline_id` values `live-deploy-evidence-run`,
`-2`, `-3`) with fresh idempotency keys each time. This flooded the
orchestrator (250+ requests/minute observed) badly enough that legitimate
traffic, including this investigation's own test runs, sat unprocessed for
hours behind the backlog. Found via `Get-Process` (30 minutes of accumulated
CPU time on a process started the day before) and killed. Traffic dropped
to normal levels immediately. Not a code defect — an operational hygiene
finding: a stray background script from earlier manual testing was never
stopped.

## Live result

A real `migration.requested` publish against the live 9-service fleet, with
all seven bug fixes above deployed, genuinely advanced the whole way:

```
REQUESTED -> DISCOVERED -> ANALYZED -> RISK_ASSESSED -> PLANNED
  -> MIGRATING -> VALIDATING -> PASSED -> READY_FOR_APPROVAL
```

Three real Cloud Run Job executions, each reading real rows from Cloud SQL
Postgres and loading them into BigQuery:

| Target | Source rows | Loaded rows |
|---|---|---|
| retail.customers | 5 | 5 |
| retail.orders | 5 | 5 |
| retail.tags | 3 | 3 |

All 15 reconciliation checks genuinely passed (row_count, hash, schema,
aggregate, null_profile × 3 tables) — matching source/target hashes and
values, not a fabricated or skipped verdict. This is the deepest a live run
driven purely through the deployed Pub/Sub/Cloud-Run/Cloud-Run-Jobs
topology has ever gotten in this project, and the first genuine, complete
exercise of the async data-plane job path end to end.

## What's still open

- `READY_FOR_APPROVAL -> APPROVED -> CUTOVER -> MONITORING -> COMPLETE`
  requires a human approval token (`tools/approval_service.py`) — by
  design, never something automation should manufacture on its own. Not
  attempted in this pass.
- Every stage before `MIGRATING` still runs in-process inside the
  orchestrator (`runtime.type=local`), not over the typed HTTP dispatch
  path to each agent's own deployed service — the same known, separate,
  larger piece of follow-up work noted in the prior report.
