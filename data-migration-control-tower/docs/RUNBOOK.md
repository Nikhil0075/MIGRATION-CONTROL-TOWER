# End-to-end runbook

Two paths. **A** onboards and assesses a new estate through the console —
read-only, safe to repeat. **B** drives the full migration lifecycle to
cutover, and only works against the WideWorldImporters estate.

Run everything from `data-migration-control-tower/`.

---

## Before you start

```bash
docker ps --format "{{.Names}}  {{.Status}}"
```

Expect `legacy-sqlserver` and `retail-postgres`, both healthy. If either is
missing:

```bash
cd simulator/source_setup && docker compose up -d && ./restore_wwi.sh
```

```bash
make second-estate
```

Then the control plane:

```bash
make seed
```

```bash
cd .. && python run_ui.py
```

(`run_ui.py` sits at the repository root and changes into this directory
itself — it is the one command here that is not run from
`data-migration-control-tower/`.)

`make seed` is idempotent. Nothing resolves an agent capability until the
Agent Registry is seeded, and nothing resolves a source connection until an
estate exists.

**`python run_ui.py` is the only command this runbook needs.** The console
publishes a durable command for every operator action, and the event
consumers now run *inside* that same process — so a click is the whole
action, not the first half of one. There is no worker script to run per
click any more.

Check it once, in the console: **System Health → Event consumers**. Nine
consumers, `idle`, lease held by this process. That panel is also the
answer whenever an operation sits at `queued` — see "When something does
not happen" below.

The CLI workers still exist and are still documented, but as the debugging
path for when a consumer is paused. They warn you if a supervisor is
already consuming, because otherwise "No … message was available" reads
like a broken publish when it actually means the console already handled
it.

---

## Path A — onboard an estate and assess it

Read-only throughout: metadata, lineage, risk and a proposed plan. No
target writes.

### A1. Sign in

Open http://127.0.0.1:8080. Use Google, or enter a provisioned domain email and
password. The sign-in method proves identity only; neither option lets the user
choose operator or approver access.

To create a dedicated full-flow test identity, run once from the repository
root and enter a strong password at the no-echo prompts:

```bash
python tools/provision_e2e_user.py --email control-tower-e2e@your-domain.com
```

The Firebase Email/Password provider must be enabled. The command grants the
dedicated account wildcard `viewer`, `operator`, and `approver` custom claims,
which are enough to perform the complete assessment, migration, worker-control,
and cutover flow. Optionally set `CONTROL_TOWER_E2E_EMAIL_DOMAIN` in `.env` to
lock provisioning to your corporate domain. Delete the test identity after the
exercise with the same command plus `--delete`.

Your account needs a role. Roles are granted, never chosen at sign-in —
otherwise anyone with a Google account could self-grant operator. For local
work put your address in `OPERATOR_ALLOWLIST` in `.env` and restart the
server. If you land on **No access yet**, that screen names the variable to
set.

### A2. Onboard

**Estates → Onboard estate**, then:

| Step | Value |
|---|---|
| Identity | Estate ID `acme-finance` (lowercase, digits, hyphens — no spaces), display name `Acme Finance` |
| Source | Source ID `finance-postgres`, adapter **postgres**, database `retaildb` |
| Connection | `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USER`, password env `POSTGRES_PASSWORD` — clear the prefilled `SQLSERVER_*` values |
| Validate | expect **HEALTHY · PostgreSQL 16 · 4 base tables** |
| Pack | `postgres_retail_v1` |
| Review | justification of 8+ characters → **Create estate** |

The connection step collects credential *references* only — a Secret
Manager name or the NAME of an environment variable. There is no password
field anywhere in the wizard, and the API rejects a connection profile
carrying a credential value.

Validation must succeed before Next unlocks, and editing any source or
connection field invalidates it.

### A3. Start the assessment

**Start assessment** in the console. Nothing else — no terminal.

The button returns `202 Accepted` and writes a durable operation record;
the `assessment` consumer picks it up within a poll interval and drives
the whole read-only chain:

```
REQUESTED → DISCOVERED → ANALYZED → RISK_ASSESSED → PLANNED
```

Watch the progress bar move `queued → published → active → done`. It
should never sit on **"Waiting for a worker"** — that label existed
because nothing consumed the command, and seeing it now means the
consumer is paused, erroring, or not running here. System Health says
which.

### A4. Read the result

**Assessments** shows the run at `PLANNED · 100% · Migration plan ready`.

Check the plan — these are the branches WideWorldImporters cannot
exercise, because it contains no composite primary keys at all:

- `retail.order_items` — **blocked**, composite primary key. The extractor
  orders by a single key and reconciliation compares ordered key lists, so
  guessing one column would be quietly wrong.
