"""Every topic the code publishes to must be provisioned (Day 11 Phase 9).

A real failure this catches: starting an assessment from the console
returned

    The operation was recorded but could not be published:
    404 Resource not found (resource=assessment.requested)

`assessment.requested` was declared in infrastructure/gcp_setup.sh but did
not exist in the project — the script had been run before that entry was
added, and nothing re-checked. The operation record was written and the
command was never published, so the run silently never started.

These tests are static: they read the source and the setup script, so they
run without GCP and fail in review rather than in front of an operator.
The live cross-check against the real project is
test_every_declared_topic_exists, which skips when Pub/Sub is unreachable.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import REPO_ROOT

SETUP = REPO_ROOT / "infrastructure" / "gcp_setup.sh"


def _declared_topics() -> set[str]:
    """Topic names from the TOPICS array in gcp_setup.sh."""
    text = SETUP.read_text(encoding="utf-8")
    block = re.search(r"TOPICS=\((.*?)\)", text, re.S)
    assert block, "gcp_setup.sh no longer declares a TOPICS array"
    # Hyphens allowed: topic names are dotted (migration.requested) but
    # the dead-letter topic is not an event, so it is named like the
    # subscriptions instead.
    return set(re.findall(r'"([a-z][a-z.-]+)"', block.group(1)))


def _declared_subscriptions() -> dict[str, str]:
    text = SETUP.read_text(encoding="utf-8")
    block = re.search(r"SUBSCRIPTIONS=\((.*?)\n\)", text, re.S)
    assert block, "gcp_setup.sh no longer declares a SUBSCRIPTIONS array"
    return dict(re.findall(r'\["([a-z-]+)"\]="([a-z][a-z.-]+)"', block.group(1)))


def _published_topics() -> set[str]:
    """Topics the application publishes to.

    Two call shapes: publish("topic.name", ...) directly, and topic="..."
    passed into queue_operation. Missing the second is why an earlier
    audit of this same question came back clean while the console was
    broken.
    """
    topics: set[str] = set()
    for path in list((REPO_ROOT / "agents").rglob("*.py")) + list(
        (REPO_ROOT / "frontend").rglob("*.py")
    ):
        source = path.read_text(encoding="utf-8")
        topics |= set(re.findall(r'publish\(\s*"([a-z][a-z.]+)"', source))
        topics |= set(re.findall(r'topic="([a-z][a-z.]+)"', source))
    return topics


def _pulled_subscriptions() -> set[str]:
    subs: set[str] = set()
    for path in (REPO_ROOT / "agents").rglob("*.py"):
        subs |= set(re.findall(r'"([a-z-]+-sub)"', path.read_text(encoding="utf-8")))
    return subs


def test_the_setup_script_still_declares_both_arrays():
    """Guards the guard: a rename would otherwise make every check below
    pass against an empty set."""
    assert len(_declared_topics()) >= 10
    assert len(_declared_subscriptions()) >= 7


def test_every_published_topic_is_provisioned():
    missing = sorted(_published_topics() - _declared_topics())
    assert not missing, (
        f"these topics are published but never created by "
        f"infrastructure/gcp_setup.sh, so publishing 404s at run time: {missing}"
    )


def test_every_pulled_subscription_is_provisioned():
    missing = sorted(_pulled_subscriptions() - set(_declared_subscriptions()))
    assert not missing, (
        f"these subscriptions are pulled but never created: {missing}"
    )


def test_every_subscription_points_at_a_declared_topic():
    declared = _declared_topics()
    orphans = {
        sub: topic
        for sub, topic in _declared_subscriptions().items()
        if topic not in declared
    }
    assert not orphans, f"subscriptions bound to undeclared topics: {orphans}"


@pytest.mark.requires_pubsub
def test_every_declared_topic_exists_in_the_project():
    """The live cross-check. A script entry added after provisioning is
    exactly how assessment.requested went missing — the script was right
    and the project was stale."""
    import os

    from google.cloud import pubsub_v1

    project = os.environ.get("GCP_PROJECT_ID")
    client = pubsub_v1.PublisherClient()
    existing = {
        topic.name.split("/")[-1]
        for topic in client.list_topics(request={"project": f"projects/{project}"})
    }
    missing = sorted(_declared_topics() - existing)
    assert not missing, (
        f"declared but absent from project {project!r}: {missing}. "
        f"Re-run infrastructure/gcp_setup.sh — it is idempotent."
    )


# ---------------------------------------------------------------------------
# The supervisor's consumer set (Day 11 Phase 10)
# ---------------------------------------------------------------------------


def test_every_supervisor_consumer_has_a_provisioned_subscription():
    """The same drift class that broke assessments, one layer up.

    A ConsumerSpec naming a subscription that gcp_setup.sh never creates
    fails at RUNTIME, inside a background thread, as a repeating pull
    error the operator sees only as a consumer stuck in `error`. Catching
    it statically is the difference between a failing test and a console
    that quietly never does anything.
    """
    from tools.worker_supervisor import default_specs

    declared = set(_declared_subscriptions())
    missing = sorted(
        f"{spec.name} -> {spec.subscription}"
        for spec in default_specs()
        if spec.subscription not in declared
    )
    assert not missing, f"consumers bound to subscriptions gcp_setup.sh never creates: {missing}"


def test_the_supervisor_does_not_consume_validation_passed():
    """Pinned here as well as in test_worker_supervisor.py, because this
    file is what a person reads when adding a subscription — and adding
    the obvious-looking missing consumer is exactly the mistake.
    `advance_through_validation` consumes validation-passed-sub as an
    assertion that the event reached the wire; a second consumer would
    steal it and hang make run / make harness / evaluation/scenarios.py
    whenever the console is up."""
    from tools.worker_supervisor import default_specs

    assert "validation-passed-sub" not in {spec.subscription for spec in default_specs()}


# ---------------------------------------------------------------------------
# Dead-lettering (Day 11 Phase 10)
# ---------------------------------------------------------------------------
#
# A looping consumer that nacks re-pulls the same bad message instantly.
# Backoff stops it spinning; only a dead-letter policy makes it stop.

DEAD_LETTER_TOPIC = "dead-letter"
DEAD_LETTER_SUB = "dead-letter-sub"


def _setup_text() -> str:
    return SETUP.read_text(encoding="utf-8")


def test_the_dead_letter_topic_and_its_subscription_are_declared():
    """A dead-letter topic with no subscription is a black hole: messages
    land there, nothing retains them, and they are dropped — while the
    policy makes it look like they were captured."""
    assert DEAD_LETTER_TOPIC in _declared_topics()
    assert _declared_subscriptions().get(DEAD_LETTER_SUB) == DEAD_LETTER_TOPIC


def test_every_consumed_subscription_gets_a_dead_letter_policy():
    """The policy is attached by looping over SUBSCRIPTIONS, so a new
    subscription inherits it. This pins the loop, not a list that would
    need editing twice."""
    text = _setup_text()
    assert "--dead-letter-topic=" in text
    assert "--max-delivery-attempts=" in text
    assert 'for sub in "${!SUBSCRIPTIONS[@]}"' in text


def test_the_dead_letter_subscription_is_excluded_from_the_policy():
    """Dead-lettering the dead-letter subscription onto its own topic
    would loop a message back to where it already failed."""
    assert '[ "$sub" = "$DEAD_LETTER_SUB" ] && continue' in _setup_text()


def test_the_pubsub_service_agent_is_granted_both_roles():
    """The single easiest thing to miss here, and silent when missed:
    Pub/Sub's own service agent forwards the message, and it needs
    publisher on the dead-letter topic AND subscriber on the source
    subscription. Without both, forwarding fails, the message is
    redelivered forever, and the policy looks configured while doing
    nothing."""
    text = _setup_text()
    assert "gcp-sa-pubsub.iam.gserviceaccount.com" in text
    assert "topics add-iam-policy-binding" in text
    assert "subscriptions add-iam-policy-binding" in text


def test_the_policy_is_applied_by_update_not_only_on_create():
    """Every subscription already exists in an established project, so a
    create-only guard would leave exactly the projects running the workers
    without a policy — the drift class that left assessment.requested
    unprovisioned while the script claimed otherwise."""
    assert "gcloud pubsub subscriptions update" in _setup_text()


def test_max_delivery_attempts_is_in_the_range_pubsub_accepts():
    """Pub/Sub rejects anything outside 5..100, and the script would fail
    at the very end of a long provisioning run."""
    match = re.search(r"MAX_DELIVERY_ATTEMPTS=(\d+)", _setup_text())
    assert match, "the setup script no longer sets MAX_DELIVERY_ATTEMPTS"
    assert 5 <= int(match.group(1)) <= 100
