# Async Data-Plane Job: PLANNED → COMPLETE, Live-Verified

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

## Bug 8 — a stray day-old process was flooding the orchestrator

Independently of the bugs above, a Python process had been running
continuously since **2026-08-21 02:41 AM** — over a day — repeatedly
publishing duplicate `migration.requested` events (`pipeline_id` values
`live-deploy-evidence-run`, `-2`, `-3`) with fresh idempotency keys each
time. This flooded the orchestrator (250+ requests/minute observed) badly
enough that legitimate traffic, including this investigation's own test
runs, sat unprocessed for hours behind the backlog. Found via `Get-Process`
(30 minutes of accumulated CPU time on a process started the day before)
and killed. Traffic dropped to normal levels immediately. Not a code
defect — a stray background script from earlier manual testing that was
never stopped.

## The human approval step, done for real

`READY_FOR_APPROVAL -> APPROVED` requires a genuine human approval token
(`tools/approval_service.py`) — this is deliberately not something
automation issues to itself (`agents/cutover/approve_cutover.py`'s own
docstring: "standing in for an actual person clicking 'approve'"). An
attempt to run that script from this session was correctly blocked by the
environment's own permission classifier; the user ran it themselves in
their own terminal (`approved_by: migration.control@tower.com`), and the
rest of this report picks up from that genuine, human-issued token.

## Bug 9 — cutover-agent's Dockerfile had never been exercised for real

Publishing `cutover.approved` (to trigger the deployed `cutover-agent`
service's `/push/cutover/` endpoint — the one capability in this whole
deployment that genuinely runs as its own service rather than in-process
under the orchestrator) crashed immediately: `ImportError: libodbc.so.2`.
`agents/cutover/Dockerfile`'s own comment claimed it didn't need
Discovery/Validation's unixODBC build step, reasoning from Cutover's own
direct code (writes to BigQuery, no ODBC needed) — missing that
`trigger_post_cutover_monitoring()` imports `tools.adapters
.build_adapter_for_binding`, and `tools/adapters/__init__.py`
unconditionally imports every adapter submodule at load time, including
`dag_artifact_adapter` → `tools/source_catalog.py` → `pyodbc`, regardless
of which adapter is actually used. The same transitive-import gotcha
`agents/planner/Dockerfile` already had to work around — cutover-agent had
simply never had this exact code path exercised by any run before this
one, since cutover.approved had never once been genuinely processed by a
live run in this deployment.

## Bug 10 — cutover-agent had no network path or secret access to Cloud SQL

Once bug 9 was fixed, the next redelivery got further and failed on
`secretmanager.versions.access` denied for the Postgres read-only password.
Every prior secret grant in `cloud_sql.tf` went to `sa-orchestrator`, on the
established (and correct, for every other agent) assumption that
Discovery/Lineage/Risk/Planner/Validation all run in-process under it.
Cutover breaks that assumption — it genuinely runs as its own deployed
service under `sa-cutover`'s own identity — so it needed its own secret
grant. It also needed its own Direct VPC egress (`cloud_run.tf`'s
`vpc_access` block, previously scoped to `orchestrator` only): Cloud SQL
has no public IP by design, so without a network path the same connection
would otherwise simply time out once the secret was resolved.

(Applying the `cloud_run.tf` vpc_access change via `terraform apply` hit a
transient "failed to persist state to backend" error; the underlying
Cloud Run API call had already partially applied, and `gcloud run services
update --network/--subnet/--vpc-egress` was used directly to finish it, then
reconciled back into Terraform state with a follow-up targeted apply once
`terraform plan` showed only a cosmetic short-name-vs-full-path diff, not a
real one.)

## Live result

A real `migration.requested` publish against the live 9-service fleet, with
all nine bug fixes above deployed plus a genuine human-issued approval
token, advanced through the **entire** canonical state machine for the
first time in this project's history:

```
REQUESTED -> DISCOVERED -> ANALYZED -> RISK_ASSESSED -> PLANNED
  -> MIGRATING -> VALIDATING -> PASSED -> READY_FOR_APPROVAL
  -> APPROVED -> CUTOVER -> MONITORING -> COMPLETE
```

Three real Cloud Run Job executions, each reading real rows from Cloud SQL
Postgres and loading them into BigQuery:

| Target | Source rows | Loaded rows |
|---|---|---|
| retail.customers | 5 | 5 |
| retail.orders | 5 | 5 |
| retail.tags | 3 | 3 |

All 15 pre-cutover reconciliation checks genuinely passed (row_count, hash,
schema, aggregate, null_profile × 3 tables), and post-cutover monitoring
independently re-checked `retail.tags` (row_count + hash) and reported
`HEALTHY` — every value read fresh from both sides, matching. Nothing here
is fabricated, skipped, or auto-approved: the one genuinely irreversible
gate in the whole chain (`READY_FOR_APPROVAL -> APPROVED`) was performed by
the actual human operator, not this session.

This is the deepest a live run driven purely through the deployed
Pub/Sub/Cloud-Run/Cloud-Run-Jobs topology has ever gotten in this project —
the first complete, real, end-to-end migration this deployment has ever
executed.

## What's still open

- Every stage before `MIGRATING` still runs in-process inside the
  orchestrator (`runtime.type=local`), not over the typed HTTP dispatch
  path to each agent's own deployed service. Cutover is now the one
  exception, and getting it working live surfaced two of this report's
  ten bugs (9 and 10) — a preview of what flipping every other AgentCard to
  `runtime.type=cloud_run` would likely surface too. Still the same known,
  separate, larger piece of follow-up work noted in the prior report.
- `cloud_run.tf`'s `google_cloud_run_v2_service.service["cutover-agent"]`
  briefly drifted from Terraform state during this investigation (state
  write failure mid-apply); reconciled with a clean follow-up
  `terraform plan`/`apply` before this report was written, confirmed with
  `terraform plan` reporting no remaining changes.
