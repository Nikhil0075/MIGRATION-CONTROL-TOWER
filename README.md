# Migration Control Tower

A governed fleet of AI agents that discovers a legacy data estate, assesses
migration risk, plans and validates a migration to BigQuery, and coordinates a
human-approved cutover.

The product is not one migration. It is a **migration control plane**: connect
an estate, discover it, assess it, plan it, execute through scalable data
services, validate deterministically, learn from prior runs, and cut over only
within explicit enterprise policy.

---

## The two rules everything else follows from

**1. Models interpret. Deterministic Python decides.**

Gemini/ADK agents read code, documents and failures and explain what they mean.
Authorization, state transitions, row counts, schema and hash comparisons,
policy enforcement and approval verification are ordinary Python. A model can
propose or explain; it can never be the thing that decides whether an action is
allowed or a check passed.

This is why prompt injection is structurally inert here rather than defended
against: `tools/policy_engine.py` takes no free-text estate content as input, so
there is no channel through which a malicious table comment could reach an
authorization decision.

**2. Estates are configuration, not code.**

Onboarding a second estate means adding an adapter, a Migration Pack, an estate
document and one registry entry. It never means editing the six core agents.

That claim is enforced mechanically, not by review:
`tests/test_clean_estate_onboarding.py` greps every file under `agents/` for any
mention of the second source. A single `if source == "postgres"` inside an agent
would pass every other test in the suite while quietly making the claim false —
so a test, not a reviewer, holds the line.

---

## Architecture

```
                 +------------------------------------------+
 CONTROL PLANE   |  Discovery -> Lineage -> Risk -> Planner  |
 reasoning,      |          -> Validation -> Cutover         |
 policy, plans   |  resolved by CAPABILITY, never by import  |
                 +--------------------+---------------------+
                                      | authorized, scoped request
                                      v
                 +------------------------------------------+
 DATA PLANE      |  extract -> load -> deterministic         |
 bulk movement   |  reconciliation (counts, sums, hashes)    |
 no LLM involved +--------------------+---------------------+
                                      v
                                  BigQuery
```

### The state machine is the spine

`agents/orchestrator/run_lifecycle.py` owns one Firestore document per run and a
hard-coded legal transition graph. Illegal transitions raise; there are no silent
state jumps.

```
REQUESTED -> DISCOVERED -> ANALYZED -> RISK_ASSESSED -> PLANNED -> MIGRATING
          -> VALIDATING -> (FAILED -> INVESTIGATING -> REMEDIATING -> VALIDATING)
          -> PASSED -> READY_FOR_APPROVAL -> APPROVED -> CUTOVER -> MONITORING
          -> COMPLETE
```

### Agents never call each other

`tools/registry.py` is a real Agent Registry. The orchestrator calls
`registry.invoke_capability("discovery.catalog.estate", ...)`, which resolves the
APPROVED card advertising that capability and dynamically imports its handler.
There is no `from agents.discovery.agent import ...` anywhere in the dispatch
path.

Capabilities in use: `discovery.catalog.estate`, `lineage.graph.build`,
`risk.assess.estate`, `planner.plan.propose`,
`validation.reconcile.source_target`, plus a wildcard `impact.assessment.*` for
cross-department agents.

Publishing is two-step (`publish()` to DRAFT, `approve()` to APPROVED) and
`approve()` refuses when `approved_by == published_by` — the same separation of
duties the human cutover approval enforces.

### Estates, adapters and packs

| Concept | Lives in | Answers |
|---|---|---|
| **Estate** | `estates/{id}` in Firestore, or `config/estates/*.yaml` | Which systems, reached how |
| **Source adapter** | `tools/adapters/` | How to talk to one source family |
| **Migration Pack** | `packs/{pack_id}/pack.yaml` | Type rules, classification and dialect notes for a source-to-target pattern |
| **Migration Plan** | `migration_runs/{run}/migration_plan/current` | Which tables this run migrates, on which columns |

