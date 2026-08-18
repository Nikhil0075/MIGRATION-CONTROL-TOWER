-- Second-estate fixture schema (Day 11 Phase 7, master doc §32.11).
--
-- Deliberately shaped to exercise every branch of tools/plan_builder.py's
-- target derivation, not just the happy one. WideWorldImporters happens to
-- contain no composite primary keys at all, so without this fixture the
-- blocked-target path would ship untested.
--
--   customers   single-column PK + numeric + nullable  -> fully migratable
--   orders      single-column PK + numeric             -> fully migratable
--   order_items COMPOSITE PK                           -> blocked, with a reason
--   tags        single-column PK, NO numeric column    -> aggregate check omitted
--
-- customers also carries PII-shaped column names (email, phone) so the Risk
-- agent has something real to classify rather than a sanitised toy.

CREATE SCHEMA IF NOT EXISTS retail;

CREATE TABLE retail.customers (
    customer_id   integer PRIMARY KEY,
    customer_name varchar(120) NOT NULL,
    email_address varchar(200),
    phone_number  varchar(40),
    credit_limit  numeric(12,2) NOT NULL,
    created_at    timestamp NOT NULL DEFAULT now()
);

CREATE TABLE retail.orders (
    order_id    integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES retail.customers(customer_id),
    order_total numeric(12,2) NOT NULL,
    placed_at   timestamp NOT NULL DEFAULT now(),
    notes       text
);

-- Composite primary key: the executor orders extraction by a single key
-- column and reconciliation compares ordered key lists, so this table must
-- be BLOCKED with a stated reason rather than migrated on a guess.
CREATE TABLE retail.order_items (
    order_id  integer NOT NULL REFERENCES retail.orders(order_id),
    line_no   integer NOT NULL,
    sku       varchar(40) NOT NULL,
    quantity  integer NOT NULL,
    PRIMARY KEY (order_id, line_no)
);

-- No numeric column beyond the key: the aggregate check must be recorded
-- as not_applicable rather than compared against a fabricated zero.
CREATE TABLE retail.tags (
    tag_id integer PRIMARY KEY,
    label  varchar(60) NOT NULL,
    note   text
);
