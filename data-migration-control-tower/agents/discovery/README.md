# Discovery Agent (Day 2)

Inventories the legacy estate. Tools: `catalog_sql_server_tables`,
`catalog_oracle_corpus`, `catalog_dag_artifacts` (all in
`tools/source_catalog.py`) — metadata only, per the §4.2 tool boundary.

```bash
python agents/discovery/run_discovery.py
```

Creates a run, inventories WWI + the Oracle corpus + the DAG stubs, and
persists the catalog under `migration_runs/{run_id}/catalog/*` and
`.../pipelines/*` in Firestore. See root README for prerequisites.
