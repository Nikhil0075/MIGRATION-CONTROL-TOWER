"""Deterministic policy decision point (PDP) — master doc §5.1, §9.

    Agent request -> identity verified -> action + resource classified
    -> least-privilege policy evaluated -> Model Armor / content safety
    checks -> ALLOW | DENY | REQUIRE_APPROVAL -> tool executes only if
    permitted -> result, policy decision, evidence hashes, and trace ID
    recorded.

This module IS that policy-evaluated step, and it is plain Python
control flow reading policies/agent_permissions.yaml — no model call,
no prompt, nothing an attacker-controlled string (a table comment, a DAG
owner field, a "description") can talk its way around. That is
deliberate: §9 assigns "permission checks and IAM/policy decisions" to
deterministic code, and §5.2 requires "any prompt or file attempting to
override system policy is treated as untrusted content and cannot grant
permissions."

Model Armor / content-safety checks are out of scope for this evaluator
today — Model Armor itself is Block C scope (§17.2) and, per the §19
degradation ladder, this deterministic policy engine plus the untrusted-
content handling already in tools/source_catalog.py (never executing
discovered content) is exactly the documented Rung-2/3 substitution.

Every evaluation is recorded as a PolicyDecision (contracts/
metadata_model.json), whether or not a run_id is supplied, so denials
are auditable even for ad hoc/pre-run checks.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from tools.firestore_client import get_client

if TYPE_CHECKING:
    from tools.invocation_context import InvocationContext

REPO_ROOT = Path(__file__).resolve().parents[1]
PERMISSIONS_PATH = REPO_ROOT / "policies" / "agent_permissions.yaml"

DECISION_ALLOW = "ALLOW"
DECISION_DENY = "DENY"
DECISION_REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class PolicyDenied(PermissionError):
    """Raised by authorize() when a tool-level call is DENY/REQUIRE_APPROVAL,
    or when it's missing a resource_class entirely (fail-closed — see
    authorize()'s docstring). Carries the full PolicyDecision record so
    callers can log/display the reason, not just "denied"."""

    def __init__(self, record: dict):
        self.record = record
        super().__init__(
            f"policy {record.get('decision')} for {record.get('agent_id')}."
            f"{record.get('action')} ({record.get('reason')})"
        )


@lru_cache(maxsize=1)
def _load_permissions() -> dict:
    with open(PERMISSIONS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def evaluate(
    agent_key: str,
    action: str,
    resource_class: str = "METADATA",
    run_id: str | None = None,
    *,
    tool_name: str | None = None,
    trace_id: str | None = None,
) -> dict:
    """Evaluates one agent tool-call request and returns + records a PolicyDecision.

    agent_key must match a top-level key in policies/agent_permissions.yaml
    (e.g. "discovery", "risk", "cutover") — the registry-resolved short
    name, not the AGENT_ID display string (master doc §20's agent_id is
    the display/registry identity; this is the ceiling-permission lookup
    key until the registry itself is built in Block C). agent_key may be
    None (e.g. a registry card with no permissions_key declared, Deploy &
    Harden Phase 1a) — that denies rather than raising, since a missing
    identity must fail closed, not crash past the check.
    """
    permissions = _load_permissions()
    agent_policy = permissions.get(agent_key) if agent_key else None
    if agent_policy is None:
        decision, reason = DECISION_DENY, f"no policy declared for agent_key={agent_key!r}"
    else:
        decision, reason = _decide(agent_policy, action, resource_class)

    evidence_basis = f"{agent_key}|{action}|{resource_class}|{run_id or ''}|{decision}|{reason}"
    agent_key_label = agent_key or "UNKNOWN"
    record = {
        "policy_id": f"POL-{action.upper()}-{agent_key_label.upper()}",
        "agent_id": agent_key,
        "action": action,
        "tool_name": tool_name or action,
        "resource_class": resource_class,
        "decision": decision,
        "approval_required": decision == DECISION_REQUIRE_APPROVAL,
        "run_id": run_id,
        "reason": reason,
        "trace_id": trace_id,
        "evidence_hash": hashlib.sha256(evidence_basis.encode()).hexdigest(),
        "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _record_decision(record)
    return record


def _decide(agent_policy: dict, action: str, resource_class: str) -> tuple[str, str]:
    approval_required_actions = set(agent_policy.get("approval_required_actions", []))
    denied_tools = set(agent_policy.get("denied_tools", []))
    allowed_tools = set(agent_policy.get("allowed_tools", []))
    data_classes = agent_policy.get("data_classes")  # None = no ceiling declared

    # Approval-gated actions are checked first: an action can appear in
    # both allowed_tools (it IS this agent's job) and
    # approval_required_actions (but only with a human token) — cutover's
    # cutover.execute.approved is exactly this shape.
    if action in approval_required_actions:
        return DECISION_REQUIRE_APPROVAL, "action requires human approval token"

    if action in denied_tools:
        return DECISION_DENY, "action explicitly denied for this agent"

    if data_classes is not None and resource_class not in data_classes:
        return (
            DECISION_DENY,
            f"resource_class={resource_class!r} exceeds this agent's data-class ceiling {sorted(data_classes)}",
        )

    if action in allowed_tools:
        return DECISION_ALLOW, "action explicitly allowed for this agent"

    # Least privilege: unlisted actions are denied by default, not allowed.
    return DECISION_DENY, "action not in this agent's allowed_tools (default deny)"


def _record_decision(record: dict) -> str:
    client = get_client()
    if record["run_id"]:
        collection = client.collection("migration_runs").document(record["run_id"]).collection(
            "policy_decisions"
        )
    else:
        collection = client.collection("policy_decisions")
    doc_ref = collection.document(str(uuid.uuid4()))
    doc_ref.set(record)
    return doc_ref.path


def authorize(ctx: "InvocationContext") -> dict:
    """Tool-level policy checkpoint (Deploy & Harden Phase 1b, ADR 0001).

    This is the second, finer-grained layer alongside
    `tools/registry.py::invoke_capability()`'s coarse capability-dispatch
    gate. Call this immediately before any sensitive tool operation
    (adapter read/write, BigQuery access, secret access) — not only at
    the moment an agent capability starts.

    Fails closed: an InvocationContext with no resource_class is refused
    here, before evaluate() ever runs, rather than silently inheriting
    the capability gate's METADATA default. A masked-row read, a
    production write, or an approval-protected operation must state its
    real classification explicitly — "I forgot to classify this" and "I
    classified this as METADATA" must never look the same to the policy
    engine.

    Raises PolicyDenied (never returns) on anything other than ALLOW —
    callers that need to react to REQUIRE_APPROVAL specifically (Cutover
    already has its own human-approval flow via tools/approval_service.py)
    should catch PolicyDenied and inspect `.record["decision"]`.
    """
    if not ctx.resource_class:
        record = {
            "policy_id": f"POL-{ctx.action.upper()}-{ctx.agent_id.upper()}",
            "agent_id": ctx.agent_id,
            "action": ctx.action,
            "tool_name": ctx.tool_name or ctx.action,
            "resource_class": None,
            "decision": DECISION_DENY,
            "approval_required": False,
            "run_id": ctx.run_id,
            "reason": "no resource_class supplied — fail closed rather than default to METADATA",
            "trace_id": ctx.trace_id,
            "evidence_hash": hashlib.sha256(
                f"{ctx.agent_id}|{ctx.action}|MISSING_RESOURCE_CLASS|{ctx.run_id or ''}".encode()
            ).hexdigest(),
            "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        _record_decision(record)
        raise PolicyDenied(record)

    record = evaluate(
        ctx.agent_id,
        ctx.action,
        ctx.resource_class,
        ctx.run_id,
        tool_name=ctx.tool_name,
        trace_id=ctx.trace_id,
    )
    if record["decision"] != DECISION_ALLOW:
        raise PolicyDenied(record)
    return record
