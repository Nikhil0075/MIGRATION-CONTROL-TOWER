# Cloud SQL Discovery Path — Wired Up and Live-Verified

Deploy & Harden Phase 5 close-out, continuation of the live acceptance testing that found and
fixed 3 push-routing/auth bugs earlier the same day. This is what it took to get Discovery,
Lineage, and Risk genuinely running against a live, reachable Cloud SQL Postgres source — and
three more real bugs it surfaced along the way.

## What "wiring up the discovery path" actually meant

The Cloud SQL Postgres instance (`postgres-retail-exec-demo`) has existed since the earlier
deployment pass, but nothing had ever actually loaded data into it, granted its read-only user
real permissions, or given any Cloud Run service a network path to reach its private IP. None of
that was optional — every piece below was required before Discovery could do anything real.

### 1. Real schema and data, loaded for real

New `tools/data_plane_job/bootstrap_retail_db.py` — a one-off Cloud Run Job entrypoint (reuses the
data-plane job's own image; `infrastructure/terraform/data_plane_job.tf`'s new
`google_cloud_run_v2_job.db_bootstrap` just overrides the container command) that creates the
`retail` schema and loads the exact same fixture data
`simulator/source_setup/postgres/init/{01_schema,02_seed}.sql` already uses for the local
docker-compose Postgres estate — same 4 tables, same 5 customers, same rows. Not a divergent copy.
Idempotent (`IF NOT EXISTS` / `ON CONFLICT DO NOTHING`), so re-running it is a no-op.

Also applies the actual `REVOKE`/`GRANT` for `migration_readonly` that
`infrastructure/terraform/cloud_sql.tf`'s own header comment has said since Phase 3 was "applied
post-create via a SQL migration script" — this is that script, finally written and run.

Executed live: **succeeded**. `tables=['customers', 'order_items', 'orders', 'tags'], customers=5`.

### 2. A dedicated superuser credential, used exactly once

New `google_sql_user.postgres_superuser` + Secret Manager secret, readable only by a new dedicated
`sa-db-bootstrap` service account — never handed to any agent SA. The only thing that ever reads
it is the bootstrap job above.

### 3. Network path: Private Services Access + Direct VPC egress

The Postgres instance already had `ipv4_enabled = false` (no public IP) as a design decision, but
nothing gave anything a route to its private IP. `network.tf` already provisioned the VPC peering
during the earlier Cloud SQL deploy pass; what was missing was Direct VPC egress on whichever
Cloud Run service actually needs to reach it.

That turned out to be the **orchestrator**, not `discovery-agent` — confirmed live, not assumed:
Discovery's AgentCard is still `runtime.type=local` (an honest, already-documented state — see
`docs/compliance_matrix.md`), so `tools/registry.py::invoke_capability()` runs Discovery's code
in-process inside the orchestrator's own container, under `sa-orchestrator`'s identity. Added
`vpc_access` to `cloud_run.tf`'s `orchestrator` service specifically (not all 9 services), and
granted `sa-orchestrator` — not `sa-discovery` — read access to the `migration_readonly` password
secret, for the same reason.

### 4. A real estate, registered in Firestore

