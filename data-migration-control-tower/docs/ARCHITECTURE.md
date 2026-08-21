# Architecture

What actually runs today, what the Deploy & Harden effort (`docs/adr/`) is changing it into, and
where each piece lives in the repo. Section references (§N) point back to the master
documentation this repo implements.

## The shape today: one process, event-driven internally

```
┌─────────────────────────── one Python process ───────────────────────────┐
│                                                                            │
│  frontend/app.py (FastAPI)          tools/worker_supervisor.py           │
│  Control Tower console + API   ┌──> WorkerSupervisor: 9 consumer threads │
│                                 │    pulling Pub/Sub subscriptions,       │
│                                 │    each calling a handler function      │
│                                 │    directly in-process                  │
│                                 │                                         │
│  agents/orchestrator/           │                                         │
│  orchestrator.py: 7 of the 9    │                                         │
│  handlers — the state-machine   │                                         │
│  driver, dispatching via        │                                         │
│  tools/registry.py::            │                                         │
│  invoke_capability()            │                                         │
│         │                       │                                         │
│         ▼                       │                                         │
│  agents/{discovery,lineage,     │                                         │
│  risk,planner,validation,       │                                         │
│  cutover,finance}/agent.py      │                                         │
│  — dynamically imported and     │                                         │
│  called, same process           │                                         │
└────────────────────────────────┴─────────────────────────────────────────┘
              │                                    │
              ▼                                    ▼
      Firestore (state, registry,           BigQuery (migration
      policy decisions, memory)             target, reconciliation)
```

`tools/worker_supervisor.py`'s own docstring calls this "the rung below the production shape
(Cloud Run + Eventarc push subscriptions)" — stated plainly, not hidden. Only `hello-agent`
(`agents/orchestrator/hello_agent/`) has ever been deployed to Cloud Run as its own service; every
other agent's code runs inside this one process under Application Default Credentials, regardless
of the seven distinct IAM service accounts `infrastructure/gcp_setup.sh` declares for them.

**Staging deployment note** (Deploy & Harden Phase 0): this project deploys into the same GCP
project (`autonomous-data-migration`) it develops against, rather than a separate staging project
— simpler for a single trial-credit-funded window, at the cost that one infrastructure mistake
(a bad `terraform apply`, an over-broad IAM grant) affects the only environment there is. Accepted
explicitly, not by default.

## The state plane: Firestore

Every agent, tool, and the UI read/write `migration_runs/{run_id}/*` subcollections plus a handful
of global collections:

| Collection | Owner | Purpose |
|---|---|---|
| `migration_runs/{run_id}` | `agents/orchestrator/run_lifecycle.py` | The run document — state, `state_history`, pinned agent versions |
| `migration_runs/{run_id}/policy_decisions` | `tools/policy_engine.py` | Every ALLOW/DENY/REQUIRE_APPROVAL, run-scoped |
| `migration_runs/{run_id}/usage_events` | `tools/usage_meter.py` | Measured model/BigQuery usage, priced from `contracts/price_book.json` |
| `migration_runs/{run_id}/budget/bigquery` | `tools/usage_meter.py::reserve_bigquery_budget()` | Per-run cumulative byte reservation counter (Phase 1c) |
| `agent_registry/{agent_id}/versions/{version}` | `tools/registry.py` | AgentCards — publish/approve/discover/deprecate |
| `policy_decisions` (global) | `tools/policy_engine.py` | Ad hoc policy checks with no `run_id` |
| `memory_bank` (global) | `tools/memory_bank.py` | Cross-run remediation memory, keyed by defect signature |
| `estates` | `tools/estate_registry.py` | Registered source estates |

Two separate Firestore *databases* exist: `(default)` (what the console reads/writes) and a
dedicated test database (`mct-tests` by default, `tests/probes.py::resolve_test_database()`) that
`pytest` targets so test runs never pollute production data. **A fix applied to one does not reach
the other automatically** — re-seed both explicitly (`python infrastructure/seed_registry.py` for
`(default)`, `python -m infrastructure.seed_test_database` for the test database) after any change
to seeded data. This was a real gap found during Deploy & Harden Phase 1 (see `docs/adr/0001`).

## The event backbone: Pub/Sub

