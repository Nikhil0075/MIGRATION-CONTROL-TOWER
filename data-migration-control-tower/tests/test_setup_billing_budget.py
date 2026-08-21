"""Tests for infrastructure/setup_billing_budget.py's pure validation
logic (Deploy & Harden Phase 1d).

google-cloud-billing-budgets is an optional, lazily-imported dependency
(same Rung-2 pattern as secret_resolver.py's Secret Manager import) —
these tests exercise BudgetSpec's validation and threshold math, which
needs neither that package nor a live billing account, matching the
"pure function, no live GCP call" split the module's own docstring
describes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "infrastructure"))

from infrastructure.setup_billing_budget import (  # noqa: E402
    BudgetSpec,
    _parse_thresholds,
    main,
)


def _spec(**overrides) -> BudgetSpec:
    defaults = dict(
        billing_account="012089-52BECE-777B6D",
        display_name="test-budget",
        amount=28440.0,
        currency="INR",
        thresholds=(0.5, 0.9, 1.0),
        pubsub_topic="billing-budget-alerts",
    )
    defaults.update(overrides)
    return BudgetSpec(**defaults)


def test_a_well_formed_spec_is_accepted():
    spec = _spec()
    assert spec.currency == "INR"


def test_a_non_positive_amount_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        _spec(amount=0)
    with pytest.raises(ValueError, match="positive"):
        _spec(amount=-50)


@pytest.mark.parametrize("currency", ["", "US", "USDD", "12A", "usd1"])
def test_a_malformed_currency_code_is_rejected(currency):
    with pytest.raises(ValueError, match="ISO 4217"):
        _spec(currency=currency)


def test_a_well_formed_currency_code_is_accepted_case_sensitively_as_given():
    # main() upper()s the CLI arg before construction; BudgetSpec itself
    # just validates shape, not case, since a caller may already have a
    # normalized value.
    assert _spec(currency="inr").currency == "inr"


@pytest.mark.parametrize("thresholds", [(0.0,), (1.5,), (-0.1,), ()])
def test_thresholds_outside_zero_to_one_are_rejected(thresholds):
    with pytest.raises(ValueError):
        _spec(thresholds=thresholds)


def test_threshold_amounts_are_sorted_and_computed_from_the_total():
    spec = _spec(amount=1000.0, thresholds=(1.0, 0.5, 0.9))
    assert spec.threshold_amounts() == [(0.5, 500.0), (0.9, 900.0), (1.0, 1000.0)]


def test_parse_thresholds_splits_and_converts():
    assert _parse_thresholds("0.5,0.9,1.0") == (0.5, 0.9, 1.0)
    assert _parse_thresholds(" 0.5 , 0.9 ") == (0.5, 0.9)


def test_main_refuses_a_bad_amount_without_prompting(capsys):
    exit_code = main([
        "--billing-account", "012089-52BECE-777B6D",
        "--amount", "-5",
        "--currency", "INR",
        "--yes",
    ])
    assert exit_code == 1
    assert "ERROR" in capsys.readouterr().err


def test_main_aborts_when_the_confirmation_prompt_is_declined(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    exit_code = main([
        "--billing-account", "012089-52BECE-777B6D",
        "--amount", "28440",
        "--currency", "INR",
    ])
    assert exit_code == 1
    assert "aborted" in capsys.readouterr().out
