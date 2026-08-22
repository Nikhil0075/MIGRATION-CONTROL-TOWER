#!/usr/bin/env python
"""One-off Cloud Run Job entrypoint that bootstraps the Cloud SQL Postgres
demo instance (Deploy & Harden Phase 5 close-out — "wire up the Cloud SQL
discovery path"): creates the `retail` schema, loads the exact same
fixture data `simulator/source_setup/postgres/init/`'s local docker-compose
Postgres estate uses (same schema, same rows — the Cloud SQL demo source
and the local dev one stay identical on purpose, not a divergent copy),
and applies the actual REVOKE/GRANT for `migration_readonly` that
infrastructure/terraform/cloud_sql.tf's own header comment has always
said was "applied post-create via a SQL migration script" — this is that
script.

Reuses the same image as tools/data_plane_job/run_job.py (same
Dockerfile, same psycopg dependency, same Direct VPC egress wiring) —
infrastructure/terraform/data_plane_job.tf's db_bootstrap job just
overrides the container command to run this module instead. Idempotent:
every DDL/DML statement is safe to re-run (IF NOT EXISTS / ON CONFLICT),
so re-running this job after the first bootstrap is a no-op, not a
duplicate-data risk.

Connects as the `postgres` superuser (infrastructure/terraform/cloud_sql.tf's
`google_sql_user.postgres_superuser`) — the ONLY thing in this whole
deployment that ever uses that credential. Every other consumer of this
Cloud SQL instance (the data-plane job, any future Discovery run) uses
`migration_readonly`, which this script itself is what locks down to
read-only.

Config: env vars, same convention as run_job.py (Cloud Run Jobs pass
parameters this way).
"""

from __future__ import annotations

import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bootstrap_retail_db")

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS retail;

CREATE TABLE IF NOT EXISTS retail.customers (
    customer_id   integer PRIMARY KEY,
    customer_name varchar(120) NOT NULL,
    email_address varchar(200),
    phone_number  varchar(40),
    credit_limit  numeric(12,2) NOT NULL,
    created_at    timestamp NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail.orders (
    order_id    integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES retail.customers(customer_id),
    order_total numeric(12,2) NOT NULL,
    placed_at   timestamp NOT NULL DEFAULT now(),
    notes       text
);

CREATE TABLE IF NOT EXISTS retail.order_items (
    order_id  integer NOT NULL REFERENCES retail.orders(order_id),
    line_no   integer NOT NULL,
    sku       varchar(40) NOT NULL,
    quantity  integer NOT NULL,
    PRIMARY KEY (order_id, line_no)
);

CREATE TABLE IF NOT EXISTS retail.tags (
    tag_id integer PRIMARY KEY,
    label  varchar(60) NOT NULL,
    note   text
);
"""

# Same fixture rows as simulator/source_setup/postgres/init/02_seed.sql,
# with ON CONFLICT DO NOTHING added so re-running this job is a no-op.
SEED_SQL = """
INSERT INTO retail.customers (customer_id, customer_name, email_address, phone_number, credit_limit) VALUES
  (1, 'Northwind Supplies',  'ap@northwind.example',   '+1-206-555-0101', 25000.00),
  (2, 'Contoso Retail',      'billing@contoso.example', NULL,             18000.50),
  (3, 'Fabrikam Wholesale',  NULL,                      '+1-425-555-0142', 42000.00),
  (4, 'Tailspin Traders',    'ar@tailspin.example',     '+1-312-555-0177',  9500.25),
  (5, 'Adventure Outfitters', NULL,                     NULL,              15750.75)
ON CONFLICT (customer_id) DO NOTHING;

INSERT INTO retail.orders (order_id, customer_id, order_total, notes) VALUES
  (1001, 1, 1250.00, 'expedited'),
  (1002, 2,  875.50, NULL),
  (1003, 1, 2400.00, NULL),
  (1004, 4,  310.25, 'backordered'),
  (1005, 3, 5600.00, NULL)
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO retail.order_items (order_id, line_no, sku, quantity) VALUES
  (1001, 1, 'SKU-100', 4), (1001, 2, 'SKU-221', 1),
  (1002, 1, 'SKU-100', 2),
  (1003, 1, 'SKU-300', 10), (1003, 2, 'SKU-221', 3),
  (1004, 1, 'SKU-410', 1),
  (1005, 1, 'SKU-100', 20), (1005, 2, 'SKU-300', 5), (1005, 3, 'SKU-410', 2)
ON CONFLICT (order_id, line_no) DO NOTHING;

INSERT INTO retail.tags (tag_id, label, note) VALUES
  (1, 'priority', 'expedite on request'),
  (2, 'wholesale', NULL),
  (3, 'at-risk', 'credit review pending')
ON CONFLICT (tag_id) DO NOTHING;
"""

# Cloud SQL's own google_sql_user resource has no built-in read-only
# role (infrastructure/terraform/cloud_sql.tf's own comment on this) —
# this is the actual lockdown: revoke write privileges the default
# GRANT ALL ON SCHEMA grants a new role's default search path, then grant
# SELECT explicitly, on both existing tables and anything created later.
READONLY_GRANT_SQL = """
GRANT USAGE ON SCHEMA retail TO migration_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA retail TO migration_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA retail GRANT SELECT ON TABLES TO migration_readonly;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA retail FROM migration_readonly;
REVOKE CREATE ON SCHEMA retail FROM migration_readonly;
REVOKE ALL ON DATABASE retail FROM migration_readonly;
GRANT CONNECT ON DATABASE retail TO migration_readonly;
"""


def _env(name: str, *, required: bool = True) -> str | None:
    value = os.environ.get(name)
    if required and not value:
        raise RuntimeError(f"bootstrap_retail_db: required env var {name!r} is unset or empty.")
    return value or None


def main() -> int:
    import psycopg

    host = _env("POSTGRES_HOST")
    port = int(_env("POSTGRES_PORT", required=False) or "5432")
    database = _env("POSTGRES_DATABASE", required=False) or "retail"
    superuser_password = _env("POSTGRES_SUPERUSER_PASSWORD")

    logger.info("bootstrap_retail_db: connecting to %s:%s/%s as postgres", host, port, database)
    conn = psycopg.connect(
        host=host, port=port, dbname=database,
        user="postgres", password=superuser_password,
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            logger.info("Applying schema (idempotent)...")
            cur.execute(SCHEMA_SQL)
            logger.info("Loading seed data (idempotent)...")
            cur.execute(SEED_SQL)
            logger.info("Applying migration_readonly REVOKE/GRANT...")
            cur.execute(READONLY_GRANT_SQL)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM retail.customers")
            customer_count = cur.fetchone()[0]
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'retail' ORDER BY table_name"
            )
            tables = [row[0] for row in cur.fetchall()]
        logger.info("bootstrap_retail_db COMPLETE: tables=%s, customers=%d", tables, customer_count)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
