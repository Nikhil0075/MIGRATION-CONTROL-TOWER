# Validation & Reconciliation Agent (Day 3)

Runs five deterministic checks (`tools/reconciliation.py`) between
`Sales.Customers` (source) and `{dataset}.customers_dim` (target):
schema, row_count, aggregate, null_profile, hash. Pass/fail thresholds
are fixed arithmetic — never a model judgment call (master doc §9).

```bash
python agents/validation/run_validation.py [run_id]   # defaults to the most recent run
```

Writes `migration_runs/{run_id}/reconciliation/*` and transitions the run
to `VALIDATING` then `PASSED` or `FAILED`. Run
`simulator/failure_injector/seed_row_loss.py` first to see `row_count`,
`hash`, and `aggregate` fail on the seeded defect. `schema` also fails
honestly — the fixture loader can't carry SQL Server's `geography` type
into a BigQuery-autodetected load, which is itself a legitimate
schema-drift finding (see the defect's `excluded_columns`), not a bug.
