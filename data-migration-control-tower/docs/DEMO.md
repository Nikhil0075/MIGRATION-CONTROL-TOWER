# Demo — live four-minute walkthrough script

This is the **live, screen-recorded** product demonstration script — distinct from
`docs/SUBMISSION_VIDEO.md`'s 30-second generated illustrative loop, which is explicitly *not* a
screen recording (its own "Honest limits" section says so). Recording this is a manual step this
document cannot do — it's the shot list and narration to record against the actual running
console once Deploy & Harden Phase 2's deployment is live, so the recording itself doesn't drift
from what's real.

## Prerequisites before recording

- Console deployed and reachable (Deploy & Harden Phase 2), or running locally against live
  Firestore/Pub-Sub/BigQuery/SQL Server if recording pre-deployment.
- A clean demo estate seeded (`infrastructure/seed_estates.py`) with no stale run currently
  occupying the dashboard's "active run" slot.
- The seeded row-loss fault (`simulator/failure_injector/seed_row_loss.py`) primed so the
  recording can show a real reconciliation failure and recovery, not a scripted-looking success
  path only.

## Shot list (target: under 4 minutes)

| # | Time | Screen | What to show | Says |
|---|---|---|---|---|
| 1 | 0:00–0:20 | Estates | The registered demo estate, sources listed | "This is a legacy SQL Server estate — WideWorldImporters — about to be migrated to BigQuery, governed end to end by an AI agent fleet." |
| 2 | 0:20–0:50 | Trigger a run → Assessments/Discovery | Discovery catalogs the estate live; table/pipeline counts populate | "Discovery inventories the estate — metadata only, never raw PII — and the catalog is what everything downstream reasons from." |
| 3 | 0:50–1:20 | Risk → Policy denial | Attempt (or show a prior recorded) a PII-read attempt being denied by the policy engine, with the denial visible in the console's policy-decision log | "Risk classifies sensitivity. Watch this: an attempt to read raw PII is denied — not by the agent's judgment, but by a deterministic policy engine that agents can't talk their way around." |
| 4 | 1:20–1:50 | Planner → Migration executing | The plan, then the wave-scheduled migration in flight | "The Planner proposes a migration plan; the Wave Manager schedules execution with real concurrency limits, not an unbounded blast." |
| 5 | 1:50–2:30 | Validation → seeded row-loss failure → Recovery | The reconciliation failure appears (row count mismatch), the run enters `FAILED → INVESTIGATING`, memory-bank recall surfaces a prior fix, remediation runs, reconciliation passes | "A row-loss defect is caught — not silently, the run fails loudly. Memory recalls a prior fix as a hint, but re-validates deterministically rather than trusting the memory alone." |
| 6 | 2:30–3:00 | Cutover → approval flow | A human approver reviews and approves the cutover in the console (or the API), the approval token binds to the plan hash | "Cutover requires a human. The agent that requested it cannot approve its own request — that's enforced, not just documented." |
| 7 | 3:00–3:30 | Run reaches COMPLETE → Evidence/Reports | The finished run's timeline, the evidence report, the audit trail | "The run completes, fully audited — every policy decision, every agent version, every approval is recorded and downloadable as evidence." |
| 8 | 3:30–4:00 | System Health / Registry / (if deployed) Cloud Trace | Agent registry versions, live worker status, and — once Phase 2 is deployed — a real multi-service Cloud Trace span | "This isn't a demo shell — the registry, the policy engine, and the worker fleet are the same components running in production." |

## What must be true before this is recorded (not yet, as of this doc)

- A live public URL (Deploy & Harden Phase 2) — recording against `localhost` is a fallback, not
  the target; label it explicitly if used.
- Shot 3's PII denial and shot 5's row-loss/recovery should be genuinely triggered on camera, not
  narrated over a static screenshot — both are real, deterministic, reproducible behaviors
  (`tests/test_policy_engine.py`, `simulator/failure_injector/`), so there's no reason to fake them.
- Shot 8's Cloud Trace panel only shows something meaningful once Phase 2's distributed services
  exist — before that, a single-process trace is honest but less illustrative; consider whether to
  cut this shot shorter pre-Phase-2 rather than imply a distributed trace that isn't real yet.

## Relationship to the generated loop

`docs/SUBMISSION_VIDEO.md`'s 30-second loop is a narrative/illustrative asset (AI-generated stills
animated into clips) — useful as a title card or social-post asset, explicitly not offered as
evidence the software runs. This document's live walkthrough is the actual proof. Do not conflate
the two in submission materials; caption the generated loop as illustration if it's used alongside
this recording.
