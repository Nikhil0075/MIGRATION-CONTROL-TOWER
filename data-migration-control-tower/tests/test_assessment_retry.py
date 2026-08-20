"""A retried assessment must not leave a new abandoned run behind each time.

`run_assessment` calls `create_run` before `adapter.discover_tables()`,
and discovery is the step that actually fails — a source that is down, a
credential that expired, a connection that times out. The worker then
nacks, Pub/Sub redelivers the same message, and the whole function ran
again from the top, including `create_run`.

At ten delivery attempts that is ten runs for one operator command, nine
of them stranded in REQUESTED forever. Twenty-nine were found on one
estate in the live project. They are not merely untidy: the console picks
the newest run when deciding what lineage to draw, so an abandoned empty
run made the Lineage page render blank.
"""

from __future__ import annotations

import pytest

from agents.discovery import run_assessment as module


class Recorder:
    """Stands in for the run lifecycle, counting what gets created."""

    def __init__(self, states: dict[str, str] | None = None):
        self.created: list[str] = []
        self.states = states or {}
        self._next = 0

    def create_run(self, *_args, **_kwargs) -> str:
        self._next += 1
        run_id = f"run_new_{self._next}"
        self.created.append(run_id)
        self.states[run_id] = "REQUESTED"
        return run_id

    def get_run(self, run_id: str) -> dict:
        if run_id not in self.states:
            raise KeyError(run_id)
        return {"run_id": run_id, "state": self.states[run_id]}


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(module, "create_run", rec.create_run)
    monkeypatch.setattr(module, "get_run", rec.get_run)
    return rec


def test_a_run_that_never_started_is_reused_rather_than_replaced(recorder):
    recorder.states["run_stalled"] = "REQUESTED"
    assert module._reusable("run_stalled") == "run_stalled"


def test_a_run_that_made_progress_is_not_reused(recorder):
    """It cannot be, and pretending otherwise would fail later and worse.

    `transition_state` refuses DISCOVERED -> DISCOVERED by design, so
    replaying this function over a run that already catalogued something
    would raise partway through. A fresh run starts, and the partial one
    stays as the evidence of a partial attempt that it is.
    """
    for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED"):
        recorder.states["run_partial"] = state
        assert module._reusable("run_partial") is None, state


def test_a_run_that_no_longer_exists_is_not_reused(recorder):
    # Deleted between attempts — by cleanup, by an operator, by the
    # orphan purge. Reusing the id would write a subcollection under a
    # document that is not there, which is exactly how orphans are made.
    assert module._reusable("run_deleted") is None


def test_no_previous_run_means_a_new_one(recorder):
    assert module._reusable(None) is None


def test_ten_delivery_attempts_produce_one_run_not_ten(monkeypatch, recorder):
    """The whole point, expressed as the failure that was observed.

    Discovery fails every time; the message is redelivered ten times.
    Before this, that left ten runs. It must now leave one.
    """
    attached: list[str] = []
    monkeypatch.setattr(module, "load_pack", lambda _path: {"pack_id": "p", "version": 1})
    monkeypatch.setattr(
        module, "binding_for_estate_pack",
        lambda *_a, **_k: type("B", (), {"source_id": "src"})(),
    )

    class DeadSource:
        system = "sqlserver"

        def discover_tables(self):
            raise ConnectionError("the source is unreachable")

    monkeypatch.setattr(module, "build_adapter_for_estate", lambda *_a, **_k: DeadSource())

    run_id: str | None = None
    for _attempt in range(10):
        with pytest.raises(ConnectionError):
            module.run_assessment(
                "packs/whatever.yaml",
                estate_id="acme-finance",
                on_run_created=attached.append,
                reuse_run_id=run_id,
            )
        # What the worker does between deliveries: it records the run on
        # the operation and hands it back on the next attempt.
        run_id = attached[-1]

    assert len(recorder.created) == 1, (
        f"one command produced {len(recorder.created)} runs: {recorder.created}"
    )
    assert set(attached) == {recorder.created[0]}


def test_a_successful_first_attempt_still_creates_exactly_one_run(monkeypatch, recorder):
    """Guards the obvious regression: reuse must not stop runs being made."""
    monkeypatch.setattr(module, "load_pack", lambda _path: {"pack_id": "p", "version": 1})
    monkeypatch.setattr(
        module, "binding_for_estate_pack",
        lambda *_a, **_k: type("B", (), {"source_id": "src"})(),
    )

    class Source:
        system = "sqlserver"

        def discover_tables(self):
            return [{"name": "t"}]

        def discover_pipelines(self):
            return []

    monkeypatch.setattr(module, "build_adapter_for_estate", lambda *_a, **_k: Source())
    monkeypatch.setattr(module, "write_catalog", lambda *_a, **_k: {"tables": 1})
    monkeypatch.setattr(module, "build_dependency_graph", lambda *_a, **_k: {"edges_written": 0})
    monkeypatch.setattr(module, "transition_state", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "classify_estate", lambda *_a, **_k: {})
    monkeypatch.setattr(
        module, "verify_pii_access_boundary", lambda *_a, **_k: {"decision": "DENY"}
    )
    monkeypatch.setattr(
        module, "propose_plan", lambda *_a, **_k: {"steps": [], "plan_hash": "h"}
    )

    report = module.run_assessment("packs/whatever.yaml", estate_id="e")

    assert len(recorder.created) == 1
    assert report["run_id"] == recorder.created[0]


def test_the_worker_hands_the_previous_run_back_to_the_assessment(monkeypatch):
    """The two halves have to agree, or the reuse never happens.

    `run_assessment` can only reuse a run it is told about, and the only
    record of the previous attempt's run is the operation document the
    worker wrote it to. A worker that stops passing it would leave the
    reuse logic correct and completely inert.
    """
    from agents.discovery import run_assessment_worker as worker

    captured: dict = {}

    class Ref:
        def get(self):
            return type("Snap", (), {"to_dict": lambda _self: {"run_id": "run_from_attempt_1"}})()

        def set(self, *_a, **_k):
            pass

        def update(self, *_a, **_k):
            pass

    monkeypatch.setattr(
        worker, "get_client",
        lambda: type("C", (), {
            "collection": lambda _self, _name: type("Col", (), {
                "document": lambda _s, _id: Ref(),
            })(),
        })(),
    )

    def fake_run_assessment(_pack_path, **kwargs):
        captured.update(kwargs)
        return {"run_id": "run_from_attempt_1", "pack_id": "p"}

    monkeypatch.setattr(worker, "run_assessment", fake_run_assessment)

    worker.handle_assessment_requested(
        {"operation_id": "op-1", "pack_path": "packs/p.yaml", "estate_id": "e"}
    )

    assert captured["reuse_run_id"] == "run_from_attempt_1"
