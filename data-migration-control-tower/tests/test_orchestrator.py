"""Tests for agents/orchestrator/orchestrator.py — Day 10 Phase 2's
extended event chain (handle_risk_assessed, handle_planned,
handle_validation_requested, handle_validation_failed).

Full live end-to-end coverage of this chain already exists — every
evaluation/scenarios.py scenario that touches the shared migration
fixture (S-02, S-04, S-05, S-10, S-13, S-14) and durability_demo.py (via
S-11) exercise it through agents/orchestrator/pipeline_stages.py::
advance_to_passed(), which now delegates entirely to this module's
advance_through_validation(). This file adds focused, cheaper coverage
for the pieces those live runs don't isolate: the _open_incident()
lookup logic and the pull-or-timeout contract, using real Firestore
(skip automatically when unreachable, same pattern as the rest of this
suite) but monkeypatched registry/agent calls so these don't need a live
SQL Server + BigQuery round trip to run.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.orchestrator import orchestrator  # noqa: E402
from tests.conftest import seed_migration_plan  # noqa: E402


def _firestore_reachable() -> bool:
    # Delegates to the shared probe, which performs a real round trip.
    # This used to call `get_client()` and return True — but the Firestore
    # client is lazy and does no I/O when constructed, so it answered True
    # whenever the import worked, and the skipif below never skipped.
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


def test_pull_or_timeout_raises_when_nothing_arrives(monkeypatch):
    monkeypatch.setattr(orchestrator, "pull", lambda subscription, timeout: [])
    with pytest.raises(TimeoutError, match="migration.requested"):
        orchestrator._pull_or_timeout("some-sub", "migration.requested", poll_timeout=1.0)


def test_pull_or_timeout_returns_first_message(monkeypatch):
    monkeypatch.setattr(orchestrator, "pull", lambda subscription, timeout: [{"run_id": "r1"}, {"run_id": "r2"}])
    assert orchestrator._pull_or_timeout("some-sub", "some.topic", poll_timeout=1.0) == {"run_id": "r1"}


@skip_if_no_firestore
def test_migration_requested_uses_canonical_binding_and_estate_discovery(monkeypatch):
    from agents.orchestrator.run_lifecycle import delete_run, get_run

    invoked = []
    published = []

    def invoke(capability, *args, **kwargs):
        invoked.append((capability, args, kwargs))
        return ([], []), "discovery-agent", "1.1.0"

    monkeypatch.setattr(orchestrator.registry, "invoke_capability", invoke)
    monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: published.append((topic, payload)))
    result = orchestrator.handle_migration_requested({
        "pipeline_id": "wwi_sqlserver_v1",
        "estate_id": "estate-one",
        "source_id": "primary-sql",
        "pack_id": "wwi_sqlserver_v1",
        "drop_fraction": 0.0,
    })
    try:
        run = get_run(result["run_id"])
        assert run["pipeline_id"] == "wwi_sqlserver_v1"
        assert run["estate_id"] == "estate-one"
        assert run["source_id"] == "primary-sql"
        assert run["pack_id"] == "wwi_sqlserver_v1"
        # run_id is now threaded through to discovery (Deploy & Harden
        # Phase 1b) so its tool-level policy checks can be scoped/audited
        # against the run, not just the capability-dispatch gate.
        assert invoked == [(
            orchestrator.DISCOVERY_CAPABILITY,
            (),
            {"estate_id": "estate-one", "run_id": result["run_id"]},
        )]
        assert published == [("discovery.completed", {"run_id": result["run_id"]})]
    finally:
        delete_run(result["run_id"])


@skip_if_no_firestore
def test_open_incident_finds_pending_and_ignores_resolved():
    from tools.firestore_client import get_client

    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    client = get_client()
    incidents_ref = client.collection("migration_runs").document(run_id).collection("incidents")
    incidents_ref.document("resolved-one").set({"incident_id": "resolved-one", "outcome": "RESOLVED"})
    incidents_ref.document("pending-one").set({"incident_id": "pending-one", "outcome": "PENDING"})

    found = orchestrator._open_incident(run_id)
    assert found is not None
    assert found["incident_id"] == "pending-one"


@skip_if_no_firestore
def test_open_incident_returns_none_when_nothing_pending():
    from tools.firestore_client import get_client

    run_id = f"test_run_{uuid.uuid4().hex[:8]}"
    client = get_client()
    incidents_ref = client.collection("migration_runs").document(run_id).collection("incidents")
    incidents_ref.document("resolved-one").set({"incident_id": "resolved-one", "outcome": "RESOLVED"})

    assert orchestrator._open_incident(run_id) is None


@skip_if_no_firestore
def test_handle_validation_requested_publishes_passed_and_transitions(monkeypatch):
    from agents.orchestrator.run_lifecycle import create_run, delete_run, get_run, transition_state

    run_id = create_run("test.orchestrator.validation_requested")
    published = []
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING", "VALIDATING"):
            transition_state(run_id, state)

        monkeypatch.setattr(
            orchestrator.registry,
            "invoke_capability",
            lambda capability, *args: (
                {"run_id": run_id, "overall_status": "PASSED", "checks": [{"check_type": "row_count", "status": "PASS"}]},
                "validation-agent",
                "1.0.0",
            ),
        )
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: published.append((topic, payload)))

        result = orchestrator.handle_validation_requested({"run_id": run_id})

        assert result["overall_status"] == "PASSED"
        assert get_run(run_id)["state"] == "PASSED"
        assert ("validation.passed", {"run_id": run_id}) in published
        assert not any(topic == "validation.failed" for topic, _ in published)
    finally:
        delete_run(run_id)


@skip_if_no_firestore
def test_handle_validation_requested_publishes_failed_and_transitions(monkeypatch):
    from agents.orchestrator.run_lifecycle import create_run, delete_run, get_run, transition_state

    run_id = create_run("test.orchestrator.validation_requested_fail")
    published = []
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING", "VALIDATING"):
            transition_state(run_id, state)

        monkeypatch.setattr(
            orchestrator.registry,
            "invoke_capability",
            lambda capability, *args: (
                {"run_id": run_id, "overall_status": "FAILED", "checks": [{"check_type": "row_count", "status": "FAIL"}]},
                "validation-agent",
                "1.0.0",
            ),
        )
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: published.append((topic, payload)))

        result = orchestrator.handle_validation_requested({"run_id": run_id})

        assert result["overall_status"] == "FAILED"
        assert get_run(run_id)["state"] == "FAILED"
        assert ("validation.failed", {"run_id": run_id}) in published
    finally:
        delete_run(run_id)


@skip_if_no_firestore
def test_validation_passed_prepares_approval_idempotently(monkeypatch):
    from agents.orchestrator.run_lifecycle import create_run, delete_run, get_run, transition_state

    run_id = create_run("test.orchestrator.approval_preparation")
    message_id = f"test-{uuid.uuid4().hex}"
    calls = []
    try:
        for state in (
            "DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED",
            "MIGRATING", "VALIDATING", "PASSED",
        ):
            transition_state(run_id, state)

        def invoke(capability, requested_run_id):
            calls.append((capability, requested_run_id))
            return {"status": "PENDING"}, "cutover-agent", "1.0.0"

        monkeypatch.setattr(orchestrator.registry, "invoke_capability", invoke)
        payload = {"run_id": run_id, "_pubsub_message_id": message_id}
        first = orchestrator.handle_validation_passed(payload)
        second = orchestrator.handle_validation_passed(payload)

        assert calls == [(orchestrator.CUTOVER_CAPABILITY, run_id)]
        assert first["state"] == "READY_FOR_APPROVAL"
        assert second.get("deduped") is True
        assert get_run(run_id)["state"] == "READY_FOR_APPROVAL"
    finally:
        delete_run(run_id)
        get_client_for_test().collection("processed_messages").document(
            f"handle_validation_passed:{message_id}"
        ).delete()


@skip_if_no_firestore
def test_handle_validation_requested_closes_open_incident_on_pass(monkeypatch):
    """The re-check after a remediation must close the PENDING incident
    with the REAL outcome of this attempt, not assume success."""
    from tools.firestore_client import get_client

    from agents.orchestrator.run_lifecycle import create_run, delete_run, transition_state

    run_id = create_run("test.orchestrator.close_incident_on_pass")
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING", "VALIDATING", "FAILED"):
            transition_state(run_id, state)
        seed_migration_plan(run_id)

        client = get_client()
        incident = {"incident_id": "inc-1", "signature": "row_loss:Sales.Customers", "outcome": "PENDING"}
        client.collection("migration_runs").document(run_id).collection("incidents").document("inc-1").set(incident)
        transition_state(run_id, "INVESTIGATING")
        transition_state(run_id, "REMEDIATING")
        transition_state(run_id, "VALIDATING")

        monkeypatch.setattr(
            orchestrator.registry,
            "invoke_capability",
            lambda capability, *args: ({"overall_status": "PASSED", "checks": []}, "validation-agent", "1.0.0"),
        )
        closed = []
        monkeypatch.setattr(
            orchestrator.recovery,
            "close_incident",
            lambda rid, inc, resolved: closed.append((rid, inc["incident_id"], resolved)),
        )
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

        orchestrator.handle_validation_requested({"run_id": run_id})

        assert closed == [(run_id, "inc-1", True)]
    finally:
        delete_run(run_id)


@skip_if_no_firestore
def test_handle_validation_failed_drives_recovery_and_republishes(monkeypatch):
    from agents.orchestrator.run_lifecycle import create_run, delete_run, get_run, transition_state

    run_id = create_run("test.orchestrator.validation_failed")
    published = []
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING", "VALIDATING", "FAILED"):
            transition_state(run_id, state)
        seed_migration_plan(run_id)

        fake_incident = {
            "incident_id": "inc-2",
            "signature": "row_loss:Sales.Customers",
            "root_cause_generated_by": "deterministic",
        }
        monkeypatch.setattr(orchestrator.recovery, "investigate", lambda rid, table_ref=None: fake_incident)
        monkeypatch.setattr(orchestrator.recovery, "remediate", lambda rid, incident, **kwargs: {"target_count": 663})
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: published.append((topic, payload)))

        result = orchestrator.handle_validation_failed({"run_id": run_id})

        assert result["incident"] == fake_incident
        assert get_run(run_id)["state"] == "VALIDATING"
        assert ("validation.requested", {"run_id": run_id}) in published
    finally:
        delete_run(run_id)


# --- Idempotency across every handler (fix pass after the second audit) --


@skip_if_no_firestore
def test_handle_planned_is_idempotent_on_message_redelivery(monkeypatch):
    """The most important one: handle_planned() actually MOVES DATA
    (calls execute_migration). A redelivered plan.created message must
    NOT re-run a real migration — this test proves it by counting real
    execute_migration calls, not just checking state consistency."""
    from agents.orchestrator.run_lifecycle import create_run, delete_run, transition_state

    run_id = create_run("test.orchestrator.idempotent_planned", drop_fraction=0.0)
    message_id = f"test-{uuid.uuid4().hex}"
    payload = {"run_id": run_id, "_pubsub_message_id": message_id}
    execute_calls = []
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED"):
            transition_state(run_id, state)
        seed_migration_plan(run_id)

        def fake_execute_migration(**kwargs):
            execute_calls.append(kwargs)
            return {"target_count": 1, "source_count": 1}

        monkeypatch.setattr(orchestrator, "execute_migration", fake_execute_migration)
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

        first = orchestrator.handle_planned(payload)
        second = orchestrator.handle_planned(payload)  # simulates Pub/Sub redelivering the same message

        assert len(execute_calls) == 1, "execute_migration must only run once despite the redelivered message"
        assert not first.get("deduped")
        assert second.get("deduped") is True
        assert second["target_count"] == first["target_count"]
    finally:
        delete_run(run_id)
        get_client_for_test().collection("processed_messages").document(f"handle_planned:{message_id}").delete()


@skip_if_no_firestore
def test_handle_risk_assessed_is_idempotent_on_message_redelivery(monkeypatch):
    from agents.orchestrator.run_lifecycle import create_run, delete_run, transition_state

    run_id = create_run("test.orchestrator.idempotent_risk_assessed")
    message_id = f"test-{uuid.uuid4().hex}"
    payload = {"run_id": run_id, "_pubsub_message_id": message_id}
    invoke_calls = []
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED"):
            transition_state(run_id, state)

        fake_plan = {"plan_hash": "abc123", "steps": []}

        def fake_invoke_capability(capability, *args):
            invoke_calls.append(capability)
            return fake_plan, "planner-agent", "1.0.0"

        monkeypatch.setattr(orchestrator.registry, "invoke_capability", fake_invoke_capability)
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

        first = orchestrator.handle_risk_assessed(payload)
        second = orchestrator.handle_risk_assessed(payload)

        assert len(invoke_calls) == 1, "Planner must only be invoked once despite the redelivered message"
        assert not first.get("deduped")
        assert second.get("deduped") is True
    finally:
        delete_run(run_id)
        get_client_for_test().collection("processed_messages").document(f"handle_risk_assessed:{message_id}").delete()


@skip_if_no_firestore
def test_handle_validation_requested_is_idempotent_on_message_redelivery(monkeypatch):
    from agents.orchestrator.run_lifecycle import create_run, delete_run, get_run, transition_state

    run_id = create_run("test.orchestrator.idempotent_validation_requested")
    message_id = f"test-{uuid.uuid4().hex}"
    payload = {"run_id": run_id, "_pubsub_message_id": message_id}
    invoke_calls = []
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING", "VALIDATING"):
            transition_state(run_id, state)

        def fake_invoke_capability(capability, *args):
            invoke_calls.append(capability)
            return {"overall_status": "PASSED", "checks": []}, "validation-agent", "1.0.0"

        monkeypatch.setattr(orchestrator.registry, "invoke_capability", fake_invoke_capability)
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

        first = orchestrator.handle_validation_requested(payload)
        second = orchestrator.handle_validation_requested(payload)

        assert len(invoke_calls) == 1
        assert get_run(run_id)["state"] == "PASSED"  # only transitioned once, not raising an illegal-transition error
        assert not first.get("deduped")
        assert second.get("deduped") is True
    finally:
        delete_run(run_id)
        get_client_for_test().collection("processed_messages").document(
            f"handle_validation_requested:{message_id}"
        ).delete()


@skip_if_no_firestore
def test_handle_validation_failed_is_idempotent_on_message_redelivery(monkeypatch):
    from agents.orchestrator.run_lifecycle import create_run, delete_run, transition_state

    run_id = create_run("test.orchestrator.idempotent_validation_failed")
    message_id = f"test-{uuid.uuid4().hex}"
    payload = {"run_id": run_id, "_pubsub_message_id": message_id}
    investigate_calls = []
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING", "VALIDATING", "FAILED"):
            transition_state(run_id, state)
        seed_migration_plan(run_id)

        fake_incident = {
            "incident_id": "inc-idem",
            "signature": "row_loss:Sales.Customers",
            "root_cause_generated_by": "deterministic",
        }

        def fake_investigate(rid, table_ref=None):
            # table_ref is now derived inside recovery.investigate() from the
            # failing reconciliation records, so the orchestrator no longer
            # passes it — in a multi-target run only the results know which
            # table actually failed.
            investigate_calls.append(rid)
            return fake_incident

        monkeypatch.setattr(orchestrator.recovery, "investigate", fake_investigate)
        monkeypatch.setattr(orchestrator.recovery, "remediate", lambda rid, incident, **kwargs: {"target_count": 663})
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

        first = orchestrator.handle_validation_failed(payload)
        second = orchestrator.handle_validation_failed(payload)

        assert len(investigate_calls) == 1, "recovery.investigate() (and remediate()) must only run once"
        assert not first.get("deduped")
        assert second.get("deduped") is True
    finally:
        delete_run(run_id)
        get_client_for_test().collection("processed_messages").document(
            f"handle_validation_failed:{message_id}"
        ).delete()


def get_client_for_test():
    from tools.firestore_client import get_client

    return get_client()


# --- Assessment-mode enforcement at the execution boundary ---------------


@skip_if_no_firestore
def test_handle_planned_refuses_to_execute_for_an_assessment_mode_run():
    from agents.orchestrator.run_lifecycle import create_run, delete_run, transition_state

    run_id = create_run("test.orchestrator.assessment_guard", mode="assessment")
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED"):
            transition_state(run_id, state)
        seed_migration_plan(run_id)

        with pytest.raises(RuntimeError, match="assessment mode"):
            orchestrator.handle_planned({"run_id": run_id})
    finally:
        delete_run(run_id)


# --- Async data-plane executor flow (Deploy & Harden Phase 3, docs/adr/0003) ---


@skip_if_no_firestore
def test_handle_planned_async_branch_submits_and_stays_in_migrating(monkeypatch):
    """When _select_data_plane_executor() returns a remote executor,
    handle_planned() must submit every target and stop — NOT transition
    to VALIDATING or publish validation.requested. That's
    handle_migration_completed's job, once every submitted execution
    reports in."""
    from agents.orchestrator.run_lifecycle import create_run, delete_run, get_run, transition_state

    run_id = create_run("test.orchestrator.async_planned", drop_fraction=0.0)
    targets = [
        {
            "target_id": "t1", "source_schema": "public", "source_table": "orders",
            "target_table": "orders_dim", "key_column": "id", "order_by": "id",
            "scheduled": True, "blocked": False,
        },
        {
            "target_id": "t2", "source_schema": "public", "source_table": "customers",
            "target_table": "customers_dim", "key_column": "id", "order_by": "id",
            "scheduled": True, "blocked": False,
        },
    ]
    publish_calls = []
    execute_calls = []
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED"):
            transition_state(run_id, state)
        seed_migration_plan(run_id, targets=targets)

        sentinel_executor = object()
        monkeypatch.setattr(orchestrator, "_select_data_plane_executor", lambda: sentinel_executor)

        def fake_execute_migration(*, run_id, target, binding, drop_fraction, data_plane=None):
            assert data_plane is sentinel_executor  # threaded through correctly
            execute_calls.append(target["target_id"])
            return {"execution_id": f"exec-{target['target_id']}", "status": "PENDING", "run_id": run_id}

        monkeypatch.setattr(orchestrator, "execute_migration", fake_execute_migration)
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: publish_calls.append((topic, payload)))

        result = orchestrator.handle_planned({"run_id": run_id})

        assert result["async"] is True
        assert result["submitted_executions"] == 2
        assert len(execute_calls) == 2
        assert get_run(run_id)["state"] == "MIGRATING"  # NOT VALIDATING
        assert ("validation.requested", {"run_id": run_id}) not in publish_calls
        assert get_run(run_id).get("pending_remote_executions") == 2
    finally:
        delete_run(run_id)


@skip_if_no_firestore
def test_handle_migration_completed_waits_for_every_pending_execution(monkeypatch):
    """Two submitted executions -> the run must stay in MIGRATING after
    only the first migration.completed arrives, and only transition to
    VALIDATING once the second one does too."""
    from agents.orchestrator.run_lifecycle import create_run, delete_run, get_run, transition_state

    run_id = create_run("test.orchestrator.migration_completed", drop_fraction=0.0)
    publish_calls = []
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING"):
            transition_state(run_id, state)
        orchestrator._set_pending_executions(run_id, 2)

        client = get_client_for_test()
        executions_ref = client.collection("migration_runs").document(run_id).collection("migration_executions")
        for i, exec_id in enumerate(["exec-a", "exec-b"]):
            executions_ref.document(exec_id).set({
                "execution_id": exec_id, "status": "COMPLETED", "target_count": 10 + i,
                "source_count": 10 + i, "target_table": f"table_{i}", "started_at": f"2026-01-01T00:0{i}:00+00:00",
            })

        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: publish_calls.append((topic, payload)))

        first = orchestrator.handle_migration_completed(
            {"run_id": run_id, "execution_id": "exec-a", "status": "COMPLETED", "_pubsub_message_id": "msg-a"}
        )
        assert first["still_pending"] == 1
        assert get_run(run_id)["state"] == "MIGRATING"
        assert publish_calls == []

        second = orchestrator.handle_migration_completed(
            {"run_id": run_id, "execution_id": "exec-b", "status": "COMPLETED", "_pubsub_message_id": "msg-b"}
        )
        assert second["targets_migrated"] == 2
        assert second["target_count"] == 10 + 11  # aggregated, not the last execution's alone
        assert get_run(run_id)["state"] == "VALIDATING"
        assert ("validation.requested", {"run_id": run_id}) in publish_calls
    finally:
        delete_run(run_id)
        for exec_id in ("exec-a", "exec-b"):
            get_client_for_test().collection("migration_runs").document(run_id).collection(
                "migration_executions"
            ).document(exec_id).delete()
        for msg_id in ("msg-a", "msg-b"):
            get_client_for_test().collection("processed_messages").document(
                f"handle_migration_completed:{msg_id}"
            ).delete()


@skip_if_no_firestore
def test_handle_migration_completed_is_idempotent_on_message_redelivery(monkeypatch):
    from agents.orchestrator.run_lifecycle import create_run, delete_run, transition_state

    run_id = create_run("test.orchestrator.migration_completed_dedup", drop_fraction=0.0)
    message_id = f"test-{uuid.uuid4().hex}"
    payload = {"run_id": run_id, "execution_id": "exec-only", "status": "COMPLETED", "_pubsub_message_id": message_id}
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING"):
            transition_state(run_id, state)
        orchestrator._set_pending_executions(run_id, 1)
        client = get_client_for_test()
        client.collection("migration_runs").document(run_id).collection("migration_executions").document(
            "exec-only"
        ).set({"execution_id": "exec-only", "status": "COMPLETED", "target_count": 5, "source_count": 5, "started_at": "2026-01-01T00:00:00+00:00"})
        monkeypatch.setattr(orchestrator, "publish", lambda topic, payload: None)

        first = orchestrator.handle_migration_completed(payload)
        second = orchestrator.handle_migration_completed(payload)  # simulated redelivery

        assert not first.get("deduped")
        assert second.get("deduped") is True
        assert second["target_count"] == first["target_count"]
    finally:
        delete_run(run_id)
        get_client_for_test().collection("migration_runs").document(run_id).collection(
            "migration_executions"
        ).document("exec-only").delete()
        get_client_for_test().collection("processed_messages").document(
            f"handle_migration_completed:{message_id}"
        ).delete()


# --- Pub/Sub ack/nack semantics (real defect: pull() used to auto-ack) ---


def test_consume_acks_only_after_the_handler_succeeds(monkeypatch):
    """A message must not be acked until its handler has actually
    finished — this is what makes _dedup_claim's redelivery story real
    instead of aspirational."""
    calls = {"ack": [], "nack": []}
    monkeypatch.setattr(
        orchestrator, "pull", lambda subscription, timeout: [{"run_id": "r1", "_pubsub_ack_id": "ack-1"}]
    )
    monkeypatch.setattr(orchestrator, "ack", lambda sub, ack_id: calls["ack"].append((sub, ack_id)))
    monkeypatch.setattr(orchestrator, "nack", lambda sub, ack_id: calls["nack"].append((sub, ack_id)))

    result = orchestrator._consume("some-sub", "some.topic", lambda payload: {"ok": payload["run_id"]}, 1.0)

    assert result == {"ok": "r1"}
    assert calls["ack"] == [("some-sub", "ack-1")]
    assert calls["nack"] == []


def test_consume_nacks_and_reraises_when_the_handler_fails(monkeypatch):
    """Reproduces the exact defect: handle_planned() raising on a Wave
    Manager HOLD used to have the message already gone (auto-acked by
    pull() before the handler ever ran) — no redelivery, run stuck. Now
    a handler failure must nack (not ack) so Pub/Sub genuinely
    redelivers, and the exception must still propagate to the caller."""
    calls = {"ack": [], "nack": []}
    monkeypatch.setattr(
        orchestrator, "pull", lambda subscription, timeout: [{"run_id": "r1", "_pubsub_ack_id": "ack-1"}]
    )
    monkeypatch.setattr(orchestrator, "ack", lambda sub, ack_id: calls["ack"].append((sub, ack_id)))
    monkeypatch.setattr(orchestrator, "nack", lambda sub, ack_id: calls["nack"].append((sub, ack_id)))

    def _failing_handler(payload):
        raise RuntimeError("simulated Wave Manager HOLD exhaustion")

    with pytest.raises(RuntimeError, match="simulated Wave Manager HOLD"):
        orchestrator._consume("some-sub", "some.topic", _failing_handler, 1.0)

    assert calls["nack"] == [("some-sub", "ack-1")]
    assert calls["ack"] == []


# --- Atomic dedup claim (real defect: check-then-act-then-mark was not atomic) ---


@skip_if_no_firestore
def test_dedup_claim_claims_a_brand_new_message():
    message_id = f"test-claim-{uuid.uuid4().hex}"
    payload = {"_pubsub_message_id": message_id}
    try:
        status, cached = orchestrator._dedup_claim(payload, "test_handler")
        assert status == "claimed"
        assert cached is None
    finally:
        get_client_for_test().collection("processed_messages").document(f"test_handler:{message_id}").delete()


@skip_if_no_firestore
def test_dedup_claim_returns_done_after_dedup_complete():
    message_id = f"test-claim-{uuid.uuid4().hex}"
    payload = {"_pubsub_message_id": message_id}
    try:
        orchestrator._dedup_claim(payload, "test_handler")
        orchestrator._dedup_complete(payload, "test_handler", {"answer": 42})

        status, cached = orchestrator._dedup_claim(payload, "test_handler")
        assert status == "done"
        assert cached == {"answer": 42, "deduped": True}
    finally:
        get_client_for_test().collection("processed_messages").document(f"test_handler:{message_id}").delete()


@skip_if_no_firestore
def test_dedup_claim_detects_a_stale_claim_from_a_simulated_crash():
    """Reproduces the exact defect: a claim written, then nothing ever
    calling _dedup_complete (simulating a process crash between a side
    effect and the final mark). A second claim attempt for the SAME
    message must not be treated as brand new — it must come back
    'stale_claim' so the caller knows to redo the work deliberately,
    not silently skip it or silently treat it as already-done."""
    message_id = f"test-claim-{uuid.uuid4().hex}"
    payload = {"_pubsub_message_id": message_id}
    try:
        first_status, _ = orchestrator._dedup_claim(payload, "test_handler")
        assert first_status == "claimed"

        second_status, second_cached = orchestrator._dedup_claim(payload, "test_handler")
        assert second_status == "stale_claim"
        assert second_cached is None
    finally:
        get_client_for_test().collection("processed_messages").document(f"test_handler:{message_id}").delete()


def test_dedup_claim_without_a_message_id_always_proceeds():
    """A direct/test call with no _pubsub_message_id (bypassing real
    Pub/Sub) must not be blocked by dedup — there's nothing to dedup
    against."""
    status, cached = orchestrator._dedup_claim({}, "test_handler")
    assert status == "claimed"
    assert cached is None
