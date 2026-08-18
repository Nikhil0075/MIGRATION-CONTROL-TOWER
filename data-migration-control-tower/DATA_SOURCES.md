# Data Sources & Attribution

This project reconstructs a representative enterprise legacy estate from
official vendor sample databases and self-authored, benchmark-style
fixtures. No confidential, production, or client data is used anywhere in
this repository.

## 1. WideWorldImporters (primary legacy source)

- **Vendor**: Microsoft
- **Format**: SQL Server `.bak` backup, restored into a local Docker
  container (`simulator/source_setup/docker-compose.yml`)
- **License**: MIT (see Microsoft's `sql-server-samples` repository)
- **Source**: https://learn.microsoft.com/en-us/sql/samples/wide-world-importers-what-is
- **Role in this project**: principal simulated on-prem SQL Server estate —
  an OLTP database used as the Discovery/Lineage/Risk/Validation source of
  truth. Restored via `simulator/source_setup/restore_wwi.sh`.

## 2. Oracle-dialect script corpus

- **Format**: static `.sql` files under
  `simulator/source_setup/oracle_dialect_corpus/`
- **Status**: **self-authored**, modeled on the shape and naming of
  Oracle's public sample schemas (Customer Orders / Sales History / HR —
  https://github.com/oracle-samples/db-sample-schemas). These are NOT an
  export from a live Oracle instance and NOT copied from the Oracle
  repository; they are hand-written DDL/procedures that exercise
  Oracle-specific constructs (`NVL`, `DECODE`, `CONNECT BY`) so the Risk
  and Migration Planner agents have real dialect-incompatibility material
  to reason about, without requiring a running Oracle container
  (see master doc §18.3 — "never let the source estate depend on a
  running Oracle instance").

## 3. DAG / scheduling artifacts

- **Format**: static Airflow-style DAG stub files under
  `simulator/source_setup/dags/`
- **Status**: self-authored, modeled on the structure of Apache Airflow
  example DAGs (https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html)
  to describe WideWorldImporters-style ETL scheduling metadata (owners,
  schedule, upstream/downstream table references) for the Discovery and
  Lineage agents to parse.

## 4. Injected faults

All failure-injection scenarios (schema drift, row loss, PII policy
violations, unsupported SQL, duplicate keys, null drift, broken
dependencies, malicious-instruction content) are authored deliberately by
this project team for evaluation purposes and are documented alongside the
evaluation harness as they are added (see master doc §7.2).

## What this project explicitly does NOT use

- No confidential or production client data.
- No live Oracle Database instance (Oracle dialect handling is a
  code-parsing concern against the static corpus above).
- Kaggle datasets are not currently part of the Day 1–2 build; if added
  later for scale/volume testing, they will be listed here with source
  links before use.
