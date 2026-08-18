# Operational utility baseline — friction table (master doc §25)

Generated 2026-08-16T11:33:25.869990+00:00 against fleet run `run_20260816_112943_97d8d9c3`.

| Measure | Manual baseline | Control tower | Basis |
|---|---|---|---|
| Wall-clock time for full assessment | 188s | 172s | Same estate (WWI + Oracle corpus + DAGs), same inputs, timed |
| Defects detected before cutover | 2 | 7 | Against 3 seeded ground-truth defects |
| Defects missed | 1 | 0 | Seeded defects not surfaced |
| Lineage edges recovered | 15 (hand-traced) | 25 | Precision/recall vs seeded DAG+SQL-view ground truth |
| Policy violations blocked | 0 — enforcement is procedural | 2 | Denials at the tool layer vs. reliance on reviewer discipline |
| Human decisions required | 6+ (a review decision at every activity) | 1 | Approval gates vs. every-step review |
| Cost per assessed pipeline | ~0.05 analyst-hours | single-pipeline GCP free-tier usage (SQL Server local, BigQuery <1MB load, a handful of Gemini 3.5 Flash calls) — see README's cost note; no Cloud Billing export configured in this environment, so this is not an invoiced figure | Measured spend divided by pipelines assessed |

## Manual activity log (evaluation/baseline_timer.py)

| Activity | Seconds | Method |
|---|---|---|
| asset_inventory | 52.3 | Queried INFORMATION_SCHEMA.TABLES/COLUMNS in Sales schema by hand (12 tables, Customers=30 columns), grepped CREATE TABLE across 5 Oracle-corpus .sql files (10 tables), read 4 DAG stub files for job/schedule/owner/upstream/downstream metadata (4 pipelines). Total: 22 tables + 4 pipeline artifacts. |
| dependency_mapping | 31.2 | Read 4 DAG files' upstream_tables/downstream_tables declarations by hand; grepped FROM/JOIN clauses in the 2 CREATE OR REPLACE VIEW blocks in legacy_reporting_views.sql and manually traced each referenced table to its source. Constructed 15 reads/writes edges total. |
| dialect_review | 19.9 | Grepped for NVL|DECODE|CONNECT BY|SYSDATE across all 5 Oracle-corpus .sql files by hand; found dialect-incompatible constructs in all 5 files (co_customer_orders, co_procedures, hr_employees, legacy_reporting_views, sh_sales_history), meaning all 10 corpus-sourced tables require translation review before a BigQuery load. |
| documentation_reconciliation | 33.1 | Visually inspected simulator/documentation/erd_sales_customers.png and compared its 5 documented columns (CustomerID, CustomerName, EmailAddress, PhoneNumber, CreditLimit) against the 30 real Sales.Customers columns enumerated during asset_inventory: found EmailAddress documented but absent from the live schema (stale doc), PhoneNumber marked PUBLIC in the ERD despite classifying PII by policy, and 25 real columns present in the schema but absent from the ERD. |
| reconciliation_design | 29.8 | Hand-wrote 5 paired source/target SQL queries (row_count, schema, aggregate on CreditLimit, null_profile on PhoneNumber, key-set on CustomerID) for the Sales.Customers -> customers_dim pipeline, the same 5 check types tools/reconciliation.py automates. Query design only (not executed against target — target values require the migration to have already run). |
| sensitivity_classification | 21.7 | Manually compared all 30 Sales.Customers columns (enumerated during asset_inventory) against the policies/data_classification.yaml rule list by eye: CustomerName->PII(name), PhoneNumber/FaxNumber->PII(phone), DeliveryAddressLine1/2+PostalAddressLine1/2+DeliveryPostalCode+PostalPostalCode->PII(address); remainder METADATA. Cross-checked HR.EMPLOYEES columns in hr_employees.sql the same way. |
