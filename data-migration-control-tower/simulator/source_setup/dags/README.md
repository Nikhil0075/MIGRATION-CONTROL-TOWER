# DAG / scheduling artifact stubs

**Status: self-authored.** These are small, static, Airflow-style DAG
definitions (Python, using only `dict`/lightweight `@dataclass`-style
metadata — not a real Airflow install) modeled on the structure of Apache
Airflow example DAGs
(https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html),
describing WideWorldImporters-style ETL scheduling metadata: owner,
schedule, criticality, and upstream/downstream table references.

`tools/source_catalog.py`'s `catalog_dag_artifacts()` parses these into
`Pipeline` records (master doc §7.1) — `pipeline_id`, `source_system`,
`target_system`, `schedule`, `owner`, `criticality`, `code_path`,
`status` — and the table references become candidate Lineage edges once
the Lineage agent is built (19 Aug).

| File | Models |
|---|---|
| `dag_customer_orders_etl.py` | CO.CUSTOMERS/ORDERS -> BigQuery nightly load |
| `dag_sales_history_etl.py` | SH.SALES fact load, large-volume nightly job |
| `dag_hr_employees_sync.py` | HR.EMPLOYEES -> BigQuery daily sync (feeds finance reporting view) |
| `dag_wwi_customers_full_load.py` | WideWorldImporters `Sales.Customers` -> BigQuery, the pipeline used in the Day 1-2 walkthrough |
