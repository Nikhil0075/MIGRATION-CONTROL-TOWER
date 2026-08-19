"""In-process event consumers (Day 11 Phase 10).

No live infrastructure: the Pub/Sub layer is monkeypatched, so these run
anywhere and cannot consume a real in-flight message.

The test that matters most is
test_the_lease_is_released_before_ack_never_after. A modify_ack_deadline
landing after a nack would restore the lease and cancel the redelivery the
nack exists to cause; landing after an ack it targets a message that no
longer exists. Ordering is the whole mitigation, so it is asserted against
a single ordered call log rather than per call.
"""

from __future__ import annotations

import threading
import time

import pytest

from tools import worker_supervisor as ws
from tools.worker_supervisor import ConsumerSpec, WorkerSupervisor


class FakeLock:
    """Stands in for InstanceLock without touching Firestore."""

    def __init__(self, held: bool = True):
        self.owner_id = "fake-owner"
        self.ttl_seconds = 90
        self.held = held
        self.released = False

    def try_acquire(self) -> bool:
        return self.held

    def renew(self) -> bool:
        return self.held

    def release(self) -> None:
        self.released = True
        self.held = False

    def holder(self):
        return {"owner_id": self.owner_id, "hostname": "fake", "pid": 1, "is_self": self.held}


@pytest.fixture
def pubsub(monkeypatch):
    """Records every Pub/Sub call in one ordered log."""

    class Recorder:
        def __init__(self):
            self.calls: list[tuple] = []
            self.queue: list[dict] = []
            self.lock = threading.Lock()

        def pull(self, subscription, max_messages=1, timeout=5.0):
            with self.lock:
                if not self.queue:
                    return []
                return [self.queue.pop(0)]

        def ack(self, subscription, ack_id):
            with self.lock:
                self.calls.append(("ack", ack_id))

        def nack(self, subscription, ack_id):
            with self.lock:
                self.calls.append(("nack", ack_id))

        def modify(self, subscription, ack_id, seconds):
            with self.lock:
                self.calls.append(("extend", ack_id, seconds))

        def names(self):
            with self.lock:
                return [call[0] for call in self.calls]

    recorder = Recorder()
    monkeypatch.setattr(ws, "pull", recorder.pull)
    monkeypatch.setattr(ws, "ack", recorder.ack)
    monkeypatch.setattr(ws, "nack", recorder.nack)
    monkeypatch.setattr(ws, "modify_ack_deadline", recorder.modify)
    monkeypatch.setattr(ws, "warm_clients", lambda: None)
    return recorder


def _message(n: int = 1) -> dict:
    return {"run_id": f"run-{n}", "_pubsub_ack_id": f"ack-{n}", "_pubsub_message_id": f"msg-{n}"}


def _supervisor(handler, *, held=True, **kwargs) -> WorkerSupervisor:
    return WorkerSupervisor(
        [ConsumerSpec("test", "test-sub", handler)],
        lock=FakeLock(held=held),
        poll_timeout=0.05,
        controls_enabled=False,
        **kwargs,
    )