- `retail.tags` — `aggregate_check: not_applicable`, no numeric column.
  The check is omitted with a stated reason rather than compared against a
  fabricated zero.
- `retail.customers` — key `customer_id`, aggregate `credit_limit`,
  null-check `email_address`, all derived from discovered metadata.

Risk should also flag `email_address` and `phone_number` as PII.

### A5. Path A stops here — deliberately

`postgres_retail_v1` is `default_mode: assessment` and
`execution_supported: false`, so the console shows it as assessment-only
rather than offering an action that would fail.

That is §32.5's adoption principle: production-write access must never be a
prerequisite for discovering an estate, estimating effort, or proving
lineage and risk coverage.

---

## Path B — the full lifecycle, to cutover

Any registered SQL Server source assigned the executable
`wwi_sqlserver_v1` Migration Pack supports this path. A discovered DAG
pipeline is optional: the pack is the execution entry point and supplies the
derived pipeline identifier when none exists. This **writes to BigQuery**.

For an existing deployment, inspect the targeted demo repair before applying
it. The command changes only a missing demo `pack_id`, preserves every other
field, records a revision, and refuses a conflicting value:

```bash
python -m tools.backfill_demo_pack
python -m tools.backfill_demo_pack --apply
```

### B1. One command, end to end

```bash
python agents/orchestrator/run_full_migration.py
```

Roughly five minutes. It deliberately seeds a row-loss defect and then
recovers from it, because a migration tool that only works when nothing
goes wrong proves very little.

What to watch for in the output:

| Line | What it proves |
|---|---|
| `pii_read_denied=True` | the policy engine denied an unauthorised PII read |
| `first pass: loaded 656/663 rows (defect seeded)` | the injected fault |
| `incident=row_loss:Sales.Customers, root_cause_by=recalled_memory` | cross-run memory recalled a prior confirmed fact |
| `final validation: overall_status=PASSED` | all five deterministic checks after a clean reload |
| `cutover self-approval denied: True` | the Cutover agent cannot approve itself |
| `final_state=COMPLETE` | the run finished |

### B2. Or drive it from the console, with no terminal at all

1. **Runs → Start migration** on `wwi-demo-estate`. The `migration`
   consumer takes it from there; discovery, risk, planning, execution and
   validation each publish the next event and the next consumer picks it
   up. The seeded row-loss defect is recovered without a manual step.
2. Watch **Runs** advance to `PASSED`. The dedicated `approval` consumer on
   `approval-preparation-sub` requests cutover approval idempotently and moves
   the run to `READY_FOR_APPROVAL`; `validation-passed-sub` remains reserved
   for CLI/evaluation assertions.
3. **Approve** in the console, with a justification. This is the human
   action and a distinct identity from every agent — the Cutover agent
   still cannot approve itself, and the token is bound to the plan hash,
   so an approval cannot be replayed against a changed plan.
4. The `cutover` consumer performs the cutover and post-cutover
   monitoring. The run reaches `COMPLETE`.

If a target is not HEALTHY after cutover, the run stops at `MONITORING`
and the operation is recorded **failed**, carrying the monitoring
evidence — not `done`. A cutover that did not complete must never be
reported as one that did.

`agents/cutover/approve_cutover.py` remains the CLI equivalent of step 3.
Only it and the console approval endpoint call `approve()` — never agent
code.

For a separately labelled live browser acceptance run, start the built app,
provision a marked Firebase test identity, and execute:

```bash
npm run test:e2e:live --prefix frontend/client
```

Set `CONTROL_TOWER_E2E_EMAIL`, `CONTROL_TOWER_E2E_PASSWORD`, and a unique
`CONTROL_TOWER_E2E_ESTATE_ID` in the invoking process. Delete the identity
afterward with `python -m tools.provision_e2e_user --email <address> --delete`.

---

## Verifying the portability claim

```bash
make onboarding-gate
```

A second estate on a different database engine, discovered and planned with
**zero edits under `agents/`**. The test greps every agent module for
`postgres`, `psycopg`, `retaildb` and the estate and pack ids. Review
cannot catch a single `if source == "postgres"` inside an agent; that grep
can.

```bash
make test
```

---

## When something does not happen

**An operation sits at `queued` and nothing advances.** Open **System
Health → Event consumers**. There are only four answers and the panel
names each one:

| What it says | What it means |
|---|---|
| the consumer is `paused` | someone paused it, with a justification recorded in `operation_audit`. Resume it there. |
| `Standby — another instance holds the worker lease` | a second process is doing the work. Normal, not a fault: only the lease holder consumes, so no message is handled twice. |
| `In-process workers are not running here` | this process was started with `CONTROL_TOWER_WORKERS=0` — the deployed image does that deliberately. |
| the consumer is `error` with a last error | the handler is failing. The error is the diagnosis. |

