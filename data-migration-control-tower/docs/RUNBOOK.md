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
python run_ui.py
```

`make seed` is idempotent. Nothing resolves an agent capability until the
Agent Registry is seeded, and nothing resolves a source connection until an
estate exists.

**One thing that is easy to miss.** The console publishes commands; workers
consume them. Every worker below handles **one message and exits** — it is
not a daemon. One click in the console, one run of the matching command. If
a worker prints "No … message was available", nothing was queued.

---

## Path A — onboard an estate and assess it

Read-only throughout: metadata, lineage, risk and a proposed plan. No
target writes.

### A1. Sign in

http://127.0.0.1:8080 → **Sign in with Google**.

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

### A3. Start the assessment, then run its worker

**Start assessment** in the console returns `202 Accepted` and writes a
durable operation record. Then:

```bash
python agents/discovery/run_assessment_worker.py
```

One invocation drives the whole read-only chain:

```
REQUESTED → DISCOVERED → ANALYZED → RISK_ASSESSED → PLANNED
```

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

Only `wwi-demo-estate` supports execution. This **writes to BigQuery**.

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

### B2. Or drive it from the console

Start the orchestrator, which consumes `migration.requested` onward:

```bash
python agents/orchestrator/run_orchestrator.py
```

Then **Runs → Start migration** in the console. Approval is a separate,
human step:

```bash
python agents/cutover/approve_cutover.py <run_id>
```

```bash
python agents/cutover/run_cutover_worker.py
```

Only `approve_cutover.py` calls `approve()` — never agent code — and the
token is bound to the plan hash, so an approval cannot be replayed against
a changed plan.

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

**A worker exits saying no message was available.** The click did not
publish. Check the operation record:

```bash
python -c "import sys;sys.path.insert(0,'.');from dotenv import load_dotenv;load_dotenv('.env');from tools.firestore_client import get_client;[print(d.id,(d.to_dict() or {}).get('status'),(d.to_dict() or {}).get('error','')) for d in get_client().collection('operation_requests').stream()]"
```

`published` means it is queued and the worker should find it.
`publish_failed` means the command never left, and `error` says why.

**`404 Resource not found (resource=<topic>)`.** That topic does not exist
in the project. `infrastructure/gcp_setup.sh` is idempotent — re-run it.
This is provisioning drift: the script gains a topic, an already-provisioned
project never gets it, and the operation record is written while the command
is silently never published. `tests/test_pubsub_provisioning.py` now fails
in review instead.

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

## Cleaning up

An estate referenced by runs is disabled, never deleted — run history
points at `estate_id`, and removing it makes that history uninterpretable.
Use **Estates → Disable**, which refuses while runs are still in flight.

```bash
bash infrastructure/teardown.sh --dry-run
```