New estate `retail-cloudsql-demo` / source `retail-cloudsql`, pack `postgres_retail_exec_v1`,
`connection_profile.host` set directly to the Cloud SQL private IP (`10.11.0.3` — a literal,
non-secret field per `contracts/metadata_model.json`'s own schema), password resolved via
`password_secret_ref` pointing at the readonly secret.

## Three more real bugs found live, fixed, and now covered by regression tests

Getting from "network path exists" to "a real run actually advances" surfaced three genuine bugs —
none catchable without an actual live run against a real database, and none related to the earlier
routing/auth bugs.

### 4. `pin_agent_version()` had never once succeeded with a real agent_id

`agents/orchestrator/run_lifecycle.py::pin_agent_version()` built its Firestore update key as a
dotted string (`f"pinned_agents.{agent_key}"`). Firestore's Python SDK parses dotted-string update
keys with `FieldPath.from_string()`, which rejects any path segment containing a character outside
`[a-zA-Z0-9_]` — and every agent_id in this entire system is hyphenated
(`discovery-agent`, `lineage-agent`, ...). This call had never once succeeded against real
Firestore with a real agent_id before tonight — nothing had ever exercised it against a live
backend with a real hyphenated key.

Fixed with `FieldPath("pinned_agents", agent_key).to_api_repr()` (the backtick-escaped form —
passing a bare `FieldPath` object directly to `.update()` fails differently on this SDK version,
confirmed by testing both). New regression test:
`tests/test_state_machine.py::test_pin_agent_version_accepts_a_real_hyphenated_agent_id`.

### 5. Wildcard capability requests were checked against the wrong policy key

`tools/registry.py::invoke_capability()`'s Phase 1a policy gate checked the caller's *raw wildcard
query string* (e.g. `"impact.assessment.*"`) against `policies/agent_permissions.yaml`'s
`allowed_tools` — but policy entries are always written as concrete capabilities (e.g.
`capability:impact.assessment.finance_reporting`), never wildcard patterns. Every
wildcard-resolved capability call was denied regardless of what the target agent is actually
permitted to do, silently defeating Phase 1a's policy gate for this whole class of dispatch. Found
live via `handle_discovery_completed`'s cross-department finance-impact check (§20.3) — the exact
call this bug was guaranteed to hit, the first time it was ever exercised against the real policy
engine end to end.

Fixed: when the capability query ends in `.*`, resolve which of the matched card's own
`capabilities` entries it actually matched, and check policy against *that* concrete name. New
regression test:
`tests/test_registry.py::test_invoke_capability_with_a_wildcard_query_checks_policy_against_the_resolved_concrete_capability`.

### 6. A policy DENY crashed the whole handler instead of degrading gracefully

`trigger_finance_impact_check()`'s own docstring already promised "if it's ever deprecated, this
logs and returns None rather than crashing" — but only caught `registry.NoApprovedProvider`, not
`registry.CapabilityDenied`. Bug #5 above meant every call hit exactly the uncaught case, crashing
`handle_discovery_completed()` entirely — taking down the whole run's `ANALYZED -> RISK_ASSESSED`
progression over one *optional* cross-department check that's explicitly designed to be skippable.

Fixed: catch `CapabilityDenied` the same way as `NoApprovedProvider` — log and return `None`. New
regression test:
`tests/test_orchestrator.py::test_trigger_finance_impact_check_degrades_gracefully_on_policy_denial`.

## Live result

A real `migration.requested` publish against the live 9-service fleet, targeting the real Cloud SQL
estate, genuinely advanced:

```
REQUESTED -> DISCOVERED -> ANALYZED -> RISK_ASSESSED
pinned_agents: {'discovery-agent': '2.0.0', 'lineage-agent': '2.0.0', 'risk-agent': '1.1.0'}
```

Three real agents, resolved by capability, invoked for real, against real data read from a real
Cloud SQL Postgres instance over a real private network path — the deepest a live run driven purely
through the deployed Pub/Sub/Cloud-Run topology has ever gotten in this project.

## What's still open

- `RISK_ASSESSED -> PLANNED` (the Planner stage) had not yet been re-verified live as of this
  report — bugs 4-6 above were found and fixed in the same investigation that reached
  `RISK_ASSESSED`; a fresh run with all three fixes deployed is the next thing to confirm.
- Same architectural note as before: every stage so far ran in-process inside the orchestrator
  (`runtime.type=local`), not over the typed HTTP dispatch path to each agent's own deployed
  service. That's a real, separate, larger piece of follow-up work — this session deliberately
  scoped itself to "make discovery against Cloud SQL work," not "flip every AgentCard to
  `cloud_run` dispatch."
