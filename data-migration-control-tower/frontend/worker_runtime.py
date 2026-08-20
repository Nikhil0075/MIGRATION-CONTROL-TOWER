"""Owns the one supervisor instance belonging to this API process.

Separate from tools/worker_supervisor.py on purpose. That module is the
mechanism and knows nothing about a web server; this one is the policy —
whether workers run here at all, which ones, and how the request handlers
in frontend/api_v1.py reach the running instance.

The accessor is module-level rather than `app.state.supervisor` because
the routes live on an APIRouter in another module. Reaching the app object
from there means threading `Request` through handlers that otherwise need
nothing from it, and a router mounted on a second app would silently read
the wrong instance. One process holds one supervisor; a module global says
exactly that.

Disabled here does not mean broken: `status()` still answers, reporting
`enabled: false` with the reason, so the console's Workers panel explains
itself instead of rendering an empty table.
"""

from __future__ import annotations

import logging
import os
import threading

from tools.worker_supervisor import WorkerSupervisor, selected_specs, workers_enabled

logger = logging.getLogger("worker_runtime")

_supervisor: WorkerSupervisor | None = None
_disabled_reason: str | None = None
_startup_thread: threading.Thread | None = None
_shutdown_requested = False


def start_supervisor_background() -> None:
    """Initialize consumers without holding the ASGI startup boundary.

    Client warming and the Firestore lease are external calls. If either is
    slow, operators still need the console and System Health page that explain
    worker readiness; binding the HTTP port must not depend on them.
    """
    global _startup_thread, _disabled_reason, _shutdown_requested
    if _supervisor is not None or (_startup_thread and _startup_thread.is_alive()):
        return
    _shutdown_requested = False
    _disabled_reason = "the worker supervisor is initializing"

    def initialize() -> None:
        global _supervisor
        supervisor = start_supervisor()
        if _shutdown_requested and supervisor is not None:
            supervisor.stop(timeout=2.0)
            _supervisor = None

    _startup_thread = threading.Thread(
        target=initialize,
        name="worker-supervisor-startup",
        daemon=True,
    )
    _startup_thread.start()


def start_supervisor() -> WorkerSupervisor | None:
    """Starts the in-process consumers unless configuration says otherwise.

    Returns None when disabled, which is a supported deployment: the
    container image cannot run the workers today (it carries neither the
    Discovery fixtures nor the ODBC runtime), so CONTROL_TOWER_WORKERS=0
    is how that image runs the console alone.
    """
    global _supervisor, _disabled_reason

    if _supervisor is not None:
        return _supervisor

    if not workers_enabled():
        _disabled_reason = "CONTROL_TOWER_WORKERS is set to off for this process"
        logger.info("in-process workers disabled: %s", _disabled_reason)
        return None

    specs = selected_specs()
    if not specs:
        _disabled_reason = (
            "CONTROL_TOWER_WORKER_CONSUMERS selected no consumers "
            f"({os.environ.get('CONTROL_TOWER_WORKER_CONSUMERS')!r})"
        )
        logger.warning("in-process workers disabled: %s", _disabled_reason)
        return None

    supervisor = WorkerSupervisor(
        specs,
        poll_timeout=float(os.environ.get("CONTROL_TOWER_WORKER_POLL_SECONDS", "20")),
    )
    try:
        supervisor.start()
    except Exception as exc:  # noqa: BLE001
        # A console that will not start is worse than one whose workers
        # did not: the operator loses the page that would have told them
        # why. Report it through status() instead of failing startup.
        _disabled_reason = f"the worker supervisor failed to start: {exc}"
        logger.exception("worker supervisor failed to start")
        return None

    _supervisor = supervisor
    _disabled_reason = None
    return supervisor


def stop_supervisor(timeout: float = 10.0) -> None:
    global _supervisor, _shutdown_requested
    _shutdown_requested = True
    if _supervisor is None:
        return
    try:
        _supervisor.stop(timeout=timeout)
    except Exception:  # noqa: BLE001 — shutdown must not raise
        logger.exception("worker supervisor did not stop cleanly")
    finally:
        _supervisor = None


def get_supervisor() -> WorkerSupervisor | None:
    return _supervisor


def status() -> dict:
    """Always answers, running or not.

    "Nothing is happening" was the failure mode that produced the
    404-on-assessment confusion. An empty response would reproduce it, so
    a disabled process reports that it is disabled and why.
    """
    supervisor = get_supervisor()
    if supervisor is None:
        initializing = bool(_startup_thread and _startup_thread.is_alive())
        return {
            "enabled": False,
            "started_at": None,
            "reason": _disabled_reason
            or ("the worker supervisor is initializing" if initializing else "in-process workers are not running for this process"),
            "initializing": initializing,
            "lease": {"held": False, "owner_id": None, "holder": None, "standby_reason": None},
            "consumers": [],
        }
    return {**supervisor.status(), "initializing": False}
