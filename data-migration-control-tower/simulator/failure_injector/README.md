# Failure injector

Deliberately, honestly injects the fault-injection matrix from master doc
§7.2 so the Validation Agent's deterministic checks have something real
to catch. Every seeded defect is recorded as a manifest entry under
`migration_runs/{run_id}/seeded_defects/*` — this is fixture data for
proving the fleet's detection/recovery loop, never presented as an
undocumented bug.

| Script | Fault class (§7.2) | Table |
|---|---|---|
| `seed_row_loss.py` | Row loss (0.2%-1% dropped rows) | `Sales.Customers` -> `{dataset}.customers_dim` |

```bash
python simulator/failure_injector/seed_row_loss.py [run_id]
```

As of Day 5, `seed_row_loss.py` is a thin wrapper over
`tools/migration_executor.py`'s `drop_fraction` option — the same
real data-movement path the Migration Planner uses, so there's exactly
one place that knows how to copy a SQL Server table into BigQuery, not
two that could drift apart. `agents/orchestrator/run_full_migration.py`
seeds this same defect automatically as part of the full milestone
chain; run this script directly only when debugging Validation against
an already-existing run in isolation.

Run after Discovery (so a catalog/run exists) and before Validation.
Additional fault classes (schema drift, PII policy violation, unsupported
SQL, duplicate key, null drift, broken dependency, malicious-instruction
content) are added on later build days as the agents that detect them
are built.
