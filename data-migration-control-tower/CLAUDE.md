# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A governed fleet of AI agents (Discovery, Lineage, Risk, Planner,
Validation, Cutover) that discovers a legacy data estate, assesses
migration risk, plans and validates a migration to BigQuery, and
coordinates a human-approved cutover. Built for the All Things Agentic
Hackathon (Track 3). The full design spec is the master documentation
this repo implements; day-by-day build order follows its Revised Master
Schedule — see the root [README.md](README.md) for what's built so far
and section references (§N) throughout the code point back to specific
requirements in that spec.

**Core architectural rule, stated everywhere in code comments**:
Gemini/agents handle interpretation, reasoning, and explanation.
Deterministic Python handles authorization, state transitions, row
counts/schemas/checksums, policy enforcement, and approval verification.
A model can propose or explain; it can never be the thing that decides
whether an action is allowed or a check passed.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/Scripts/activate  # Git Bash on Windows
pip install -r requirements.txt
cp .env.example .env   # fill in real values

# Legacy source estate (SQL Server / WideWorldImporters)
cd simulator/source_setup && docker compose up -d && ./restore_wwi.sh

# GCP infra (idempotent) + Agent Registry seeding
bash infrastructure/gcp_setup.sh
python infrastructure/seed_registry.py
python infrastructure/seed_finance_agent.py

# Run the whole system end to end (recommended entry point)
python agents/orchestrator/run_full_migration.py

# Tests
pytest tests/ -v                          # full suite
pytest tests/test_registry.py -v          # one file
pytest tests/ -k test_name -v             # one test

