from __future__ import annotations

from unittest.mock import MagicMock


def test_post_cutover_monitoring_uses_plan_binding_and_target(monkeypatch):
    from agents.cutover import agent

    target = {
        "target_id": "source-one:Sales.Customers",
        "source_schema": "Sales",
        "source_table": "Customers",
        "target_table": "customers_from_plan",
        "key_column": "CustomerID",
    }
    binding = object()
    adapter = MagicMock()
    adapter.source_facts.return_value = {"row_count": 2, "keys": ["1", "2"]}
    built = []
    target_calls = []
    bq_calls = []

    monkeypatch.setattr("tools.migration_plan.plan_binding", lambda _run_id: binding)
    monkeypatch.setattr(
        "tools.migration_plan.target_for_table_ref",
        lambda run_id, table_ref: (target_calls.append((run_id, table_ref)), target)[1],
    )
    monkeypatch.setattr(
        "tools.migration_plan.get_plan",
        lambda _run_id: {"target": {"system": "bigquery", "dataset_env": "PLAN_BQ_DATASET"}},
    )
    monkeypatch.setattr(
        "tools.adapters.build_adapter_for_binding",
        lambda value: (built.append(value), adapter)[1],
    )
    monkeypatch.setenv("PLAN_BQ_DATASET", "estate_target")
    monkeypatch.setattr(
        agent,
        "bq_row_count",
        lambda table, **kwargs: (bq_calls.append(("count", table, kwargs)), 2)[1],
    )
    monkeypatch.setattr(
        agent,
        "get_key_values",
        lambda table, column, **kwargs: (
            bq_calls.append(("keys", table, column, kwargs)), ["1", "2"]
        )[1],
    )
    monkeypatch.setattr(agent, "get_client", lambda: MagicMock())

    result = agent.trigger_post_cutover_monitoring(
        "run-one", "Sales", "Customers", "ignored_global_target", "IgnoredKey"
    )

    assert result["status"] == "HEALTHY"
    assert built == [binding]
    adapter.source_facts.assert_called_once_with(target)
    assert target_calls == [("run-one", "Sales.Customers")]
    assert bq_calls == [
        ("count", "customers_from_plan", {"dataset": "estate_target"}),
        ("keys", "customers_from_plan", "CustomerID", {"dataset": "estate_target"}),
    ]
