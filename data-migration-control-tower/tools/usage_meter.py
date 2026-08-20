"""Records what a run actually consumed, and prices it from a dated card.

The Overview panel said "Token and priced job usage are not yet
recorded", which was accurate: nothing recorded them. This module is the
recording half and the pricing half, kept deliberately separate.

**Usage is measured.** Token counts come from the model's own
`usage_metadata`; bytes come from the BigQuery job's own
`total_bytes_billed`. Neither is estimated from prompt length or row
counts, because a cost figure derived from a guess is the kind of
invented evidence this project exists not to produce.

**Price is declared**, in contracts/price_book.json, with an effective
date and a source URL. The console shows the measured usage, the rates
applied and the card's date, so the number can be recomputed or
rejected. A rate card is the one part of this that cannot be measured
from inside the system, so it is stated rather than smuggled in as a
constant.

Recording must never break the thing it is measuring. Every entry point
here swallows its own failures: an agent that completed its work has
completed it, whether or not the meter managed to write a row.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PRICE_BOOK_PATH = REPO_ROOT / "contracts" / "price_book.json"

#: Subcollection under migration_runs/{run_id}. Run-scoped because cost
#: belongs to the work that caused it — a fleet total is a sum over runs,
#: not a separate global counter that can drift away from them.
USAGE_COLLECTION = "usage_events"

_BYTES_PER_TIB = 1024 ** 4
_TOKENS_PER_UNIT = 1_000_000


#: The run that work on this thread belongs to. A context variable rather
#: than a parameter because tools/bigquery_tools.py is called from four
#: agents through helpers whose signatures say nothing about runs
#: (`get_row_count(table)`), and threading run_id through all of them to
#: satisfy an accounting concern would put cost bookkeeping into the
#: signature of every data-plane call. Unset means "not attributed",
#: which the meter records as nothing rather than as a guess.
_ATTRIBUTED_RUN: ContextVar[str | None] = ContextVar("attributed_run", default=None)


@contextmanager
def attributed_to(run_id: str | None):
    """Attributes usage recorded inside this block to `run_id`."""
    token = _ATTRIBUTED_RUN.set(run_id)
    try:
        yield
    finally:
        _ATTRIBUTED_RUN.reset(token)


def current_run_id() -> str | None:
    return _ATTRIBUTED_RUN.get()


def attributes_usage(func):
    """Attributes everything the wrapped function meters to its run_id.

    A decorator rather than a `with` block inside each body, because the
    scope has to be released on the failure path too — and a reconciliation
    that raises is exactly when someone wants to know what it cost. Applied
    to the run-scoped entry points that reach BigQuery; `run_id` is their
    first argument, which is what makes this uniform.
    """

    @functools.wraps(func)
    def wrapper(run_id, *args, **kwargs):
        with attributed_to(run_id):
            return func(run_id, *args, **kwargs)

    return wrapper


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_price_book(path: Path | None = None) -> dict:
    return json.loads((path or PRICE_BOOK_PATH).read_text(encoding="utf-8"))


def record_model_usage(
    run_id: str | None,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    purpose: str,
    thinking_tokens: int = 0,
    cached_tokens: int = 0,
    request_id: str | None = None,
) -> dict | None:
    """Records one model call's token usage against a run.

    `purpose` names what the call was for — the recovery narrative, the
    documentation extractor — so a cost can be attributed to a feature
    rather than appearing as an undifferentiated total.
    """
    return _write(
        run_id,
        {
            "kind": "model",
            "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "thinking_tokens": int(thinking_tokens),
            "cached_tokens": int(cached_tokens),
            "purpose": purpose,
            "request_id": request_id,
        },
    )


def record_bigquery_usage(
    run_id: str | None,
    *,
    job_kind: str,
    bytes_billed: int | None,
    bytes_processed: int | None = None,
    purpose: str = "",
) -> dict | None:
    """Records one BigQuery job's billed bytes against a run.

    `bytes_billed`, not `bytes_processed`: BigQuery bills a 10 MB minimum
    per query, so a thousand tiny reconciliation counts cost far more
    than the bytes they touched. Processed bytes are kept alongside
    because the gap between the two is itself worth seeing.
    """
    return _write(
        run_id,
        {
            "kind": "bigquery",
            "job_kind": job_kind,
            "bytes_billed": int(bytes_billed) if bytes_billed is not None else None,
            "bytes_processed": int(bytes_processed) if bytes_processed is not None else None,
            "purpose": purpose,
        },
    )


def _write(run_id: str | None, event: dict) -> dict | None:
    if not run_id:
        return None
    record = {**event, "at": _now()}
    try:
        from tools.firestore_client import get_client

        get_client().collection("migration_runs").document(run_id).collection(
            USAGE_COLLECTION
        ).add(record)
    except Exception:  # noqa: BLE001
        # Deliberately swallowed. The meter observes work; it does not
        # get to fail it. An agent that produced a correct migration
        # produced one whether or not this row was written, and raising
        # here would turn an accounting problem into an outage.
        return None
    return record


def price_usage(events: list[dict], price_book: dict | None = None) -> dict:
    """Prices measured usage, and says what it could not price.

    Returns the total, a per-kind breakdown, the usage totals the price
    was applied to, and `unpriced` — events whose rate is not in the
    card. Unpriced work is reported rather than dropped: silently costing
    an unknown model at zero produces a total that is confidently wrong,
    which is worse than one that is visibly incomplete.
    """
    book = price_book or load_price_book()
    rates = book.get("rates", {})
    model_rates = rates.get("model", {}).get("models", {})

    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cached_tokens": 0,
        "bytes_billed": 0,
        "model_calls": 0,
        "bigquery_jobs": 0,
    }
    by_kind: dict[str, float] = {}
    unpriced: list[str] = []

    for event in events:
        kind = event.get("kind")
        if kind == "model":
            model = str(event.get("model") or "")
            rate = model_rates.get(model) or model_rates.get("default")
            totals["model_calls"] += 1
            totals["input_tokens"] += int(event.get("input_tokens") or 0)
            totals["output_tokens"] += int(event.get("output_tokens") or 0)
            totals["thinking_tokens"] += int(event.get("thinking_tokens") or 0)
            totals["cached_tokens"] += int(event.get("cached_tokens") or 0)
            if not rate:
                unpriced.append(f"model:{model or 'unknown'}")
                continue
            if model and model not in model_rates:
                # Priced at the fallback, and said so. The number is
                # usable; the caveat travels with it.
                unpriced.append(f"model:{model} (priced at default rate)")
            cost = (
                int(event.get("input_tokens") or 0) * rate["input"]
                + (int(event.get("output_tokens") or 0) + int(event.get("thinking_tokens") or 0)) * rate["output"]
            ) / _TOKENS_PER_UNIT
            by_kind["model"] = by_kind.get("model", 0.0) + cost
        elif kind == "bigquery":
            job_kind = str(event.get("job_kind") or "query")
            rate_key = f"bigquery_{job_kind}"
            rate = rates.get(rate_key) or rates.get("bigquery_query")
            billed = event.get("bytes_billed")
            totals["bigquery_jobs"] += 1
            if billed is None:
                unpriced.append(f"bigquery:{job_kind} (no bytes_billed reported)")
                continue
            totals["bytes_billed"] += int(billed)
            if not rate:
                unpriced.append(f"bigquery:{job_kind}")
                continue
            cost = int(billed) * float(rate["price"]) / _BYTES_PER_TIB
            by_kind[rate_key] = by_kind.get(rate_key, 0.0) + cost
        else:
            unpriced.append(f"unknown:{kind}")

    return {
        "amount": round(sum(by_kind.values()), 6),
        "currency": book.get("currency", "USD"),
        "by_kind": {key: round(value, 6) for key, value in by_kind.items()},
        "usage": totals,
        "unpriced": sorted(set(unpriced)),
        "price_book_version": book.get("version"),
        "price_book_effective_date": book.get("effective_date"),
        "basis": book.get("basis"),
        "region": book.get("region"),
    }


def extract_model_usage(response: Any) -> tuple[int, int] | None:
    """Reads token counts off a Vertex AI response, or gives up cleanly.

    The SDK exposes `usage_metadata` with `prompt_token_count` and
    `candidates_token_count`. Tolerant of it being absent — a fallback
    path may return a plain object, and the meter must not turn that into
    an exception in the middle of a recovery narrative.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_token_count", None)
    candidates = getattr(usage, "candidates_token_count", None)
    if prompt is None and candidates is None:
        return None
    return int(prompt or 0), int(candidates or 0)