Adapters declare `capabilities`, and the console acts on the declaration — an
assessment-only source is visibly disabled rather than offered and then failed.
The declaration is held honest by a parametrized contract test: a declared
capability must be backed by a real override, not an inherited stub.

| Adapter | Capabilities |
|---|---|
| `sqlserver` | discover, health, reconcile, transfer |
| `postgres` | discover, health, reconcile, transfer |
| `oracle_corpus` | discover *(static DDL, no live server)* |
| `dag_artifacts` | discover *(pipeline metadata only)* |

The Planner derives what to migrate from discovered metadata: the key column
from the primary key, the aggregate column from the pack's numeric type rules,
the null-check column from column nullability. A table with a **composite**
primary key is blocked with a stated reason rather than migrated on a guess,
because the extractor orders by a single key and reconciliation compares ordered
key lists. A table with no numeric column records `aggregate_check:
not_applicable` rather than comparing against a fabricated zero.

### Credentials are references, never values

Estate documents carry a Secret Manager reference or the **name** of an
environment variable. `tools/secret_resolver.py` resolves it at connect time and
wraps the result in a type whose `repr` redacts itself, so logging the object
cannot leak the password.

`ConnectionProfile` is a closed schema at both the contract and API layers, and
422 responses are stripped of the rejected input — otherwise refusing a
submitted password would echo it straight back into the caller's console and
logs.

When Secret Manager is unavailable, resolution falls back to the declared
environment variable and **logs a WARNING naming which path answered**. A
fallback firing unnoticed in production yields a *working* connection to the
wrong database, so it is never silent, and `health_check()` reports which
backend authenticated.

---

## Setup

### Prerequisites

Python 3.11+, Node 22, Docker, and a GCP project with Firestore, Pub/Sub and
BigQuery enabled.

### 1. Install

```bash
cd data-migration-control-tower && python -m venv .venv && pip install -r requirements.txt && cp .env.example .env
```

Then fill in `GCP_PROJECT_ID` and credentials in `.env`.

### 2. Start the source estates

```bash
cd data-migration-control-tower/simulator/source_setup && docker compose up -d && ./restore_wwi.sh
```

```bash
make second-estate
```

The second estate is a PostgreSQL fixture on port **5433**, not 5432: a
developer machine often already runs Postgres, and a fixture that silently
connects to your real database is worse than one that refuses to start.

### 3. Provision GCP and seed the registries

```bash
bash infrastructure/gcp_setup.sh
```

```bash
make seed
```

`make seed` is idempotent. Nothing can resolve an agent capability until the
Agent Registry is seeded, and nothing can resolve a source connection until an
estate exists.

### 4. Run it

```bash
python run_ui.py
```

That is the only command an operator runs. The console publishes a durable
Pub/Sub command for every action, and the event consumers run inside the
same process — assessment, migration, retry, approval and cutover are all
initiated *and completed* from the browser. **System Health → Event
consumers** shows the eight consumers, who holds the worker lease, and lets
an operator pause or resume any of them.

Two supervisors in one deployment is normal rather than exceptional
(`uvicorn --reload` runs a reloader parent and a child; Cloud Run
autoscales), so consumption is gated on a Firestore lease and the loser
idles in standby. Set `CONTROL_TOWER_WORKERS=0` for a console-only
process — the deployed image does exactly that, because it carries neither
the Discovery fixtures nor the ODBC runtime.

The CLI chain still works and still proves the same thing end to end:

```bash
make run
```

For the step-by-step operator flow — onboarding an estate through the
console, running the worker that consumes each queued command, and driving
the full lifecycle to cutover — see
[docs/RUNBOOK.md](data-migration-control-tower/docs/RUNBOOK.md).

That drives the full lifecycle: discovery through planning, a deliberately
seeded row-loss defect, memory-assisted investigation, deterministic
remediation, re-validation, human approval and cutover.