def _wait_for(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_start_and_stop_leaves_no_threads_running(pubsub):
    supervisor = _supervisor(lambda payload: None)
    supervisor.start()
    assert _wait_for(lambda: any(t.is_alive() for t in supervisor._threads))
    supervisor.stop(timeout=5)
    assert not any(t.is_alive() for t in supervisor._threads)
    assert supervisor.lock.released, "the lease must be released on shutdown"


def test_a_message_is_handled_and_acked_exactly_once(pubsub):
    handled = []
    supervisor = _supervisor(lambda payload: handled.append(payload["run_id"]))
    pubsub.queue.append(_message())
    supervisor.start()
    try:
        assert _wait_for(lambda: pubsub.names().count("ack") == 1)
    finally:
        supervisor.stop(timeout=5)

    assert handled == ["run-1"]
    assert "nack" not in pubsub.names()
    assert supervisor.states["test"].snapshot()["processed_count"] == 1


# ---------------------------------------------------------------------------
# The lease: a 60s ack deadline against multi-minute handlers
# ---------------------------------------------------------------------------


def test_a_slow_handler_keeps_its_lease_extended(pubsub):
    """Without this a migration exceeding 60s is redelivered mid-flight and
    re-run from the top."""
    release = threading.Event()
    # The handler blocks until the test releases it, rather than for a
    # fixed span: with a 1s extension interval a 2s handler could finish
    # before the second beat, which made this assertion racy rather than
    # wrong.
    supervisor = _supervisor(lambda payload: release.wait(10.0), lease_seconds=3)
    pubsub.queue.append(_message())
    supervisor.start()
    try:
        assert _wait_for(
            lambda: pubsub.names().count("extend") >= 2, timeout=6.0
        ), f"expected repeated lease extensions, saw {pubsub.calls}"
    finally:
        release.set()
        supervisor.stop(timeout=5)

    assert all(seconds > 0 for _, _, seconds in (c for c in pubsub.calls if c[0] == "extend"))


def test_the_lease_is_released_before_ack_never_after(pubsub):
    """An extension arriving after the ack targets a message that no longer
    exists; after a nack it would cancel the redelivery."""
    supervisor = _supervisor(lambda payload: time.sleep(0.3), lease_seconds=3)
    pubsub.queue.append(_message())
    supervisor.start()
    try:
        assert _wait_for(lambda: "ack" in pubsub.names())
        time.sleep(1.5)  # long enough for a stray heartbeat to fire
    finally:
        supervisor.stop(timeout=5)

    names = pubsub.names()
    assert "extend" not in names[names.index("ack"):], f"lease extended after ack: {pubsub.calls}"


def test_no_lease_extension_after_a_failure_nacks(pubsub):
    def explode(payload):
        time.sleep(0.2)
        raise RuntimeError("handler failed")

    supervisor = _supervisor(explode, lease_seconds=3, backoff_seconds=0.05)
    pubsub.queue.append(_message())
    supervisor.start()
    try:
        assert _wait_for(lambda: "nack" in pubsub.names())
        time.sleep(1.5)
    finally:
        supervisor.stop(timeout=5)

    names = pubsub.names()
    assert "extend" not in names[names.index("nack"):]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_a_failing_handler_nacks_and_the_consumer_survives(pubsub):
    attempts = []

    def flaky(payload):
        attempts.append(payload["run_id"])
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")

    supervisor = _supervisor(flaky, backoff_seconds=0.05)
    pubsub.queue.append(_message(1))
    supervisor.start()
    try:
        assert _wait_for(lambda: "nack" in pubsub.names())
        pubsub.queue.append(_message(2))
        assert _wait_for(lambda: "ack" in pubsub.names()), "the consumer must keep running"
    finally:
        supervisor.stop(timeout=5)

    assert supervisor.states["test"].snapshot()["error_count"] == 1


def test_a_poison_message_backs_off_instead_of_spinning(pubsub):
    """nack sets the deadline to 0, so a bad message returns immediately. A
    one-shot worker exited after one failure; a loop would spin forever."""

    def poison(payload):
        raise RuntimeError("poison")

    supervisor = _supervisor(poison, backoff_seconds=0.4, max_backoff_seconds=2.0)
    for n in range(1, 6):
        pubsub.queue.append(_message(n))
    supervisor.start()
    started = time.monotonic()
    try:
        assert _wait_for(lambda: pubsub.names().count("nack") >= 2, timeout=5.0)
    finally:
        supervisor.stop(timeout=5)

    assert time.monotonic() - started >= 0.4, "two failures cannot complete instantly"


# ---------------------------------------------------------------------------
# Pause and standby
# ---------------------------------------------------------------------------


def test_a_paused_consumer_stops_pulling_and_resumes(pubsub):
    handled = []
    supervisor = _supervisor(lambda payload: handled.append(payload))
    supervisor.start()
    try:
        supervisor.set_paused("test", True, actor="op@example.internal")
        assert _wait_for(lambda: supervisor.states["test"].snapshot()["state"] == "paused")

        pubsub.queue.append(_message())
        time.sleep(0.5)
        assert handled == [], "a paused consumer must not take work"

        supervisor.set_paused("test", False, actor="op@example.internal")
        assert _wait_for(lambda: len(handled) == 1), "resume must pick the work back up"
    finally:
        supervisor.stop(timeout=5)


def test_an_instance_without_the_lease_consumes_nothing(pubsub):
    """Cloud Run scaling and `uvicorn --reload` both produce a second
    supervisor. The loser must idle in standby, not double-consume."""
    handled = []
    supervisor = _supervisor(lambda payload: handled.append(payload), held=False)
    pubsub.queue.append(_message())
    supervisor.start()
    try:
        assert _wait_for(lambda: supervisor.states["test"].snapshot()["state"] == "standby")
        time.sleep(0.4)
        assert handled == []
        assert pubsub.calls == []
    finally:
        supervisor.stop(timeout=5)


def test_standby_says_who_holds_the_lease(pubsub):
    """Nothing-is-happening must be answerable from the console."""
    supervisor = _supervisor(lambda payload: None, held=False)
    status = supervisor.status()
    assert status["lease"]["held"] is False
    assert "another instance holds the worker lease" in status["lease"]["standby_reason"]


def test_status_reports_backlog_as_unknown_rather_than_inventing_it(pubsub):
    """Real queue depth needs google-cloud-monitoring and a new IAM role.
    None renders as "Not available"; a zero would be a lie."""
    supervisor = _supervisor(lambda payload: None)
    assert supervisor.status()["consumers"][0]["backlog"] is None


# ---------------------------------------------------------------------------
# The consumer set
# ---------------------------------------------------------------------------


def test_validation_passed_is_never_consumed():
    """advance_through_validation consumes validation-passed-sub as an
    assertion. Stealing it breaks make run / make harness / evaluation."""
    assert "validation-passed-sub" not in {spec.subscription for spec in ws.default_specs()}


def test_every_orchestrator_handler_has_a_consumer():
    from agents.orchestrator import orchestrator as orch

    subscriptions = {spec.subscription for spec in ws.default_specs()}
    for sub in (
        orch.MIGRATION_REQUESTED_SUB,
        orch.DISCOVERY_COMPLETED_SUB,
        orch.RISK_ASSESSED_SUB,
        orch.PLAN_CREATED_SUB,
        orch.VALIDATION_REQUESTED_SUB,
        orch.VALIDATION_FAILED_SUB,
    ):
        assert sub in subscriptions


def test_no_consumer_calls_run_once():
    """run_once PUBLISHES migration.requested before consuming it, so a
    looping caller would inject a phantom run on every tick."""
    from agents.orchestrator import orchestrator as orch

    handlers = {spec.handler for spec in ws.default_specs()}
    assert orch.run_once not in handlers
    assert orch.advance_through_validation not in handlers


def test_one_consumer_per_subscription():
    """_dedup_claim explicitly assumes a single consumer per subscription."""
    subs = [spec.subscription for spec in ws.default_specs()]
    assert len(subs) == len(set(subs))


def test_consumer_selection_honours_configuration(monkeypatch):
    monkeypatch.setenv("CONTROL_TOWER_WORKER_CONSUMERS", "assessment,cutover")
    assert {spec.name for spec in ws.selected_specs()} == {"assessment", "cutover"}


def test_workers_are_enabled_by_default(monkeypatch):
    """The whole point is that starting the server is the only thing anyone
    runs, so the default must be on."""
    monkeypatch.delenv("CONTROL_TOWER_WORKERS", raising=False)
    assert ws.workers_enabled() is True
    monkeypatch.setenv("CONTROL_TOWER_WORKERS", "0")
    assert ws.workers_enabled() is False
