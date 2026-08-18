# Migration Planner Agent (Day 5)

Proposes a `MigrationPlan` (`contracts/metadata_model.json`): target
table names, execution order (critical-dependency tables first, per
Risk's findings), SQL translation notes for Oracle-dialect tables, and a
rollback strategy. Never executes anything.

```bash
python agents/planner/run_planner.py [run_id]   # defaults to the most recent run
```

Writes `migration_runs/{run_id}/migration_plan/current` and transitions
`RISK_ASSESSED -> PLANNED`. Only `Sales.Customers` is `scheduled: true`
this run (matches the canonical demo pipeline used since Day 2); the
rest of the catalog gets a proposed target name and order but isn't
executed — see `tools/plan_builder.py`'s docstring for why a full
dependency-graph topological sort isn't used yet.
