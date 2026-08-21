# Requirement Compliance Matrix

Master doc §26's concept: every mandatory/optional element mapped to
where it's satisfied and the artifact that evidences it — reviewed
before submission, and (per the doc's own closing principle) the place
"Section 32 was demonstrated at pattern-scale, not 20,000-definition
scale" gets stated explicitly rather than left for a judge to assume.

Status legend: ✅ done and verified live · ⚠️ done via a documented
substitution/partial implementation · ❌ not attempted (with the reason).

## §26.1 Mandatory technology requirements

| Requirement | Satisfied by | Evidence artifact | Status |
|---|---|---|---|
| Gemini 3.5+ via Vertex AI | Bounded structured reasoning uses `gemini-3.7-flash` high for Discovery, Lineage and Planner; optional recovery explanation uses the same audited gateway; the read-only console assistant uses `gemini-3.5-flash` medium | `tools/model_gateway.py`; `frontend/assistant_service.py`; `infrastructure/seed_registry.py`; run-scoped `agent_execution_events` | ⚠️ implemented and locally contract-tested; the separately labelled live Vertex AI acceptance run is still a public-release gate |
| At least one Google agent framework | Google ADK attempted first in every agent module, with a documented Rung-2 direct-tool-call fallback when `google-adk` isn't importable in this environment | Every `agents/*/agent.py`'s `try: from google.adk.agents import Agent` block; `AGENT_FRAMEWORK` variable records which one actually ran per process | ⚠️ ADK is real and preferred; the fallback is honestly labeled, not hidden |
| At least one Google Cloud infrastructure service | Cloud Run (`hello-agent`, live), Firestore (state + TTL), Pub/Sub, BigQuery, Vertex AI and private Cloud Storage report artifacts | `infrastructure/gcp_setup.sh`; `evaluation/reports/cloud_deployment_evidence.md` | ⚠️ core services verified live previously; new Vertex AI reasoning/report-storage acceptance remains pending before public release |

## §26.2 Track 3 named capabilities

| Capability | Implementation | Visible where | Status |
|---|---|---|---|
| Agent Registry — publish, version, discover | `tools/registry.py`: publish/approve/discover/resolve/deprecate; capability-URN dynamic dispatch via `importlib` | `infrastructure/seed_registry.py`; Registry screen (`frontend/`); `agents/orchestrator/orchestrator.py`'s `registry.invoke_capability()` calls | ✅ |
| Agent Runtime — long-running async execution | Pub/Sub event chain (`migration.requested` → ... → `validation.passed`/`failed`) with Firestore write-ahead state at every transition; `tools/events.py::pull()` only acks a message after its handler genuinely succeeds (fix pass, see below — this used to auto-ack before the handler even ran) | Kill-and-resume proof (`agents/orchestrator/durability_demo.py`); run timeline (`state_history` on every run doc); live `run_full_migration.py` run with every `processed_messages` doc reaching `status="done"`, none stuck `"claimed"` | ⚠️ Event-driven and durable, real; only `hello-agent` is deployed as its own Cloud Run service today — the rest run in one orchestrator process (honestly stated, see `orchestrator.py`'s docstring) |
| Memory Bank — persistent cross-session context | `tools/memory_bank.py`: global (not run-scoped) collection keyed by defect signature, exact-match recall | A fresh run recalling a prior remediation (`root_cause_generated_by=recalled_memory`, reuse count growing across every live run — 11 reuses observed by the last Phase-5 verification run) | ✅ |
| Agent Identity — zero-trust access control | Per-agent IAM service accounts with genuinely differing role bindings, declared for all 6 agents; deterministic policy engine as the actual enforcement point | `infrastructure/gcp_setup.sh`'s per-agent SA creation; `tools/policy_engine.py` denial records naming the acting identity | ⚠️ partial — the SA-attachment mechanism is real and verified live for `hello-agent` (the one agent actually deployed as its own Cloud Run service); the other 5 agents' declared SAs are never exercised at runtime today since those agents run in-process inside the orchestrator, not as separately-deployed services (see `cloud_deployment_evidence.md`) |
| Agent Gateway — unified routing and policy enforcement | `tools/policy_engine.py` is a real single policy decision point, but only two agents actually call `evaluate()` today: Risk (`verify_pii_access_boundary`) and Cutover (`attempt_self_approval`, the approval-gate audit log) | `policy_decisions` records on every audited action that does call it; S-03/S-09 in the evaluation harness | ⚠️ partial — real for the actions that call it, but "every tool call traverses the policy engine" overstates coverage; Discovery/Lineage/Planner/Validation never call `evaluate()` |
| Model Armor — inline guardrails | `tools/untrusted_content.py`: deterministic containment scan + envelope, a documented Rung-2 substitution (Model Armor itself unavailable in this project's region/setup) | `simulator/injection_corpus/` (12 cases, all families contained); S-08 in the evaluation harness | ⚠️ documented substitution, real containment proven |
| Agent Observability — OpenTelemetry audit and reasoning traces | `tools/tracing.py`: OTel spans exported to Cloud Trace, tagged with `run_id` and resolved `agent_id`/`version` | Live Cloud Trace query (`evaluation/reports/cloud_deployment_evidence.md`) showing a real 10-span trace tree for one run, including the FAILED→recovery→PASSED loop | ✅ (Day 10 Phase 5) |
| Cross-department cataloging | Finance Reporting Impact Agent, owned by a distinct department/identity, discovered purely via capability wildcard | `infrastructure/seed_finance_agent.py`; `orchestrator.py::trigger_finance_impact_check()`'s zero-hardcoded-knowledge resolution | ✅ |
| Weeks of asynchronous context | Durability design + all three doc-specified proofs | Kill-and-resume (`durability_demo.py`), long-horizon fixture (`agents/orchestrator/seed_long_horizon_fixture.py`, flagged ⚠ everywhere in the UI), memory recall across real runs | ✅ |

## §26.3 Submission deliverables

These are Block D scope (demo recording, write-up, submission) — not
attempted by the engineering work this matrix otherwise covers. Listed
here for completeness, honestly marked not-yet-done.

| Deliverable | Status |
|---|---|
| Category selected: Fortified Enterprise Fleet | not yet declared |
| Hosted project URL | ⚠️ production Oracle JET Redwood UI and multi-stage Cloud Run image are implemented and locally browser-verified; a public deployment URL has not been created in this workspace |
| Text descriptions (features, technologies, data sources, findings) | ❌ not drafted — this matrix and `evaluation/reports/` are the real-output source they'd be drawn from |
| Public repository URL | ⚠️ `github.com/Nikhil0075/MIGRATION-CONTROL-TOWER` exists with real commit history; not yet confirmed public/judge-accessible |
| README spin-up instructions, verified from a clean clone | ✅ `scripts/clean_clone_check.sh` (Day 10 Phase 6) — fresh venv + `pip install -r requirements.txt` + full test collection, verified live in an isolated copy with no `.env` |
| Architecture diagram | ⚠️ `docs/ARCHITECTURE.md` (Deploy & Harden Phase 1g) — a written system diagram + component description; a rendered image asset is still open |
| Demo video (~4 min) | ⚠️ `docs/DEMO.md`'s shot list/script exists; the actual live screen recording is still open — see that doc's "What must be true before this is recorded" section |
| Bonus: technical build article | ❌ not created |
| Bonus: social post | ❌ not created |
| Bonus: additional Google model (Gemma) | ⚠️ documented substitution — `tools/fast_pii_screen.py` plays Gemma's architectural role (a cheap, independent pre-screen) using a naive deterministic classifier, since no Gemma model was pulled in this dev environment (confirmed with the user rather than downloading one unprompted) |

## Master doc §32 (Volume II): portability and scale

Addressed directly by the Day 10 hardening plan's Phases 3–4, scoped
explicitly to **"the pattern, not the scale"** per the user's own call —
stated here so this is never mistaken for the full 20,000-definition
claim.

| §32 element | Built | Evidence | Scope note |
|---|---|---|---|
| Canonical source-adapter interface | `tools/adapters/` — `SourceAdapter` ABC + 3 implementations | `tests/test_adapters.py`: every adapter's output verified byte-identical to the pre-adapter function calls, live; `agents/discovery/agent.py::discover_estate()` (the exact function `orchestrator.py` resolves via the registry for every real run) now calls the adapters directly, not `tools/source_catalog.py` — verified with a live discovery run (58 tables, 4 pipelines, unchanged) | ✅ real, and now the flagship path's actual source (fix pass after the second audit — previously proven only in isolation by tests) |
| Estate configuration | `simulator/source_setup/estate.yaml` | Loaded by `tools/pack_loader.py` | ✅ real |
| Contract tests across sources | `tests/test_adapters.py` (parametrized) | 10/10 passing, including live SQL Server | ✅ real |
| Versioned Migration Packs | `packs/wwi_sqlserver_v1/`, `packs/oracle_corpus_v1/` | `contracts/metadata_model.json`'s `MigrationPack` definition; `agents/discovery/run_assessment.py` resolves a pack to run assessment mode | ⚠️ partial — real and load-bearing for assessment-mode/second-estate runs, but the flagship orchestrator's `handle_migration_requested` still resolves its source paths from hardcoded module constants (`ORACLE_CORPUS_PATH`, `DAG_ARTIFACTS_PATH`), not by loading a pack at dispatch time; making Migration Packs the *execution* path's source too (not just assessment's) is unfinished |
| Assessment vs. Execution mode | `run_lifecycle.py::create_run(mode=...)`; `handle_planned()` now also refuses to execute a migration for any `mode="assessment"` run that reaches it, structurally, not just by `run_assessment.py`'s own call graph never publishing `plan.created` | `agents/discovery/run_assessment.py`; `tests/test_orchestrator.py::test_handle_planned_refuses_to_execute_for_an_assessment_mode_run` | ✅ real |
| Second-estate onboarding test | Oracle-dialect corpus run through the identical assessment flow as WWI | `evaluation/reports/assessment_oracle_corpus_v1_*.md` — 10 tables, 6 real lineage edges, 3 PII tables, 10 dialect findings, zero BigQuery calls | ✅ real, but a second *source family*, not a second live database — the honest limit of what's provable without new infrastructure |
| Wave Manager / backpressure | `tools/wave_manager.py` — per-source concurrency caps, CRITICAL-risk cap, backlog-age escalation; `reserve_slot()`/`release_slot()` (fix pass after the second audit) are a transactional Firestore reservation wired into real dispatch (`handle_planned()`, bounded-retried, released in a `finally`), not just `evaluate_wave()`'s pure-function simulation used by tests/the scale harness | `tests/test_wave_manager.py`; live scale-harness run: 6 admitted / 244 held out of 250; live `run_full_migration.py` run showing a real ADMIT/release cycle in `wave_state/slots` | ✅ real, deterministic, and now load-bearing in real dispatch |
| Data-plane / control-plane separation | `tools/migration_executor.py`'s `DataPlaneExecutor` interface; `InMemoryExecutor` is the only implementation | `tests/test_migration_executor.py`; streaming via `fetchmany()` batches instead of one `fetchall()` | ⚠️ documented Rung-2 substitution — no `DataflowExecutor` built (would need a second live estate/real scale to justify the GCP cost) |
| Scale evaluation harness | `evaluation/scale_harness.py` + `simulator/scale_fixtures/` | `evaluation/reports/scale_metrics.md` — real measured p50/p95 latency for schema validation, Wave Manager scheduling, and policy decisions | ⚠️ **control-plane scale at 250 synthetic definitions, NOT the master doc's 20,000-definition bulk-data claim** — that would need a live estate that large to prove honestly |

## Day 10 audit findings → resolution

The six-phase hardening plan this matrix closes out, phase by phase:

| Phase | Finding | Resolution |
|---|---|---|
| 1 | Stored XSS in `frontend/static/app.js`; hardcoded/unauthenticated approval identity; wildcard CORS; hardcoded `fleet_health` | `esc()` escaping everywhere + no inline `onclick` attributes (`tests/test_frontend_xss.py`); real Firebase ID-token verification (`get_approver_identity` dependency); env-configured CORS allowlist + CSP header; `_compute_fleet_health()` derived from real run state |
| 2 | Orchestrator only event-driven through Risk; Planner/migration/Validation/recovery ran as direct calls | 4 new event handlers extending the same registry-resolved Pub/Sub pattern through `validation.passed`/`failed`; verified live with `pinned_agents` showing `planner-agent`/`validation-agent` newly present |
| 3 | No adapter abstraction, estate config, or Migration Pack concept; no second-estate proof | `tools/adapters/` + `estate.yaml` + Migration Packs; Oracle corpus run through the identical flow as WWI, live |
| 4 | No concurrency/backpressure controls; `migration_executor` buffered whole tables in memory | `tools/wave_manager.py`; streaming `fetch_source_rows`/`execute_migration`; a real per-batch BigQuery schema-autodetect bug found and fixed along the way |
| 5 | No Cloud Trace instrumentation despite the dependency being present | `tools/tracing.py`; verified with a direct Cloud Trace API query against a real run's trace tree |
| 6 | No LICENSE, Makefile, or teardown script | This matrix, `LICENSE`, `Makefile`, `infrastructure/teardown.sh`, `scripts/clean_clone_check.sh` |

Every item above marked ✅ or ⚠️ was checked against real infrastructure
during this build, not merely implemented — see each phase's README
section for the specific live-run evidence. That does not mean every
✅/⚠️ item is complete: several were independently re-verified by a
second audit round and found to have real gaps (below), which is
exactly why some rows above are marked ⚠️ rather than ✅ — checked
against real infrastructure and found partial is a different, honest
claim from fully done.

## Fix pass after the second audit round

A second, more skeptical audit re-checked every phase's claims against
the actual code (not this matrix's word) and found specific remaining
gaps. Items 1–6 and 8 were fixed in the original pass; item 7 is now
implemented as an Oracle JET 20.1.3 VDOM/Preact/TypeScript console with
the native Redwood theme:

| # | Finding | Resolution |
|---|---|---|
| 1 | Approval endpoint verified *who* signed in (Firebase ID token) but never checked they were *allowed* to approve — any authenticated Google account could approve a cutover | `APPROVER_ALLOWLIST` env var, checked in `frontend/app.py::get_approver_identity` after token verification; fails closed (empty/unset denies everyone, no "allow all authenticated" default) |
| 2 | Only `handle_migration_requested` had idempotency dedup; a redelivered message to any other handler could double-execute | `_dedup_claim`/`_dedup_complete` (namespaced `{handler_name}:{message_id}`) added to all 6 orchestrator event handlers — later made atomic, see the Pub/Sub semantics fix pass below |
| 3 | Assessment mode worked by construction (`run_assessment.py` never publishes `plan.created`) but had no boundary enforced at the execution function itself | `handle_planned()` now raises if `run.mode == "assessment"`, regardless of how it was reached |
| 4 | Adapters (`tools/adapters/`) were proven byte-identical only in isolated tests; the real Discovery capability still called `tools/source_catalog.py` directly | `agents/discovery/agent.py::discover_estate()` now calls the 3 adapter classes; live discovery run confirmed unchanged output (58 tables, 4 pipelines) |
| 5 | `tools/wave_manager.py`'s `evaluate_wave()` was only ever called by tests and the scale harness — never real dispatch | `reserve_slot()`/`release_slot()`: a transactional Firestore reservation wired into `handle_planned()` (bounded retry-on-HOLD, released in a `finally`); `within_approval_window()` wired into `agents/cutover/agent.py::perform_cutover()` |
| 6 | This matrix overclaimed several ✅ rows (Agent Gateway, Agent Identity, Migration Packs) that were actually partial | Downgraded to ⚠️ with the precise gap stated in each row, above |
| 7 | Legacy three-tab UI lacked production information architecture, responsive drawers, typed API contracts, and operator workflows | Oracle JET Redwood console under `frontend/client`; eleven responsive routes, contextual inspector, Firebase-claim RBAC, idempotent guided actions, durable telemetry views, generated TypeScript client, Vitest/API coverage, multi-stage Docker build, and `/legacy` compatibility route |
| 8 | Teardown didn't cover the `hello-agent` Cloud Run service or its Artifact Registry image; a stray `=6.5.0` file (leftover pip-install output, misinterpreted PowerShell redirection) was committed | See `infrastructure/teardown.sh`; stray file removed |

## Fix pass: Pub/Sub acknowledgement + atomic idempotency semantics

Independently flagged as "the most serious remaining defect" — verified
against the actual code (file:line) and confirmed real:

| # | Finding | Resolution |
|---|---|---|
| 1 | `tools/events.py::pull()` auto-acked every message immediately, before returning it to the caller — so any handler failure downstream (a Wave Manager capacity HOLD in `handle_planned()` raising, any exception) silently discarded the message with no Pub/Sub redelivery. A run could get stuck in `PLANNED` forever despite handler code that explicitly raised expecting to be retried. | `pull()` no longer acks. Added `ack()`/`nack()`; `orchestrator.py::_consume()` (the new single choke point for every `pull → handler` call) acks only after the handler succeeds and `nack()`s — an immediate ack-deadline reset, not waiting out the default deadline — on any exception, so a HOLD genuinely triggers redelivery now. |
| 2 | The idempotency ledger (`_dedup_check` reads, handler runs its side effects, `_dedup_mark` writes) was check-then-act-then-mark, not atomic — a process crash between a side effect (`execute_migration`, `publish`, `transition_state`) and the final mark left no record the work had started, so a redelivered copy of the same message would repeat it in full. | Replaced with `_dedup_claim()`/`_dedup_complete()`: the existence check and the claim write happen inside one Firestore transaction. A fresh message returns `"claimed"`; an already-completed one returns `"done"` with the cached result; a claimed-but-never-completed message (the crash-recovery case) returns `"stale_claim"` and is safely redone — safe because `execute_migration()` truncates-and-reloads on its first batch (already idempotent-safe) and `transition_state()` is graph-checked, failing loudly on an illegal double-transition rather than silently double-applying it. This project runs a single orchestrator process (no worker fleet), so a fresh claim can only mean crash recovery, never real concurrent contention — stated explicitly rather than over-engineered into a distributed exactly-once guarantee this project doesn't need. |
| — | Found while fixing #2, not originally flagged: `handle_migration_requested` marked itself "processed" immediately after `create_run()`, *before* `write_catalog()`/`publish()` ran — the earliest possible point, not the latest. A crash there left a run stuck at `REQUESTED` forever while a redelivered copy was treated as already fully done. | Marked done only at the very end, matching every other handler — with special handling since `create_run()` (unlike every other handler's side effects, which act on an existing `run_id`) genuinely isn't safe to call twice: a stale-claim redo reuses the `run_id` recorded immediately after the original `create_run()` succeeded, rather than creating a second run for the same message. |

**Evidence**: 6 new regression tests (`tests/test_orchestrator.py`) —
2 unit tests proving `_consume()` acks only on success and nacks (not
acks) on a handler exception; 4 live-Firestore tests proving
`_dedup_claim()`'s claimed/done/stale_claim states. Full suite at the
time of this fix: **224/224 passing** — historical; the suite has grown
substantially since (see a dated `evaluation/reports/` snapshot for the
current count, not this number). Live `run_full_migration.py` run against real
Pub/Sub/Firestore/SQL Server/BigQuery reached `COMPLETE` with every
`processed_messages` doc from that run's chain at `status="done"`, none
stuck `"claimed"`, and `wave_state/slots` cleanly released.
