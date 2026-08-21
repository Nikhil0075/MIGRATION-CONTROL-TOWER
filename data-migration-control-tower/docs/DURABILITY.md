# Durability

How this project survives a crash mid-run, a redelivered message, or two instances racing for the
same work — and what changes once Deploy & Harden Phase 2 splits the single process into separate
Cloud Run services.

## Delivery: at-least-once, never exactly-once

Every Pub/Sub subscription has `--ack-deadline=60` and a dead-letter policy
(`infrastructure/gcp_setup.sh`): after **10 delivery attempts**, a message that keeps failing moves
to the shared `dead-letter` topic instead of retrying forever. Inspect what was dropped:

```bash
gcloud pubsub subscriptions pull dead-letter-sub --limit=10 --format=json
```

or via the console's dead-letter view (`GET /api/v1/dead-letters`, `frontend/api_v1.py`), which
also offers replay/discard actions the raw `gcloud` pull doesn't.

**This is at-least-once delivery, not exactly-once**, regardless of how many separate services
eventually consume these topics. A message can be redelivered after a timeout, a crash mid-handler,
or a non-2xx response — Deploy & Harden Phase 2's move to independently-deployed Cloud Run services
with `--concurrency=1` does not change this; it only removes the need for this project's own lease
mechanism (below) *within* one subscription, not the need for idempotency.

## Idempotency: `_dedup_claim` / `_dedup_complete`

`agents/orchestrator/orchestrator.py`'s handlers each check-and-claim inside one Firestore
transaction before doing any real work:

- A fresh message returns `"claimed"` — the handler proceeds.
- An already-completed message returns `"done"` with the cached result — the handler no-ops and
  re-acks, so a redelivered duplicate never repeats a side effect.
- A claimed-but-never-completed message (the crash-recovery case: something claimed it, then the
  process died before finishing) returns `"stale_claim"` and is safely redone — safe because
  `execute_migration()` truncates-and-reloads on its first batch (already idempotent) and
  `transition_state()` is graph-checked, failing loudly on an illegal double-transition rather than
  silently double-applying it.

This is mandatory for every consumer, split into its own service or not — see
`docs/adr/0002-typed-http-dispatch.md`'s explicit correction of an earlier draft that assumed
per-service concurrency limits alone would make this redundant. They don't: Pub/Sub's delivery
guarantee is unrelated to how many processes are running.

## The lease: one supervisor holds the work today

`tools/instance_lock.py::InstanceLock` plus `tools/worker_supervisor.py::_LeaseHeartbeat` exist
because `_dedup_claim` alone assumes one consumer per subscription — true for a single
`WorkerSupervisor` process, but not automatically true under `uvicorn --reload` or Cloud Run
autoscaling, both of which can start a second instance. The loser of the lease runs in standby
(visible in the console's System Health page) rather than exiting; an idle standby instance is
normal, not a fault. While a message is in flight, `_LeaseHeartbeat` extends its Pub/Sub ack
deadline periodically so a genuinely slow handler (a multi-minute migration, not a hung one) isn't
redelivered out from under itself.

**What changes under Phase 2**: once a consumer is split into its own Cloud Run service with
`--concurrency=1`, that service's own concurrency limit plus Pub/Sub push delivery already prevent
two instances of *that* service processing the same message at once — making the lease's
per-instance role redundant for that specific consumer. The lease remains relevant for: (a) any
consumer not yet split out, and (b) the console's own operator-facing pause/resume controls
(`WorkerSupervisor.set_paused()`/`is_paused()`), which are a Firestore-backed control mechanism
independent of the lease and must survive the split unchanged.

## Crash-recovery resume semantics

`agents/orchestrator/orchestrator.py::handle_migration_requested` is the one handler where
"redelivered = safely redo" needed special handling: unlike every other handler (which acts on an
already-existing `run_id`), this one calls `run_lifecycle.create_run()`, which is **not** safe to
call twice — a second call would create a second run for the same logical request. Fixed by moving
the "processed" mark to the very end (matching every other handler) and having a stale-claim redo
reuse the `run_id` recorded immediately after the original `create_run()` succeeded, rather than
creating a new one.

## Long-horizon operation

A migration run can span state transitions separated by real wall-clock time — a human approval
that takes hours or days, a remediation loop that waits on an operator. `run_lifecycle.py`'s
canonical state graph (`_CANONICAL_TRANSITIONS`) has no time-based expiry built into the
transitions themselves; anything that needs one (an approval token's validity window) enforces it
explicitly (`tools/approval_service.py`) rather than relying on the state machine to time out on
its own. `agents/orchestrator/durability_demo.py` is the kill-and-resume proof: it interrupts a run
mid-flight and confirms it resumes correctly rather than restarting or getting stuck.

**Not yet proven**: a real multi-week deployed run, or a Cloud Scheduler-driven monitoring loop
watching for stuck runs in production. The kill-and-resume proof demonstrates the mechanism;
Deploy & Harden Phase 2's live deployment window (Aug 30 – Oct 3, 2026) is the first opportunity to
observe it under real elapsed time rather than a simulated interruption.

## SLO for the live demo window (Deploy & Harden Phase 5)

Once the console is publicly deployed, the target is:

- **99% of API responses succeed** (non-5xx), measured via Cloud Run's own request metrics.
- **No lifecycle stage backlogs longer than its ack-deadline-driven redelivery window** — a stuck
  `"claimed"` message with no matching `"done"` past a few multiples of the lease interval is a
  genuine alert condition, not routine.

Both are targets to alert against (Deploy & Harden Phase 5's monitoring policies), not yet measured
SLIs from a real production window — stated as a goal, not a result, until there's live data behind
it.
