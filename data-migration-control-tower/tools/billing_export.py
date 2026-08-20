"""Snapshots real spend from the Cloud Billing export into Firestore.

"Actual cost" is the one figure in the console that this system cannot
derive from its own behaviour. Estimated cost is measured usage priced
from a committed rate card; it is an upper bound on list, and it knows
nothing about committed-use discounts, free tiers or whatever is
negotiated on the billing account. Only Google's own billing export
knows what was actually charged.

Run periodically — a cron, or by hand before a review:

    python -m tools.billing_export
    python -m tools.billing_export --days 30

Reads, never writes, the billing table. Writes one Firestore document,
`cost_snapshots/current`, which the console reads.

**Why a snapshot rather than a live query.** The billing export is a
BigQuery table, and querying it on every dashboard load would make the
cost panel a recurring cost of its own — a cost dashboard that bills you
for looking at it. The snapshot carries `observed_at`, and the console
marks it stale rather than pretending it is current.

Setup, which has to happen in the Cloud Console because it is a
billing-account setting and not a project one:

  1. Billing -> Billing export -> BigQuery export -> Standard usage cost.
  2. Choose (or create) a dataset in this project and enable it.
  3. Set CLOUD_BILLING_EXPORT_DATASET to `project.dataset` and let this
     find the table, or set CLOUD_BILLING_EXPORT_TABLE to the full
     `project.dataset.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX`.

The dataset must be a US or EU MULTI-REGION. A single-region dataset is
refused by the export dialog with "Invalid dataset region", which reads
like a permissions problem and is not one.

The export only accrues from the moment it is enabled; there is no
backfill, so the first days after switching it on genuinely have no data
and the snapshot will say so rather than reporting zero.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

#: A fully-qualified BigQuery table and nothing else. This value is
#: interpolated into SQL — it cannot be a bound parameter, because a
#: table name is not a value in SQL — so it is validated against the
#: shape of an identifier rather than trusted. The env var is operator
#: configuration, but "configuration we control" is exactly the
#: assumption injection defects are built on.
_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")

SNAPSHOT_COLLECTION = "cost_snapshots"
SNAPSHOT_DOCUMENT = "current"

#: What Cloud Billing names its standard-usage export table. The suffix is
#: the billing account id with the hyphens replaced, which nobody should
#: have to transcribe by hand from a console page.
EXPORT_TABLE_PREFIX = "gcp_billing_export_v1_"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def validate_table(table: str) -> str:
    if not _TABLE_RE.match(table or ""):
        raise ValueError(
            f"CLOUD_BILLING_EXPORT_TABLE must be project.dataset.table, got {table!r}"
        )
    return table


def discover_table(dataset: str) -> str:
    """Finds the export table in `dataset`, so nobody has to transcribe it.

    The table is named `gcp_billing_export_v1_<billing account id>`, and
    the account id is not something an operator has memorised. Worse, the
    table does not exist until the first daily batch lands — hours after
    the export is enabled, with no backfill — so the common experience is
    looking for a table that is not there yet and assuming the setup
    failed. This says which of those it is.

    Refuses to guess when there are several: a project can carry a
    detailed export and a pricing export alongside the standard one, and
    silently picking the first would report the wrong numbers.
    """
    from tools.bigquery_tools import get_client as bq_client

    client = bq_client()
    tables = [
        t.table_id
        for t in client.list_tables(dataset)
        if t.table_id.startswith(EXPORT_TABLE_PREFIX)
    ]
    if not tables:
        raise LookupError(
            f"No {EXPORT_TABLE_PREFIX}* table in {dataset} yet. Cloud Billing "
            f"creates it when the first daily batch lands, which can take a day "
            f"after enabling the export, and there is no backfill. If the export "
            f"shows Enabled in the console, this is a wait, not a fault."
        )
    if len(tables) > 1:
        raise LookupError(
            f"Several export tables in {dataset}: {sorted(tables)}. Set "
            f"CLOUD_BILLING_EXPORT_TABLE explicitly to the one you want."
        )
    return f"{dataset}.{tables[0]}"


def build_query(table: str, days: int) -> str:
    """Cost and credits by service over the window.

    Credits are summed and applied, not ignored: free-tier and committed-
    use credits arrive as negative amounts on the same rows, and a total
    that counted only gross cost would overstate the bill — which is the
    specific way a cost dashboard loses an operator's trust.

    Partitioned on `_PARTITIONTIME` via `usage_start_time` so the scan is
    bounded by the window rather than the table's whole history.
    """
    validate_table(table)
    return f"""
        SELECT
          service.description AS service,
          SUM(cost) AS cost,
          SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
          MIN(usage_start_time) AS period_start,
          MAX(usage_end_time) AS period_end,
          ANY_VALUE(currency) AS currency
        FROM `{table}`
        WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)
        GROUP BY service
        ORDER BY cost DESC
    """


def summarise(rows: list[dict], *, table: str, days: int, observed_at: str) -> dict:
    """Turns the query result into the snapshot the console renders."""
    by_service = [
        {
            "service": row.get("service"),
            "cost": round(float(row.get("cost") or 0), 6),
            "credits": round(float(row.get("credits") or 0), 6),
            "net": round(float(row.get("cost") or 0) + float(row.get("credits") or 0), 6),
        }
        for row in rows
    ]
    gross = round(sum(item["cost"] for item in by_service), 6)
    credits = round(sum(item["credits"] for item in by_service), 6)
    return {
        # `amount` is NET — what the account is actually charged. The
        # gross figure is kept beside it rather than replaced, because a
        # large credit is worth seeing and a net figure alone hides it.
        "amount": round(gross + credits, 6),
        "gross": gross,
        "credits": credits,
        "currency": next((row.get("currency") for row in rows if row.get("currency")), "USD"),
        "by_service": by_service,
        "days": days,
        "period_start": str(min((row.get("period_start") for row in rows if row.get("period_start")), default="")),
        "period_end": str(max((row.get("period_end") for row in rows if row.get("period_end")), default="")),
        "source_table": table,
        "observed_at": observed_at,
        "row_count": len(rows),
    }


def snapshot(days: int = 7, table: str | None = None) -> dict:
    from tools.bigquery_tools import get_client as bq_client
    from tools.firestore_client import get_client

    table = validate_table(table or os.environ.get("CLOUD_BILLING_EXPORT_TABLE", ""))
    rows = [dict(row) for row in bq_client().query(build_query(table, days)).result()]
    record = summarise(rows, table=table, days=days, observed_at=_now().isoformat())
    get_client().collection(SNAPSHOT_COLLECTION).document(SNAPSHOT_DOCUMENT).set(record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="Window to summarise. Default 7.")
    parser.add_argument("--table", help="Overrides CLOUD_BILLING_EXPORT_TABLE.")
    parser.add_argument(
        "--dataset",
        help=(
            "project.dataset holding the export; the table is found in it. "
            "Defaults to CLOUD_BILLING_EXPORT_DATASET."
        ),
    )
    args = parser.parse_args(argv)

    table = args.table or os.environ.get("CLOUD_BILLING_EXPORT_TABLE")
    dataset = args.dataset or os.environ.get("CLOUD_BILLING_EXPORT_DATASET")
    if not table and dataset:
        try:
            table = discover_table(dataset)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"found export table: {table}")
    if not table:
        print(
            "Neither CLOUD_BILLING_EXPORT_TABLE nor CLOUD_BILLING_EXPORT_DATASET is "
            "set. Enable Cloud Billing export to BigQuery (Billing -> Billing export "
            "-> BigQuery export -> Standard usage cost) against a US or EU "
            "multi-region dataset, then set CLOUD_BILLING_EXPORT_DATASET to it and "
            "this will find the table itself. See this module's docstring.",
            file=sys.stderr,
        )
        return 1

    try:
        record = snapshot(days=args.days, table=table)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not record["row_count"]:
        # Not an error. A newly enabled export genuinely has no rows yet,
        # and saying so beats writing a confident 0.00.
        print(
            f"No billed usage in the last {args.days} day(s) in {table}. "
            f"A newly enabled export has no backfill; give it a day."
        )
        return 0

    print(f"{record['currency']} {record['amount']:.2f} net over {record['days']} day(s)")
    print(f"  gross {record['gross']:.2f}, credits {record['credits']:.2f}")
    for item in record["by_service"][:8]:
        print(f"  {item['service']:<34} {item['net']:>10.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