---

## Verifying it works

```bash
make test
```

| Suite | Count | Command |
|---|---|---|
| Backend | 556 | `make test` |
| Component (vitest) | 37 | `cd frontend/client && npm test` |
| Browser (Playwright + axe) | 25 | `cd frontend/client && npm run test:e2e` |

Backend tests run against **live Firestore**, so any test creating a run or a
registry card must delete it in teardown — a leaked run becomes the console's
"active run". Tests needing SQL Server, Postgres or BigQuery skip automatically
when those are unreachable.

### The release gate

```bash
make onboarding-gate
```

This is the clean-estate onboarding test: a second estate, a different database
engine, discovered and planned with zero edits under `agents/`. It also
exercises paths WideWorldImporters cannot — WWI contains no composite primary
keys, so without the Postgres fixture the blocked-target path would ship
untested.

---

## The console

```bash
uvicorn frontend.app:app --reload --port 8080
```

Oracle JET 20.1.3 and Preact on the Redwood theme: eleven operational routes
plus an estate onboarding wizard at `/estates/new`. The wizard collects
credential *references* — there is no password input anywhere in it, asserted by
both a component test and a browser test.

Roles come from Firebase custom claims and are **scoped per estate**:

```jsonc
{"estate_roles": {"*": ["viewer"], "acme-finance": ["operator", "approver"]}}
```

A global `operator` was defensible with one estate; with several it means
someone onboarded for one customer can act on another's. Estate authorization is
an explicit in-handler call rather than a dependency, because a FastAPI
dependency cannot read an arbitrary request-body field — and a test enumerates
every mutating route to prove none of them skips it.

---

## Repository layout

```
agents/          the six core agents + orchestrator (estate-agnostic)
tools/           deterministic, framework-free logic: adapters, registry,
                 policy engine, reconciliation, plan builder, secrets
contracts/       metadata_model.json, the model of record for every entity
packs/           Migration Packs: source-to-target rules and type mappings
config/estates/  estate documents (drop in a YAML, no code change)
policies/        agent permissions, data classification, wave limits
frontend/        FastAPI API + Oracle JET console
simulator/       source estates: WideWorldImporters, Postgres, Oracle corpus
evaluation/      scenario harness and scale reports
tests/           556 backend tests
```

Detailed day-by-day build history, including what each stage proves and why
specific decisions were made, is in
[data-migration-control-tower/README.md](data-migration-control-tower/README.md).

---

## Honest limits

Stated plainly rather than discovered later:

- **Execution against Postgres is unproven.** Its pack is `assessment` mode by
  design; discovery, planning and reconciliation are exercised, but no
  Postgres-to-BigQuery load has run.
- **Secret Manager is unproven live.** Local runs use the documented environment
  fallback, and `health_check` says so explicitly.
- **Workers are in-process, not a managed runtime.** The consumers run as
  threads inside the API process behind a Firestore lease. That is the rung
  below the production shape (Cloud Run + Eventarc push subscriptions), and
  it is not a pretence of being it: no subscription has a dead-letter
  policy, and Pub/Sub caps a message's outstanding lifetime near an hour
  regardless of lease extensions, so a handler exceeding that will be
  redelivered and re-run.
- **The data plane is in-process.** `DataPlaneExecutor` is a real interface with
  one in-memory implementation; a Dataflow-backed executor is not attempted.
- **Scale figures are bounded.** The harness measures 100-500 synthetic
  definitions, not the 20,000 the design targets.
- **Firebase custom claims cap near 1000 bytes**, so per-estate role grants stop
  scaling past roughly 15-20 estates; the escape hatch is a Firestore grants
  collection with claims as a cache.

## Data sources

WideWorldImporters is Microsoft's sample database. See
[DATA_SOURCES.md](data-migration-control-tower/DATA_SOURCES.md) for attribution.
