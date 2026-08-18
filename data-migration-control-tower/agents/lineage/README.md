# Lineage Agent (Day 4)

Derives the dependency graph — never seeds it. Two real sources:
DAG-declared `upstream_tables`/`downstream_tables` on each run's Pipeline
records (confidence 1.0), and a regex SQL parse of the Oracle-dialect
corpus's `CREATE OR REPLACE VIEW ... FROM/JOIN` clauses (confidence
0.85). Both in `tools/lineage_graph.py`.

```bash
python agents/lineage/run_lineage.py [run_id]   # defaults to the most recent run
```

Writes `migration_runs/{run_id}/dependencies/*` and transitions
`DISCOVERED -> ANALYZED`. Run after Discovery, before Risk (Risk now
requires `ANALYZED` per the state machine in `run_lifecycle.py`).
