"""In-process event consumers, so the console is the only thing an operator uses.

Every console action already publishes a durable, audited Pub/Sub command.
Until now nothing consumed those commands: a human had to run a one-shot
script per click, and each script handled exactly one message and exited.
`GET /api/v1/operations/{id}` said so in words — an operation with no run
yet reported **"Waiting for a worker"**. This is the thing that waits.

Nothing about the event model changes. The six orchestrator handlers were
already independent consumers — pure functions of one payload, idempotent
through `_dedup_claim`, each publishing the next topic, none calling
another. What was missing was only the loop that pulls each subscription.

Four constraints shaped this, all discovered in the code rather than
assumed:

1. **The ack deadline is 60s and nothing extended it.** A real assessment
   or migration runs for minutes. A one-shot worker hid the redelivery —
   the duplicate only surfaced on the next manual run. A loop picks it
   straight back up, so every in-flight message gets a lease heartbeat
   (tools/events.py::modify_ack_deadline).

2. **`_dedup_claim` assumes one consumer per subscription** and says so.
   `uvicorn --reload` and Cloud Run autoscaling both break that, so
   consumption is gated on a Firestore lease (tools/instance_lock.py).
   The loser runs in standby rather than exiting — an idle second instance
   is normal, not a fault.

3. **`run_once` publishes `migration.requested` before consuming it.**
   Calling it from a loop would inject a phantom run on every tick. This
   module calls handlers directly and never touches the CLI drivers.

4. **`validation-passed-sub` is deliberately NOT consumed.**
   `advance_through_validation` consumes it with a no-op handler as an
   assertion that the event reached the wire. Consuming it here would
   steal that message and make `make run`, `make harness` and
   evaluation/scenarios.py time out whenever the console is running.

This is the rung below the production shape (Cloud Run + Eventarc push
subscriptions) that infrastructure/README.md names. It is not a pretence
of being that.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from tools.events import MAX_ACK_DEADLINE_SECONDS, ack, modify_ack_deadline, nack, pull, warm_clients
from tools.firestore_client import get_client
from tools.instance_lock import InstanceLock

logger = logging.getLogger("worker_supervisor")

CONTROLS_COLLECTION = "worker_controls"

DEFAULT_POLL_TIMEOUT = 20.0
DEFAULT_LEASE_SECONDS = 600
DEFAULT_BACKOFF_SECONDS = 5.0
DEFAULT_MAX_BACKOFF_SECONDS = 120.0
CONTROL_REFRESH_SECONDS = 5.0


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass(frozen=True)
class ConsumerSpec:
    name: str
    subscription: str
    handler: Callable[[dict], Any]


@dataclass
class ConsumerState:
    name: str
    subscription: str
    state: str = "stopped"
    last_message_at: str | None = None
    last_completed_at: str | None = None
    last_error: str | None = None
    error_count: int = 0
    processed_count: int = 0
    in_flight_since: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "subscription": self.subscription,
                "state": self.state,
                "last_message_at": self.last_message_at,
                "last_completed_at": self.last_completed_at,
                "last_error": self.last_error,
                "error_count": self.error_count,
                "processed_count": self.processed_count,
                "in_flight_since": self.in_flight_since,
                # Real queue depth needs google-cloud-monitoring and a new
                # IAM role. Reporting None renders as "Not available" in the
                # console rather than inventing a number.
                "backlog": None,
            }


class _LeaseHeartbeat:
    """Keeps one in-flight message leased while its handler works.

    Must be stopped and joined BEFORE ack or nack. A `modify_ack_deadline`
    landing after a `nack(0)` would restore the lease and silently cancel
    the redelivery the nack exists to cause — that is the actual race, and
    joining first removes it rather than papering over it with a lock.
    """

    def __init__(self, subscription: str, ack_id: str, seconds: int):
        self.subscription = subscription
        self.ack_id = ack_id
        self.seconds = min(seconds, MAX_ACK_DEADLINE_SECONDS)
        # Re-extend well before expiry; a third of the lease leaves room
        # for two missed beats before the message is redelivered.
        self.interval = max(1.0, self.seconds / 3)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.extensions = 0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"lease-{self.subscription}", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if self._stop.is_set():
                return
            try:
                modify_ack_deadline(self.subscription, self.ack_id, self.seconds)
                self.extensions += 1
            except Exception as exc:  # noqa: BLE001 — the message may already be acked
                logger.debug("lease extension failed on %s: %s", self.subscription, exc)
                return

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)


class WorkerSupervisor:
    """One consumer thread per subscription, plus one housekeeping thread."""

    def __init__(
        self,
        specs: list[ConsumerSpec],
        *,
        lock: InstanceLock | None = None,
        poll_timeout: float = DEFAULT_POLL_TIMEOUT,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        controls_enabled: bool = True,
    ):
        self.specs = list(specs)
        self.lock = lock if lock is not None else InstanceLock()
        self.poll_timeout = poll_timeout
        self.lease_seconds = lease_seconds
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.controls_enabled = controls_enabled

        self.states: dict[str, ConsumerState] = {
            spec.name: ConsumerState(name=spec.name, subscription=spec.subscription)
            for spec in self.specs
        }
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._paused: dict[str, bool] = {}
        self._started_at: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        # Build the cached clients here, on the calling thread: lru_cache
        # construction is not lock-protected, and two consumers racing into
        # it each build a client, one of which is discarded with its gRPC
        # transport still open.
        warm_clients()
        self._refresh_controls()
        self.lock.try_acquire()
        self._started_at = _now_iso()

        for spec in self.specs:
            thread = threading.Thread(
                target=self._consume_forever, args=(spec,), name=f"worker-{spec.name}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

        housekeeping = threading.Thread(
            target=self._housekeep, name="worker-housekeeping", daemon=True
        )
        housekeeping.start()
        self._threads.append(housekeeping)
        logger.info("worker supervisor started with %d consumer(s)", len(self.specs))

    def stop(self, timeout: float = 10.0) -> None:
        """Signals every thread and waits, bounded.

        Threads are daemons and the join is bounded because a blocking
        `pull` cannot be interrupted — without both, shutting down the web
        server would hang for the poll timeout, or a long handler would
        hold it indefinitely.
        """
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        still_running = [t.name for t in self._threads if t.is_alive()]
        if still_running:
            logger.warning("supervisor stopping with work still in flight: %s", still_running)
        for state in self.states.values():
            with state._lock:
                state.state = "stopped"
        self.lock.release()
        self._threads.clear()
        logger.info("worker supervisor stopped")

    # -- control -----------------------------------------------------------

    def _control_doc(self, name: str):
        return get_client().collection(CONTROLS_COLLECTION).document(name)

    def _refresh_controls(self) -> None:
        """Pause state is durable, not a local flag.

        On Cloud Run a pause request lands on an arbitrary instance, very
        likely not the lock holder, so an in-process flag would silently do
        nothing. It also has to survive a restart — a consumer paused for a
        reason must not resume itself on the next deploy.
        """
        if not self.controls_enabled:
            return
        try:
            snapshot = {
                doc.id: bool((doc.to_dict() or {}).get("paused"))
                for doc in get_client().collection(CONTROLS_COLLECTION).stream()
            }
        except Exception as exc:  # noqa: BLE001 — never let control lookup stop work
            logger.debug("could not refresh worker controls: %s", exc)
            return
        self._paused = snapshot

    def set_paused(self, name: str, paused: bool, *, actor: str, justification: str = "") -> dict:
        if name != "all" and name not in self.states:
            raise KeyError(name)
        targets = list(self.states) if name == "all" else [name]
        record = {
            "paused": paused,
            "actor": actor,
            "justification": justification,
            "updated_at": _now_iso(),
        }
        for target in targets:
            if self.controls_enabled:
                self._control_doc(target).set({**record, "consumer": target})
            self._paused[target] = paused
        return {"consumers": targets, **record}

    def is_paused(self, name: str) -> bool:
        return bool(self._paused.get(name))

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        holder = self.lock.holder()
        return {
            "enabled": True,
            "started_at": self._started_at,
            "lease": {
                "held": self.lock.held,
                "owner_id": self.lock.owner_id,
                "holder": holder,
                # A standby instance must be able to say WHO is working,
                # or "nothing is happening here" reads as a fault.
                "standby_reason": None
                if self.lock.held
                else (
                    f"another instance holds the worker lease "
                    f"({holder.get('hostname')}:{holder.get('pid')})"
                    if holder
                    else "the worker lease is not held by this instance"
                ),
            },
            "consumers": [self.states[spec.name].snapshot() for spec in self.specs],
        }

    # -- threads -----------------------------------------------------------

    def _housekeep(self) -> None:
        renew_every = max(5.0, self.lock.ttl_seconds / 3)
        last_renew = 0.0
        while not self._stop.wait(CONTROL_REFRESH_SECONDS):
            self._refresh_controls()
            if time.monotonic() - last_renew >= renew_every:
                last_renew = time.monotonic()
                try:
                    if not self.lock.renew():
                        # Lost or never held — try to take it, so a
                        # standby instance promotes itself when the holder
                        # dies rather than idling forever.
                        self.lock.try_acquire()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("worker lease renewal failed: %s", exc)

    def _consume_forever(self, spec: ConsumerSpec) -> None:
        state = self.states[spec.name]
        backoff = self.backoff_seconds

        while not self._stop.is_set():
            if not self.lock.held:
                self._set(state, "standby")
                self._stop.wait(CONTROL_REFRESH_SECONDS)
                continue
            if self.is_paused(spec.name):
                self._set(state, "paused")
                self._stop.wait(CONTROL_REFRESH_SECONDS)
                continue

            self._set(state, "idle")
            try:
                messages = pull(spec.subscription, max_messages=1, timeout=self.poll_timeout)
            except Exception as exc:  # noqa: BLE001
                self._record_error(state, f"pull failed: {exc}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.max_backoff_seconds)
                continue

            if not messages:
                backoff = self.backoff_seconds
                continue

            payload = messages[0]
            ack_id = payload.get("_pubsub_ack_id")
            with state._lock:
                state.state = "working"
                state.last_message_at = _now_iso()
                state.in_flight_since = _now_iso()

            heartbeat = _LeaseHeartbeat(spec.subscription, ack_id, self.lease_seconds)
            heartbeat.start()
            try:
                spec.handler(payload)
            except Exception as exc:  # noqa: BLE001 — one bad message must not kill the consumer
                heartbeat.stop()  # BEFORE nack — see _LeaseHeartbeat
                if ack_id:
                    nack(spec.subscription, ack_id)
                self._record_error(state, str(exc))
                logger.exception("%s failed to handle a message", spec.name)
                # Backoff matters more here than it looks: nack sets the
                # deadline to 0, so a poison message is redelivered
                # instantly. A one-shot worker exited after one failure; a
                # loop would spin on it forever without this.
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.max_backoff_seconds)
            else:
                heartbeat.stop()  # BEFORE ack
                if ack_id:
                    ack(spec.subscription, ack_id)
                with state._lock:
                    state.processed_count += 1
                    state.last_completed_at = _now_iso()
                    state.last_error = None
                    state.in_flight_since = None
                backoff = self.backoff_seconds
            finally:
                with state._lock:
                    state.in_flight_since = None

        self._set(state, "stopped")

    def _set(self, state: ConsumerState, value: str) -> None:
        with state._lock:
            state.state = value

    def _record_error(self, state: ConsumerState, message: str) -> None:
        with state._lock:
            state.state = "error"
            state.error_count += 1
            state.last_error = message
            state.in_flight_since = None


# ---------------------------------------------------------------------------
# The consumer set
# ---------------------------------------------------------------------------


def default_specs() -> list[ConsumerSpec]:
    """Built lazily: importing this module must not drag in the orchestrator
    (and through it wave_manager, migration_executor and pyodbc), because
    frontend/app.py imports it at startup and tools/export_openapi.py
    imports frontend.app.
    """
    from agents.cutover.run_cutover_worker import handle_cutover_approved
    from agents.discovery.run_assessment_worker import handle_assessment_requested
    from agents.orchestrator import orchestrator as orch

    return [
        ConsumerSpec("assessment", "assessment-requested-sub", handle_assessment_requested),
        ConsumerSpec("migration", orch.MIGRATION_REQUESTED_SUB, orch.handle_migration_requested),
        ConsumerSpec("discovery", orch.DISCOVERY_COMPLETED_SUB, orch.handle_discovery_completed),
        ConsumerSpec("risk", orch.RISK_ASSESSED_SUB, orch.handle_risk_assessed),
        ConsumerSpec("plan", orch.PLAN_CREATED_SUB, orch.handle_planned),
        ConsumerSpec("validation", orch.VALIDATION_REQUESTED_SUB, orch.handle_validation_requested),
        ConsumerSpec("approval", orch.APPROVAL_PREPARATION_SUB, orch.handle_validation_passed),
        ConsumerSpec("recovery", orch.VALIDATION_FAILED_SUB, orch.handle_validation_failed),
        ConsumerSpec("cutover", "cutover-approved-sub", handle_cutover_approved),
        # NOT validation-passed-sub. advance_through_validation consumes it
        # as an assertion; stealing it breaks make run / make harness /
        # evaluation/scenarios.py whenever the console is up.
        # Deploy & Harden Phase 3 (docs/adr/0003) — the async data-plane
        # job's completion event. A no-op consumer unless
        # DATA_PLANE_EXECUTOR=cloud_run_job is set (handle_planned()'s own
        # gate); harmless to always register, matching how every other
        # consumer here is unconditional.
        ConsumerSpec("migration_completed", orch.MIGRATION_COMPLETED_SUB, orch.handle_migration_completed),
    ]


def selected_specs() -> list[ConsumerSpec]:
    """Honours CONTROL_TOWER_WORKER_CONSUMERS ("all", or a comma list)."""
    wanted = os.environ.get("CONTROL_TOWER_WORKER_CONSUMERS", "all").strip()
    specs = default_specs()
    if not wanted or wanted == "all":
        return specs
    names = {part.strip() for part in wanted.split(",") if part.strip()}
    return [spec for spec in specs if spec.name in names]


def workers_enabled() -> bool:
    return os.environ.get("CONTROL_TOWER_WORKERS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def warn_if_a_supervisor_is_running() -> dict | None:
    """Tells a CLI worker that something else is already consuming.

    Both one-shot workers block for up to 30s on an empty pull and then
    exit with "No message was available." Once a console is running, that
    is the NORMAL outcome — the supervisor took the message a moment
    earlier — and it reads exactly like a broken publish. This is the
    difference between that and a real diagnosis.

    Returns the holder, or None when no supervisor holds the lease.
    """
    try:
        holder = InstanceLock().holder()
    except Exception:  # noqa: BLE001 — a diagnostic must never be the failure
        return None
    if not holder:
        return None
    print(
        f"NOTE: a worker supervisor is already consuming ({holder.get('hostname')}"
        f":{holder.get('pid')}, since {holder.get('acquired_at')}).\n"
        "      It will usually take the message before this script sees it, so an\n"
        "      empty pull here is expected rather than a fault. Pause the consumer\n"
        "      from the console's System Health page to drive it by hand."
    )
    return holder
