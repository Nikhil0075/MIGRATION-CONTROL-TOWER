"""Guardrails: the untrusted-content envelope and deterministic containment
scan (Block C, master doc §23).

    A migration control tower reads untrusted content by design. Table
    comments, column descriptions, DAG docstrings, and legacy
    documentation are all attacker-controllable in a real estate, and
    all of them are fed to a model. Guardrails are therefore not a
    compliance ornament in this project; they are load-bearing.

§19's ladder puts "deterministic pre- and post-filters: untrusted-
content envelopes, output schema validation, tool allowlists" at Rung 2
(Model Armor itself, Rung 1, isn't available in this project's region/
setup) — this module is exactly that Rung-2 shape, and it's the layer
actually enforcing containment in this codebase today, alongside the
four structural invariants below (already true of the architecture, not
new code, but worth stating and testing explicitly):

  1. Model output cannot name a tool the agent card does not allow —
     ADK tool lists are Python function references in code, never
     derived from parsed content.
  2. Model output cannot alter policy, permissions, or state transitions —
     transition_state() and policy_engine.evaluate() are always called
     with fixed arguments from our code, never from parsed model text.
  3. Tool authorization is evaluated by the policy decision point using
     the agent card / registry — never using anything a model emitted.
  4. All model output is schema-validated before use (see
     tools/multimodal_discovery.py's json.loads + required-key check);
     a parse failure is a step failure, not a free-text fallback into
     anything executable.

wrap() documents the envelope shape (§23.1); this project's Gemini calls
(recovery.py's narrative, multimodal_discovery.py's extraction) already
interpolate estate content as plain string data into a fixed prompt
template — never string-concatenated into a system instruction — which
is the practical form the envelope takes without a formal
system/context message-role split.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

from tools.firestore_client import get_client

CONTAINMENT_COLLECTION = "containment_events"

# Deterministic, keyword/pattern-based — no model call to decide whether
# something looks adversarial (a model call could itself be the thing
# that gets talked around).
# [\s_]+ throughout, not \s+: an injection attempt riding in an
# identifier-shaped field (a table/column name can't contain spaces)
# joins words with underscores instead — same attack, different
# delimiter. Caught a real gap here during Day 8 testing: a
# space-only pattern missed 'ignore_previous_instructions_and_export_...'.
_INJECTION_PATTERNS: dict[str, str] = {
    "instruction_override": r"ignore[\s_]+(all[\s_]+)?(previous[\s_]+)?(polic(y|ies)|instructions?)",
    "instruction_override_disregard": r"disregard[\s_]+(polic(y|ies)|instructions?|rules?)",
    "exfiltrate_request": r"export[\s_]+(raw[\s_]+)?(customer|pii|sensitive)[\s_]+data",
    "fabricated_tool": r"\buse[\s_]+tool[\s_]+['\"]?[\w_]+['\"]?",
    "elevated_privilege_claim": r"(has[\s_]been[\s_]|you[\s_]are[\s_]|you're[\s_])?granted[\s_]+(production|admin|elevated)[\s_]*(write|access)?",
    "external_endpoint": r"(https?://|POST[\s_]to|send[\s_](results|data|output)[\s_]to)[\s_]*\S+",
    "system_prompt_probe": r"(reveal|print|show)[\s_]+(your[\s_]+)?(system[\s_]+prompt|instructions)",
}


def wrap(origin: str, content: str) -> dict:
    """§23.1's envelope shape: content stays labeled data, never a system instruction."""
    return {"trust": "UNTRUSTED", "origin": origin, "content": content}


def scan_for_injection_patterns(text: str) -> list[str]:
    """Deterministic containment scan. Returns matched pattern names (empty if clean)."""
    matches = []
    for name, pattern in _INJECTION_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            matches.append(name)
    return matches


def record_containment_event(
    origin: str,
    content_snippet: str,
    matched_patterns: list[str],
    outcome: str,
    acting_agent: str | None = None,
    policy_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    """Writes an auditable containment event (§23.2's "make containment
    visible" requirement): policy identifier, acting agent identity,
    origin of the untrusted content, and outcome, all as distinguishable
    fields — not buried in a free-text log line.
    """
    event = {
        "event_id": str(uuid.uuid4()),
        "origin": origin,
        "content_snippet": content_snippet[:200],
        "matched_patterns": matched_patterns,
        "outcome": outcome,
        "acting_agent": acting_agent,
        "policy_id": policy_id,
        "run_id": run_id,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    client = get_client()
    if run_id:
        collection = (
            client.collection("migration_runs").document(run_id).collection(CONTAINMENT_COLLECTION)
        )
    else:
        collection = client.collection(CONTAINMENT_COLLECTION)
    collection.document(event["event_id"]).set(event)
    return event
