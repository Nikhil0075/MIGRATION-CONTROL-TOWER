"""Typed context for a policy-checked tool call (Deploy & Harden Phase 1b, ADR 0001).

Before this module, `tools/policy_engine.py::evaluate()` callers passed
`run_id` as a bare positional string, and the one place that considered
adding a universal check (`tools/registry.py::invoke_capability()`) would
have inferred `run_id` from "the first positional argument that happens to
be a string" — which breaks the moment that string is actually an
estate_id, source_id, or table_id instead of a run_id. `discover_estate()`
is called both as `invoke_capability(DISCOVERY_CAPABILITY, estate_id=...)`
(no positional args at all) and, on the legacy path, with two positional
path strings that are not a run_id either.

InvocationContext makes every field explicit and required at the call
site instead: nothing is inferred, and a missing field is a construction
error, not a silent wrong guess. `resource_class` deliberately has no
default here — see tools/policy_engine.py::authorize()'s fail-closed rule
docstring for why.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InvocationContext:
    """Everything a tool-level policy decision needs, supplied explicitly.

    agent_id:        registry `permissions_key` (e.g. "discovery"), the
                      same lookup key `tools/policy_engine.py::evaluate()`
                      already reads from `policies/agent_permissions.yaml`.
    action:           the specific tool/operation being attempted (e.g.
                      "source.catalog.sql_server") — matched against that
                      agent's `allowed_tools`/`denied_tools`, not the
                      coarser `capability:...` string the outer dispatch
                      gate in `tools/registry.py::invoke_capability()`
                      checks.
    resource_class:   METADATA | MASKED | PII | PRODUCTION — the *actual*
                      sensitivity of what this specific call touches.
                      Required (no default) so a caller cannot silently
                      inherit the capability gate's METADATA default for
                      an operation that actually reads masked rows or
                      writes to production.
    run_id:           the migration run this call belongs to, or None for
                      an ad hoc/pre-run check (evaluate() already supports
                      recording those to the global `policy_decisions`
                      collection rather than a run-scoped subcollection).
    estate_id:        which estate this call is scoped to, when known.
    acting_identity:  the agent/service identity making the call (falls
                      back to `agent_id` when no finer-grained identity —
                      e.g. a specific service account — is available).
    tool_name:        human-readable tool label for the audit record;
                      defaults to `action` in `evaluate()` when omitted.
    trace_id:         OpenTelemetry/Cloud Trace id, when tracing is active.
    """

    agent_id: str
    action: str
    resource_class: str
    run_id: str | None = None
    estate_id: str | None = None
    acting_identity: str | None = None
    tool_name: str | None = None
    trace_id: str | None = None

    def with_action(self, action: str, resource_class: str | None = None) -> "InvocationContext":
        """Returns a copy for a different tool call within the same run/estate —
        the common case of one agent invocation making several distinct,
        separately-authorized tool calls in sequence."""
        return InvocationContext(
            agent_id=self.agent_id,
            action=action,
            resource_class=resource_class or self.resource_class,
            run_id=self.run_id,
            estate_id=self.estate_id,
            acting_identity=self.acting_identity,
            tool_name=None,
            trace_id=self.trace_id,
        )
