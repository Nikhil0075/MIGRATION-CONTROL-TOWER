# Evaluation

What this project measures today, how, and what Deploy & Harden Phase 4 is adding — kept as three
genuinely distinct things rather than one number, because conflating them is exactly how "20,000"
turned into a headline claim the audit correctly challenged.

## Three separate measurements, never conflated

| Measurement | Question it answers | Tooling |
|---|---|---|
| **Functional/scenario evaluation** | Does the fleet behave correctly across realistic scenarios (PII denial, row-loss recovery, human approval)? | `evaluation/run_harness.py` — 14 scenarios (S-01…S-14) |
| **Control-plane scale** | How does schema validation, wave scheduling, and policy-decision latency behave at 1k/5k/20k *pipeline definitions*? | `evaluation/scale_harness.py` |
| **Data-plane scale** | How many actual rows/bytes can genuinely be migrated, and how fast? | New in Phase 4 — `evaluation/data_plane_scale_test.py`, run against Phase 3's Cloud SQL Postgres source |
| **Operational load** | How does the system behave under concurrent runs/users, and what does the Pub/Sub backlog do? | New in Phase 4 — `evaluation/load_test.py` |

**A control-plane run at 20,000 pipeline definitions is not a 20,000-object migration.** It proves
planning/scheduling/policy-decision behavior scales; it proves nothing about actually moving
20,000 objects' worth of data, which is what the data-plane row/data measurement is for instead.
Call the control-plane number exactly what it is: *"a 20,000-object control-plane planning and
scheduling benchmark."*

## Functional evaluation: `evaluation/run_harness.py`

Fourteen scenarios exercise the fleet against real Firestore/Pub-Sub/BigQuery (skipping
automatically where a live dependency like SQL Server is unreachable) — PII access denial,
cutover self-approval denial, reconciliation failure and recovery, human approval, and others.
These have real automated coverage; not all of them have been separately captured as a *live*
recorded demonstration distinct from the automated test run — see `docs/DEMO.md` for which ones
the submission demo actually walks through on camera versus which are proven only by the harness.

## Control-plane scale: `evaluation/scale_harness.py`

Generates N synthetic pipeline definitions (`--count`, historically bounded to 100–500; Deploy &
Harden Phase 4 raises this for the 1k/5k/20k tiers) and times three real operations against each:
schema validation (`contracts/metadata_model.json`), `tools/wave_manager.py::evaluate_wave()`
scheduling, and a *sampled* `tools/policy_engine.py::evaluate()` pass (capped at
`POLICY_SAMPLE_SIZE`, since each call is a real Firestore write and running one per definition
would make the harness's own cost scale with `--count` for no added signal). No live data reads, no
model calls — deliberately control-plane-only, and the harness's own docstring says so. Reports to
`evaluation/reports/scale_metrics.md` and, per definition, to Firestore's
`evaluation_scale_reports/{tier}` (tier-keyed as of Phase 4, replacing a single `"current"` doc so
1k/5k/20k results don't overwrite each other) — a `current` alias is kept pointing at the latest
tier so the existing console wiring (`/api/v1/evaluations`) keeps working unmodified.

**What Phase 4 adds to this harness**: a throughput field (records/sec, derived from the existing
latency data), and an explicit `model_calls: 0 (control-plane-only)` note rather than silently
implying model cost scales with `--count` — it doesn't, on purpose.

## Data-plane scale: `evaluation/data_plane_scale_test.py` (Phase 4, new)

Runs a real `tools/migration_executor.py::execute_migration()` and reports genuinely measured rows
moved, duration, and throughput — not extrapolated, not simulated. Works against whichever
`DataPlaneExecutor` is selected: today's default `InMemoryExecutor` (against whatever live source
is actually reachable — WWI SQL Server in this dev environment), or Phase 3's `CloudRunJobExecutor`
once `DATA_PLANE_EXECUTOR=cloud_run_job` and the Cloud SQL Postgres demo source are live. A
genuinely different question from the control-plane harness (real rows/bytes vs. zero), reported
separately rather than folded into one "scale" number.

## Operational load: `evaluation/load_test.py` (Phase 4, new)

Two parts: (1) real, runnable-today concurrent throughput — N simultaneous
`tools/policy_engine.py::evaluate()` calls via a thread pool, measuring whether latency degrades
under real Firestore contention; (2) a live query of whatever Cloud Run services/Pub-Sub
subscriptions are actually deployed right now (honest about what's missing, the same pattern
`evaluation/collect_deployment_evidence.py` uses — a service that doesn't exist is reported as
such, not omitted). Live Cloud Run instance-count-under-load specifically needs Cloud Monitoring's
`run.googleapis.com/container/instance_count` metric against a real traffic window once the full
fleet is deployed — not fabricated here.

## Cost estimation methodology: `evaluation/estimate_ladder_cost.py` (Phase 4)

Three separate scenarios (`--scenario control-plane|data-plane|operational-load`), because each has
a genuinely different cost driver — folding them into one number would hide which dimension
actually costs money:
- **control-plane**: near-zero and CONSTANT across every tier (`evaluation/infra_price_book.json`'s
  Firestore write rate × a fixed ~102 writes — the harness touches no BigQuery/Vertex AI at any
  scale, confirmed by its own docstring, not assumed).
- **data-plane**: extrapolated from a real measured sample (`--sample-run-id`, pulling actual
  `bytes_billed`/`target_count` from `tools/usage_meter.py`'s recorded usage) using
  `contracts/price_book.json`'s BigQuery rate, or an explicitly-labeled generic assumption when no
  sample is given.
- **operational-load**: `evaluation/infra_price_book.json`'s Cloud Run CPU/memory-second rates — no
  usage-measurement instrumentation exists for this yet, so it's assumption-based and labeled as such.

Every scenario prints its full breakdown and requires explicit confirmation (`--yes` to skip the
interactive prompt for non-interactive use) before the plan's own condition — "checked against
remaining trial credit, never auto-run" — is satisfied. The script never triggers a scale run
itself; it only estimates and asks.

## What "evaluated" does not mean here

A scenario passing in `run_harness.py` or a control-plane tier completing in `scale_harness.py`
means exactly what its own report says, and nothing about the dimensions it didn't measure. In
particular: passing the control-plane harness at 20,000 definitions says nothing about the data
plane's throughput at that scale, and passing 14 functional scenarios locally says nothing about
behavior under concurrent operational load until the Phase 4 load test actually exercises that.
Keep the three tables in `docs/compliance_matrix.md` and this file in sync as each dimension gets
real numbers — an unfilled row stays `❌`, not silently implied by an adjacent `✅`.
