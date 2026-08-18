# source_system: oracle-corpus (DAG stub)
# Self-authored, Airflow-style DAG metadata stub — see dags/README.md.
#
# Models the daily sync of HR.EMPLOYEES into BigQuery. This pipeline feeds
# CO.V_CUSTOMER_ACCOUNT_SUMMARY, so it is the "critical dependency" used
# by the Risk agent's downstream-impact scoring (master doc §7.1) and by
# the cross-department Finance Reporting Impact Agent scenario (§20.3).

DAG_METADATA = {
    "pipeline_id": "hr.employees_sync",
    "source_system": "oracle-corpus",
    "target_system": "bigquery",
    "schedule": "30 1 * * *",  # daily at 01:30, before customer_orders_etl
    "owner": "hr-systems@example.internal",
    "criticality": "HIGH",
    "code_path": "simulator/source_setup/dags/dag_hr_employees_sync.py",
    "status": "ACTIVE",
    "upstream_tables": [
        "HR.EMPLOYEES",
        "HR.DEPARTMENTS",
        "HR.JOBS",
    ],
    "downstream_tables": [
        "bigquery.migration_target.employees_dim",
        "CO.V_CUSTOMER_ACCOUNT_SUMMARY",
    ],
}


def run():
    steps = [
        "extract_hr_employees",     # contains PII: EMAIL, PHONE_NUMBER
        "mask_pii_columns",
        "load_bigquery_employees_dim",
    ]
    return steps
