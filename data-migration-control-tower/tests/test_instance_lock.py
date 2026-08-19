"""The single-instance lock (Day 11 Phase 10).

This is what makes "event workers run inside the web server" safe.
`_dedup_claim` states it assumes one consumer per subscription, and both
`uvicorn --reload` (reloader parent + child) and Cloud Run autoscaling
produce more than one. Without the lock those consumers race for the same
message.

Live Firestore, like the rest of this suite; every test deletes the
document it created.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tools.instance_lock import InstanceLock


@pytest.fixture
def lock_name(firestore_cleanup):
    name = f"test-lock-{uuid.uuid4().hex[:8]}"
    firestore_cleanup.document(f"supervisor_locks/{name}")
    return name


@pytest.mark.requires_firestore
def test_a_free_lock_is_acquired(lock_name):
    assert InstanceLock(lock_name).try_acquire() is True


@pytest.mark.requires_firestore
def test_a_second_process_cannot_take_a_held_lock(lock_name):
    """The property the whole design rests on."""
    first = InstanceLock(lock_name)
    second = InstanceLock(lock_name)
    assert first.owner_id != second.owner_id, "each process must be distinguishable"

    assert first.try_acquire() is True
    assert second.try_acquire() is False
    assert second.held is False


@pytest.mark.requires_firestore
def test_acquiring_twice_from_the_same_process_is_idempotent(lock_name):
    lock = InstanceLock(lock_name)
    assert lock.try_acquire() is True
    assert lock.try_acquire() is True


@pytest.mark.requires_firestore
def test_an_expired_lease_is_reclaimable(lock_name):
    """A SIGKILLed instance cannot release anything, so the lease has to
    expire on its own or the fleet deadlocks permanently."""
    from tools.firestore_client import get_client

    dead = InstanceLock(lock_name, ttl_seconds=1)
    assert dead.try_acquire() is True

    stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=300)).isoformat()
    get_client().collection("supervisor_locks").document(lock_name).update(
        {"heartbeat_at": stale}
    )

    successor = InstanceLock(lock_name, ttl_seconds=1)
    assert successor.try_acquire() is True


@pytest.mark.requires_firestore
def test_renew_fails_once_the_lease_was_stolen(lock_name):
    """Renewal must never resurrect a lease another process now owns —
    otherwise both would consume."""
    from tools.firestore_client import get_client

    original = InstanceLock(lock_name, ttl_seconds=1)
    assert original.try_acquire() is True

    get_client().collection("supervisor_locks").document(lock_name).update(
        {"heartbeat_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=300)).isoformat()}
    )
    thief = InstanceLock(lock_name, ttl_seconds=90)
    assert thief.try_acquire() is True

    assert original.renew() is False
    assert original.held is False


@pytest.mark.requires_firestore
def test_renew_extends_our_own_lease(lock_name):
    lock = InstanceLock(lock_name)
    lock.try_acquire()
    before = lock.holder()["heartbeat_at"]
    assert lock.renew() is True
    assert lock.holder()["heartbeat_at"] >= before


@pytest.mark.requires_firestore
def test_release_by_a_non_owner_does_nothing(lock_name):
    """A slow shutdown can overlap another process acquiring the expired
    lease; an unguarded delete would drop the new holder's lease."""
    holder = InstanceLock(lock_name)
    holder.try_acquire()

    stranger = InstanceLock(lock_name)
    stranger.release()

    assert holder.holder() is not None, "the real holder's lease survived"
    assert holder.holder()["is_self"] is True


@pytest.mark.requires_firestore
def test_release_frees_the_lock_for_the_next_process(lock_name):
    first = InstanceLock(lock_name)
    first.try_acquire()
    first.release()
    assert first.held is False
    assert InstanceLock(lock_name).try_acquire() is True


@pytest.mark.requires_firestore
def test_holder_reports_who_is_running_for_the_standby_message(lock_name):
    """A standby instance must be able to say WHO is doing the work, or
    'nothing is happening here' looks like a fault."""
    lock = InstanceLock(lock_name)
    lock.try_acquire()

    observer = InstanceLock(lock_name)
    holder = observer.holder()
    assert holder["owner_id"] == lock.owner_id
    assert holder["hostname"] and holder["pid"]
    assert holder["is_self"] is False


@pytest.mark.requires_firestore
def test_holder_is_none_when_the_lease_has_expired(lock_name):
    from tools.firestore_client import get_client

    lock = InstanceLock(lock_name, ttl_seconds=1)
    lock.try_acquire()
    get_client().collection("supervisor_locks").document(lock_name).update(
        {"heartbeat_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=300)).isoformat()}
    )
    assert lock.holder() is None
