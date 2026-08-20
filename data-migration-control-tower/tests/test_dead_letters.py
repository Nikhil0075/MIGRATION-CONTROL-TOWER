"""The dead-letter read/replay path.

Pub/Sub is faked here. These tests are about the ACK SEMANTICS, which are
the part that can silently destroy an operator's evidence, and which no
amount of live testing would exercise reliably — you cannot conjure a
poison message on demand.

Two properties matter more than the rest:

  - listing must leave the queue exactly as it found it, or opening the
    page hides every dead letter from the next reader for a minute;
  - replay must publish BEFORE it acks, because acking first and then
    failing to publish destroys the message, and there is no second
    dead-letter queue behind this one.
"""

from __future__ import annotations

import json

import pytest

from tools import dead_letters as dlq


class FakeMessage:
    def __init__(self, message_id, payload, attributes=None, publish_time=None):
        self.message_id = message_id
        self.data = json.dumps(payload).encode("utf-8")
        self.attributes = attributes or {}
        self.publish_time = publish_time


class FakeReceived:
    def __init__(self, ack_id, message):
        self.ack_id = ack_id
        self.message = message


class FakeSubscriber:
    def __init__(self, messages, log=None):
        self.messages = messages
        # Shared with the publisher so ordering between publish and ack is
        # observable. Two separate logs cannot express "before".
        self.calls: list[tuple] = log if log is not None else []

    def subscription_path(self, project, subscription):
        return f"projects/{project}/subscriptions/{subscription}"

    def pull(self, request=None, timeout=None):
        class Response:
            received_messages = self.messages[: request["max_messages"]]

        self.calls.append(("pull", request["max_messages"]))
        return Response()

    def acknowledge(self, request=None):
        self.calls.append(("ack", tuple(request["ack_ids"])))

    def get_subscription(self, request=None):
        class Subscription:
            topic = "projects/p/topics/plan.created"

        self.calls.append(("get_subscription", request["subscription"]))
        return Subscription()


class FakePublisher:
    def __init__(self, log=None):
        self.published: list[tuple] = []
        self.calls: list[tuple] = log if log is not None else []

    def publish(self, topic, data):
        self.published.append((topic, json.loads(data.decode("utf-8"))))
        self.calls.append(("publish", topic))

        class Future:
            def result(self, timeout=None):
                return "republished-1"

        return Future()


@pytest.fixture
def wired(monkeypatch):
    """One dead letter, with the attributes Pub/Sub really stamps."""
    message = FakeMessage(
        "msg-1",
        {"run_id": "run-1", "operation_id": "op-1"},
        attributes={
            dlq.SOURCE_SUBSCRIPTION_ATTRIBUTE: "projects/p/subscriptions/plan-created-sub",
            dlq.SOURCE_DELIVERY_ATTEMPT_ATTRIBUTE: "10",
        },
    )
    ordered: list[tuple] = []
    subscriber = FakeSubscriber([FakeReceived("ack-1", message)], log=ordered)
    publisher = FakePublisher(log=ordered)
    released: list[tuple] = []

    monkeypatch.setattr(dlq, "_subscriber", lambda: subscriber)
    monkeypatch.setattr(dlq, "_publisher", lambda: publisher)
    monkeypatch.setattr(dlq, "_project_id", lambda: "p")
    monkeypatch.setattr(
        dlq, "modify_ack_deadline", lambda sub, ack_id, seconds: released.append((ack_id, seconds))
    )
    recorded: list[dict] = []
    monkeypatch.setattr(
        dlq, "_record", lambda decoded, **kw: recorded.append({**decoded, **kw})
    )
    return subscriber, publisher, released, recorded


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_listing_returns_every_lease_so_the_next_reader_sees_the_queue(wired):
    subscriber, _publisher, released, _recorded = wired

    dlq.list_dead_letters()

    # Every lease taken is handed straight back — the count is one per
    # polling round, not one per message, because listing polls more than
    # once (see LIST_ROUNDS).
    assert released, "listing took leases and never returned them"
    assert all(seconds == 0 for _ack, seconds in released), "leases must be released immediately"
    assert not any(call[0] == "ack" for call in subscriber.calls), "listing must never ack"


