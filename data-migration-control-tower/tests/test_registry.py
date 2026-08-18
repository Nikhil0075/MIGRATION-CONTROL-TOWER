"""Tests for tools/registry.py.

All of these need live Firestore (the registry is persisted there), so
they skip automatically when it isn't reachable — same pattern as the
other live-service tests in this suite.

Every card a test publishes is hard-deleted in teardown
(tools/registry.py's delete_card, test/dev-only) via the `registered`
fixture below. A card left behind isn't just test litter — a real
capability query like the Finance-agent wildcard lookup
('impact.assessment.*') can accidentally resolve to it (this happened
once during Day 6 development: a leftover test card intercepted the real
orchestrator's finance-impact dispatch).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools import registry  # noqa: E402


def _firestore_reachable() -> bool:
    try:
        from tools.firestore_client import get_client

        get_client()
        return True
    except Exception:  # noqa: BLE001
        return False


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


@pytest.fixture()
def registered():
    """Yields a `publish(card, published_by)` helper; deletes every card
    it published (any version) once the test finishes, pass or fail."""
    created: list[tuple[str, str]] = []

    def _publish(card: dict, published_by: str) -> dict:
        result = registry.publish(card, published_by=published_by)
        created.append((card["agent_id"], card["version"]))
        return result

    yield _publish

    for agent_id, version in created:
        registry.delete_card(agent_id, version)


def _make_card(agent_id: str, capability: str, handler: str = "os:getcwd") -> dict:
    return {
        "agent_id": agent_id,
        "display_name": f"Test agent {agent_id}",
        "version": "1.0.0",
        "owner": {"team": "QA", "department": "Testing"},
        "capabilities": [capability],
        "handler": handler,
        "runtime": {"type": "local"},
        "permissions": {},
    }


@skip_if_no_firestore
def test_publish_sets_draft_status(registered):
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    card = registered(_make_card(agent_id, "test.cap"), published_by="pub@example.internal")
    assert card["status"] == "DRAFT"


@skip_if_no_firestore
def test_self_approval_is_denied(registered):
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    registered(_make_card(agent_id, "test.cap"), published_by="pub@example.internal")
    with pytest.raises(PermissionError):
        registry.approve(agent_id, "1.0.0", approved_by="pub@example.internal")


@skip_if_no_firestore
def test_approval_by_distinct_identity_succeeds(registered):
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    registered(_make_card(agent_id, "test.cap"), published_by="pub@example.internal")
    approved = registry.approve(agent_id, "1.0.0", approved_by="governance@example.internal")
    assert approved["status"] == "APPROVED"
    assert approved["approved_by"] == "governance@example.internal"


@skip_if_no_firestore
def test_discover_only_returns_approved_cards(registered):
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    capability = f"test.discover.{uuid.uuid4().hex[:8]}"
    registered(_make_card(agent_id, capability), published_by="pub@example.internal")

    assert registry.discover(capability) == []  # still DRAFT

    registry.approve(agent_id, "1.0.0", approved_by="governance@example.internal")
    found = registry.discover(capability)
    assert len(found) == 1
    assert found[0]["agent_id"] == agent_id


@skip_if_no_firestore
def test_discover_wildcard_matches_prefix(registered):
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    # Deliberately NOT under the real 'impact.assessment.*' namespace —
    # a leftover test card there could shadow the real Finance agent.
    capability = f"test.wildcard.{uuid.uuid4().hex[:8]}.check"
    prefix = capability.rsplit(".", 1)[0]
    registered(_make_card(agent_id, capability), published_by="pub@example.internal")
    registry.approve(agent_id, "1.0.0", approved_by="governance@example.internal")

    found = registry.discover(f"{prefix}.*")
    assert any(c["agent_id"] == agent_id for c in found)


@skip_if_no_firestore
def test_deprecate_removes_from_discovery(registered):
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    capability = f"test.deprecate.{uuid.uuid4().hex[:8]}"
    registered(_make_card(agent_id, capability), published_by="pub@example.internal")
    registry.approve(agent_id, "1.0.0", approved_by="governance@example.internal")
    assert len(registry.discover(capability)) == 1

    registry.deprecate(agent_id, "1.0.0", deprecated_by="governance@example.internal")
    assert registry.discover(capability) == []


@skip_if_no_firestore
def test_resolve_capability_handler_raises_when_nothing_matches():
    with pytest.raises(registry.NoApprovedProvider):
        registry.resolve_capability_handler(f"no.such.capability.{uuid.uuid4().hex}")


@skip_if_no_firestore
def test_invoke_capability_dynamically_calls_the_handler(registered):
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    capability = f"test.invoke.{uuid.uuid4().hex[:8]}"
    registered(
        _make_card(agent_id, capability, handler="os.path:isdir"),
        published_by="pub@example.internal",
    )
    registry.approve(agent_id, "1.0.0", approved_by="governance@example.internal")

    result, resolved_agent_id, resolved_version = registry.invoke_capability(capability, ".")
    assert result is True  # os.path.isdir(".") — the repo root, which exists
    assert resolved_agent_id == agent_id
    assert resolved_version == "1.0.0"


@skip_if_no_firestore
def test_get_history_includes_publish_and_approve_events(registered):
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    registered(_make_card(agent_id, "test.cap"), published_by="pub@example.internal")
    registry.approve(agent_id, "1.0.0", approved_by="governance@example.internal")

    history = registry.get_history(agent_id)
    assert len(history) == 1
    assert history[0]["published_by"] == "pub@example.internal"
    assert history[0]["approved_by"] == "governance@example.internal"
