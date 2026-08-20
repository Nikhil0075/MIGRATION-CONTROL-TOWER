"""Cost: measured usage priced from a dated card, and real spend beside it.

Two figures that must not be conflated, which is why they are two
figures. Estimated cost is usage this system measured, priced from
contracts/price_book.json — list prices, no discounts, no free tier. It
is an upper bound on list. Actual cost is what Google's billing export
says was charged, which knows about committed-use discounts and credits
and everything else this system cannot see.

The tests here are mostly about what happens when something cannot be
priced or cannot be measured, because a cost dashboard that quietly
rounds those to zero is worse than one that admits the gap.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from frontend.api_v1 import _actual_cost, _estimated_cost, _is_stale
from tools import billing_export
from tools.usage_meter import (
    attributed_to,
    attributes_usage,
    current_run_id,
    load_price_book,
    price_usage,
)

BOOK = {
    "version": "test",
    "effective_date": "2026-01-01",
    "currency": "USD",
    "basis": "published list price",
    "region": "us-central1",
    "rates": {
        "model": {"models": {"default": {"input": 1.0, "output": 2.0},
                             "known-model": {"input": 0.5, "output": 1.5}}},
        "bigquery_query": {"price": 6.25},
        "bigquery_load": {"price": 0.0},
    },
}


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_tokens_are_priced_per_million_not_per_token():
    priced = price_usage(
        [{"kind": "model", "model": "known-model", "input_tokens": 1_000_000, "output_tokens": 0}],
        BOOK,
    )
    assert priced["amount"] == pytest.approx(0.5)


def test_input_and_output_tokens_are_priced_differently():
    """They are billed differently, and averaging them would be a guess."""
    priced = price_usage(
        [{"kind": "model", "model": "known-model", "input_tokens": 0, "output_tokens": 1_000_000}],
        BOOK,
    )
    assert priced["amount"] == pytest.approx(1.5)


def test_bigquery_is_priced_on_bytes_billed_per_tebibyte():
    priced = price_usage(
        [{"kind": "bigquery", "job_kind": "query", "bytes_billed": 1024 ** 4}], BOOK
    )
    assert priced["amount"] == pytest.approx(6.25)


def test_a_free_operation_is_priced_at_zero_and_still_counted():
    """Zero because the rate card says zero, not because it was skipped.

    A batch load is genuinely free. Recording it at a declared zero rate
    lets a reader tell a free operation from one nobody measured.
    """
    priced = price_usage(
        [{"kind": "bigquery", "job_kind": "load", "bytes_billed": 5 * 1024 ** 3}], BOOK
    )
    assert priced["amount"] == 0.0
    assert priced["usage"]["bigquery_jobs"] == 1
    assert priced["unpriced"] == []


def test_an_unknown_model_is_priced_at_the_default_and_flagged():
    """Neither dropped nor silently trusted.

    Costing an unlisted model at zero produces a total that is
    confidently wrong. Refusing to price it at all loses real spend. It
    is priced at the fallback, and the caveat travels with the number.
    """
    priced = price_usage(
        [{"kind": "model", "model": "brand-new-model", "input_tokens": 1_000_000,
          "output_tokens": 0}],
        BOOK,
    )
    assert priced["amount"] == pytest.approx(1.0)
    assert any("brand-new-model" in item for item in priced["unpriced"])


def test_a_job_that_reported_no_bytes_is_not_counted_as_free():
    priced = price_usage(
        [{"kind": "bigquery", "job_kind": "query", "bytes_billed": None}], BOOK
    )
    assert priced["amount"] == 0.0
    assert any("no bytes_billed" in item for item in priced["unpriced"])


def test_the_price_book_version_travels_with_the_figure():
    """Otherwise the number has no provenance and cannot go stale visibly."""
    priced = price_usage([{"kind": "model", "model": "known-model",
                           "input_tokens": 10, "output_tokens": 10}], BOOK)
    assert priced["price_book_effective_date"] == "2026-01-01"
    assert priced["basis"] == "published list price"


def test_the_committed_price_book_is_valid_and_dated():
    book = load_price_book()
    assert book["currency"] and book["region"] and book["basis"]
    dt.date.fromisoformat(book["effective_date"])
    # A fallback rate must exist, or the first unlisted model silently
    # costs nothing.
    assert book["rates"]["model"]["models"]["default"]["input"] > 0
    assert book["sources"]["vertex_ai"].startswith("https://")


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_usage_outside_a_run_is_attributed_to_nothing_rather_than_guessed():
    assert current_run_id() is None


def test_the_decorator_releases_the_scope_even_when_the_work_fails():
    """The failure path is exactly when someone asks what it cost.

    A `with` block inside each body would do this too; a decorator makes
    it uniform across the three entry points and impossible to forget on
    an early return.
    """

    @attributes_usage
    def explode(run_id):
        assert current_run_id() == run_id
        raise RuntimeError("reconciliation failed")

    with pytest.raises(RuntimeError):
        explode("run-1")
    assert current_run_id() is None


def test_nested_attribution_restores_the_outer_run():
    with attributed_to("outer"):
        with attributed_to("inner"):
            assert current_run_id() == "inner"
        assert current_run_id() == "outer"


# ---------------------------------------------------------------------------
# The /overview panels
# ---------------------------------------------------------------------------


def test_no_recorded_usage_reads_as_unconfigured_not_as_free():
    result = _estimated_cost([])
    assert result["status"] == "not_configured"
    assert result["value"] is None


def test_recorded_usage_reports_an_amount_and_names_the_card():
    result = _estimated_cost(
        [
            {"kind": "model", "model": "gemini-2.5-flash", "input_tokens": 1000,
             "output_tokens": 100, "at": "2026-08-20T10:00:00Z"},
            {"kind": "bigquery", "job_kind": "query", "bytes_billed": 10 * 1024 ** 2,
             "at": "2026-08-20T11:00:00Z"},
        ]
    )
    assert result["status"] == "available"
    assert result["value"]["amount"] > 0
    assert "price book" in result["reason"]
    assert result["last_observed_at"] == "2026-08-20T11:00:00Z"


def test_actual_cost_without_the_export_says_what_to_configure(monkeypatch):
    monkeypatch.delenv("CLOUD_BILLING_EXPORT_TABLE", raising=False)
    result = _actual_cost()
    assert result["status"] == "not_configured"
    assert "CLOUD_BILLING_EXPORT_TABLE" in result["reason"]


def test_a_snapshot_older_than_a_day_and_a_half_is_stale_not_current():
    fresh = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4)).isoformat()
    assert _is_stale(fresh) is False
    assert _is_stale(old) is True
    # Unparseable or absent is stale, never current: an unknown age is
    # not evidence of freshness.
    assert _is_stale(None) is True
    assert _is_stale("last tuesday") is True


# ---------------------------------------------------------------------------
# The billing export
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        "proj.dataset.tbl; DROP TABLE x",
        "proj.dataset.tbl` UNION SELECT 1 --",
        "not-a-table",
        "proj..tbl",
        "",
    ],
)
def test_a_table_name_that_is_not_a_table_name_is_refused(table):
    """The one place operator configuration reaches SQL.

    A table name cannot be a bound parameter — it is not a value in SQL —
    so it is interpolated, and therefore validated. "It comes from our
    own environment" is the assumption injection defects are built on.
    """
    with pytest.raises(ValueError):
        billing_export.validate_table(table)


def test_a_well_formed_table_is_accepted():
    assert billing_export.validate_table("my-project.billing.gcp_billing_export_v1_ABC_DEF_123")


def test_credits_are_applied_rather_than_ignored():
    """A total that counted only gross cost would overstate the bill.

    Free-tier and committed-use credits arrive as negative amounts on the
    same rows. Reporting gross as "actual cost" is the specific way a
    cost dashboard loses an operator's trust.
    """
    record = billing_export.summarise(
        [
            {"service": "BigQuery", "cost": 10.0, "credits": -3.0, "currency": "USD",
             "period_start": "2026-08-01", "period_end": "2026-08-07"},
            {"service": "Vertex AI", "cost": 5.0, "credits": 0.0, "currency": "USD",
             "period_start": "2026-08-01", "period_end": "2026-08-07"},
        ],
        table="p.d.t",
        days=7,
        observed_at="2026-08-20T00:00:00+00:00",
    )
    assert record["gross"] == 15.0
    assert record["credits"] == -3.0
    assert record["amount"] == 12.0
    # Gross is kept beside net, not replaced by it: a large credit is
    # worth seeing, and a net figure alone hides it.
    assert record["by_service"][0]["net"] == 7.0


def test_an_export_with_no_rows_yet_is_not_reported_as_zero_spend():
    """A newly enabled export has no backfill.

    Writing a confident 0.00 for a window that simply has no data yet is
    the same class of error as summing unmeasured tables to zero bytes.
    """
    record = billing_export.summarise([], table="p.d.t", days=7, observed_at="2026-08-20T00:00:00+00:00")
    assert record["row_count"] == 0
    assert record["amount"] == 0
    assert record["period_end"] == ""


def test_the_query_is_bounded_by_the_window_it_claims_to_cover():
    query = billing_export.build_query("p.d.t", 14)
    assert "INTERVAL 14 DAY" in query
    assert "`p.d.t`" in query
    # Grouped by service so a total can be explained rather than just asserted.
    assert "GROUP BY service" in query


def test_the_day_count_cannot_smuggle_sql_through_the_interval():
    query = billing_export.build_query("p.d.t", int("30"))
    assert "INTERVAL 30 DAY" in query
    with pytest.raises((ValueError, TypeError)):
        billing_export.build_query("p.d.t", "7 DAY) OR TRUE --")  # type: ignore[arg-type]


def test_the_module_docstring_tells_an_operator_how_to_turn_it_on():
    # The setting is on the billing ACCOUNT, not the project, so it
    # cannot be provisioned by infrastructure/gcp_setup.sh and has to be
    # written down somewhere a person will find it.
    doc = billing_export.__doc__ or ""
    assert "Billing export" in doc
    assert "CLOUD_BILLING_EXPORT_TABLE" in doc
    assert "backfill" in doc


# ---------------------------------------------------------------------------
# Finding the export table
# ---------------------------------------------------------------------------


class _FakeTable:
    def __init__(self, table_id: str):
        self.table_id = table_id


def _bq_listing(tables: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.bigquery_tools as bq

    monkeypatch.setattr(
        bq,
        "get_client",
        lambda: type("C", (), {"list_tables": lambda _s, _d: [_FakeTable(t) for t in tables]})(),
    )


def test_the_export_table_is_found_without_transcribing_the_account_id(monkeypatch):
    """The suffix is the billing account id, which nobody has memorised.

    The id here is deliberately fake. The first draft of this file used
    the real one, copied from the project while checking the dataset
    region — a billing account id is not a credential, but it identifies
    a real account and has no business being committed as filler when
    any string would do.
    """
    _bq_listing(["gcp_billing_export_v1_0X0X0X_1Y1Y1Y_2Z2Z2Z"], monkeypatch)
    assert (
        billing_export.discover_table("proj.billing_export")
        == "proj.billing_export.gcp_billing_export_v1_0X0X0X_1Y1Y1Y_2Z2Z2Z"
    )


def test_a_missing_table_is_reported_as_a_wait_not_a_fault(monkeypatch):
    """The common experience, and the one worth getting right.

    The export creates its table only when the first daily batch lands —
    hours later, with no backfill. Someone who enabled it five minutes
    ago and finds an empty dataset will otherwise conclude the setup
    failed and start changing things that were correct.
    """
    _bq_listing([], monkeypatch)
    with pytest.raises(LookupError) as excinfo:
        billing_export.discover_table("proj.billing_export")
    message = str(excinfo.value)
    assert "backfill" in message
    assert "wait, not a fault" in message


def test_several_export_tables_are_refused_rather_than_guessed(monkeypatch):
    """A project can carry detailed and pricing exports alongside standard.

    Picking the first would silently report a different set of numbers
    than the operator believes they are looking at.
    """
    _bq_listing(
        [
            "gcp_billing_export_v1_0X0X0X_1Y1Y1Y_2Z2Z2Z",
            "gcp_billing_export_v1_999999_AAAAAA_BBBBBB",
        ],
        monkeypatch,
    )
    with pytest.raises(LookupError, match="Several export tables"):
        billing_export.discover_table("proj.billing_export")


def test_unrelated_tables_in_the_dataset_are_ignored(monkeypatch):
    _bq_listing(
        ["scratch", "gcp_billing_export_v1_0X0X0X_1Y1Y1Y_2Z2Z2Z", "notes"], monkeypatch
    )
    assert billing_export.discover_table("p.d").endswith("0X0X0X_1Y1Y1Y_2Z2Z2Z")


def test_a_discovered_table_still_has_to_pass_validation(monkeypatch):
    """Discovery is convenience; it is not a reason to skip the check."""
    _bq_listing(["gcp_billing_export_v1_0X0X0X_1Y1Y1Y_2Z2Z2Z"], monkeypatch)
    found = billing_export.discover_table("proj.billing_export")
    assert billing_export.validate_table(found) == found


def test_neither_variable_set_names_both_ways_to_configure_it(capsys):
    import os

    for key in ("CLOUD_BILLING_EXPORT_TABLE", "CLOUD_BILLING_EXPORT_DATASET"):
        os.environ.pop(key, None)
    assert billing_export.main([]) == 1
    err = capsys.readouterr().err
    assert "CLOUD_BILLING_EXPORT_DATASET" in err
    # And the region trap, which is the thing that actually blocks people.
    assert "multi-region" in err
