"""Agent Registry service (Block C, master doc §20).

    The design rule: the orchestrator must never name an agent in code.
    It resolves actors by querying the registry for a capability, and it
    records the resolved agent version in the run record.

Firestore layout: agent_registry/{agent_id}/versions/{version} — one
AgentCard (contracts/metadata_model.json) per published version.

Enforced deterministically, no model in the loop:
  - publish() writes DRAFT; approve() requires a DIFFERENT identity than
    the publisher (§20.2: "an agent cannot approve itself, and the
    publisher cannot approve its own card") — raises PermissionError
    otherwise, the same separation-of-duties pattern as
    tools/approval_service.py's human-approval gate.
  - discover() only ever returns APPROVED cards to a capability query, so
    a DEPRECATED or DRAFT card is invisible to normal dispatch (§20.3's
    negative proof).
  - invoke_capability() is what makes this real rather than a display
    list: it dynamically imports and calls the resolved card's handler,
    so agents/orchestrator/orchestrator.py's dispatch genuinely cannot
    function without a matching APPROVED card.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
from pathlib import Path

import jsonschema

from tools.firestore_client import get_client

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "metadata_model.json"
REGISTRY_COLLECTION = "agent_registry"
VERSIONS_SUBCOLLECTION = "versions"


def _agent_card_schema() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["definitions"]["AgentCard"]


_SCHEMA = _agent_card_schema()


class NoApprovedProvider(LookupError):
    """Raised when no APPROVED card advertises the requested capability.

    A real, distinct exception type (not a generic LookupError) so
    calling code can catch it specifically and escalate deliberately,
    per §20.3's negative proof: "the orchestrator records that no
    approved provider exists ... and escalates rather than proceeding
    blindly."
    """


class CapabilityDenied(PermissionError):
    """Raised by invoke_capability() when the resolved card's
    permissions_key fails the capability-dispatch policy gate (Deploy &
    Harden Phase 1a, ADR 0001) — "is this agent allowed to be invoked for
    this capability at all." This is deliberately coarse: it does not
    authorize whatever the handler does once it's running — see
    tools/policy_engine.py::authorize() for the tool-level layer that
    covers that. Carries the PolicyDecision record for callers that want
    the reason, not just the fact of denial."""

    def __init__(self, record: dict):
        self.record = record
        super().__init__(
            f"capability dispatch {record.get('decision')} for "
            f"{record.get('agent_id')} (capability={record.get('tool_name')}): {record.get('reason')}"
        )


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def publish(card: dict, published_by: str) -> dict:
    """Registers a new agent version in DRAFT status (§20.2's Publish)."""
    record = {
        **card,
        "status": "DRAFT",
        "published_by": published_by,
        "published_at": _now(),
        "approved_by": None,
        "approved_at": None,
        "deprecated_at": None,
    }
    jsonschema.validate(instance=record, schema=_SCHEMA)

    client = get_client()
    client.collection(REGISTRY_COLLECTION).document(record["agent_id"]).collection(
        VERSIONS_SUBCOLLECTION
    ).document(record["version"]).set(record)
    return record


def approve(agent_id: str, version: str, approved_by: str, status: str = "APPROVED") -> dict:
    """Transitions DRAFT -> APPROVED or RESTRICTED (§20.2's Approve).

    Raises PermissionError if approved_by is the same identity that
    published this card — no self-approval, mirroring
    tools/approval_service.py's human-approval separation of duties.
    """
    card = resolve(agent_id, version)
    if card is None:
        raise ValueError(f"No such card: {agent_id!r} v{version!r}")
    if approved_by == card["published_by"]:
        raise PermissionError(
            f"Identity {approved_by!r} published {agent_id!r} v{version!r} and cannot "
            "also approve it — a governance identity distinct from the publisher is required."
        )
    if status not in ("APPROVED", "RESTRICTED"):
        raise ValueError(f"Invalid approval status: {status!r}")

    updated = {**card, "status": status, "approved_by": approved_by, "approved_at": _now()}
    client = get_client()
    client.collection(REGISTRY_COLLECTION).document(agent_id).collection(
        VERSIONS_SUBCOLLECTION
    ).document(version).set(updated)
    return updated


def resolve(agent_id: str, version: str) -> dict | None:
    """Returns a pinned version (§20.2's Resolve) — used when a run resumes
    and must reuse the exact version it started with."""
    client = get_client()
    doc = (
        client.collection(REGISTRY_COLLECTION)
        .document(agent_id)
        .collection(VERSIONS_SUBCOLLECTION)
        .document(version)
        .get()
    )
    return doc.to_dict() if doc.exists else None


def _capability_matches(query: str, capabilities: list[str]) -> bool:
    if query.endswith(".*"):
        prefix = query[:-1]  # keep trailing '.'
        return any(cap.startswith(prefix) for cap in capabilities)
    return query in capabilities


def discover(capability: str, status: str = "APPROVED") -> list[dict]:
    """The orchestrator's only route to an actor (§20.2's Discover).

    Filters client-side rather than with a Firestore composite index —
    the registry is small (single-digit agents today), and this avoids
    an index-provisioning step that would otherwise block first use.
    Returns candidates ordered by version descending (newest first).
    """
    client = get_client()
    # collection_group("versions") is scoped correctly as long as no other
    # part of the system uses a subcollection literally named "versions"
    # (it doesn't, today) — no extra parent-path filtering needed.
    all_cards = [doc.to_dict() for doc in client.collection_group(VERSIONS_SUBCOLLECTION).stream()]
    matches = [
        card
        for card in all_cards
        if card.get("status") == status and _capability_matches(capability, card.get("capabilities", []))
    ]
    return sorted(matches, key=lambda c: c["version"], reverse=True)


def deprecate(agent_id: str, version: str, deprecated_by: str) -> dict:
    """Marks a version unavailable for new runs (§20.2's Deprecate) — the
    mechanic that makes weeks-long operation safe across releases. In-flight
    runs stay pinned to whatever version they resolved (§20.4/§21.1)."""
    card = resolve(agent_id, version)
    if card is None:
        raise ValueError(f"No such card: {agent_id!r} v{version!r}")
    updated = {**card, "status": "DEPRECATED", "deprecated_at": _now(), "deprecated_by": deprecated_by}
    client = get_client()
    client.collection(REGISTRY_COLLECTION).document(agent_id).collection(
        VERSIONS_SUBCOLLECTION
    ).document(version).set(updated)
    return updated


def delete_card(agent_id: str, version: str) -> None:
    """Hard-delete, for test/dev hygiene only — production code should
    call deprecate(), never this. A card left behind by a test run stays
    APPROVED forever and can pollute real capability discovery (e.g. a
    test card registered under 'impact.assessment.*' would be found by
    the real Finance-agent wildcard lookup); tests must call this in
    teardown for every card they publish.
    """
    client = get_client()
    client.collection(REGISTRY_COLLECTION).document(agent_id).collection(
        VERSIONS_SUBCOLLECTION
    ).document(version).delete()


def get_history(agent_id: str) -> list[dict]:
    """Full version lineage (§20.2's Audit): publisher, approver, timestamps."""
    client = get_client()
    docs = (
        client.collection(REGISTRY_COLLECTION)
        .document(agent_id)
        .collection(VERSIONS_SUBCOLLECTION)
        .stream()
    )
    return sorted((d.to_dict() for d in docs), key=lambda c: c["published_at"])


def resolve_capability_handler(capability: str) -> dict:
    """Returns the single best APPROVED card for a capability.

    Raises NoApprovedProvider — never returns None/empty and never falls
    back to a hardcoded default — if nothing matches.
    """
    candidates = discover(capability)
    if not candidates:
        raise NoApprovedProvider(
            f"No APPROVED provider exists for capability {capability!r}. "
            "Escalating rather than proceeding blindly (master doc §20.3)."
        )
    return candidates[0]


def invoke_capability(capability: str, *args, **kwargs):
    """Resolves capability -> APPROVED card -> dynamically imports and
    calls its handler. Returns (result, agent_id, version) so the caller
    can pin the resolved version into the run record (§20.4).

    Capability-dispatch policy gate (Deploy & Harden Phase 1a, ADR 0001):
    before calling the handler, checks whether the resolved card's
    `permissions_key` is authorized to be invoked for this capability at
    all. This is the OUTER, coarse layer only — it does not authorize
    whatever the handler goes on to do internally (adapter reads,
    Firestore writes, BigQuery access); those call
    tools/policy_engine.py::authorize() independently, with their own
    real resource_class, at the point they happen. See
    docs/GOVERNANCE.md for the full two-layer model.

    `run_id` is extracted best-effort for the audit record (evaluate()
    tolerates run_id=None) — from an explicit `run_id` kwarg first, else
    the first positional string arg. This is deliberately NOT trusted for
    anything beyond audit-trail attribution: unlike the tool-level layer
    (tools/invocation_context.py), a wrong guess here only means a policy
    decision gets recorded against the wrong run_id, not a wrong
    authorization outcome — the ALLOW/DENY decision itself never depends
    on run_id.
    """
    from tools import policy_engine

    card = resolve_capability_handler(capability)
    run_id = kwargs.get("run_id")
    if run_id is None and args and isinstance(args[0], str):
        run_id = args[0]

    decision = policy_engine.evaluate(
        card.get("permissions_key"),
        f"capability:{capability}",
        "METADATA",
        run_id,
        tool_name=capability,
    )
    if decision["decision"] != policy_engine.DECISION_ALLOW:
        raise CapabilityDenied(decision)

    runtime = card.get("runtime") or {}
    if runtime.get("type") == "cloud_run":
        # Deploy & Harden Phase 2c (docs/adr/0002-typed-http-dispatch.md):
        # this card is served by an independently-deployed Cloud Run
        # service, not this process — call it over a typed, OIDC-verified
        # HTTP envelope instead of dynamically importing its handler.
        # estate_id isn't in invoke_capability()'s own signature (callers
        # pass it as a kwarg like any other handler argument, e.g.
        # discover_estate(estate_id=...)), so it's read the same
        # best-effort way run_id is above, not required.
        from tools.capability_dispatch_client import invoke_remote_capability

        service_url = runtime.get("url")
        if not service_url:
            raise CapabilityDenied(
                {**decision, "reason": f"card {card['agent_id']!r} declares runtime.type=cloud_run but no url"}
            )
        result = invoke_remote_capability(
            service_url=service_url,
            capability=capability,
            args=list(args),
            kwargs=kwargs,
            run_id=run_id,
            estate_id=kwargs.get("estate_id"),
        )
        return result, card["agent_id"], card["version"]

    module_path, func_name = card["handler"].split(":")
    module = importlib.import_module(module_path)
    handler = getattr(module, func_name)
    result = handler(*args, **kwargs)
    return result, card["agent_id"], card["version"]
