"""One-process-at-a-time lock over a Firestore document.

Exists for a specific correctness requirement, not for tidiness.
agents/orchestrator/orchestrator.py's `_dedup_claim` says plainly in its
docstring that it assumes "a single orchestrator process … no fleet of
concurrent workers pulling the same subscription". Two things break that
assumption the moment event consumers move inside the API process:

  - `uvicorn --reload` runs a reloader parent AND a child, and both would
    start a supervisor. Every developer runs it that way; CLAUDE.md and
    frontend/app.py's own docstring both recommend it.
  - Cloud Run autoscales. Two instances means two consumers on one
    subscription, each claiming the same message.

So this is what makes "the workers run inside the web server" safe rather
than merely convenient. The loser does not crash or exit — it runs in
standby, consuming nothing, and reports who holds the lock, because a
second Cloud Run instance sitting idle is a normal state and must not look
like a fault.

Deliberately a lease, not a mutex: a process killed with SIGKILL cannot
release anything, so the lock has to expire on its own. TTL 90s with
renewal every 30s means a hard-killed instance strands the lock for at
most 90s.

The transaction shape mirrors tools/wave_manager.py::reserve_slot — same
@firestore.transactional + doc_ref.get(transaction=...) idiom, so there is
one concurrency pattern in this codebase rather than two.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import socket
import uuid

from google.cloud import firestore

from tools.firestore_client import get_client

logger = logging.getLogger("instance_lock")

COLLECTION = "supervisor_locks"
DEFAULT_NAME = "worker-supervisor"
DEFAULT_TTL_SECONDS = 90


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse(value) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


class InstanceLock:
    """A renewable lease. One holder at a time, expiring on its own."""

    def __init__(
        self,
        name: str = DEFAULT_NAME,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        collection: str = COLLECTION,
    ):
        self.name = name
        self.ttl_seconds = ttl_seconds
        self.collection = collection
        #: Unique per process. Two instances of the same build must not be
        #: able to renew each other's lease.
        self.owner_id = uuid.uuid4().hex
        self._held = False

    @property
    def held(self) -> bool:
        """Whether THIS process currently believes it holds the lease.

        Local, not a read. Consumers check it before every pull, so it has
        to be cheap; the housekeeping thread is what keeps it truthful.
        """
        return self._held

    def _doc(self):
        return get_client().collection(self.collection).document(self.name)

    def _claim(self, *, renew_only: bool) -> bool:
        doc_ref = self._doc()
        owner_id = self.owner_id
        ttl = self.ttl_seconds
        now = _now()

        @firestore.transactional
        def _txn(transaction: firestore.Transaction) -> bool:
            snapshot = doc_ref.get(transaction=transaction)
            current = snapshot.to_dict() if snapshot.exists else None

            if current:
                mine = current.get("owner_id") == owner_id
                heartbeat = _parse(current.get("heartbeat_at"))
                expired = heartbeat is None or (now - heartbeat).total_seconds() > ttl
                # Renewal must never resurrect a lease someone else took.
                if renew_only and not mine:
                    return False
                if not mine and not expired:
                    return False

            transaction.set(
                doc_ref,
                {
                    "lock_name": self.name,
                    "owner_id": owner_id,
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                    "build_version": os.environ.get("BUILD_VERSION", "development"),
                    "acquired_at": (current or {}).get("acquired_at")
                    if current and current.get("owner_id") == owner_id
                    else now.isoformat(),
                    "heartbeat_at": now.isoformat(),
                    "ttl_seconds": ttl,
                },
            )
            return True

        acquired = _txn(get_client().transaction())
        self._held = bool(acquired)
        return self._held

    def try_acquire(self) -> bool:
        """Takes the lease if it is free, expired, or already ours."""
        return self._claim(renew_only=False)

    def renew(self) -> bool:
        """Extends our own lease. False means we lost it — the caller must
        stop consuming rather than assume it still holds."""
        return self._claim(renew_only=True)

    def release(self) -> None:
        """Releases only if we still hold it.

        Guarded because a slow shutdown can overlap another process
        acquiring the expired lease; deleting unconditionally would drop
        the new holder's lease and let a third process in.
        """
        doc_ref = self._doc()
        owner_id = self.owner_id

        @firestore.transactional
        def _txn(transaction: firestore.Transaction) -> None:
            snapshot = doc_ref.get(transaction=transaction)
            if snapshot.exists and (snapshot.to_dict() or {}).get("owner_id") == owner_id:
                transaction.delete(doc_ref)

        try:
            _txn(get_client().transaction())
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            logger.warning("could not release lock %s: %s", self.name, exc)
        finally:
            self._held = False

    def holder(self) -> dict | None:
        """Who holds the lease right now, for the console's standby line.

        Returns None when free or expired. This is what turns "nothing is
        happening" into "another instance is doing the work".
        """
        try:
            snapshot = self._doc().get()
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not read lock %s: %s", self.name, exc)
            return None
        if not snapshot.exists:
            return None
        current = snapshot.to_dict() or {}
        heartbeat = _parse(current.get("heartbeat_at"))
        if heartbeat is None or (_now() - heartbeat).total_seconds() > current.get(
            "ttl_seconds", self.ttl_seconds
        ):
            return None
        return {
            "owner_id": current.get("owner_id"),
            "hostname": current.get("hostname"),
            "pid": current.get("pid"),
            "build_version": current.get("build_version"),
            "acquired_at": current.get("acquired_at"),
            "heartbeat_at": current.get("heartbeat_at"),
            "is_self": current.get("owner_id") == self.owner_id,
        }
