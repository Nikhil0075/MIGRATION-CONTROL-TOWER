"""Tests for evaluation/estimate_ladder_cost.py (Deploy & Harden
Phase 4c) — pure cost-math functions, no live GCP needed (the data-plane
scenario's --sample-run-id lookup is the one live-Firestore path, tested
separately with the sample explicitly passed instead)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evaluation.estimate_ladder_cost import (  # noqa: E402
    estimate_control_plane,
    estimate_data_plane,
    estimate_operational_load,
    load_infra_price_book,
    main,
)

BOOK = {
    "currency": "USD",
    "rates": {
        "firestore_write": {"price": 0.18},
        "cloud_run_cpu_second": {"price": 0.000024},
        "cloud_run_memory_gib_second": {"price": 0.0000025},
    },
}


def test_control_plane_cost_does_not_scale_with_tier():
    """The whole point: 1k, 5k, and 20k must cost the SAME, since the
    harness's Firestore write count is fixed regardless of --count."""
    small = estimate_control_plane(1_000, book=BOOK)
    large = estimate_control_plane(20_000, book=BOOK)
    assert small["total"] == large["total"]
    assert small["scales_with_tier"] is False


def test_control_plane_touches_no_bigquery_or_vertex():
    estimate = estimate_control_plane(20_000, book=BOOK)
    assert estimate["line_items"]["bigquery"]["amount"] == 0.0
    assert estimate["line_items"]["vertex_ai"]["amount"] == 0.0


def test_control_plane_cost_is_near_zero():
    estimate = estimate_control_plane(20_000, book=BOOK)
    assert 0 < estimate["total"] < 0.01  # genuinely tiny, not literally zero (real Firestore writes happen)


def test_data_plane_cost_scales_with_rows():
    small = estimate_data_plane(1_000, price_book={"currency": "USD", "rates": {"bigquery_query": {"price": 6.25}}})
    large = estimate_data_plane(1_000_000, price_book={"currency": "USD", "rates": {"bigquery_query": {"price": 6.25}}})
    assert large["total"] > small["total"]
    assert large["scales_with_tier"] is True


def test_data_plane_flags_when_using_the_generic_assumption_vs_a_measured_sample():
    unmeasured = estimate_data_plane(1_000, price_book={"currency": "USD", "rates": {"bigquery_query": {"price": 6.25}}})
    assert unmeasured["measured_basis"] is False
    assert "GENERIC" in unmeasured["line_items"]["bigquery_query"]["note"]

    measured = estimate_data_plane(
        1_000, sample_bytes_per_row=500.0, price_book={"currency": "USD", "rates": {"bigquery_query": {"price": 6.25}}}
    )
    assert measured["measured_basis"] is True
    assert "measured sample" in measured["line_items"]["bigquery_query"]["note"]


def test_data_plane_batch_loads_are_free():
    estimate = estimate_data_plane(1_000, price_book={"currency": "USD", "rates": {"bigquery_query": {"price": 6.25}}})
    assert estimate["line_items"]["bigquery_load"]["amount"] == 0.0


def test_operational_load_scales_with_concurrency_and_duration():
    low = estimate_operational_load(1, 5.0, book=BOOK)
    high = estimate_operational_load(20, 30.0, book=BOOK)
    assert high["total"] > low["total"]
    assert high["scales_with_tier"] is True


def test_operational_load_is_flagged_as_assumption_based():
    estimate = estimate_operational_load(5, 10.0, book=BOOK)
    assert estimate["measured_basis"] is False


def test_the_committed_infra_price_book_is_valid_and_dated():
    import datetime as dt

    book = load_infra_price_book()
    assert book["currency"] and book["region"] and book["basis"]
    dt.date.fromisoformat(book["effective_date"])
    assert book["sources"]["cloud_run"].startswith("https://")
    assert book["rates"]["firestore_write"]["price"] > 0


# -- CLI validation (main()) -------------------------------------------------


def test_main_requires_tier_for_control_plane(capsys):
    exit_code = main(["--scenario", "control-plane", "--yes"])
    assert exit_code == 1
    assert "tier" in capsys.readouterr().err


def test_main_requires_rows_for_data_plane(capsys):
    exit_code = main(["--scenario", "data-plane", "--yes"])
    assert exit_code == 1
    assert "rows" in capsys.readouterr().err


def test_main_requires_concurrency_and_duration_for_operational_load(capsys):
    exit_code = main(["--scenario", "operational-load", "--yes"])
    assert exit_code == 1
    assert "concurrent-runs" in capsys.readouterr().err


def test_main_prints_the_estimate_and_succeeds_with_yes(capsys):
    exit_code = main(["--scenario", "control-plane", "--tier", "5000", "--yes"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "control-plane" in out
    assert "TOTAL" in out


def test_main_aborts_when_the_confirmation_prompt_is_declined(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    exit_code = main(["--scenario", "control-plane", "--tier", "1000"])
    assert exit_code == 1
    assert "Not confirmed" in capsys.readouterr().out
