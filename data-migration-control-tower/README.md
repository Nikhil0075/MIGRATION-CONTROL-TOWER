# Autonomous Data Migration Control Tower

A governed fleet of specialized AI agents discovers legacy data pipelines,
reconstructs lineage, assesses migration risk, proposes migration plans,
validates source-to-target data, learns from prior incidents, and
coordinates a human-approved production cutover. Probabilistic AI
reasoning is surrounded by deterministic enterprise controls.

Built for the All Things Agentic Hackathon, Track 3 — Fortified Enterprise
Fleet. Full design is in the master documentation this repo implements;
day-by-day build order follows the Revised Master Schedule (16–31 Aug
2026).

This README is the build log: what was added on each day, what it proves,
and why specific decisions were made. It runs from **Day 1** (foundation)
through the **six-agent fleet** (Day 5), the **Agent Registry and
cross-department capability discovery** (Day 6), the **Memory Bank and
kill-and-resume durability** (Day 7), **multimodal drift discovery and
injection defense** (Day 8), the **Control Tower UI** (Day 9), the
**evaluation harness and hardening phases** (Day 10), and **multi-estate
portability** (Day 11 — the platform becomes plug-and-play).

For a top-level overview — architecture, setup and the current shape of the
system rather than how it got here — see the [repository README](../README.md).

## Prerequisites

- Docker Desktop running
- Python 3.11+ (`python --version`)
- `gcloud` CLI, authenticated as the GCP account you intend to use, with
  `gcloud auth application-default login` also run for that account
- A GCP project created and billing (or free-tier trial) linked — this
  repo's scripts do **not** create the project or link billing; that is
  an interactive console step you do once, outside of any script here
- ODBC Driver 18 for SQL Server installed locally (for `pyodbc`) —
  https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server