# Control Tower UI
uvicorn frontend.app:app --reload --port 8080
```

Individual agents also run standalone (each defaults to the most
recently created run if no `run_id` is given): `python
agents/discovery/run_discovery.py`, `agents/lineage/run_lineage.py`,
`agents/risk/run_risk.py`, `agents/planner/run_planner.py`,
`agents/validation/run_validation.py`, `agents/cutover/run_cutover.py`.
See the root README's "step by step" section for the full manual chain
and what each stage proves.

## Architecture

### The state machine is the spine

`agents/orchestrator/run_lifecycle.py` owns a Firestore document per
migration run (`migration_runs/{run_id}`) and a hard-coded legal
transition graph (`_CANONICAL_TRANSITIONS`). `transition_state()` raises
`ValueError` on any illegal transition — there are no silent state
jumps, and no `_PROVISIONAL_TRANSITIONS` shortcuts remain (they existed
temporarily in earlier build days and were removed once the owning
agent was built; check that dict's docstring before assuming a shortcut
exists). Every transition also appends to `state_history` on the run
doc, which is what the Control Tower UI renders as a timeline.

The canonical path: `REQUESTED → DISCOVERED → ANALYZED → RISK_ASSESSED →
PLANNED → MIGRATING → VALIDATING → (FAILED → INVESTIGATING →
REMEDIATING → VALIDATING loop) → PASSED → READY_FOR_APPROVAL → APPROVED
→ CUTOVER → MONITORING → COMPLETE`.

### Agents don't call each other directly — the registry resolves them

`tools/registry.py` is a real Agent Registry (Firestore
`agent_registry/{agent_id}/versions/{version}`), not a display list.
`agents/orchestrator/orchestrator.py`'s event handlers call
`registry.invoke_capability("discovery.catalog.estate", ...)`, which
looks up the APPROVED card advertising that capability and dynamically
imports/calls its `handler` string — there is no `from
agents.discovery.agent import ...` anywhere in the dispatch path.
Publishing is two-step (`publish()` → `DRAFT`, then `approve()` →
`APPROVED`) and `approve()` refuses if `approved_by == published_by`
(separation of duties, mirrors the human cutover-approval pattern
below). New agents/capabilities must be seeded via a script like
`infrastructure/seed_registry.py` before the orchestrator can find them.

### Tools vs agents

`tools/` holds deterministic, agent-framework-free functions
(classification rules, the policy engine, reconciliation checks, the
migration executor, the registry, etc.) — directly unit-testable, no
Firestore-run coupling where possible. `agents/*/agent.py` wires those
tools to a specific run's Firestore data and exposes them as ADK tools.
Each `agents/*/agent.py` tries `from google.adk.agents import Agent`
and falls back to `AGENT_FRAMEWORK = "direct-fallback"` on
`ImportError` — check which framework is active in a run's logs before
assuming ADK-specific behavior actually ran.

### Policy engine, not agent judgment

`tools/policy_engine.py::evaluate(agent_key, action, resource_class,
run_id)` is the single ALLOW/DENY/REQUIRE_APPROVAL decision point,
reading `policies/agent_permissions.yaml`. It takes no free-text estate
content as input — this is what makes prompt injection structurally
inert (see `tools/untrusted_content.py` and
`tests/test_injection_defense.py`). Every evaluation is recorded to
`migration_runs/{run_id}/policy_decisions/`. Human approval is a
separate, parallel mechanism (`tools/approval_service.py`): only
`agents/cutover/approve_cutover.py` (never agent code) calls
`approve()`, and `consume()` binds the token to the plan's hash so an
approval can't be replayed against a changed plan.

### Untrusted content discipline

Anything sourced from the legacy estate (column names, DAG docstrings,
table comments, documentation PDFs/images) is treated as data, never as
instructions — parsed with regex/`ast.literal_eval`/JSON-schema
validation, never `eval`/`exec`'d, never string-concatenated into a
model system prompt. `tools/source_catalog.py`'s DAG parser and
`tools/multimodal_discovery.py`'s Gemini-vision extractor both follow
this; `simulator/injection_corpus/` + `tests/test_injection_defense.py`
is the regression suite for it.

### Firestore is the only datastore

Every agent, tool, and the UI read/write the same `migration_runs/{run_id}/*`
subcollections (see the top of `run_lifecycle.py` for the full layout)
plus a few global collections: `agent_registry`, `memory_bank`,
`policy_decisions`/`containment_events` (when not run-scoped). There is
no separate UI database — `frontend/app.py` is a read-only API over
this same data, reusing `tools/registry.py` and `run_lifecycle.py`
directly rather than reimplementing queries.

### Cross-run memory, not chat history

`tools/memory_bank.py` is a global collection keyed by a normalized
defect signature (e.g. `row_loss:Sales.Customers`), deliberately
separate from any run's session-scoped data. `recovery.py::investigate()`
checks it first and cites a match as evidence, but a recalled fact never
skips the deterministic re-validation that follows — see that module's
`canonical_root_cause` field (the stable fact text) vs `root_cause` (the
display-wrapped "recalled from memory..." narrative); conflating the two
was a real bug that made recalled facts nest wrapper text across
generations.

### Fallback patterns to know before "fixing" something

Several integrations have a documented Rung-2 deterministic fallback
that fires automatically on any failure (missing credentials, model
unavailable, malformed response) — this is intentional, not a bug:
- `agents/*/agent.py`: ADK import failure → direct tool-call fallback
- `agents/orchestrator/recovery.py::_try_gemini_narrative`: Vertex AI
  call failure → `_deterministic_narrative` template
- `tools/multimodal_discovery.py`: Gemini vision/file call failure →
  hardcoded schema matching `simulator/documentation/generate_fixtures.py`
- `tools/fast_pii_screen.py`: stands in for a self-hosted Gemma model
  (Ollama is installed but no model is pulled in this dev environment)
  with a deliberately naive, independent keyword screen — disagreements
  with the careful classifier are a *feature* (recorded as findings in
  both directions), not noise to suppress

## Known environment gotchas (already worked around in code, don't re-break)

- **Firestore `collection_group()` + `.where()`** needs a composite
  index that doesn't exist in this project. `registry.discover()` and
  `frontend/app.py`'s dashboard fetch the whole collection group
  unfiltered and filter in Python instead — don't add a `.where()` to a
  collection-group query without creating the index first.
- **SQL Server via pyodbc**: driver name is auto-detected
  (`tools/sqlserver_client.py`) between `ODBC Driver 18`, `17`, and the
  legacy in-box `SQL Server` driver — don't hardcode one.
- **Geography/geometry/hierarchyid columns**: carried via SQL Server's
  own `.ToString()` in `tools/migration_executor.py`, not excluded —
  excluding them used to make post-remediation schema checks
  permanently fail since the target could never match the source's full
  column set.
- **Test hygiene**: any test that calls `registry.publish()` or
  `run_lifecycle.create_run()` MUST delete what it created
  (`registry.delete_card()` / `run_lifecycle.delete_run()`) in teardown.
  A leftover test registry card can shadow a real capability lookup by
  wildcard, and a leftover test run can become the Control Tower
  dashboard's "active run" — both have happened for real during
  development. Follow the `registered`/`temp_run` fixture patterns in
  `tests/test_registry.py` / `tests/test_state_machine.py`.
- **The shared demo estate is borrowed, never owned**: `wwi-demo-estate`
  is the estate the console defaults to and every caller that omits an
  `estate_id` resolves to, so it cannot be deleted in teardown the way a
  test-created estate can — it has to be *restored*. A test that writes to
  it and walks away leaves that state for every later test and every later
  run, and the resulting failure is delayed and self-erasing:
  `import_from_yaml` refuses to overwrite an estate whose `origin` is
  `wizard` (deliberately — see
  `test_yaml_import_refuses_to_revert_a_console_edit`), so an inherited
  console-authored estate makes
  `test_importing_the_committed_demo_estate_is_idempotent` fail for a
  reason that has nothing to do with idempotency. Use the
  `borrowed_demo_estate` fixture in `tests/test_estate_registry.py`: it
  states the precondition it needs out loud, and puts the document and its
  revisions back exactly as it found them.
- **ADK agent names must be valid Python identifiers** — no hyphens.
  `AGENT_ID` (e.g. `"risk-agent"`) is the display/registry identity;
  pass `AGENT_ID.replace("-", "_")` as the ADK `Agent(name=...)`.
- **Windows/Git Bash + Docker**: absolute Unix-style paths passed to
  `docker exec`/`docker cp` get silently mangled by MSYS path
  conversion. `simulator/source_setup/restore_wwi.sh` sets
  `MSYS_NO_PATHCONV=1` for this reason — needed again in any new script
  that shells out to Docker with in-container paths.