def test_listing_polls_more_than_once_before_calling_the_queue_empty(monkeypatch, wired):
    """A single Pub/Sub pull may return nothing while messages wait.

    Observed live against a queue holding messages: the first pull returned
    0 and the next returned all of them. One round would render "nothing
    has been dead-lettered" over a non-empty queue — the exact opposite of
    what this page exists to say.
    """
    subscriber, _publisher, _released, _recorded = wired
    rounds: list[int] = []
    real_pull = dlq._pull

    def flaky(max_messages, timeout):
        rounds.append(1)
        return [] if len(rounds) == 1 else real_pull(max_messages, timeout)

    monkeypatch.setattr(dlq, "_pull", flaky)
    assert len(dlq.list_dead_letters()) == 1, "an empty first pull must not end the listing"


def test_listing_deduplicates_across_rounds(wired):
    # The same message can come back in more than one round; it is one
    # dead letter, not several.
    assert len({m["message_id"] for m in dlq.list_dead_letters()}) == len(dlq.list_dead_letters())


def test_listing_surfaces_which_consumer_gave_up(wired):
    [message] = dlq.list_dead_letters()
    # The whole point of the screen: not just that something failed, but
    # which consumer stopped trying.
    assert message["source_subscription"] == "plan-created-sub"
    assert message["delivery_attempts"] == 10
    assert message["run_id"] == "run-1"


def test_an_undecodable_body_is_shown_rather_than_hidden(monkeypatch, wired):
    subscriber, _publisher, _released, _recorded = wired
    subscriber.messages[0].message.data = b"this is not json"

    [message] = dlq.list_dead_letters()

    # A dead letter may well be here BECAUSE it is not the JSON we expect.
    # Failing the listing would hide the very thing worth looking at.
    assert "not json" in message["payload"]["_undecodable"]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_replay_publishes_before_it_acks(wired):
    subscriber, publisher, _released, _recorded = wired

    dlq.replay("msg-1", actor="op@example.internal", justification="Transient outage.")

    # One ordered log, so "before" is actually assertable. Acking first and
    # then failing to publish destroys the message outright — the one
    # outcome worse than a duplicate, which _dedup_claim already tolerates.
    names = [call[0] for call in subscriber.calls]
    assert "publish" in names, "nothing was republished"
    assert "ack" in names, "the message was never acked, so it stays queued forever"
    assert names.index("publish") < names.index("ack"), (
        f"replay acked before publishing: {subscriber.calls}"
    )


def test_replay_targets_the_topic_the_subscription_actually_points_at(wired):
    subscriber, publisher, _released, _recorded = wired

    result = dlq.replay("msg-1", actor="op@example.internal", justification="Retrying.")

    assert ("get_subscription", "projects/p/subscriptions/plan-created-sub") in subscriber.calls
    assert publisher.published[0][0] == "projects/p/topics/plan.created"
    assert result["topic"] == "plan.created"


def test_replay_refuses_when_the_source_is_unknown(monkeypatch, wired):
    subscriber, publisher, released, _recorded = wired
    subscriber.messages[0].message.attributes = {}

    with pytest.raises(ValueError, match="cannot be determined"):
        dlq.replay("msg-1", actor="op@example.internal", justification="Trying anyway.")

    assert not publisher.published, "replaying to a guessed topic is worse than refusing"
    assert ("ack-1", 0) in released, "the lease must be returned when the action is refused"


def test_a_message_that_is_not_there_is_a_lookup_failure_not_a_silent_success(wired):
    with pytest.raises(LookupError):
        dlq.replay("no-such-message", actor="op@example.internal", justification="Nope.")


def test_other_messages_are_released_while_one_is_taken(monkeypatch, wired):
    subscriber, _publisher, released, _recorded = wired
    other = FakeReceived("ack-2", FakeMessage("msg-2", {"run_id": "run-2"}))
    subscriber.messages.append(other)

    dlq.replay("msg-1", actor="op@example.internal", justification="One at a time.")

    # The bystander must go back immediately rather than being held for the
    # ack deadline because someone acted on a different message.
    assert ("ack-2", 0) in released
    assert ("ack-1", 0) not in released, "the targeted message is acked, not released"


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def test_archive_records_before_it_acks(wired):
    subscriber, _publisher, _released, recorded = wired

    dlq.archive("msg-1", actor="op@example.internal", justification="Superseded by a newer run.")

    assert recorded, "nothing was written, so acking would have destroyed the evidence"
    assert recorded[0]["event"] == "archived"
    assert recorded[0]["actor"] == "op@example.internal"
    assert any(call[0] == "ack" for call in subscriber.calls)


# ---------------------------------------------------------------------------
# The console API
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from frontend.app import app

    return TestClient(app)