## Setup

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env             # then fill in real values
```

### 1. Legacy source estate (SQL Server / WideWorldImporters)

```bash
cd simulator/source_setup
docker compose up -d
./restore_wwi.sh
```

This restores Microsoft's official WideWorldImporters OLTP sample into a
local SQL Server 2022 container. See [DATA_SOURCES.md](DATA_SOURCES.md)
for attribution.

The Oracle-dialect script corpus (`oracle_dialect_corpus/`) and DAG
artifact stubs (`dags/`) are static files — no database container needed
for those; see §18.3 of the master doc for why.

### 2. GCP infrastructure

```bash
gcloud config set account <your-account>
gcloud config set project <your-project-id>
bash infrastructure/gcp_setup.sh
```

Idempotent — enables required APIs, creates the Firestore database,
BigQuery dataset, Pub/Sub topics, the `sa-orchestrator` service account,
and (Block C) a distinct service account per agent
(`sa-discovery`, `sa-lineage`, `sa-risk`, `sa-planner`, `sa-validation`,
`sa-cutover`, `sa-finance-impact`). Safe to re-run.

```bash
python infrastructure/seed_registry.py          # publish+approve the 6 fleet agents
python infrastructure/seed_finance_agent.py     # publish+approve the cross-department Finance agent
```

Populates the Agent Registry (`agent_registry/{agent_id}/versions/{version}`
in Firestore) — required before running the orchestrator, which resolves
every actor by capability query against these cards, not by hardcoded
import.

### Public-release AI, reports and assistant gates

The production hardening features are deliberately off by default:

```bash
ENABLE_AGENT_REASONING_V2=1   # Gemini 3.7 Flash high, structured + audited
ENABLE_REPORTS=1              # private SHA-addressed PDF/JSON artifacts
ENABLE_AI_ASSISTANT=1         # Gemini 3.5 Flash medium, read-only + cited
REPORTS_BUCKET=<private-bucket-created-by-gcp-setup>
GOOGLE_CLOUD_LOCATION=global
```

Do not enable them publicly until the live acceptance run passes. The setup
script grants Vertex AI only to model-running identities, creates the private
report bucket with retention, and enables 30-day Firestore TTL for assistant
data. The console never stores chain-of-thought: it exposes final rationale,
evidence references, tool outcomes, validation state, model/token usage and
fallback status in the Agents decision trail.

Reports are generated asynchronously from persisted run evidence and are
downloaded only through authenticated estate-scoped API routes. The global
assistant is read-only; it has no operation or approval tools.

Before testing a public hostname, add that exact hostname in Firebase
Authentication → Settings → Authorized domains and include its HTTPS origin in
`UI_ALLOWED_ORIGINS`. Keep `localhost` and `127.0.0.1` authorized for local
acceptance. The application CSP limits the Firebase popup/redirect flow to the
required Google origins; it does not weaken `script-src` for arbitrary CDNs.

### 3. Day 1 — hello-agent

```bash
python agents/orchestrator/hello_agent/local_run.py       # local: reads WWI table list
```

See [agents/orchestrator/hello_agent/README.md](agents/orchestrator/hello_agent/README.md)
for the Cloud Run deploy steps (build context must be the repo root, and
`--memory 1Gi` is required for the real `google-adk` import chain).

### 4. Day 2 — Discovery Agent

```bash
python agents/discovery/run_discovery.py
```

Creates a new migration run, inventories the WWI tables + Oracle corpus +
DAG artifacts, and persists the structured catalog under
`migration_runs/{run_id}/catalog/*` and `.../pipelines/*` in Firestore.

### 5. Day 5 — the full milestone, one command (recommended path)

```bash
python agents/orchestrator/run_full_migration.py
```

The complete path **DISCOVERED -> ... -> COMPLETE** in one run: Day 4's
event-driven Discovery -> Lineage -> Risk cascade, a Migration Planner
plan, a migration execution with a deliberately seeded row-loss defect,
a failed validation, an investigate/remediate recovery loop, a second
(passing) validation, a human-approved cutover, and post-cutover
monitoring — matching master doc §17.1's Thu-20-Aug milestone: *"full
path DISCOVERED → … → COMPLETE with one denial, one reconciliation
failure, one remediation, one approval."* The script prints a summary
tallying exactly those four events at the end. See
[agents/orchestrator/README.md](agents/orchestrator/README.md).

### 5a. Or step by step (useful for debugging one stage at a time)

```bash
python agents/discovery/run_discovery.py                 # Day 2
python agents/lineage/run_lineage.py                      # Day 4: derive dependency edges
python agents/risk/run_risk.py                            # Day 3: classify + prove PII denial
python agents/planner/run_planner.py                      # Day 5: propose the migration plan
python simulator/failure_injector/seed_row_loss.py         # Day 3/5: migrate with a seeded ~1% row-loss defect
python agents/validation/run_validation.py                 # Day 3: run deterministic reconciliation (FAILS)
# recovery loop is orchestrator-only today (agents/orchestrator/recovery.py) —
# see run_full_migration.py for investigate()/remediate() usage, or re-run
# seed_row_loss.py-equivalent logic with drop_fraction=0.0 and re-validate
python agents/cutover/run_cutover.py                       # Day 5: request approval (state PASSED)
python agents/cutover/approve_cutover.py                   # Day 5: the human step
python agents/cutover/run_cutover.py                       # Day 5: perform cutover + monitoring (state APPROVED)
```

Each script defaults to the most recently created run, so this chains in
order. What this proves (master doc §17.1's Day-3 through Day-5 exit
conditions):

- **Lineage**: dependency edges are *derived*, not seeded — from each
  run's DAG-declared `upstream_tables`/`downstream_tables` (confidence
  1.0) and a regex SQL parse of the Oracle-corpus view definitions
  (confidence 0.85), written to `migration_runs/{run_id}/dependencies/*`.
- **Orchestrator**: `agents/orchestrator/run_lifecycle.py` now enforces
  the full §8 state machine — an illegal transition (e.g. skipping
  straight to `RISK_ASSESSED`) raises `ValueError` instead of silently
  succeeding. `tools/events.py` + `agents/orchestrator/orchestrator.py`
  drive real state transitions off real Pub/Sub messages.

- **Risk**: every discovered table gets a real classification (not
  Discovery's `UNCLASSIFIED` placeholder) via `tools/data_classifier.py`
  against `policies/data_classification.yaml`; dialect-incompatibility
  and critical-dependency findings are written to
  `migration_runs/{run_id}/risk_findings/*`; an unauthorized raw-PII
  read attempt is evaluated by the deterministic policy engine
  (`tools/policy_engine.py` against `policies/agent_permissions.yaml`)
  and denied — recorded in `.../policy_decisions/*`.
- **Planner**: `tools/plan_builder.py` proposes a `MigrationPlan`
  (target table names, execution order, SQL translation notes for
  dialect-incompatible tables, a `plan_hash` that later binds the
  approval token) written to `.../migration_plan/current`.
- **Migration executor**: `tools/migration_executor.py` is the single
  real source->BigQuery copy path, used both for the plan's first-pass
  execution and by the failure injector. Spatial types (`geography`) are
  carried via SQL Server's own `.ToString()` rather than silently
  dropped, so a clean reload can genuinely pass every check, including
  schema. Every execution is logged to
  `.../migration_executions/*`.
- **Validation**: five deterministic checks (schema, row_count,
  aggregate, null_profile, hash) run against real source/target data,
  written to `.../reconciliation/*` — no model ever decides whether
  reconciliation "looks okay." The seeded row-loss defect fails
  `row_count`, `hash`, and `aggregate`.
- **Recovery loop** (`agents/orchestrator/recovery.py`): `investigate()`
  builds an `Incident` from the failed checks' evidence and a Lineage
  traversal identifying the responsible pipeline — the root-cause
  *narrative* is attempted via a direct Gemini call (§9's Gemini-eligible
  half) with a deterministic template as an honest fallback if that call
  isn't available; `remediate()` applies the one pre-cataloged
  deterministic fix (clean reload) and the run re-validates, now passing.
- **Cutover**: requests human approval (`READY_FOR_APPROVAL`); a
  self-approval attempt is evaluated by the policy engine and denied —
  `tools/approval_service.approve()` is only ever called by
  `approve_cutover.py`, standing in for an actual person, never by agent
  code. Once approved, cutover performs and a one-shot post-cutover
  reconciliation check confirms `COMPLETE`.

### 6. Block C / Day 6 — Agent Registry, Identity, cross-department discovery

Once the registry is seeded (step 2 above), `run_orchestrator.py` and
`run_full_migration.py` work exactly as before, except the orchestrator
now resolves Discovery/Lineage/Risk by capability query
(`tools/registry.py`), not `from agents.discovery.agent import ...`:

```bash
python agents/orchestrator/run_orchestrator.py
```

What this proves (master doc §20):

- **No hardcoded agent names**: `agents/orchestrator/orchestrator.py`'s
  dispatch calls `registry.invoke_capability("discovery.catalog.estate",
  ...)`, which discovers the APPROVED card, dynamically imports its
  `handler`, and calls it. Deprecate a card and the orchestrator can no
  longer find it — there is no fallback import to catch the fall.
- **Publisher cannot self-approve**: `registry.approve()` raises
  `PermissionError` if `approved_by == published_by`, the same
  separation-of-duties pattern as `tools/approval_service.py`'s human
  cutover approval.
- **Cross-department discovery** (§20.3): the Finance Reporting Impact
  Agent is owned by "Finance Systems" (not Technology), published and
  approved by distinct Finance-Systems identities
  (`infrastructure/seed_finance_agent.py`), and invoked by the
  orchestrator purely via a `impact.assessment.*` wildcard capability
  query — `orchestrator.py` has no import of, or knowledge about,
  `agents/finance/impact_agent.py`. It reports which real
  Lineage-derived views look like finance reports and which source
  tables feed them.
- **Negative proof**: deprecating the Finance agent and re-running
  causes the orchestrator to log "no approved provider" and continue
  (rather than crash or invent an answer) — the exact §20.3 requirement:

  ```bash
  python infrastructure/seed_finance_agent.py --deprecate
  python agents/orchestrator/run_orchestrator.py   # finance_impact: None, no crash
  python infrastructure/seed_finance_agent.py       # re-approve for next time
  ```
- **Version pinning** (§20.4/§21.1): every resolved agent's version is
  written into the run record's `pinned_agents` map before dispatch —
  `get_run(run_id)["pinned_agents"]` shows exactly which card version
  handled each stage.

### 7. Block C / Day 7 — Memory Bank, durability, long-horizon fixture

```bash
python agents/orchestrator/run_full_migration.py       # run 1: resolves + confirms the row_loss incident into memory
python agents/orchestrator/run_full_migration.py       # run 2: recalls it — see the "Recalled from memory" root cause
python agents/orchestrator/durability_demo.py           # kill-and-resume across separate OS processes
python agents/orchestrator/seed_long_horizon_fixture.py # labeled ~3-week backdated fixture
```

What this proves (master doc §5.3, §21):

- **Cross-run memory recall** (§21.2 proof 3): `tools/memory_bank.py` is
  a global collection (`memory_bank/{signature}`), deliberately *not*
  under any run — §21.1's session-vs-memory rule enforced structurally,
  not just by convention. `agents/orchestrator/recovery.py`'s
  `investigate()` checks it first; when a matching signature is found,
  the Incident's `root_cause_generated_by` becomes `'recalled_memory'`
  and cites the prior run — but deterministic re-validation still runs
  afterward exactly as before. `close_incident()` confirms newly
  resolved incidents into memory so later runs can recall them. This is
  the §19 Rung-3 shape (deterministic keyed lookup on a normalized
  signature like `row_loss:Sales.Customers`), not vector similarity —
  honestly scoped to what's actually verifiable without embedding
  infrastructure.
- **Kill-and-resume durability** (§21.2 proof 1): this project doesn't
  deploy each agent as its own Cloud Run service yet, so
  `agents/orchestrator/durability_demo.py` proves the underlying claim
  the Cloud Run version would prove — compute is disposable, state lives
  independently of any process — using genuinely separate OS processes
  instead. It pauses a run at `READY_FOR_APPROVAL`, then resumes it
  through two more `subprocess.run()` calls (fresh `python`
  interpreters, zero shared memory with the process that got the run
  this far) and verifies the full trace is intact afterward.
- **Long-horizon fixture** (§21.2 proof 2): `seed_long_horizon_fixture.py`
  runs the real pipeline end to end and backdates exactly two
  timestamps — the run's `created_at` (~21 days ago) and the approval's
  `requested_at` (~18 days ago) — then approves for real, today,
  producing a genuine 18-day `elapsed_days` gap. Both documents are
  stamped `is_seeded_fixture: true` with a human-readable
  `fixture_label` directly in Firestore — per §21.2's explicit warning,
  *"a judge who discovers an unlabeled backdated run will discount
  everything else in the submission,"* this is never left implicit.

### 8. Block C / Day 8 — multimodal discovery & guardrails

```bash
python simulator/documentation/generate_fixtures.py   # one-time: builds the ERD PNG + PDF fixtures
python agents/risk/run_risk.py                          # now also runs the fast screen + documentation-drift check
pytest tests/test_injection_defense.py -v                # the 12-case injection corpus
```

What this proves (master doc §22, §23):

- **Documentation-drift risk finding** (§22.2, this build day's exit
  condition): `tools/multimodal_discovery.py` extracts the documented
  schema from a self-authored ERD image and PDF data dictionary
  (`simulator/documentation/`, real Gemini vision/file call attempted
  first, deterministic fallback if unavailable — either way labeled via
  `extraction_method`) and diffs it against the run's real catalog.
  Every seeded discrepancy is itemized in
  `simulator/documentation/README.md` — e.g. a documented `EmailAddress`
  column that doesn't exist (`MISSING_IN_ACTUAL`), a `PhoneNumber`
  documented `PUBLIC` that the estate classifies `PII`
  (`CLASSIFICATION_GAP`), an undocumented real `NATIONAL_ID` column
  (`MISSING_IN_DOCUMENTED`) — each written as a `RiskFinding` with
  evidence tracing back to the source artifact.
- **Fast PII pre-screen** (§22.3): `tools/fast_pii_screen.py` is a
  documented Gemma substitution (Ollama is installed but no model was
  pulled — confirmed with the user rather than downloading one
  unprompted) that plays the same architectural role: a cheap, broad,
  independent first pass. Disagreements with the careful classifier —
  in *either* direction — are recorded as
  `SENSITIVITY_SCREEN_DISAGREEMENT` findings, never silently resolved.
- **Guardrails & injection defense** (§23): `tools/untrusted_content.py`
  is the deterministic containment layer — the untrusted-content
  envelope shape and a pattern-based scan. `simulator/injection_corpus/`
  has 12 self-authored adversarial payloads across the 4 required
  families; `tests/test_injection_defense.py` asserts a real,
  family-specific containment invariant for each against this
  codebase's actual functions (e.g. a fabricated "tool" name is
  verified to resolve to zero registry providers — capabilities only
  ever come from explicit `publish()` calls, never parsed estate
  content) and records a `containment_event`.

### 9. Block C / Day 9 — the Control Tower UI

```bash
uvicorn frontend.app:app --reload --port 8080
```

Open http://localhost:8080/overview — matching master doc §11's exit
condition: *"a judge can follow a complete run visually without reading
logs."* The production surface is Oracle JET 20.1.3 VDOM with Preact,
TypeScript, responsive Core Pack drawers, and the native Redwood theme.
Eleven workspaces cover estates, assessments, waves, runs, lineage,
reconciliation, policies/approvals, agents, evaluations, and system health.
New writes use role-gated, idempotent `/api/v1` operation requests; the prior
vanilla UI remains temporarily at `/legacy`. See
[frontend/README.md](frontend/README.md).

Adding this surface required a real, if small, structural change:
`run_lifecycle.py` now records a `state_history` array on every run
(added this build day) — previously only the *current* state was
persisted, which isn't enough to render a timeline.

### 10. Block C / Day 10 — evaluation harness, failure injection matrix, deployment evidence

```bash
python evaluation/run_harness.py                    # all 14 Appendix D scenarios
python evaluation/run_harness.py --skip-expensive    # skip S-11 (a second full migration run)
python evaluation/friction_report.py                 # Section 25 friction table from real run data
```

Matches master doc §17.2's Fri 28 Aug definition of done: *"All
scenarios in Appendix D pass or fail reproducibly and metrics are
generated, never hand-entered."*

- **Evaluation harness** (`tools/evaluation_harness.py` +
  `evaluation/scenarios.py`): runs the fourteen S-01…S-14 scenarios from
  Appendix D against real infrastructure — no mocks. Every scenario
  function either returns evidence or raises; the harness records
  PASS/FAIL/duration/evidence for each to Firestore
  (`evaluation_runs/{harness_run_id}/scenarios/`) and writes a generated
  JSON + Markdown report to `evaluation/reports/` — nothing here is
  typed into the write-up by hand. Five scenarios (S-02, S-04, S-13,
  S-14, S-11) need evidence from a real end-to-end migration; four of
  them share ONE `advance_to_passed()` call (memoized per harness run)
  to keep cloud cost bounded — only S-11, which specifically proves
  durability *after* CUTOVER, needs its own separate run and is flagged
  `--skip-expensive`-skippable for a fast/cheap pass.
- **New deterministic checks** this scenario catalog required and that
  didn't exist before Day 10: `check_schema_types` (column *type*
  mismatch, not just name-set mismatch — S-01), `check_uniqueness`
  (duplicate keys within the target — S-05), a configurable
  `null_profile` tolerance read from
  `policies/reconciliation_tolerances.yaml` instead of a hardcoded
  number (S-06), and `tools/lineage_graph.find_unresolved_dependencies`
  wired into the Planner so a table with a dangling upstream dependency
  is marked `execution_blocked` and never scheduled (S-07).
- **A real bug this work surfaced**: `agents/orchestrator/orchestrator.py`
  now dedups Pub/Sub message redelivery via a `processed_messages`
  Firestore ledger (S-12) — Pub/Sub is at-least-once, so the same
  `migration.requested` message can legitimately arrive twice. Building
  and testing this uncovered a second, unrelated bug: any direct call to
  `handle_migration_requested()` (bypassing the normal `run_once()`
  publish→pull round trip — which is exactly what S-12's scenario and
  this file's own tests do) still really publishes `discovery.completed`
  to Pub/Sub. Left undrained, that message sits in
  `discovery-completed-sub` and gets picked up by the *next* unrelated
  `pull()` — a live `advance_to_passed()` call handed a stale, possibly
  already-deleted `run_id` and crashing on `pin_agent_version()`'s
  `.update()`. Fixed by draining that subscription in every test/scenario
  that calls the handler directly, documented in
  `tests/test_evaluation_harness.py::_drain_discovery_completed`.
- **Operational utility baseline** (§25): `evaluation/baseline_timer.py`
  is a genuine stopwatch — `start`/`stop` record real wall-clock
  timestamps to Firestore while an operator (standing in for "a real
  analyst" per §25.1's own allowance) actually performs each of the six
  manual activities against this project's real estate. This was run for
  real for this build day; the six timed activities and their methods
  are in `evaluation/reports/friction_table.md`, generated by
  `evaluation/friction_report.py` from those timings plus the matching
  fleet-measured values pulled live from a completed run's Firestore
  data (state-history wall-clock, risk findings, dependency edges,
  policy denials). Notably, the manual structural pass caught the
  documentation-drift findings but missed the seeded row-loss defect
  entirely — it has no way to see a data-level defect from schema/code
  alone, which is exactly the asymmetry §25.3 asks the write-up to lead
  with.
- **Cloud deployment + observability evidence**
  (`evaluation/reports/cloud_deployment_evidence.md`): `hello-agent`'s
  Day 1 deployment is confirmed live (`Ready=True` via `gcloud run
  services describe`) with structured Cloud Logging output showing a
  real request/response cycle. A second image, `control-tower-ui`
  (`frontend/Dockerfile`, same build-context convention as
  `agents/orchestrator/hello_agent/Dockerfile`), was built and pushed to
  Artifact Registry this build day but deliberately left undeployed
  publicly — a full UI redesign and separate branded hosting is planned,
  so shipping this version under a bare `*.run.app` URL would just be
  thrown away. Full distributed tracing via Cloud Trace (OpenTelemetry
  span propagation across agent calls) is not implemented — a
  documented, deferred Rung-2 item, not a silent gap: this project
  doesn't yet deploy each agent as its own Cloud Run service (see
  `infrastructure/README.md`'s Rung-2 substitution note), so there is no
  cross-service hop for a trace to actually span yet.

### Post-Day-10 hardening, Phase 1 — security & UI correctness

A code audit against the master doc's Volume II flagged real, verified
gaps in the Control Tower UI before continuing toward submission. Phase
1 (of a 6-phase plan; see `docs/` if a compliance matrix has landed by
the time you're reading this) fixed the cheap, high-value ones:

- **Stored XSS**: every `innerHTML` template in `frontend/static/app.js`
  interpolated unescaped Firestore-sourced strings (table/column names,
  finding text, registry owner/capability fields) — a real injection
  path given this project's own stated threat model ("legacy content is
  untrusted"). Fixed with a single `esc()` helper applied everywhere
  text enters markup, plus removing every inline `onclick="fn('${id}')"`
  attribute (a second, attribute-breakout vector) in favor of `data-*`
  attributes and delegated listeners. Regression-tested against real XSS
  payloads via `frontend/static/test_esc.js` (plain Node, no new JS
  test-framework dependency), wired into `pytest tests/` through
  `tests/test_frontend_xss.py`. Fix pass after the second audit round:
  the same script now also runs all 12 `simulator/injection_corpus/`
  prompt-injection payloads through `esc()`/`badge()` (96 checks total,
  up from 24) — one corpus, two independently-verified containment
  properties: never followed as an instruction
  (`tests/test_injection_defense.py`) and never breaks out of rendered
  HTML markup either.
- **Approval auth**: the "Approve Cutover" endpoint accepted a client-
  supplied `approver_identity` string over a wildcard-CORS API — not
  authentication. Replaced with real Firebase/Google Sign-In: the
  frontend gets an ID token, `frontend/app.py` verifies it server-side
  (`firebase_admin.auth.verify_id_token`) via a `Depends()`-based
  `get_approver_identity`, and the endpoint derives identity from the
  token's own email claim — the request body can no longer say who
  approved anything. See `frontend/README.md`'s "Approval auth" section
  for the one-time Firebase console setup this needs (same
  can't-be-scripted pattern as Day 1's GCP project creation).
- **Approval record integrity**: `tools/approval_service.py` used to
  only ever `.set()` the same `approval/current` doc — a second write
  (e.g. a re-request after a plan change) silently erased the prior
  record. Added an append-only `approval_history` subcollection; the UI
  now also shows the plan hash + scope and requires a typed
  justification before an approval fires.
- **CORS + CSP**: `allow_origins=["*"]` restricted to an env-configured
  allowlist (`UI_ALLOWED_ORIGINS`); every response now carries a
  Content-Security-Policy header plus `X-Content-Type-Options`/
  `X-Frame-Options`.
- **Real `fleet_health`**: was a hardcoded `"HEALTHY"` literal; now
  `frontend/app.py::_compute_fleet_health()` derives `DEGRADED` from a
  run stuck (stale, no further transition) in `FAILED`/`INVESTIGATING`,
  `UNKNOWN` with no run history, `HEALTHY` otherwise — simple and
  inspectable, not a scoring model.

### Post-Day-10 hardening, Phase 2 — orchestrator event-completeness

The audit correctly noted the orchestrator's own docstring admitted its
scope was `migration.requested -> Discovery -> discovery.completed ->
Lineage -> Risk -> RISK_ASSESSED` only — Planner, migration execution,
Validation, and the recovery loop all ran as direct Python calls inside
`agents/orchestrator/pipeline_stages.py::advance_to_passed()`, not
through the `plan.approved`/`migration.completed`/`validation.*` topics
`infrastructure/gcp_setup.sh` already provisioned.

`agents/orchestrator/orchestrator.py` now has four more event handlers —
`handle_risk_assessed` (Planner, publishes `plan.created`),
`handle_planned` (migration execution, publishes `validation.requested`),
`handle_validation_requested` (Validation, publishes `validation.passed`
or `validation.failed`), and `handle_validation_failed` (the recovery
loop, republishes `validation.requested` for the re-check) — extending
the same `registry.invoke_capability()` + Pub/Sub pattern already
proven for Discovery/Lineage/Risk. Three new topics
(`risk.assessed`, `plan.created`, `validation.requested`) were added
where the doc's original names didn't quite fit what the finalized
state machine actually does (see `gcp_setup.sh`'s comment on why
`risk.blocked`/`plan.approved`/`migration.completed`/`cutover.approved`/
`cutover.completed` stay provisioned-but-unused); `validation.passed`
and `validation.failed` were already provisioned and now do real work.

`agents/orchestrator/pipeline_stages.py::advance_to_passed()` — the
direct-call chain the audit flagged — is now a two-line wrapper over
`orchestrator.py::advance_through_validation()`, which drives the whole
chain via `publish()`/`pull()` pairs. `run_full_migration.py`,
`durability_demo.py`, and `evaluation/scenarios.py` needed zero code
changes: `advance_through_validation()` returns the exact same shape
`advance_to_passed()` always has. `run_once()` itself (the Day 4
exit-condition scope — `run_orchestrator.py`'s contract) is untouched;
`advance_through_validation()` is additive.

Cutover (`READY_FOR_APPROVAL -> APPROVED -> CUTOVER -> MONITORING ->
COMPLETE`) is a **deliberate** exception, documented in
`orchestrator.py`'s module docstring, not a remaining gap: the human
approval gate itself isn't a sensible thing to wire "events" around — a
person clicking "Approve" (or a script standing in for one) is the
trigger, not a message on a topic.

Verified against real infrastructure: a fresh
`python agents/orchestrator/run_full_migration.py` run completed
end-to-end unchanged in outcome (COMPLETE, one seeded row-loss defect,
one remediation, one PII-read denial, one self-approval denial, one
human approval) with `pinned_agents` on the run record now showing
`planner-agent` and `validation-agent` alongside discovery/lineage/risk
— concrete evidence those stages are now resolved through the registry
via a real Pub/Sub round trip, not a direct function call.

### Post-Day-10 hardening, Phase 3 — portability pattern

```bash
python agents/discovery/run_assessment.py packs/wwi_sqlserver_v1/pack.yaml
python agents/discovery/run_assessment.py packs/oracle_corpus_v1/pack.yaml
```

The audit's Section 32 findings: `tools/source_catalog.py` had no
adapter abstraction (`catalog_sql_server_tables()` took a raw
`pyodbc.Connection`; the Oracle-corpus and DAG catalogers were hardcoded
to fixed local paths), no estate config, no connection-profile
indirection, no Migration Pack concept, and no second-estate onboarding
test. Per the user's explicit scope call, Phase 3 builds **the pattern,
not the scale** — a real, working abstraction demonstrated end-to-end,
not the full enterprise build-out (that's out of scope without a second
live data source to prove it against).

- **`tools/adapters/`** — a `SourceAdapter` ABC (`discover_tables()`,
  `discover_pipelines()`, `fetch_rows()`) with three implementations:
  `SqlServerAdapter` and `OracleCorpusAdapter`/`DagArtifactAdapter` wrap
  the existing `tools/source_catalog.py` functions behind the interface
  — a pure refactor (`tests/test_adapters.py` asserts every adapter's
  output is byte-for-byte identical to calling the underlying function
  directly, both live against SQL Server and against the static
  corpora). `tools/adapters/__init__.py::build_adapter()` is the type
  registry a `config.yaml` source declaration resolves through.
- **`simulator/source_setup/estate.yaml`** — declares this project's own
  demo estate as data, not code: three sources (`wwi-sqlserver`,
  `oracle-corpus`, `dag-artifacts`), each naming its adapter type and
  config. Connection secrets are named (`password_secret_ref`) but
  resolved from `.env` today, honestly documented as the local-dev
  reality rather than faking a Secret Manager integration with nothing
  real behind it.
- **Migration Packs** (`packs/wwi_sqlserver_v1/`, `packs/oracle_corpus_v1/`)
  — a versioned bundle pairing one estate source with its classification
  rules, dialect notes, and default run mode
  (`contracts/metadata_model.json`'s new `MigrationPack` definition).
  Formalizes what used to be scattered, implicit WWI-only assumptions.
- **Assessment vs. execution mode**: `run_lifecycle.py::create_run()`
  gained a `mode` field. `agents/discovery/run_assessment.py` runs
  Discovery → Lineage → Risk → Planner and stops — no migration
  execution, no target write — driven entirely by a Migration Pack
  instead of hardcoded constants, and writes a generated report to
  `evaluation/reports/`.
- **The second-estate onboarding proof**: the Oracle-dialect corpus —
  already a structurally distinct source family (different dialect, no
  live database at all, per §18.3) — runs through the exact same
  `run_assessment.py` script as the real WWI SQL Server source. Verified
  live: `packs/oracle_corpus_v1/pack.yaml` produced a real assessment
  (10 tables, 6 real lineage edges from parsed SQL views, 3 PII-flagged
  tables, 10 dialect-incompatibility findings, the raw-PII-read denial
  still firing) with zero BigQuery calls — proof the adapter pattern is
  real, not just designed on paper.

### Post-Day-10 hardening, Phase 4 — scale pattern

```bash
pytest tests/test_wave_manager.py tests/test_migration_executor.py -v
python evaluation/scale_harness.py --count 250
```

The audit's findings: no per-source connection limits, risk-class
concurrency caps, backlog-age controls, or transfer quotas anywhere;
`tools/migration_executor.py::fetch_source_rows` did a single
`cursor.fetchall()` into a Python list, and `execute_migration` passed
the whole list to one `load_json_rows()` call — no chunking, no
delegation to a managed data-plane service. Per the user's scope call,
this phase builds **the pattern, not the scale** — real mechanisms,
demonstrated honestly at a bounded size, not the master doc's
20,000-definition claim.

- **`tools/wave_manager.py`** — a deterministic scheduler (same
  discipline as `tools/policy_engine.py`: arithmetic over
  `policies/wave_limits.yaml`'s declared limits, never a model's
  judgment call) that admits or holds pending items against per-source
  concurrency caps, a global CRITICAL-risk concurrency cap, and
  backlog-age escalation (an item waiting past a configured threshold
  is promoted ahead of fresher peers). A configurable approval-time-
  window gate exists and is tested but ships disabled — this project's
  own demo/evaluation runs must not be blocked by wall-clock time.
- **Streaming `migration_executor.py`** (the audit's concrete, cheap-
  to-fix finding): `fetch_source_rows` is now a generator
  (`cursor.fetchmany()` in batches, not one `fetchall()`), and
  `execute_migration` streams through a new `DataPlaneExecutor`
  interface — `InMemoryExecutor` is today's only implementation,
  explicitly documented as a Rung-2 substitution for a future
  `DataflowExecutor`, the same discipline this project already applies
  to Gemma and Model Armor. The exact §7.2 row-loss semantics
  (`drop_fraction`'s deterministic "drop the last N by key order")
  are unchanged under streaming — `source_count` comes from a real
  `COUNT(*)` query up front rather than `len()` on a buffered list.
  A real bug surfaced building this: per-batch BigQuery schema
  autodetection is not safe across multiple `load_table_from_json`
  calls (a batch whose slice of a nullable column happened to be all-
  NULL got inferred as a different type than an earlier batch's,
  producing a genuine `400 Provided Schema does not match Table`
  error) — fixed by locking the schema BigQuery settled on after the
  first (autodetect) batch and reusing it explicitly for every later
  one (`tools/bigquery_tools.py::get_table_schema`).
- **Bounded scale demonstration** (`simulator/scale_fixtures/` +
  `evaluation/scale_harness.py`): 250 synthetic, schema-only pipeline
  definitions (no real data volume — control-plane scale, not bulk-data
  scale, per §32.12) pushed through real schema validation, one real
  Wave Manager admission pass, and a sampled batch of real policy
  decisions (each a genuine Firestore write). Generates
  `evaluation/reports/scale_metrics.md` — real measured p50/p95
  latency, never hand-typed. A live run: schema validation p50 5.5ms/
  p95 8.8ms; Wave Manager scheduling 2.5ms total for 250 items (6
  admitted, 244 held — the concurrency caps doing real backpressure,
  not theater); policy decisions (n=100, real Firestore round trips)
  p50 277ms/p95 1184ms.

Verified against real infrastructure: the full `pytest tests/` suite
(194 tests) and a fresh `run_full_migration.py` run both pass
end-to-end with identical outcomes to every prior day (656/663 rows
loaded on the seeded-defect first pass, memory-recalled root cause,
clean recovery, COMPLETE) — proof the streaming rewrite changed *how*
data moves, not *what* moves.

### Post-Day-10 hardening, Phase 5 — production evidence (Cloud Trace)

The audit's finding: no OpenTelemetry/Cloud Trace instrumentation
anywhere despite `opentelemetry-exporter-gcp-trace` already being
pulled in transitively. `tools/tracing.py` wraps the OTel SDK + Cloud
Trace exporter — one span per orchestrator event handler
(`handle_migration_requested` through `handle_validation_failed`),
tagged with `run_id` and the registry-resolved `agent_id`/`version`,
nested under one outer `advance_through_validation` span per run.
Strictly best-effort: a missing `GCP_PROJECT_ID` or exporter failure
degrades to "no traces," never a broken migration run — the same
fail-safe discipline already applied to the Gemini narrative and Gemma
substitution. `tools/tracing.py::flush()` forces pending spans out
before a short CLI script exits, so a fast `run_full_migration.py`
invocation doesn't outrace OpenTelemetry's own batching.

Verified live: a real `run_full_migration.py` run, then a direct Cloud
Trace API query (`google.cloud.trace_v1.TraceServiceClient.list_traces`)
found the exact trace — all 10 spans, correctly nested and ordered,
every one tagged with that run's `run_id`, including
`handle_validation_requested` (FAILED) → `handle_validation_failed`
(`incident_signature=row_loss:Sales.Customers`,
`root_cause_generated_by=recalled_memory`) → `handle_validation_requested`
(PASSED) — the real recovery loop, visible as a real trace tree. See
`evaluation/reports/cloud_deployment_evidence.md` for the full query
output and an honest statement of what this does and doesn't prove
(genuine causality/timing/identity per stage; not yet a cross-service
network trace, since this project runs one orchestrator process rather
than six independent Cloud Run services).

### Post-Day-10 hardening, Phase 6 — reproducibility artifacts

```bash
make test              # or: pytest tests/ -v
make run                # or: python agents/orchestrator/run_full_migration.py
make harness            # or: python evaluation/run_harness.py
make teardown ARGS=--dry-run   # or: bash infrastructure/teardown.sh --dry-run
bash scripts/clean_clone_check.sh
```

The audit's finding: no `LICENSE`, no `Makefile`, no teardown script
anywhere. Closed out with:

- **`LICENSE`** (MIT) — matches WideWorldImporters' own MIT licensing;
  third-party attribution stays in `DATA_SOURCES.md`.
- **`Makefile`** — one-word wrappers around commands already documented
  throughout this README; nothing it does isn't already a plain command
  you could paste directly.
- **`infrastructure/teardown.sh`** — the idempotent inverse of
  `gcp_setup.sh`: deletes every Pub/Sub topic/subscription, the
  BigQuery dataset, and every service account it created. `--dry-run`
  lists exactly what would be deleted without deleting anything
  (verified live: correct topic/subscription/SA list, zero side
  effects). Firestore data is deliberately **not** touched by default —
  a project-wide wipe is the single hardest action here to reverse —
  `--delete-firestore` exists but requires typing the project ID back
  as confirmation.
- **`scripts/clean_clone_check.sh`** (the audit's "clean-clone release
  gate" finding) — this repo is under git version control with a real
  GitHub remote (`github.com/Nikhil0075/MIGRATION-CONTROL-TOWER`); the
  script still copies the working tree into an isolated temp directory
  rather than doing a literal `git clone` (deliberately excluding
  `.venv`, caches, `.env`, and `.git` itself — the same set a real clone
  would exclude via `.gitignore`), then proves it from scratch there: a
  fresh venv, `pip install -r requirements.txt`, and full
  `pytest --collect-only` (every import resolves, no live services
  needed). Test-collection counts here go stale quickly as the suite
  grows — see a dated `evaluation/reports/` snapshot (e.g.
  `baseline_2026-08-21.md`) for the count as of a specific run, rather
  than trusting a number fixed in this prose.
- **`docs/compliance_matrix.md`** — the master doc's §26 concept, filled
  in with what was actually built rather than left as a template:
  every §26.1/§26.2 requirement, §32's portability/scale claims scoped
  honestly ("control-plane scale at 250 definitions, not 20,000"), and
  a phase-by-phase table closing the loop on every one of the audit's
  original findings.

### Post-Day-10 hardening — fix pass after a second audit round

A second, more skeptical audit round independently re-checked every
phase's claims against the actual code (not the compliance matrix's
word) and found specific remaining gaps. Items 1–6 and 8 were fixed in
the original pass; item 7 is now implemented as the Oracle JET Redwood
production console described above:

1. **Approval authorization allowlist** — Firebase ID-token
   verification proved *who* signed in, but anyone with a Google
   account could approve a cutover. `APPROVER_ALLOWLIST` (comma-
   separated exact emails / `@domain` entries, `.env.example`) is now
   checked in `frontend/app.py::get_approver_identity` after token
   verification, failing closed (empty/unset denies everyone).
2. **Idempotency on every event handler** — only
   `handle_migration_requested` had Pub/Sub redelivery dedup.
   `_dedup_check`/`_dedup_mark` (namespaced `{handler_name}:{message_id}`)
   now guard all 6 orchestrator handlers.
3. **Assessment-mode boundary at execution** — `handle_planned()` now
   refuses to execute a migration for any `mode="assessment"` run,
   structurally, not just because `run_assessment.py`'s own call graph
   happens to never publish `plan.created`.
4. **Adapters as Discovery's real source path** —
   `agents/discovery/agent.py::discover_estate()` (the exact function
   the orchestrator resolves via the registry for every real run) now
   calls `SqlServerAdapter`/`OracleCorpusAdapter`/`DagArtifactAdapter`
   directly instead of `tools/source_catalog.py` — previously the
   adapters were proven byte-identical only in isolated tests. Verified
   live: unchanged output (58 tables, 4 pipelines).
5. **Wave Manager wired into real dispatch** — `tools/wave_manager.py`'s
   `evaluate_wave()` was only ever called by tests and the scale
   harness. New `reserve_slot()`/`release_slot()`: a transactional
   Firestore reservation (`wave_state/slots`) wired into
   `handle_planned()` (bounded retry-on-HOLD, released in a `finally`
   regardless of outcome), plus `within_approval_window()` wired into
   `agents/cutover/agent.py::perform_cutover()`. Verified live: a full
   `run_full_migration.py` run shows a real ADMIT → release cycle in
   Firestore.
6. **Compliance matrix corrections** — `docs/compliance_matrix.md`
   downgraded several overclaimed ✅ rows (Agent Gateway — only 2 of 6
   agents call `policy_engine.evaluate()`, not every tool call; Agent
   Identity — only `hello-agent` runs under its deployed service
   account; Migration Packs — real for assessment-mode runs but not yet
   the flagship execution path's source) to ⚠️ with the precise gap
   stated.
8. **Teardown scope + stray file** — `infrastructure/teardown.sh` now
   also deletes the `hello-agent` Cloud Run service and the
   `control-tower` Artifact Registry repo (both container images); a
   stray `=6.5.0` file (leftover pip-install output, misinterpreted
   PowerShell redirection from an earlier dependency install) was
   removed.

Also broadened as part of this pass: `frontend/static/test_esc.js` now
runs all 12 `simulator/injection_corpus/` payloads through
`esc()`/`badge()` in addition to the 4 hardcoded XSS strings (96 checks,
up from 24) — one corpus, two independently-verified containment
properties.

Full regression after this pass: `pytest tests/ -v` — **217/217
passing**, zero skips, live against real Firestore/SQL Server/BigQuery
(no mocks).

### Post-Day-10 hardening — Pub/Sub acknowledgement + atomic idempotency fix

Independently flagged as "the most serious remaining defect," with
precise file:line references — verified against the actual code and
confirmed real:

1. **`pull()` auto-acked before the handler ran** — `tools/events.py::pull()`
   acknowledged every message immediately, before returning it. Any
   handler failure downstream (a Wave Manager capacity HOLD in
   `handle_planned()` raising, any exception) silently discarded the
   message with no Pub/Sub redelivery — a run could get stuck in
   `PLANNED` forever despite code that explicitly raised expecting to
   be retried on redelivery. Fixed: `pull()` no longer acks; new
   `ack()`/`nack()` functions; `orchestrator.py::_consume()` (the new
   single choke point for every `pull → handler` call) acks only on
   success and `nack()`s — an immediate ack-deadline reset — on any
   exception, so a HOLD genuinely triggers redelivery now.
2. **The idempotency ledger wasn't atomic** — the old check-then-act-
   then-mark pattern (`_dedup_check` reads, the handler runs its side
   effects, `_dedup_mark` writes) left a real gap: a process crash
   between a side effect and the final mark left no record the work had
   started, so a redelivered copy of the same message would repeat it
   in full. Fixed with `_dedup_claim()`/`_dedup_complete()`: the
   existence check and the claim write happen inside one Firestore
   transaction. A claimed-but-never-completed message (the
   crash-recovery case) is detected and safely redone — safe because
   `execute_migration()` truncates-and-reloads on its first batch
   (already idempotent-safe) and `transition_state()` is graph-checked,
   failing loudly on an illegal double-transition instead of silently
   double-applying it.
3. **Found while fixing #2, not originally flagged**: `handle_migration_requested`
   marked itself "processed" immediately after `create_run()`, *before*
   `write_catalog()`/`publish()` ran — a crash there left a run stuck at
   `REQUESTED` forever while a redelivered copy was treated as already
   done. Fixed to mark done only at the very end, with special handling
   since `create_run()` (unlike every other handler's side effects, all
   of which act on an existing `run_id`) genuinely isn't safe to call
   twice — a stale-claim redo reuses the `run_id` recorded right after
   the original `create_run()` succeeded.

**Evidence**: 6 new regression tests in `tests/test_orchestrator.py` —
2 unit tests proving `_consume()` acks only on handler success and
nacks (never acks) on a handler exception; 4 live-Firestore tests
proving `_dedup_claim()`'s claimed/done/stale_claim states. Full suite at
the time of this fix: **224/224 passing** (491 as of Day 11) — both
numbers are historical; the suite has grown substantially since (see a
dated `evaluation/reports/` snapshot, e.g. `baseline_2026-08-21.md`, for
the current count — don't treat either figure above as current). A live
`run_full_migration.py` run against real
Pub/Sub/Firestore/SQL Server/BigQuery reached `COMPLETE` with every
`processed_messages` doc from that run's chain at `status="done"` (none
stuck `"claimed"`) and its wave slot cleanly released (that document became
`wave_state/{estate_id}` in Day 11). See
`docs/compliance_matrix.md`'s matching section for the full table.

### Day 11 — multi-estate: the platform becomes plug-and-play

Everything before this proved one estate end to end. This block made the
estate a *parameter* rather than an assumption, so a second migration team
can connect their own systems without the agent fleet changing.

**What was hardcoded, and where it went.** The table each run migrated —
`Sales.Customers`, its key column, its aggregate and null-check columns —
lived as module constants imported by nine modules, including the
orchestrator, the Validation agent, the cutover worker and the evaluation
harness. They are gone. The Planner now derives targets from the discovered
catalog plus the Migration Pack's type rules, writes them to
`migration_plan/current`, and every consumer reads them from there.

Derivation is deterministic and refuses to guess:

- key column from `Table.primary_key`; a **composite** primary key blocks the
  target with a stated reason, because the extractor orders by a single key
  and reconciliation compares ordered key lists
- aggregate column from the pack's declared numeric types; a table with none
  records `aggregate_check: not_applicable` rather than comparing against a
  fabricated zero
- null-check column from column nullability, which required adding
  `is_nullable` to the catalog — without it there was no non-fabricated way
  to choose that column for a new estate

**Estates became first-class.** `tools/estate_registry.py` backs
`estates/{estate_id}` in Firestore with revision history, YAML import/export,
and an `origin` guard: re-running `make seed` will not silently revert an
estate an operator edited in the console. `tools/connection_context.py`
resolves the registry first and committed YAML second, so an offline checkout
still works.

**Credentials became references.** `tools/secret_resolver.py` resolves a
Secret Manager reference or a named environment variable at connect time,
returning a type that redacts itself in `repr`. Two SQL Server estates can now
be connected from one process — previously both would have taken their
password from one process-global variable.

*(The module is named `secret_resolver`, not `secrets`: Python puts a script's
own directory on `sys.path`, so `tools/secrets.py` shadowed the standard
library's `secrets` for any script under `tools/` — which broke
`export_openapi.py`, since starlette does `from secrets import token_hex`.)*

**State became estate-scoped.** `wave_state/slots` — a single global
concurrency arena — became `wave_state/{estate_id}`. Without that split, a
second onboarded customer's runs would queue behind, and be held by, the first
customer's load. Connection health, run documents and every `/api/v1` read
endpoint are scoped the same way, with one rule throughout: a record with **no**
`estate_id` belongs to the default estate, never to nothing, so the dashboard
does not empty out between deploying the filter and running the backfill.

**Roles became estate-scoped.** Firebase custom claims carry
`estate_roles`, and each mutating endpoint calls `authorize_estate()`
explicitly — a FastAPI dependency cannot read an arbitrary request-body field.
Because an explicit call is exactly what a new endpoint forgets, a test
enumerates every mutating route and fails if one omits it.

**The second estate.** A PostgreSQL fixture (`make second-estate`), a
`PostgresAdapter`, a `postgres_retail_v1` pack and one line in `ADAPTER_TYPES`.
Its pack declares no `scheduled_tables`, so every target is derived from
discovered metadata — the path a real customer takes rather than the WWI path
that reproduces pre-existing constants.

`tests/test_clean_estate_onboarding.py` is the release gate (`make
onboarding-gate`): it greps every file under `agents/` for `postgres`,
`psycopg`, `retaildb` and the new estate and pack ids. Review cannot catch a
single `if source ==` inside an agent; that grep can.

**Also fixed, found by verifying rather than reasoning:**

- Webpack emitted **relative** asset paths, so the first nested route
  (`/estates/new`) requested its own JavaScript from `/estates/js/...`, hit the
  SPA fallback, received `index.html` and never booted. Every prior route was a
  single segment, so this had been invisible.
- FastAPI's default 422 handler **echoes the rejected input**, so refusing a
  submitted password returned it to the caller and any response-body log.
- The wizard's hint text was nested inside `<label>`, folding it into each
  control's accessible *name*.
- The keyboard-navigation accessibility test passed serially and failed in
  parallel — `page.keyboard.press` delivers to whatever the browser considers
  focused, which is not the page when workers share a machine.


## Tests

```bash
make test
```

The full backend suite plus the frontend's component and browser tests
under `frontend/client` (`npm test`, `npm run test:e2e`) — see a dated
`evaluation/reports/` snapshot (e.g. `baseline_2026-08-21.md`) for the
exact counts as of a specific point in time rather than a number fixed
here, which reliably goes stale as the suite grows (this line has
already been wrong once — see `docs/adr/`'s note on the "224/224"/"491"
figures that outlived their accuracy). They run against live Firestore,
so a test that creates a run or a registry card must delete it in
teardown; suites needing SQL Server, Postgres or BigQuery skip automatically
when those are unreachable.

```bash
make onboarding-gate
```

The clean-estate release gate: a second estate, a different database engine,
with zero edits under `agents/`.

## Repository layout

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/GOVERNANCE.md](docs/GOVERNANCE.md),
[docs/DURABILITY.md](docs/DURABILITY.md), and [docs/EVALUATION.md](docs/EVALUATION.md) for the
system design, plus the inline READMEs under `agents/`, `tools/`, `simulator/`, and
`infrastructure/`.

## Data sources & attribution

See [DATA_SOURCES.md](DATA_SOURCES.md).
