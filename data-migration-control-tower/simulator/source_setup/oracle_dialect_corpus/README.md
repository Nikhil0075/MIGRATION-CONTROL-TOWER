# Oracle-dialect script corpus

**Status: self-authored.** These `.sql` files are hand-written by this
project team, modeled on the *shape* and naming conventions of Oracle's
public sample schemas (Customer Orders / Sales History / HR —
https://github.com/oracle-samples/db-sample-schemas). They are **not**
copied from that repository and are **not** an export from a running
Oracle instance.

Purpose: give the Risk agent and Migration Planner real Oracle-specific
SQL constructs to detect and translate (`NVL`, `DECODE`, `CONNECT BY ...
START WITH`, `SYSDATE`, `%TYPE`/`%ROWTYPE` anchors), and give the Lineage
agent view-to-table dependency edges to parse — without requiring a live
Oracle Database container (master doc §18.3).

| File | Models | Dialect constructs |
|---|---|---|
| `hr_employees.sql` | Oracle HR sample | `CONNECT BY PRIOR`, `DECODE`, `SYSDATE` |
| `co_customer_orders.sql` | Oracle Customer Orders sample | `NVL`, JSON column, `%TYPE` |
| `sh_sales_history.sql` | Oracle Sales History sample | `DECODE`, partition-style comments, large fact table |
| `co_procedures.sql` | stored procedures across CO tables | `NVL`, `DECODE`, cursor loop |
| `legacy_reporting_views.sql` | cross-schema reporting views | multi-table joins for lineage edges |

Each file is tagged `-- source_system: oracle-corpus` in its header so
`tools/source_catalog.py` can attribute discovered tables correctly.
