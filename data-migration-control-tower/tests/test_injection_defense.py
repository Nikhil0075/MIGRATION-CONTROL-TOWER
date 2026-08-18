"""Injection defense evaluation harness (master doc §23.2).

Runs all 12 cases in simulator/injection_corpus/payloads.json against
this codebase's real functions — not mocks — per the containment
invariant documented for each family in
simulator/injection_corpus/README.md. The detection step
(scan_for_injection_patterns) and the pure-Python invariant checks (no
Firestore) always run; recording the containment event needs live
Firestore and skips automatically when it isn't reachable, same pattern
as the rest of this suite.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.untrusted_content import (  # noqa: E402
    record_containment_event,
    scan_for_injection_patterns,
    wrap,
)
from tools.policy_engine import DECISION_DENY, DECISION_REQUIRE_APPROVAL, _decide, _load_permissions  # noqa: E402
from tools import registry  # noqa: E402
from agents.orchestrator.recovery import _deterministic_narrative  # noqa: E402

CORPUS_PATH = REPO_ROOT / "simulator" / "injection_corpus" / "payloads.json"
CASES = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["cases"]


def _firestore_reachable() -> bool:
    try:
        from tools.firestore_client import get_client

        get_client()
        return True
    except Exception:  # noqa: BLE001
        return False


FIRESTORE_OK = _firestore_reachable()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_every_case_is_detected_by_the_deterministic_scan(case):
    matches = scan_for_injection_patterns(case["payload_text"])
    assert matches, f"corpus case {case['id']!r} was not detected by any pattern — corpus/scan out of sync"


@pytest.mark.parametrize("case", [c for c in CASES if c["family"] == "direct_instruction_override"], ids=lambda c: c["id"])
def test_direct_instruction_override_is_inert_data(case):
    """The payload becomes a Pipeline.owner-shaped string — nothing reads
    it as an instruction. evaluate()'s signature takes no free-text
    estate content at all, so it can't be swayed by anything here."""
    payload = case["payload_text"]
    envelope = wrap(origin="dag.owner_field", content=payload)
    assert envelope["trust"] == "UNTRUSTED"
    assert envelope["content"] == payload  # stored verbatim as data, not parsed

    # A denial this payload explicitly tries to talk around (Discovery
    # reading raw PII) is completely unaffected by the payload's presence
    # anywhere in memory — evaluate() never sees it.
    discovery_policy = _load_permissions()["discovery"]
    decision, _ = _decide(discovery_policy, "source.raw_pii_read", "PII")
    assert decision == DECISION_DENY


@pytest.mark.parametrize("case", [c for c in CASES if c["family"] == "tool_poisoning"], ids=lambda c: c["id"])
def test_tool_poisoning_resolves_to_no_provider(case):
    """The fabricated tool name was never published to the registry —
    discover() can only ever find explicitly approved cards."""
    match = re.search(r"tool\s+['\"]?([\w_]+)['\"]?", case["payload_text"], re.IGNORECASE)
    assert match, "test corpus entry should name a fabricated tool"
    fabricated_capability = match.group(1)

    assert registry.discover(fabricated_capability) == []
    with pytest.raises(registry.NoApprovedProvider):
        registry.resolve_capability_handler(fabricated_capability)


@pytest.mark.parametrize("case", [c for c in CASES if c["family"] == "privilege_escalation_by_assertion"], ids=lambda c: c["id"])
def test_privilege_escalation_assertion_has_no_effect(case):
    """Permissions come only from policies/agent_permissions.yaml — an
    assertion embedded in estate content is never consulted."""
    cutover_policy = _load_permissions()["cutover"]
    decision, _ = _decide(cutover_policy, "approval.self_issue", "PRODUCTION")
    assert decision == DECISION_DENY  # cutover still cannot self-approve

    decision2, _ = _decide(cutover_policy, "cutover.execute.approved", "PRODUCTION")
    assert decision2 == DECISION_REQUIRE_APPROVAL  # still gated, assertion or not


@pytest.mark.parametrize("case", [c for c in CASES if c["family"] == "exfiltration_prompt"], ids=lambda c: c["id"])
def test_exfiltration_prompt_stays_inert_string_data(case):
    """The narrative-building function that could embed this text in a
    Gemini prompt only ever interpolates it as a string — it never
    parses out a URL and never performs any I/O itself."""
    payload = case["payload_text"]
    narrative = _deterministic_narrative(
        failed_checks=[{"check_type": "row_count"}],
        table_ref=f"Sales.Customers -- {payload}",  # payload riding along in a field this function does touch
        pipelines=[],
    )
    assert isinstance(narrative, str)
    assert "http" not in narrative or payload in narrative  # only present because we echoed it, never dereferenced


@pytest.mark.skipif(not FIRESTORE_OK, reason="Firestore not reachable")
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_containment_event_is_recorded(case):
    matches = scan_for_injection_patterns(case["payload_text"])
    event = record_containment_event(
        origin=case["payload_location"],
        content_snippet=case["payload_text"],
        matched_patterns=matches,
        outcome="CONTAINED",
        acting_agent="test-harness",
        policy_id=f"INJECTION-CORPUS-{case['id'].upper()}",
    )
    assert event["outcome"] == "CONTAINED"
    assert event["matched_patterns"] == matches