**A CLI worker exits saying no message was available.** If the console is
running, this is expected — its consumer took the message first, and the
script says so before it pulls. Pause that consumer from System Health if
you want to drive it by hand. Otherwise the click did not publish; check
the operation record:

```bash
python -c "import sys;sys.path.insert(0,'.');from dotenv import load_dotenv;load_dotenv('.env');from tools.firestore_client import get_client;[print(d.id,(d.to_dict() or {}).get('status'),(d.to_dict() or {}).get('error','')) for d in get_client().collection('operation_requests').stream()]"
```

`published` means it is queued and the worker should find it.
`publish_failed` means the command never left, and `error` says why.

**`404 Resource not found (resource=<topic>)`.** That topic does not exist
in the project. `infrastructure/gcp_setup.sh` is idempotent — re-run it.
On Windows, run it as

```bash
CLOUDSDK_PYTHON="$(which python)" bash infrastructure/gcp_setup.sh
```

The bundled `bq` launcher looks for an executable literally named
`python3.13`, which the Windows installer does not create. Without that
variable `bq` fails, `set -e` aborts, and the script never reaches the
Pub/Sub steps — so it appears to have run while provisioning nothing.
This is provisioning drift: the script gains a topic, an already-provisioned
project never gets it, and the operation record is written while the command
is silently never published. `tests/test_pubsub_provisioning.py` now fails
in review instead.

**A consumer keeps failing on the same message.** After 10 delivery
attempts Pub/Sub forwards it to the `dead-letter` topic and the consumer
moves on. Read what was dropped:

```bash
gcloud pubsub subscriptions pull dead-letter-sub --limit=10 --format=json
```

Each message carries a `CloudPubSubDeadLetterSourceSubscription`
attribute naming which consumer gave up on it. The console has no
dead-letter view — this is the only place to look.

**403 on every page.** Your account holds no role. See A1.

**"Onboard estate" is greyed out.** Same cause — the button requires
`operator`, and `APPROVER_ALLOWLIST` grants `approver`, which is a
different thing.

**The console feels slow on first load.** Expected off-region: a single
Firestore round trip costs 0.5–1.2s and a page needs several. Repeat views
are served from a short TTL cache and marked `freshness: "cached"`. Set
`UI_CACHE_TTL_SECONDS=0` to disable.

**Stopping the server.** `pkill` does not reliably kill it on Windows:

```powershell
Get-NetTCPConnection -LocalPort 8080 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

---

## Where the tests write

`pytest` writes to a **separate Firestore database**, never to the one the
console reads. `tests/conftest.py` sets `FIRESTORE_DATABASE` for the whole
session before anything builds a client.

This is not hygiene for its own sake. When the suite shared `(default)`,
the live project accumulated hundreds of `test_run_*` documents and 9,891
orphaned subcollection records — and because collection-group queries here
run unfiltered (no composite index exists for group + `order_by`, see
CLAUDE.md), the console paid to stream every one of them on each uncached
request. `catalog` alone was 6,428 dead documents out of 9,090.

Provision and seed it once:

```bash
bash infrastructure/gcp_setup.sh
```

```bash
python -m infrastructure.seed_test_database
```

`gcp_setup.sh` creates the database; the seeder gives it the same baseline
a real deployment has — an Agent Registry with APPROVED cards, and the
committed estates. Both are idempotent.

Until the database exists, Firestore-backed tests **skip** with a message
naming these two commands. They deliberately do not fall back to
`(default)`: a silent fallback would put the writes back into production
at exactly the moment nobody is watching.

| Variable | Meaning |
|---|---|
| `MCT_TEST_FIRESTORE_DATABASE` | Names the test database. Default `mct-tests`. |
| `MCT_TESTS_MAY_WRITE_PRODUCTION=1` | Runs the suite against `(default)`. Exact match on `1` — `true` and `yes` do not count. |

The opt-out exists for a one-off check against real data. It is not a
normal way to run the suite.

### Clearing orphans that already exist

`delete_run` now removes a run's subcollections along with the run, so the
backlog cannot rebuild. For anything already there:

```bash
python -m tools.purge_orphans
```

```bash
python -m tools.purge_orphans --apply
```

The first prints counts and deletes nothing. The second writes every
document to a JSONL rescue copy under `var/` before deleting, and refuses
to delete at all if that export fails.

## Cleaning up

An estate referenced by runs is disabled, never deleted — run history
points at `estate_id`, and removing it makes that history uninterpretable.
Use **Estates → Disable**, which refuses while runs are still in flight.

```bash
bash infrastructure/teardown.sh --dry-run
```