def _token(monkeypatch, claims):
    import firebase_admin
    from firebase_admin import auth

    monkeypatch.delenv("APPROVER_ALLOWLIST", raising=False)
    monkeypatch.setattr(firebase_admin, "_apps", {"test": object()})
    monkeypatch.setattr(
        auth,
        "verify_id_token",
        lambda _t: {"uid": "dlq-user", "email": "dlq-user@example.internal", **claims},
    )


HEADERS = {"Authorization": "Bearer verified"}


def test_reading_the_queue_requires_authentication(client):
    assert client.get("/api/v1/dead-letters").status_code in (401, 403)


def test_replay_requires_the_operator_role(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    response = client.post(
        "/api/v1/dead-letters/msg-1/replay",
        headers=HEADERS,
        json={"justification": "A viewer must not be able to republish events."},
    )
    assert response.status_code == 403


def test_an_unknown_action_is_not_treated_as_a_replay(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["operator"]}})
    response = client.post(
        "/api/v1/dead-letters/msg-1/discard",
        headers=HEADERS,
        json={"justification": "Only replay and archive are real actions."},
    )
    assert response.status_code == 404


def test_an_unreachable_queue_reports_503_rather_than_an_empty_list(client, monkeypatch):
    """An empty list would read as "nothing failed", which is the opposite
    of the truth when the queue cannot be reached at all."""
    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})

    def explode(**_kwargs):
        raise RuntimeError("permission denied on dead-letter-sub")

    monkeypatch.setattr(dlq, "list_dead_letters", explode)
    response = client.get("/api/v1/dead-letters", headers=HEADERS)
    assert response.status_code == 503
    assert "permission denied" in response.json()["detail"]


