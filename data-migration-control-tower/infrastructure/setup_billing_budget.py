#!/usr/bin/env python
"""Creates (idempotently) a Cloud Billing Budget alert for this project's
billing account (Deploy & Harden Phase 1d, docs/adr's plan).

**A budget is an alert, not a spending cap.** This script does not stop
Cloud Run, Cloud SQL, or BigQuery from continuing to bill — it only makes
Cloud Billing publish a Pub/Sub notification when spend crosses each
threshold. Treat it as an early-warning tripwire alongside
tools/bigquery_tools.py's per-query/per-run byte caps (a different,
narrower control), not a replacement for either. See docs/GOVERNANCE.md.

Deliberately does not guess a currency or an amount — the billing account
this project is linked to is INR-denominated (confirmed via Cloud
Console, not assumed), and a script inventing a number in the wrong
currency would create a budget that alerts at the wrong point or never
fires. Both --amount and --currency are required.

Usage (from repo root, idempotent — re-running updates the existing
budget of the same --display-name rather than duplicating it):

    python infrastructure/setup_billing_budget.py \\
        --billing-account 012089-52BECE-777B6D \\
        --amount 28440 --currency INR \\
        --thresholds 0.5,0.9,1.0

Requires `google-cloud-billing-budgets` (see requirements.txt) and IAM
permission `billing.budgets.create`/`update` on the billing account —
distinct from project-level IAM, and not something
infrastructure/gcp_setup.sh's project-scoped roles cover. If that
permission is missing, this script prints a clear message and exits
nonzero rather than silently doing nothing (a budget that was never
actually created is the specific failure mode this project's own
degradation-ladder discipline says must be visible, not swallowed).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass


DEFAULT_DISPLAY_NAME = "control-tower-trial-credit"
DEFAULT_PUBSUB_TOPIC = "billing-budget-alerts"
DEFAULT_THRESHOLDS = (0.5, 0.9, 1.0)


@dataclass(frozen=True)
class BudgetSpec:
    """Pure description of the budget to create/update — separated from
    the protobuf/API construction below so the validation and threshold
    math are unit-testable without the google-cloud-billing-budgets
    package installed (it's an optional dependency, Rung-2 pattern)."""

    billing_account: str
    display_name: str
    amount: float
    currency: str
    thresholds: tuple[float, ...]
    pubsub_topic: str
    project_id: str | None = None

    def __post_init__(self):
        if self.amount <= 0:
            raise ValueError(f"--amount must be positive, got {self.amount!r}")
        if not self.currency or len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError(
                f"--currency must be a 3-letter ISO 4217 code (e.g. INR, USD), got {self.currency!r}"
            )
        for t in self.thresholds:
            if not 0 < t <= 1.0:
                raise ValueError(f"each threshold must be in (0, 1.0], got {t!r}")
        if not self.thresholds:
            raise ValueError("at least one threshold is required")

    def threshold_amounts(self) -> list[tuple[float, float]]:
        """Returns [(fraction, absolute_amount), ...] for display/logging —
        the actual API call sends fractions; this is what a human reads
        to confirm the numbers before they're applied."""
        return [(t, round(self.amount * t, 2)) for t in sorted(self.thresholds)]


def _parse_thresholds(raw: str) -> tuple[float, ...]:
    try:
        return tuple(float(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"could not parse thresholds {raw!r}: {exc}") from exc


def _ensure_pubsub_topic(topic_name: str, project_id: str) -> str:
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import pubsub_v1

    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(project_id, topic_name)
    try:
        publisher.create_topic(request={"name": topic_path})
        print(f"[billing-budget] created Pub/Sub topic {topic_path}")
    except AlreadyExists:
        print(f"[billing-budget] Pub/Sub topic {topic_path} already exists")
    return topic_path


def _find_existing_budget(client, billing_account: str, display_name: str):
    parent = f"billingAccounts/{billing_account}"
    for budget in client.list_budgets(request={"parent": parent}):
        if budget.display_name == display_name:
            return budget
    return None


def _build_budget_request(spec: BudgetSpec, topic_path: str):
    """Constructs the protobuf request. Isolated from apply() so a test
    could exercise this given a fake `billing_budgets` module, though the
    package not being installed in this environment means only
    BudgetSpec's own validation is exercised by tests today — the
    protobuf shape itself should be verified live before relying on it
    (see this file's docstring on required IAM)."""
    from google.cloud import billing_budgets_v1

    amount = billing_budgets_v1.types.Money(
        currency_code=spec.currency,
        units=int(spec.amount),
        nanos=int(round((spec.amount - int(spec.amount)) * 1e9)),
    )
    threshold_rules = [
        billing_budgets_v1.types.ThresholdRule(threshold_percent=t) for t in sorted(spec.thresholds)
    ]
    return billing_budgets_v1.types.Budget(
        display_name=spec.display_name,
        budget_filter=billing_budgets_v1.types.Filter(
            projects=[f"projects/{spec.project_id}"] if spec.project_id else None,
        ),
        amount=billing_budgets_v1.types.BudgetAmount(specified_amount=amount),
        threshold_rules=threshold_rules,
        notifications_rule=billing_budgets_v1.types.NotificationsRule(
            pubsub_topic=topic_path,
            monitoring_notification_channels=[],
        ),
    )


def apply(spec: BudgetSpec) -> None:
    from google.cloud import billing_budgets_v1

    print(f"[billing-budget] billing account: {spec.billing_account}")
    print(f"[billing-budget] amount: {spec.amount} {spec.currency}")
    print("[billing-budget] alert thresholds:")
    for fraction, absolute in spec.threshold_amounts():
        print(f"[billing-budget]   {fraction:.0%} -> {absolute} {spec.currency}")
    print(
        "[billing-budget] NOTE: this creates an ALERT, not a spending cap — nothing stops "
        "billing when a threshold is crossed. See docs/GOVERNANCE.md."
    )

    project_id = spec.project_id
    if project_id is None:
        import os

        project_id = os.environ.get("GCP_PROJECT_ID")

    topic_path = _ensure_pubsub_topic(spec.pubsub_topic, project_id) if project_id else None
    if topic_path is None:
        print(
            "[billing-budget] WARNING: no GCP_PROJECT_ID and no --project given — creating the "
            "budget with no Pub/Sub notification target. Set one to actually receive alerts."
        )

    client = billing_budgets_v1.BudgetServiceClient()
    existing = _find_existing_budget(client, spec.billing_account, spec.display_name)
    request_budget = _build_budget_request(spec, topic_path or "")

    if existing is not None:
        request_budget.name = existing.name
        client.update_budget(
            request={"budget": request_budget, "update_mask": {"paths": ["amount", "threshold_rules", "notifications_rule"]}}
        )
        print(f"[billing-budget] updated existing budget {existing.name!r}")
    else:
        created = client.create_budget(
            request={"parent": f"billingAccounts/{spec.billing_account}", "budget": request_budget}
        )
        print(f"[billing-budget] created budget {created.name!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--billing-account", required=True, help="e.g. 012089-52BECE-777B6D (no 'billingAccounts/' prefix)")
    parser.add_argument("--amount", required=True, type=float, help="Budget amount in --currency's units")
    parser.add_argument("--currency", required=True, help="ISO 4217 code matching the billing account's currency, e.g. INR")
    parser.add_argument("--display-name", default=DEFAULT_DISPLAY_NAME)
    parser.add_argument("--pubsub-topic", default=DEFAULT_PUBSUB_TOPIC)
    parser.add_argument("--project", default=None, help="Defaults to GCP_PROJECT_ID env var")
    parser.add_argument(
        "--thresholds", default="0.5,0.9,1.0", type=_parse_thresholds, help="Comma-separated fractions, e.g. 0.5,0.9,1.0"
    )
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (for non-interactive use)")
    args = parser.parse_args(argv)

    try:
        spec = BudgetSpec(
            billing_account=args.billing_account,
            display_name=args.display_name,
            amount=args.amount,
            currency=args.currency.upper(),
            thresholds=tuple(args.thresholds),
            pubsub_topic=args.pubsub_topic,
            project_id=args.project,
        )
    except ValueError as exc:
        print(f"[billing-budget] ERROR: {exc}", file=sys.stderr)
        return 1

    if not args.yes:
        print(f"[billing-budget] about to create/update a budget of {spec.amount} {spec.currency} "
              f"on billing account {spec.billing_account}.")
        confirm = input("[billing-budget] proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("[billing-budget] aborted, nothing changed.")
            return 1

    try:
        apply(spec)
    except ImportError:
        print(
            "[billing-budget] ERROR: google-cloud-billing-budgets is not installed. "
            "Run: pip install google-cloud-billing-budgets",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator, not swallowed
        print(f"[billing-budget] ERROR: {exc}", file=sys.stderr)
        print(
            "[billing-budget] Common cause: the account running this script lacks "
            "billing.budgets.create/update on this billing account — that's a "
            "billing-account-level IAM grant, separate from any project-level role "
            "infrastructure/gcp_setup.sh already applies.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