Topics (`infrastructure/gcp_setup.sh`): `migration.requested`, `discovery.completed`,
`risk.assessed`, `plan.created`, `validation.requested`, `validation.failed`, `validation.passed`,
`cutover.approved`, `cutover.completed`, `assessment.requested`, `wave.override.requested`, plus a
shared `dead-letter` topic every subscription forwards to after repeated delivery failures. A few
topics (`risk.blocked`, `plan.approved`, `migration.completed`) are reserved by the original
design but not currently published to — declared for forward compatibility, not dead code to
remove.

Each subscription has its own dedicated consumer (`tools/worker_supervisor.py::default_specs()`)
so the CLI drivers and the console's event consumers never steal each other's messages. Delivery is
at-least-once with idempotent redo (`_dedup_claim`/`_dedup_complete`, a Firestore transaction) —
this remains true regardless of how many separate services eventually consume these topics
(Deploy & Harden Phase 2's distributed deployment does not relax this; see `docs/adr/0002`).

## Dispatch: the registry, not hardcoded imports

`tools/registry.py::invoke_capability(capability, *args, **kwargs)` is the orchestrator's only
route to an actor. It resolves the highest-version APPROVED AgentCard advertising that capability
and dynamically imports/calls its `handler` string — there is no `from agents.discovery.agent
import ...` anywhere in the dispatch path. New agents/capabilities must be seeded
(`infrastructure/seed_registry.py`) before the orchestrator can find them.

As of Deploy & Harden Phase 1a, every `invoke_capability()` call also passes through a
capability-dispatch policy gate — see `docs/GOVERNANCE.md` for the full two-layer enforcement model
this project uses (coarse dispatch-level gate + fine-grained tool-level checks).

## The data plane: `InMemoryExecutor` by default, `CloudRunJobExecutor` opt-in

`tools/migration_executor.py::DataPlaneExecutor` is an abstract interface. `InMemoryExecutor`
streams rows through the orchestrator's own process memory — not a managed service, and its own
docstring says so plainly; it remains the default everywhere (local dev, tests, the WWI/SQL Server
path — nothing about its behavior changed in Phase 3).

`CloudRunJobExecutor` (Deploy & Harden Phase 3, `docs/adr/0003-async-data-plane-job.md`) is a
second, additive implementation, selected only when `DATA_PLANE_EXECUTOR=cloud_run_job` is set: it
submits a genuinely separately-deployed Cloud Run Job (`tools/data_plane_job/run_job.py`, its own
narrowly-scoped service account) and returns immediately rather than blocking — the job writes its
result and publishes `migration.completed`, which a new orchestrator consumer
(`handle_migration_completed`) consumes to resume the lifecycle. `agents/orchestrator/orchestrator.py::
handle_planned()` gained a conditional branch for this (unset env var = today's synchronous
behavior, byte-for-byte unchanged). Implemented and tested; not yet deployed live — needs
`infrastructure/terraform`'s `enable_data_plane_job` and `enable_cloud_sql` (both `false` by
default) and a real Cloud SQL for PostgreSQL instance to point at.

## Target shape (Deploy & Harden Phase 2)

Nine Cloud Run services + one Cloud Run Job, replacing the single process above:
Frontend/API, Orchestrator (the 7 state-machine-step consumers), Discovery (capability + its
assessment consumer), Lineage, Risk, Planner, Validation, Cutover (capability + its consumer),
Finance-impact — plus the data-plane Job. Agent-to-agent calls become typed HTTP (a versioned
envelope, OIDC-verified, schema-checked — `docs/adr/0002-typed-http-dispatch.md`), not dynamic
Python import, so each service genuinely runs under its own IAM service account rather than the
orchestrator's. Infrastructure moves to Terraform/OpenTofu (`docs/adr/0004`) rather than continued
`gcloud` scripting, so the target topology has one declarative source of truth.

## Known constraints, stated rather than hidden

- **On-prem network reachability**: Cloud Run cannot reach a SQL Server hosted on a laptop's local
  Docker network without a tunnel. The live WWI demo source stays local-only until/unless that's
  addressed; Phase 3's cloud-reachable proof instead uses a new Cloud SQL for PostgreSQL instance
  and a new immutable execution-capable pack (not a mutation of the existing assessment-only
  `postgres_retail_v1`).
- **Fallback patterns** (CLAUDE.md's list): ADK import failure → direct tool-call fallback;
  Vertex AI call failure → deterministic narrative template; Gemini vision failure → hardcoded
  schema matching; `tools/fast_pii_screen.py` stands in for a Gemma pre-screen with a deliberately
  naive independent keyword classifier. All fire automatically and log which path answered — this
  is intentional degradation-ladder behavior (§19), not something to "fix" by removing the check.
