# source_system: oracle-corpus (DAG stub)
# Self-authored, Airflow-style DAG metadata stub — see dags/README.md.
#
# Models a nightly ETL job moving CO.CUSTOMERS / CO.ORDERS into BigQuery.
# Parsed by tools/source_catalog.py::catalog_dag_artifacts() into a
# Pipeline record (master doc §7.1).

DAG_METADATA = {
    "pipeline_id": "co.customer_orders_etl",
    "source_system": "oracle-corpus",
    "target_system": "bigquery",
    "schedule": "0 2 * * *",  # nightly at 02:00
    "owner": "data-platform-eng@example.internal",
    "criticality": "HIGH",
    "code_path": "simulator/source_setup/dags/dag_customer_orders_etl.py",
    "status": "ACTIVE",
    "upstream_tables": [
        "CO.CUSTOMERS",
        "CO.ORDERS",
        "CO.ORDER_ITEMS",
    ],
    "downstream_tables": [
        "bigquery.migration_target.customer_orders_fact",
    ],
}


def run():
    """Stub task graph — not executed; describes intended ETL steps."""
    steps = [
        "extract_co_customers",
        "extract_co_orders",
        "extract_co_order_items",
        "transform_join_customer_orders",
        "load_bigquery_customer_orders_fact",
    ]
    return steps
