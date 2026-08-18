# source_system: sqlserver-wwi (DAG stub)
# Self-authored, Airflow-style DAG metadata stub — see dags/README.md.
#
# Models the WideWorldImporters Sales.Customers -> BigQuery full load.
# This is the pipeline used in the recommended end-to-end demo story
# (master doc §2.3) and in the Day 1-2 walkthrough (run_2026_..._wwi_customers).

DAG_METADATA = {
    "pipeline_id": "wwi.sales.customers",
    "source_system": "sqlserver-wwi",
    "target_system": "bigquery",
    "schedule": "0 4 * * *",  # nightly at 04:00
    "owner": "data-platform-eng@example.internal",
    "criticality": "CRITICAL",
    "code_path": "simulator/source_setup/dags/dag_wwi_customers_full_load.py",
    "status": "ACTIVE",
    "upstream_tables": [
        "Sales.Customers",
        "Sales.CustomerCategories",
        "Application.People",
    ],
    "downstream_tables": [
        "bigquery.migration_target.customers_dim",
    ],
}


def run():
    steps = [
        "extract_wwi_sales_customers",
        "extract_wwi_customer_categories",
        "join_application_people",
        "load_bigquery_customers_dim",
    ]
    return steps
