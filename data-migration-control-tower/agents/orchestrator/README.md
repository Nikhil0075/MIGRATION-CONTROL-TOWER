# Orchestrator

- `run_lifecycle.py` — deterministic state machine (master doc §8, §9):
  run creation, catalog/state persistence, and full transition-legality
  enforcement. `transition_state()` raises `ValueError` on an illegal
  transition instead of silently jumping state. As of Day 5 there are no
  provisional shortcuts left — `RISK_ASSESSED -> PLANNED -> MIGRATING ->
  VALIDATING` is the only legal path.
- `hello_agent/` — Day 1 foundation proof (Cloud Run + Firestore).
- `orchestrator.py` + `run_orchestrator.py` — Day 4 event-driven
  cascade: publishes/consumes real Pub/Sub events (`tools/events.py`) so
  a run advances `REQUESTED -> DISCOVERED -> ANALYZED -> RISK_ASSESSED`
  from a single published event, without running Discovery/Lineage/Risk
  as separate manual scripts. As of Block C/Day 6, every dispatch also
  resolves its actor by capability query against `tools/registry.py`
  (`registry.invoke_capability("discovery.catalog.estate", ...)`, etc.)
  instead of a static `from agents.x.agent import y` — including a
  cross-department capability lookup (`impact.assessment.*`) that finds
  `agents/finance/impact_agent.py` with zero hardcoded knowledge of it.
- `recovery.py` — Day 5 recovery loop, extended Day 7 with memory recall:
  `investigate()` checks `tools/memory_bank.py` first for a confirmed
  fact matching this defect's signature (cited as evidence, never a
  substitute for re-validation); falls back to a Gemini-attempted
  narrative, then a deterministic template, if nothing is recalled.
  `remediate()` applies the one pre-cataloged fix and reloads clean
  data; `close_incident()` confirms RESOLVED incidents into memory for
  future recall.
- `pipeline_stages.py` — the shared `advance_to_passed()` sequencing
  (migration.requested through a PASSED validation, including the
  recovery loop) factored out so `run_full_migration.py` and
  `durability_demo.py` don't each carry their own copy.
- `run_full_migration.py` — Day 5 milestone: chains the cascade through
  Planner, a seeded-defect migration, a failed validation, recovery, a
  passing re-validation, human-approved cutover, and post-cutover
  monitoring — the full `DISCOVERED -> ... -> COMPLETE` path in one
  command. Run it twice to see Day 7's memory recall kick in on the
  second run.
- `durability_demo.py` — Day 7 kill-and-resume proof: pauses a run at
  `READY_FOR_APPROVAL`, then resumes it across genuinely separate OS
  processes (`subprocess.run`, not just separate function calls) and
  verifies the trace is unbroken.
- `seed_long_horizon_fixture.py` — Day 7 labeled long-horizon fixture:
  runs the real pipeline, backdates exactly two timestamps to simulate
  an 18-day approval gap, and stamps `is_seeded_fixture`/`fixture_label`
  directly onto the Firestore documents.

```bash
python agents/orchestrator/run_full_migration.py [pipeline_id]   # the whole milestone
python agents/orchestrator/run_orchestrator.py [pipeline_id]     # just Discovery->Lineage->Risk
python agents/orchestrator/durability_demo.py [pipeline_id]      # kill-and-resume proof
python agents/orchestrator/seed_long_horizon_fixture.py [pipeline_id]  # labeled backdated fixture
```

`agents/discovery/run_discovery.py`, `agents/lineage/run_lineage.py`,
`agents/risk/run_risk.py`, `agents/planner/run_planner.py`,
`agents/validation/run_validation.py`, and `agents/cutover/run_cutover.py`
all still work standalone (each defaults to the most recent run) for
step-by-step debugging — see the root README's step-by-step section.
