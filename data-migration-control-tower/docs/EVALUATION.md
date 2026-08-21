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

## Data-plane scale (Phase 4, new)

Measures actual rows/bytes moved through Phase 3's `CloudRunJobExecutor` against the Cloud SQL for
PostgreSQL source — a genuinely different question from the control-plane harness, and reported
separately rather than folded into one "scale" number.

## Operational load (Phase 4, new)

Concurrent runs/users, Pub/Sub oldest-unacked-message age under load, and Cloud Run instance count
as load increases — exercising Deploy & Harden Phase 2's distributed deployment, not meaningful
against the single-process shape that predates it.

## Cost estimation methodology (Phase 4)

Before running the 20k control-plane tier or the data-plane row/byte benchmark,
`evaluation/estimate_ladder_cost.py` estimates the **full stack**, not just BigQuery bytes: BigQuery
(via the dry-run flow in `tools/bigquery_tools.py`), Vertex AI tokens, Cloud Run + Cloud Run Jobs
CPU/memory time, Cloud SQL uptime/storage/backups, Firestore operations, Pub/Sub, Cloud
Logging/Trace, Cloud Storage, and network egress. The estimate is printed and requires explicit
confirmation, checked against the actual verified remaining trial credit (`evaluation/reports/baseline_2026-08-21.md`),
before either run proceeds — never auto-run from a script or CI job.

## What "evaluated" does not mean here

A scenario passing in `run_harness.py` or a control-plane tier completing in `scale_harness.py`
means exactly what its own report says, and nothing about the dimensions it didn't measure. In
particular: passing the control-plane harness at 20,000 definitions says nothing about the data
plane's throughput at that scale, and passing 14 functional scenarios locally says nothing about
behavior under concurrent operational load until the Phase 4 load test actually exercises that.
Keep the three tables in `docs/compliance_matrix.md` and this file in sync as each dimension gets
real numbers — an unfilled row stays `❌`, not silently implied by an adjacent `✅`.
