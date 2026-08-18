# Simulated on-prem legacy estate

This directory reproduces a "legacy on-prem" SQL Server estate locally, per
master doc §6.3/§18.3.

```
source_setup/
  docker-compose.yml        # SQL Server 2022 Developer Edition container
  restore_wwi.sh             # downloads + restores Microsoft's WideWorldImporters OLTP sample
  oracle_dialect_corpus/     # static, self-authored Oracle-syntax SQL (no live Oracle needed)
  dags/                      # static, self-authored Airflow-style DAG stubs describing WWI ETL jobs
```

Run order:

```bash
docker compose up -d
./restore_wwi.sh
```

See root [DATA_SOURCES.md](../../DATA_SOURCES.md) for attribution and the
explicit statement that this is a simulated/labeled legacy estate, not
real production data.
