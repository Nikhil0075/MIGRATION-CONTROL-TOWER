# ADR 0001: Two-layer policy enforcement (capability gate + tool-level context)

## Status
Accepted — Deploy & Harden Phase 1.

## Context
`tools/policy_engine.py::evaluate()` is the single deterministic ALLOW/DENY/REQUIRE_APPROVAL
decision point (master doc §5.1/§9). Before this ADR it was called directly from exactly two
places: `agents/risk/agent.py` and `agents/cutover/agent.py`. Discovery, Lineage, Planner and
Validation never called it — confirmed by a direct code audit, not assumed from docs.

A first-draft fix considered adding one `evaluate()` call at
`tools/registry.py::invoke_capability()` — the single place every capability dispatch already
passes through — and calling that "universal enforcement." That is incomplete: it authorizes
*starting* a capability, not the individual tool operations (adapter reads, Firestore writes,
BigQuery access, secret reads) that capability goes on to perform once inside the handler. An
agent could pass the capability gate and then read PII, write to a table it has no business
touching, or hit BigQuery with an unclassified query — the gate would never see any of it.

## Decision
Enforce policy at two independent layers, not one:

1. **Capability dispatch authorization** (`tools/registry.py::invoke_capability()`) — coarse:
   "is this agent allowed to be invoked for this capability at all." Uses the fixed default
   `resource_class="METADATA"`, which is safe here specifically because it's the least-permissive
   ceiling every agent's `data_classes` in `policies/agent_permissions.yaml` allows today.
2. **Tool-level authorization** — every sensitive tool call (adapter I/O, Firestore writes,
   BigQuery access, secret access) independently calls `evaluate()` with its *actual*
   `resource_class`, supplied by the caller through a required typed `InvocationContext`
   (`tools/invocation_context.py`), never inferred or defaulted. A missing/absent classification
   fails closed (DENY), it does not fall back to METADATA.

## Consequences
- Closes the real gap (tool-level bypass), not just the dispatch-level one.
- More call sites to update (every sensitive tool function, not one dispatch point) — larger,
  riskier change; budget real regression-testing time for legitimate calls newly denied by the
  fail-closed rule.
- Requires plumbing `run_id`/`estate_id`/`trace_id` explicitly through call chains that
  previously inferred `run_id` from "the first positional string" — a real bug waiting to happen
  the moment that string was an estate/table/pipeline id instead of a run id.

## Real defects found while implementing this (not hypothetical)

1. `tools/policy_engine.py::evaluate()` crashed with `AttributeError` on `agent_key=None` (tried
   `None.upper()` building the record). Every existing caller had always passed a real string, so
   this was latent until the capability gate became the first caller that can legitimately pass
   `None` (a card with no `permissions_key`). Fixed to deny cleanly instead of crashing.
2. `infrastructure/seed_registry.py`'s six core-agent cards never actually persisted
   `permissions_key` onto the published `AgentCard` — the field was used transiently during
   seeding (to look up `permissions` and build the service-account name) but dropped before the
   `card` dict was written to Firestore. This is a strictly larger version of the already-known
   finance-agent gap: it would have denied every real capability dispatch for **all six** agents,
   not just Finance, the moment this gate went live. Found by a live-Firestore test failure
   (`tests/test_evaluation_harness.py`) after re-seeding, not assumed from a code read. Fixed in
   `seed_registry.py`; both `(default)` and the dedicated `mct-tests` test database
   (`python -m infrastructure.seed_test_database`) needed re-seeding to pick up the fix — a
   reminder that this project runs two separate Firestore databases and a fix applied to one does
   not reach the other automatically.
- Documentation must describe coverage precisely: *"Discovery, Lineage, Planner and Validation do
  not currently pass their tool operations through the policy decision point. Risk and Cutover
  have direct enforcement."* — not "zero enforcement" (the capability gate is real, even if
  coarse) and not "universal enforcement" until the tool-level layer actually lands everywhere.
