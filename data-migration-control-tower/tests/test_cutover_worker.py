from __future__ import annotations


def test_cutover_worker_completes_approved_run(monkeypatch):
    from agents.cutover import run_cutover_worker as worker

    state = {"value": "APPROVED"}
    transitions: list[str] = []
    published: list[tuple[str, dict]] = []

    monkeypatch.setattr(worker, "get_run", lambda _run_id: {"state": state["value"]})
    monkeypatch.setattr(worker, "perform_cutover", lambda _run_id: {"status": "performed"})
    monkeypatch.setattr(
        worker,
        "transition_state",
        lambda _run_id, next_state: (transitions.append(next_state), state.update(value=next_state)),
    )
    monitored: list[tuple] = []
    monkeypatch.setattr(
        worker,
        "trigger_post_cutover_monitoring",
        lambda *args: (monitored.append(args), {"status": "HEALTHY"})[1],
    )
    # Since Day 11 Phase 3b the worker monitors every scheduled target
    # rather than a hardcoded Sales.Customers, so what to monitor comes
    # from the run's plan.
    monkeypatch.setattr(
        worker,
        "scheduled_targets",
        lambda _run_id: [
            {"source_schema": "Sales", "source_table": "Customers",
             "target_table": "customers_dim", "key_column": "CustomerID"},
        ],
    )
    monkeypatch.setattr(worker, "publish", lambda topic, event: published.append((topic, event)))

    assert worker._finish("run-contract") == {"run_id": "run-contract", "state": "COMPLETE"}
    assert transitions == ["CUTOVER", "MONITORING", "COMPLETE"]
    assert published == [("cutover.completed", {"run_id": "run-contract"})]
    assert monitored == [("run-contract", "Sales", "Customers", "customers_dim", "CustomerID")]


def test_cutover_worker_monitors_every_scheduled_target(monkeypatch):
    """A multi-table run must not be declared healthy on one table's evidence."""
    from agents.cutover import run_cutover_worker as worker

    state = {"value": "APPROVED"}
    monitored: list[tuple] = []

    monkeypatch.setattr(worker, "get_run", lambda _run_id: {"state": state["value"]})
    monkeypatch.setattr(worker, "perform_cutover", lambda _run_id: {"status": "performed"})
    monkeypatch.setattr(
        worker, "transition_state",
        lambda _run_id, next_state: state.update(value=next_state),
    )
    monkeypatch.setattr(worker, "publish", lambda topic, event: None)
    monkeypatch.setattr(
        worker, "scheduled_targets",
        lambda _run_id: [
            {"source_schema": "Sales", "source_table": "Customers",
             "target_table": "customers_dim", "key_column": "CustomerID"},
            {"source_schema": "Sales", "source_table": "Orders",
             "target_table": "orders_fact", "key_column": "OrderID"},
        ],
    )
    monkeypatch.setattr(
        worker, "trigger_post_cutover_monitoring",
        lambda *args: (monitored.append(args), {"status": "HEALTHY"})[1],
    )

    assert worker._finish("run-multi")["state"] == "COMPLETE"
    assert len(monitored) == 2


def test_cutover_worker_halts_when_any_target_is_unhealthy(monkeypatch):
    from agents.cutover import run_cutover_worker as worker

    state = {"value": "APPROVED"}
    published: list = []

    monkeypatch.setattr(worker, "get_run", lambda _run_id: {"state": state["value"]})
    monkeypatch.setattr(worker, "perform_cutover", lambda _run_id: {"status": "performed"})
    monkeypatch.setattr(
        worker, "transition_state",
        lambda _run_id, next_state: state.update(value=next_state),
    )
    monkeypatch.setattr(worker, "publish", lambda topic, event: published.append(topic))
    monkeypatch.setattr(
        worker, "scheduled_targets",
        lambda _run_id: [
            {"source_schema": "Sales", "source_table": "Customers",
             "target_table": "customers_dim", "key_column": "CustomerID"},
            {"source_schema": "Sales", "source_table": "Orders",
             "target_table": "orders_fact", "key_column": "OrderID"},
        ],
    )
    monkeypatch.setattr(
        worker, "trigger_post_cutover_monitoring",
        lambda run_id, schema, table, *_rest: (
            {"status": "HEALTHY"} if table == "Customers" else {"status": "DEGRADED"}
        ),
    )

    result = worker._finish("run-unhealthy")
    assert result["state"] == "MONITORING"
    assert result["monitoring"]["status"] == "DEGRADED"
    assert published == [], "an unhealthy target must not publish cutover.completed"


def test_cutover_worker_is_idempotent_after_completion(monkeypatch):
    from agents.cutover import run_cutover_worker as worker

    monkeypatch.setattr(worker, "get_run", lambda _run_id: {"state": "COMPLETE"})
    monkeypatch.setattr(worker, "perform_cutover", lambda _run_id: (_ for _ in ()).throw(AssertionError()))
    assert worker._finish("run-complete") == {"run_id": "run-complete", "state": "COMPLETE"}