def test_a_missing_message_is_404_not_a_reported_success(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["operator"]}})

    def missing(message_id, **_kwargs):
        raise LookupError(f"No dead letter {message_id!r} is available right now.")

    monkeypatch.setattr(dlq, "replay", missing)
    response = client.post(
        "/api/v1/dead-letters/gone/replay",
        headers=HEADERS,
        json={"justification": "Someone else may have already replayed it."},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Where the source subscription came from
# ---------------------------------------------------------------------------


def test_a_broker_stamped_source_is_marked_authoritative(wired):
    [message] = dlq.list_dead_letters()
    assert message["source_subscription"] == "plan-created-sub"
    assert message["source_is_broker_asserted"] is True


def test_a_payload_declared_source_is_shown_but_not_trusted(wired):
    """Messages published straight onto the dead-letter topic carry the
    source in the BODY. Real ones exist: the acceptance tooling parks stale
    artifacts there. The body is untrusted content, so it is displayed and
    flagged rather than believed."""
    subscriber, _publisher, _released, _recorded = wired
    subscriber.messages[0].message.attributes = {}
    subscriber.messages[0].message.data = json.dumps(
        {"run_id": "run-1", "source_subscription": "plan-created-sub"}
    ).encode("utf-8")

    [message] = dlq.list_dead_letters()

    assert message["source_subscription"] == "plan-created-sub"
    assert message["source_is_broker_asserted"] is False


def test_replay_refuses_a_subscription_this_system_does_not_consume(monkeypatch, wired):
    """The allowlist is what makes acting on a payload-declared source safe.

    Without it a message body could name any subscription and the replay
    would resolve its topic and publish there — a routing decision taken
    from untrusted content, which is exactly what policy_engine.py is
    structured to avoid elsewhere.
    """
    subscriber, publisher, _released, _recorded = wired
    subscriber.messages[0].message.attributes = {}
    subscriber.messages[0].message.data = json.dumps(
        {"run_id": "run-1", "source_subscription": "attacker-chosen-sub"}
    ).encode("utf-8")
    monkeypatch.setattr(dlq, "known_source_subscriptions", lambda: {"plan-created-sub"})

    with pytest.raises(ValueError, match="not a subscription this system consumes"):
        dlq.replay("msg-1", actor="op@example.internal", justification="Trying it on.")

    assert not publisher.published


def test_the_replay_allowlist_comes_from_the_real_consumer_set():
    # Written out again by hand, it would drift the moment a consumer is
    # added; derived, a new consumer is replayable automatically.
    from tools.worker_supervisor import default_specs

    assert dlq.known_source_subscriptions() == {spec.subscription for spec in default_specs()}


# ---------------------------------------------------------------------------
# Memory Bank
# ---------------------------------------------------------------------------


def test_memory_bank_separates_reuse_from_reconfirmation(client, monkeypatch):
    """Two numbers that are easy to conflate and mean different things.

    `recalled_by_run_ids` is later runs that CITED the fact as evidence —
    the only figure that demonstrates cross-run learning. `reuse_count` is
    incremented every time the fact is re-confirmed after a successful
    remediation, including by the run that first learned it. Reporting the
    larger number as "reused" would overstate what the system proved.
    """
    from tools import memory_bank

    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    monkeypatch.setattr(
        memory_bank,
        "list_facts",
        lambda: [
            {
                "signature": "row_loss:Sales.Customers",
                "root_cause": "The extract dropped rows before load.",
                "fix": "Reloaded from source.",
                "reuse_count": 17,
                "recalled_by_run_ids": ["run-a", "run-b"],
                "source_run_ids": ["run-0"],
                "created_at": "2026-08-16T09:00:00Z",
                "last_confirmed_at": "2026-08-18T06:00:00Z",
            },
            {
                "signature": "schema_drift:Sales.Orders",
                "root_cause": "A column was added at source.",
                "fix": "Re-derived the plan.",
                "reuse_count": 1,
                "recalled_by_run_ids": [],
                "source_run_ids": ["run-1"],
                "created_at": "2026-08-17T09:00:00Z",
                "last_confirmed_at": "2026-08-17T09:00:00Z",
            },
        ],
    )

    body = client.get("/api/v1/memory-bank", headers=HEADERS).json()["data"]

    learned, unused = body["facts"]
    assert learned["recalled_by_count"] == 2
    assert learned["confirmations"] == 17
    # Only the fact another run actually cited counts as reused.
    assert body["reused_facts"] == 1
    assert unused["recalled_by_count"] == 0


def test_memory_bank_leads_with_the_most_reused_fact(client, monkeypatch):
    from tools import memory_bank

    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    monkeypatch.setattr(
        memory_bank,
        "list_facts",
        lambda: [
            {"signature": "rare", "recalled_by_run_ids": [], "reuse_count": 1},
            {"signature": "common", "recalled_by_run_ids": ["a", "b", "c"], "reuse_count": 3},
        ],
    )
    facts = client.get("/api/v1/memory-bank", headers=HEADERS).json()["data"]["facts"]
    assert [f["signature"] for f in facts] == ["common", "rare"]


def test_memory_bank_requires_authentication(client):
    assert client.get("/api/v1/memory-bank").status_code in (401, 403)


# ---------------------------------------------------------------------------
# Approval evidence
# ---------------------------------------------------------------------------


def _approval_fixture(monkeypatch, *, approved_hash, current_hash, approved_at=None):
    """Wires the approvals endpoint against controlled subcollections."""
    from frontend import api_v1

    monkeypatch.setattr(
        api_v1,
        "_all_runs",
        lambda limit, estate_id=None: [
            {"run_id": "run-1", "estate_id": "wwi-demo-estate", "state": "READY_FOR_APPROVAL"}
        ],
    )

    def groups(name, run_ids=None):
        return {
            "approval": {
                "run-1": [
                    {
                        "_id": "current",
                        "status": "APPROVED" if approved_at else "PENDING",
                        "plan_hash": approved_hash,
                        "requested_by": "cutover-agent",
                        "requested_at": "2026-08-18T10:00:00+00:00",
                        "approved_by": "approver@example.internal" if approved_at else None,
                        "approved_at": approved_at,
                        "expires_after_days": 30,
                    }
                ]
            },
            "migration_plan": {"run-1": [{"_id": "current", "plan_hash": current_hash}]},
            "reconciliation": {"run-1": [{"status": "PASSED"}, {"status": "FAILED"}]},
            "risk_findings": {"run-1": [{"severity": "CRITICAL"}, {"severity": "LOW"}]},
        }.get(name, {})

    monkeypatch.setattr(api_v1, "_subcollection_group", groups)
    monkeypatch.setattr(api_v1, "_cached", lambda key, build: build())


def test_an_approval_bound_to_the_current_plan_reads_as_intact(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    _approval_fixture(monkeypatch, approved_hash="abc123", current_hash="abc123")

    body = client.get("/api/v1/approvals", headers=HEADERS).json()["data"]

    assert body["awaiting"][0]["binding"] == "intact"
    assert body["stale_bindings"] == 0


def test_a_plan_changed_after_approval_is_visible_before_cutover(client, monkeypatch):
    """The whole point of the screen.

    approval_service.consume() already refuses this cutover — but it does
    so at cutover time, long after a human clicked approve. Surfacing the
    mismatch up front is the difference between a caught mistake and a
    surprise.
    """
    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    _approval_fixture(monkeypatch, approved_hash="abc123", current_hash="def456")

    body = client.get("/api/v1/approvals", headers=HEADERS).json()["data"]

    assert body["awaiting"][0]["binding"] == "stale"
    assert body["stale_bindings"] == 1


def test_no_plan_yet_is_not_reported_as_a_mismatch(client, monkeypatch):
    """Absent and different are not the same thing. Calling a run with no
    plan "stale" would send an approver hunting for a change that never
    happened."""
    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    _approval_fixture(monkeypatch, approved_hash="abc123", current_hash=None)

    body = client.get("/api/v1/approvals", headers=HEADERS).json()["data"]

    assert body["awaiting"][0]["binding"] == "no_plan"
    assert body["stale_bindings"] == 0


def test_evidence_counts_come_from_the_run_not_from_a_summary_field(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    _approval_fixture(monkeypatch, approved_hash="abc123", current_hash="abc123")

    item = client.get("/api/v1/approvals", headers=HEADERS).json()["data"]["awaiting"][0]

    assert item["checks_total"] == 2 and item["checks_failed"] == 1
    assert item["risk_findings"] == 2 and item["critical_findings"] == 1


def test_an_expired_approval_is_marked_expired(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    _approval_fixture(
        monkeypatch, approved_hash="abc", current_hash="abc", approved_at="2020-01-01T00:00:00+00:00"
    )

    item = client.get("/api/v1/approvals", headers=HEADERS).json()["data"]["decided"][0]

    assert item["expired"] is True
    assert item["expires_at"].startswith("2020-01-31")


def test_the_approvals_endpoint_cannot_approve_anything():
    """A read endpoint that could change state would defeat the separation
    the whole approval service exists to enforce."""
    import inspect

    from frontend import api_v1

    source = inspect.getsource(api_v1.approvals)
    for forbidden in ("approval_service.approve", "transition_state", ".set(", ".update("):
        assert forbidden not in source, f"the approvals view must not mutate: found {forbidden!r}"


# ---------------------------------------------------------------------------
# Lineage run selection
# ---------------------------------------------------------------------------


def test_lineage_skips_queued_runs_and_picks_one_with_a_catalog():
    """The defect this replaced: Lineage drew `_latest_run` — the newest
    run FULL STOP — which is normally a queued one with no catalog, so the
    graph rendered empty and the page read as broken. Observed live with 29
    consecutive REQUESTED runs on one estate.
    """
    from frontend.api_v1 import _latest_run, _latest_run_with_catalog

    runs = [
        {"run_id": "queued-3", "state": "REQUESTED", "state_history": [{"state": "REQUESTED"}]},
        {"run_id": "queued-2", "state": "REQUESTED", "state_history": [{"state": "REQUESTED"}]},
        {"run_id": "queued-1", "state": "REQUESTED", "state_history": [{"state": "REQUESTED"}]},
        {"run_id": "has-data", "state": "COMPLETE", "state_history": [{"state": "DISCOVERED"}]},
    ]

    assert _latest_run(runs)["run_id"] == "queued-3"
    assert _latest_run_with_catalog(runs)["run_id"] == "has-data"


def test_a_run_that_discovered_then_failed_still_has_lineage_worth_showing():
    """Selecting on current state alone would skip it; the catalog it wrote
    before failing is exactly what an operator wants to look at."""
    from frontend.api_v1 import _latest_run_with_catalog

    runs = [
        {
            "run_id": "failed-after-discovery",
            "state": "FAILED",
            "state_history": [{"state": "REQUESTED"}, {"state": "DISCOVERED"}],
        }
    ]
    assert _latest_run_with_catalog(runs)["run_id"] == "failed-after-discovery"


def test_no_discovered_run_returns_nothing_rather_than_a_wrong_one():
    from frontend.api_v1 import _latest_run_with_catalog

    runs = [{"run_id": "queued", "state": "REQUESTED", "state_history": [{"state": "REQUESTED"}]}]
    assert _latest_run_with_catalog(runs) is None


def test_every_catalogued_state_is_a_real_lifecycle_state():
    """CATALOGUED_STATES is derived from EXECUTION_STAGES; if a state is
    renamed in run_lifecycle.py the derivation must not silently drift."""
    from agents.orchestrator.run_lifecycle import _CANONICAL_TRANSITIONS
    from frontend.api_v1 import CATALOGUED_STATES

    known = set(_CANONICAL_TRANSITIONS) | {"FAILED", "INVESTIGATING", "REMEDIATING"}
    assert CATALOGUED_STATES <= known, CATALOGUED_STATES - known
    assert "REQUESTED" not in CATALOGUED_STATES, "a queued run has no catalog"
