"""Thin Pub/Sub helper (master doc §3.1 "Events" plane, §8.1 event topics).

Real GCP Pub/Sub — not an in-process queue standing in for it. Topics
are created by infrastructure/gcp_setup.sh; this module only publishes
to and pulls from them.

Uses synchronous pull (`subscriber.pull()`) rather than the streaming
callback API. That's a deliberate, documented simplification for a
demoable, finite orchestrator run (agents/orchestrator/run_orchestrator.py
polls a couple of times and exits) rather than a long-lived listener
process — see infrastructure/README.md's Rung ladder note for the
production shape (Cloud Run + Eventarc push subscriptions) this stands
in for today.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

from google.cloud import pubsub_v1


@lru_cache(maxsize=1)
def _project_id() -> str:
    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        raise RuntimeError("GCP_PROJECT_ID must be set to use tools/events.py")
    return project_id


@lru_cache(maxsize=1)
def _publisher() -> pubsub_v1.PublisherClient:
    return pubsub_v1.PublisherClient()


@lru_cache(maxsize=1)
def _subscriber() -> pubsub_v1.SubscriberClient:
    return pubsub_v1.SubscriberClient()


def publish(topic: str, payload: dict) -> str:
    """Publishes a JSON-encoded payload to `topic`; returns the message ID."""
    publisher = _publisher()
    topic_path = publisher.topic_path(_project_id(), topic)
    future = publisher.publish(topic_path, json.dumps(payload).encode("utf-8"))
    return future.result(timeout=30)


def pull(subscription: str, max_messages: int = 10, timeout: float = 5.0) -> list[dict]:
    """Synchronously pulls up to max_messages WITHOUT acknowledging them.

    Returns a list of decoded JSON payloads (empty list if nothing was
    waiting). Acking is the caller's responsibility via ack()/nack()
    below, and MUST happen only after the message has been fully and
    successfully handled — see this module's docstring for why (a real
    defect, found live: this function used to auto-ack before returning,
    so any handler failure downstream — an exception, a capacity HOLD —
    silently discarded the message with no Pub/Sub redelivery, leaving a
    run stuck with no recovery path).
    """
    from google.api_core.exceptions import DeadlineExceeded, RetryError

    subscriber = _subscriber()
    subscription_path = subscriber.subscription_path(_project_id(), subscription)
    try:
        response = subscriber.pull(
            request={"subscription": subscription_path, "max_messages": max_messages},
            timeout=timeout,
        )
    except (DeadlineExceeded, RetryError):
        # No messages arrived within `timeout` — normal, not an error.
        return []

    if not response.received_messages:
        return []

    decoded = []
    for msg in response.received_messages:
        payload = json.loads(msg.message.data.decode("utf-8"))
        # Day 10 (Appendix D S-12): carry the broker's own message_id so a
        # handler can dedup redelivery (Pub/Sub is at-least-once — the
        # same message can legitimately arrive twice). Prefixed with an
        # underscore so it never collides with a real payload field.
        payload["_pubsub_message_id"] = msg.message.message_id
        # Needed by ack()/nack() below — the caller decides when (or
        # whether) to acknowledge, this function no longer does.
        payload["_pubsub_ack_id"] = msg.ack_id
        decoded.append(payload)
    return decoded


def ack(subscription: str, ack_id: str) -> None:
    """Acknowledges one message. Call only after it has been fully and
    successfully handled — see pull()'s docstring."""
    subscriber = _subscriber()
    subscription_path = subscriber.subscription_path(_project_id(), subscription)
    subscriber.acknowledge(request={"subscription": subscription_path, "ack_ids": [ack_id]})


#: Pub/Sub refuses a single modifyAckDeadline above this.
MAX_ACK_DEADLINE_SECONDS = 600


def modify_ack_deadline(subscription: str, ack_id: str, seconds: int) -> None:
    """Resets one message's ack deadline to `seconds` from now.

    Two callers, opposite intents:
      0  — redeliver immediately (see nack below).
      >0 — extend the lease while a long handler is still working
           (tools/worker_supervisor.py's heartbeat).

    The extension exists because every subscription in
    infrastructure/gcp_setup.sh is created with --ack-deadline=60, while
    a real assessment or migration runs for minutes. A one-shot CLI
    worker hid that: the redelivered copy only reappeared on the next
    manual run. A looping consumer picks it straight back up and re-runs
    the work, so the lease has to be held deliberately.

    **It buys an hour, not forever.** Pub/Sub caps a message's total
    outstanding lifetime at roughly one hour no matter how often the
    deadline is extended. Work exceeding that WILL be redelivered and
    re-executed. That is survivable here — execute_migration truncates
    and reloads on its first batch, and transition_state raises on an
    illegal repeat rather than corrupting state — but it is a real
    ceiling, not a theoretical one.
    """
    if seconds < 0 or seconds > MAX_ACK_DEADLINE_SECONDS:
        raise ValueError(
            f"ack deadline must be between 0 and {MAX_ACK_DEADLINE_SECONDS} seconds, got {seconds}"
        )
    subscriber = _subscriber()
    subscription_path = subscriber.subscription_path(_project_id(), subscription)
    subscriber.modify_ack_deadline(
        request={
            "subscription": subscription_path,
            "ack_ids": [ack_id],
            "ack_deadline_seconds": seconds,
        }
    )


def nack(subscription: str, ack_id: str) -> None:
    """Immediately makes a message available for redelivery (resets its
    ack deadline to 0) instead of making Pub/Sub wait out the full
    default ack deadline. Call when a handler fails in a way that should
    be retried — e.g. a Wave Manager capacity HOLD (see
    agents/orchestrator/orchestrator.py's _consume() helper), which is
    exactly the scenario this function exists to make actually work."""
    modify_ack_deadline(subscription, ack_id, 0)


def warm_clients() -> None:
    """Builds the cached Pub/Sub and Firestore clients on the CALLING thread.

    The clients are @lru_cache(maxsize=1) singletons and lru_cache
    construction is not lock-protected, so two consumer threads entering
    _subscriber() together can each build a SubscriberClient — one is then
    discarded WITHOUT its gRPC transport being closed, leaking a channel.

    Call once from the main thread before starting any consumer. The unary
    RPCs themselves are thread-safe; only the lazy construction is not.
    """
    _project_id()
    _publisher()
    _subscriber()

    from tools.firestore_client import get_client

    get_client()
