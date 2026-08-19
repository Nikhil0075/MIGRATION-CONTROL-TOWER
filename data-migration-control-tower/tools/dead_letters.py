"""Read, replay and archive the messages the fleet gave up on.

The dead-letter topic and `dead-letter-sub` are provisioned and every
consumed subscription forwards to them after 10 delivery attempts — but
nothing could read them. A message that defeated a consumer left no trace
an operator could reach: the consumer showed `error`, the run sat still,
and the payload was only visible by running gcloud by hand. This is the
read path.

Two mechanics shape the whole design.

**Listing must not consume.** Pub/Sub has no "peek". A pull leases the
message for the ack deadline, so a naive listing endpoint would hide every
dead letter for 60s from anyone else — including the next refresh of the
same page. Every read here therefore pulls and immediately returns the
lease (`nack`, deadline 0), so the queue is left exactly as it was found
and two operators can look at the same time.

**Ack ids are not identifiers.** Returning the lease means the broker
redelivers the message with a NEW ack id, so an ack id captured during
listing is already stale by the time an operator clicks Replay. The
stable handle is `message_id`, which survives redelivery, so replay and
archive re-pull and match on it. The cost is honest and worth stating: if
another reader happens to hold the message at that moment, the action
reports that it could not find it rather than acting on the wrong one.

The source subscription is read from the `CloudPubSubDeadLetterSourceSubscription`
attribute Pub/Sub stamps on every forwarded message, and its topic is
looked up from the subscription itself rather than from a duplicated
mapping — a hardcoded table would drift the moment a subscription is
repointed, and replaying onto the wrong topic is the worst possible
outcome for this feature.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from tools.events import _project_id, _publisher, _subscriber, modify_ack_deadline

logger = logging.getLogger("dead_letters")

DEAD_LETTER_SUBSCRIPTION = "dead-letter-sub"
ARCHIVE_COLLECTION = "dead_letter_archive"

#: Pub/Sub stamps these on every forwarded message.
SOURCE_SUBSCRIPTION_ATTRIBUTE = "CloudPubSubDeadLetterSourceSubscription"
SOURCE_DELIVERY_ATTEMPT_ATTRIBUTE = "CloudPubSubDeadLetterSourceDeliveryCount"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _short_subscription(name: str) -> str:
    """projects/p/subscriptions/x -> x."""
    return str(name or "").rsplit("/", 1)[-1]


def _decode(received) -> dict:
    message = received.message
    attributes = dict(message.attributes or {})
    try:
        payload = json.loads(message.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # A dead letter is by definition a message something could not
        # handle; that may well be because it is not the JSON we expect.
        # Showing the raw body is more useful than failing the listing.
        payload = {"_undecodable": message.data.decode("utf-8", errors="replace")}

    # Where the source comes from decides how much it can be trusted.
    #
    # Pub/Sub stamps the attribute itself when IT forwards a message, so an
    # attribute is broker-asserted fact. But messages can also be published
    # straight onto the dead-letter topic — the acceptance tooling parks
    # stale artifacts there, carrying `source_subscription` in the BODY —
    # and a body is untrusted content. Replaying to a topic named by the
    # payload would be taking a routing decision from the message itself,
    # which is the hazard policy_engine.py exists to avoid elsewhere.
    #
    # Both are shown; only the attribute is treated as authoritative, and
    # replay validates either against the real consumer set regardless.
    attributed = _short_subscription(attributes.get(SOURCE_SUBSCRIPTION_ATTRIBUTE, ""))
    declared = _short_subscription(str(payload.get("source_subscription") or ""))
    return {
        "message_id": message.message_id,
        "published_at": message.publish_time.isoformat() if message.publish_time else None,
        # Which consumer gave up. The whole point of the panel.
        "source_subscription": attributed or declared or None,
        "source_is_broker_asserted": bool(attributed),
        "delivery_attempts": int(attributes.get(SOURCE_DELIVERY_ATTEMPT_ATTRIBUTE, 0)) or None,
        "reason": payload.get("dead_letter_reason"),
        "run_id": payload.get("run_id"),
        "operation_id": payload.get("operation_id"),
        "payload": payload,
        "attributes": attributes,
    }


def known_source_subscriptions() -> set[str]:
    """The subscriptions a replay is allowed to target.

    Derived from the consumer set rather than written out again, so a new
    consumer is replayable automatically and a name that never belonged to
    this system is not.
    """
    from tools.worker_supervisor import default_specs

    return {spec.subscription for spec in default_specs()}


def _pull(max_messages: int, timeout: float):
    from google.api_core.exceptions import DeadlineExceeded, RetryError

    subscriber = _subscriber()
    path = subscriber.subscription_path(_project_id(), DEAD_LETTER_SUBSCRIPTION)
    try:
        response = subscriber.pull(
            request={"subscription": path, "max_messages": max_messages}, timeout=timeout
        )
    except (DeadlineExceeded, RetryError):
        return []
    return list(response.received_messages)


def _release(ack_ids: list[str]) -> None:
    """Hand the lease straight back so the queue is unchanged by a read."""
    for ack_id in ack_ids:
        try:
            modify_ack_deadline(DEAD_LETTER_SUBSCRIPTION, ack_id, 0)
        except Exception as exc:  # noqa: BLE001 — a failed release self-heals at the deadline
            logger.debug("could not release dead-letter lease: %s", exc)


#: A single synchronous pull is allowed to return nothing while messages
#: are waiting — the broker answers with whatever a shard has ready rather
#: than searching. Observed directly against a queue holding three
#: messages: the first pull returned 0, the second returned all 3. One
#: round would therefore render "nothing has been dead-lettered" over a
#: non-empty queue, which is the exact opposite of what this page is for.
LIST_ROUNDS = 3


def list_dead_letters(limit: int = 25, timeout: float = 3.0, rounds: int = LIST_ROUNDS) -> list[dict]:
    """Non-destructive read. Leaves the subscription as it was found.

    Empty means "none visible right now", never "none exist" — messages
    already leased by another reader are invisible here, and the caller
    must not present the result as proof of a healthy queue.
    """
    seen: dict[str, dict] = {}
    leases: list[str] = []
    try:
        for _ in range(max(1, rounds)):
            received = _pull(limit, timeout)
            for item in received:
                leases.append(item.ack_id)
                decoded = _decode(item)
                seen.setdefault(decoded["message_id"], decoded)
            if len(seen) >= limit:
                break
    finally:
        _release(leases)
    return list(seen.values())[:limit]


def _topic_for_subscription(subscription: str) -> str:
    """Authoritative, from the subscription itself — never a local table."""
    subscriber = _subscriber()
    path = subscriber.subscription_path(_project_id(), subscription)
    return subscriber.get_subscription(request={"subscription": path}).topic


def _take(message_id: str, limit: int, timeout: float):
    """Finds one message by its stable id, holding only that lease."""
    received = _pull(limit, timeout)
    wanted = None
    release: list[str] = []
    for item in received:
        if item.message.message_id == message_id and wanted is None:
            wanted = item
        else:
            release.append(item.ack_id)
    _release(release)
    return wanted


def replay(message_id: str, *, actor: str, justification: str, limit: int = 50) -> dict:
    """Republishes to the topic it originally came from, then acks.

    Publish first, ack second, deliberately. Acking first would drop the
    message if the publish then failed — the one outcome worse than a
    duplicate, which the consumers' `_dedup_claim` already tolerates.
    """
    from google.cloud import pubsub_v1  # noqa: F401 — import cost stays out of module import

    received = _take(message_id, limit, timeout=3.0)
    if received is None:
        raise LookupError(
            f"No dead letter {message_id!r} is available right now. It may be held by another "
            f"reader; refresh and try again."
        )

    decoded = _decode(received)
    source_subscription = decoded["source_subscription"]
    if not source_subscription:
        _release([received.ack_id])
        raise ValueError(
            "This message names no source subscription, so the topic it came from cannot be "
            "determined. Replaying it would mean guessing."
        )

    # The allowlist is what makes a payload-declared source safe to act on:
    # even a body naming an arbitrary subscription can only ever resolve to
    # one this system actually consumes.
    allowed = known_source_subscriptions()
    if source_subscription not in allowed:
        _release([received.ack_id])
        raise ValueError(
            f"{source_subscription!r} is not a subscription this system consumes "
            f"({sorted(allowed)}), so there is no topic it can safely be replayed onto."
        )

    topic = _topic_for_subscription(source_subscription)
    publisher = _publisher()
    future = publisher.publish(topic, json.dumps(decoded["payload"]).encode("utf-8"))
    published_id = future.result(timeout=30)

    subscriber = _subscriber()
    subscriber.acknowledge(
        request={
            "subscription": subscriber.subscription_path(_project_id(), DEAD_LETTER_SUBSCRIPTION),
            "ack_ids": [received.ack_id],
        }
    )

    _record(decoded, event="replayed", actor=actor, justification=justification,
            extra={"republished_to": topic, "republished_message_id": published_id})
    return {
        "message_id": message_id,
        "action": "replayed",
        "topic": _short_subscription(topic),
        "republished_message_id": published_id,
    }


def archive(message_id: str, *, actor: str, justification: str, limit: int = 50) -> dict:
    """Acks the message, keeping a durable copy first.

    Acking a dead letter destroys it — there is no second dead-letter
    queue behind this one. The Firestore copy is written BEFORE the ack so
    a crash between the two leaves an extra record rather than no record.
    """
    received = _take(message_id, limit, timeout=3.0)
    if received is None:
        raise LookupError(
            f"No dead letter {message_id!r} is available right now. It may be held by another "
            f"reader; refresh and try again."
        )

    decoded = _decode(received)
    _record(decoded, event="archived", actor=actor, justification=justification)

    subscriber = _subscriber()
    subscriber.acknowledge(
        request={
            "subscription": subscriber.subscription_path(_project_id(), DEAD_LETTER_SUBSCRIPTION),
            "ack_ids": [received.ack_id],
        }
    )
    return {"message_id": message_id, "action": "archived"}


def _record(decoded: dict, *, event: str, actor: str, justification: str, extra: dict | None = None) -> None:
    from tools.firestore_client import get_client

    get_client().collection(ARCHIVE_COLLECTION).document(decoded["message_id"]).set(
        {
            **decoded,
            "event": event,
            "actor": actor,
            "justification": justification,
            "recorded_at": _now(),
            **(extra or {}),
        }
    )


def list_archive(limit: int = 50) -> list[dict]:
    from tools.firestore_client import get_client

    records = [doc.to_dict() or {} for doc in get_client().collection(ARCHIVE_COLLECTION).limit(limit).stream()]
    records.sort(key=lambda item: item.get("recorded_at") or "", reverse=True)
    return records
