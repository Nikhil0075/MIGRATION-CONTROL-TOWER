# source_system: oracle-corpus (DAG stub)
# Self-authored, Airflow-style DAG metadata stub — see dags/README.md.
#
# Models the large-volume nightly load of SH.SALES into BigQuery — the
# scale/benchmark-layer pipeline referenced in master doc §6.1/§6.2.

DAG_METADATA = {
    "pipeline_id": "sh.sales_history_etl",
    "source_system": "oracle-corpus",
    "target_system": "bigquery",
    "schedule": "0 3 * * *",  # nightly at 03:00, after customer_orders_etl
    "owner": "data-platform-eng@example.internal",
    "criticality": "CRITICAL",
    "code_path": "simulator/source_setup/dags/dag_sales_history_etl.py",
    "status": "ACTIVE",
    "upstream_tables": [
        "SH.SALES",
        "SH.PRODUCTS",
        "SH.TIMES",
        "SH.CHANNELS",
    ],
    "downstream_tables": [
        "bigquery.migration_target.sales_fact",
        "SH.V_QUARTERLY_REVENUE_BY_CHANNEL",
    ],
}


def run():
    steps = [
        "extract_sh_sales_partitioned",
        "extract_sh_dimensions",
        "transform_channel_decode",
        "load_bigquery_sales_fact",
        "refresh_quarterly_revenue_view",
    ]
    return steps
