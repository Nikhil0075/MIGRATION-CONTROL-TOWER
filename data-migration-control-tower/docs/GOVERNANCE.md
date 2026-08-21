# Governance

How this project decides what an agent is allowed to do, who can approve what, and what backstops
exist against runaway cost — and, as honestly as `docs/compliance_matrix.md`, what's enforced
today versus merely declared.

## The policy engine is the one decision point

`tools/policy_engine.py::evaluate(agent_key, action, resource_class, run_id)` is the single
deterministic ALLOW / DENY / REQUIRE_APPROVAL decision point, reading `policies/agent_permissions.yaml`.
It is plain Python control flow — no model call, no prompt — so nothing an attacker-controlled
string (a table comment, a DAG owner field) can talk its way past. Every evaluation is recorded as
a `PolicyDecision` (`contracts/metadata_model.json`), whether or not a `run_id` is supplied, so
denials are auditable even for ad hoc checks.

## Two layers, not one (Deploy & Harden Phase 1, `docs/adr/0001-two-layer-policy-enforcement.md`)

**Layer 1 — capability dispatch authorization.** `tools/registry.py::invoke_capability()` checks,
before calling any resolved handler, whether that agent's `permissions_key` is authorized for
`capability:<capability-string>` at all. This is coarse: it answers "is this agent allowed to be
invoked for this capability," not "is everything it's about to do allowed." A DENY here raises
`registry.CapabilityDenied` and the handler never runs.

**Layer 2 — tool-level authorization.** Every sensitive tool operation an agent performs *after*
dispatch — an adapter read, a Firestore write, a BigQuery query — independently calls
`tools/policy_engine.py::authorize(ctx)` with a typed `tools/invocation_context.py::InvocationContext`
carrying the *real* `resource_class` for that specific operation. This fails closed: a context with
no `resource_class` is denied before `evaluate()` is even consulted, rather than silently
inheriting Layer 1's `METADATA` default. A DENY here raises `policy_engine.PolicyDenied`.

**Coverage today**: Layer 1 covers every agent that dispatches through `invoke_capability()` — all
seven (`discovery`, `lineage`, `risk`, `planner`, `validation`, `cutover`, `finance`). Layer 2 is
fully wired through Discovery's real code path (both the registered-estate and legacy discovery
flows in `agents/discovery/agent.py`) and, from before this phase, Risk and Cutover's own
fine-grained checks (`agents/risk/agent.py`, `agents/cutover/agent.py`). **Lineage, Planner, and
Validation do not yet call `authorize()` at their own tool boundaries** — they pass Layer 1's gate
when dispatched, but their internal tool calls (Firestore writes, BigQuery queries) aren't
independently checked yet. State this precisely, not as "universal enforcement" and not as "zero
enforcement" — both would be wrong.

## Human approval is separate from the policy engine

`tools/approval_service.py` is a parallel mechanism: only `agents/cutover/approve_cutover.py`
(never agent code) calls `approve()`, and `consume()` binds the approval token to the plan's hash
so an approval can't be replayed against a changed plan. `cutover.execute.approved` and
`production.write` are declared `approval_required_actions` in `policies/agent_permissions.yaml` —
the policy engine returns `REQUIRE_APPROVAL` for them, which is a distinct outcome from `ALLOW`
that the caller must handle explicitly (Cutover does; a generic Layer 1/2 caller that doesn't
expect `REQUIRE_APPROVAL` should treat it as a refusal, not a stalled ALLOW).

## Separation of duties

- **Registry publish/approve**: `tools/registry.py::approve()` raises `PermissionError` if
  `approved_by == published_by` — an agent card cannot approve itself into the fleet.
- **Cutover approval**: `tools/approval_service.py` requires a distinct human identity from
  whoever requested it; `agents/cutover/agent.py`'s `denied_tools` includes `approval.self_issue`.

## Cost controls (Deploy & Harden Phase 1c/1d)

Two independent layers, neither a substitute for the other:

1. **BigQuery byte caps** (`tools/bigquery_tools.py::_metered_query()`) — dry-run the query first
   (free, doesn't execute) to estimate its cost; reserve that estimate against the run's
   cumulative soft budget (`tools/usage_meter.py::reserve_bigquery_budget()`, a Firestore
   transaction on `migration_runs/{run_id}/budget/bigquery`); only then run the real query with
   `maximum_bytes_billed` set from the estimate, which BigQuery itself enforces as a hard
   per-query backstop. The per-run cumulative check is explicitly a **soft, best-effort control**
   — concurrent requests racing the transaction can still overshoot briefly, and it says nothing
   about spend outside the run it's scoped to.
2. **Cloud Billing Budget alert** (`infrastructure/setup_billing_budget.py`) — the real backstop
   for total account spend. **A budget is an alert, not a spending cap.** It does not stop Cloud
   Run, Cloud SQL, or BigQuery from continuing to bill once a threshold is crossed; it only
   publishes a Pub/Sub notification. Do not describe it, in any submission or evidence material,
   as something that prevents overspend — it prevents overspend going *unnoticed*.

Billing-export support exists in code (`tools/billing_export.py`) for showing *actual* billed cost
(as opposed to the estimated/measured cost the two controls above compute) — but live export
configuration is a billing-account-level Cloud Console step (`docs/RUNBOOK.md`'s "Actual cost —
needs you" section), optional and unconfigured by default. Verify it's actually enabled before
citing an "actual cost" figure anywhere.

## What's declared but not (yet) exercised

`infrastructure/gcp_setup.sh` creates seven real, distinct IAM service accounts (one per agent)
with genuinely differing role bindings. Until Deploy & Harden Phase 2's distributed deployment
lands, every agent still runs in-process inside the orchestrator under generic Application Default
Credentials — so these service accounts are real, correctly-scoped identities that exist in IAM but
are not yet what's actually running the code. Say exactly that; don't imply per-agent workload
identity is enforced at runtime until each agent is its own deployed Cloud Run service with that
SA attached (`gcp_setup.sh`'s own closing comment already says this candidly).

## Untrusted content stays untrusted

Anything sourced from the legacy estate (column names, DAG docstrings, table comments) is parsed as
data — regex, `ast.literal_eval`, JSON-schema validation — never executed and never
string-concatenated into a model system prompt. `tools/untrusted_content.py`'s containment scan
plus `simulator/injection_corpus/` (12 adversarial cases across 4 families) and
`tests/test_injection_defense.py` are the regression suite. This is the project's documented
substitution for Model Armor (unavailable in this setup) — a deterministic containment layer, not
a model-based content-safety check, and correctly labeled as such rather than implied to be the
managed product.

## See also

- `docs/THREAT_MODEL.md` — the attack surface this deployment effort adds, and what mitigates each.
- `docs/adr/0001-two-layer-policy-enforcement.md` — the design rationale and the two real bugs
  found implementing this (a latent crash in `evaluate()`, and six agent cards that never actually
  persisted their `permissions_key`).
- `docs/compliance_matrix.md` — line-by-line status against the master doc's requirements, updated
  only after behavior is live-verified, not when code merges.
