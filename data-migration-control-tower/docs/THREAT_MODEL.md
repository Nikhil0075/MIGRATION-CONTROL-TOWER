# Threat Model — Deploy & Harden

Lightweight STRIDE-level pass over the attack surface this deployment effort adds. Existing,
already-covered surface (prompt injection into agent reasoning, the policy engine's own decision
logic, Firestore-scoped run isolation) is not re-derived here — see `tests/test_injection_defense.py`
and `tools/policy_engine.py` for that. This doc covers what's genuinely new: a public console, a
distributed agent fleet talking over HTTP, a network-reachable Cloud SQL instance, and a real
billing target.

Status legend matches `docs/compliance_matrix.md`: ✅ mitigated and verified · ⚠️ partially
mitigated / mitigation planned but not yet live-verified · ❌ not yet addressed.

## 1. Public console (Phase 5)

| Threat | Vector | Mitigation | Status |
|---|---|---|---|
| Spoofing | Unauthorized sign-in via an unrestricted OAuth redirect or Firebase hostname | Firebase authorized-domain allowlist; OAuth redirect URI locked to the deployed hostname | ⚠️ planned, Phase 5 |
| Tampering | XSS injecting script into rendered estate/table names sourced from the legacy estate | Existing `esc()`-everywhere discipline (`tests/test_frontend_xss.py`); extend regression coverage to the live deployed build, not just local dev | ⚠️ existing local coverage; live-build regression pending |
| Repudiation | A destructive write (delete estate, force-approve) with no traceable justification | Human-approval separation-of-duties pattern (`tools/approval_service.py`) already exists for cutover; extend "confirmation + justification" requirement to every destructive write, not only cutover | ⚠️ planned, Phase 5 |
| Information disclosure | `/legacy` route or a dev/test-only endpoint left reachable in the deployed build | Disable or properly authenticate `/legacy`; strip dev/test endpoints from the production image | ❌ not yet addressed |
| Information disclosure | Report bucket or Firestore readable without going through the API's authorization checks | Private report storage bucket (no public read); audit Firestore security rules for any client-SDK direct-access path | ❌ not yet addressed |
| Denial of service | Unrate-limited login, report generation, or AI assistant endpoints | Rate limiting per endpoint class; assistant daily-message-limit/concurrency-limit env vars already exist (`ASSISTANT_DAILY_MESSAGE_LIMIT`, `ASSISTANT_CONCURRENCY_LIMIT`) — verify they're actually enforced server-side, not just declared | ⚠️ limits declared in `.env.example`; server-side enforcement needs verification |
| Elevation of privilege | Custom-claim RBAC bypass; one estate's assistant session answering across estates it shouldn't see | Custom-claim RBAC verification; assistant estate-isolation tests | ❌ not yet addressed |

## 2. Distributed agent fleet (Phase 2)

| Threat | Vector | Mitigation | Status |
|---|---|---|---|
| Spoofing | A non-Pub/Sub, non-orchestrator caller hitting an agent's push/HTTP endpoint directly | OIDC audience+issuer verification plus a caller-service-account allowlist on every `cloud_run`-runtime capability endpoint (ADR 0002) | ⚠️ designed, not yet built |
| Tampering | A malformed or oversized payload exploiting the receiving service's deserialization | Pydantic request/result schema validation, request size limits, on every endpoint | ⚠️ designed, not yet built |
| Repudiation | A capability call with no durable record of which identity invoked it | `invocation_id`/`trace_id` carried in the typed envelope; recorded alongside the existing `policy_decisions` audit trail | ⚠️ designed, not yet built |
| Information disclosure | A credential value accidentally serialized into the HTTP payload | Explicit rule: secrets never travel in the payload, resolved server-side by name only (ADR 0002) | ⚠️ designed, not yet built |
| Denial of service | An unbounded request flooding one agent service, or a hung request holding a Cloud Run instance indefinitely | Request deadlines; Cloud Run max-instance caps (Terraform-managed, Phase 2) | ⚠️ planned |
| Elevation of privilege | An over-broad `iam.serviceAccountUser`/`roles/run.admin` grant letting a compromised CI job impersonate any agent's identity | Per-service-account narrow grants, `roles/run.developer` preferred over `roles/run.admin` (ADR/Phase 2e) | ⚠️ planned |

## 3. Cloud SQL for PostgreSQL (Phase 3)

| Threat | Vector | Mitigation | Status |
|---|---|---|---|
| Spoofing / tampering | Public IP with a weak or leaked connection string | Cloud SQL Python Connector or Cloud Run's built-in Cloud SQL integration; no `0.0.0.0/0` authorized network, ever | ⚠️ planned |
| Information disclosure | Plaintext connection string in an env var or committed file | Secret Manager references only; IAM database auth where practical | ⚠️ planned |
| Elevation of privilege | The data-plane job's DB user having write access to the source | Dedicated **read-only** migration user — the executor never needs source write access | ⚠️ planned |
| Denial of service | Connection exhaustion at the smallest Cloud SQL tier's low connection ceiling | Explicit connection pooling with a max-connections limit | ⚠️ planned |
| Repudiation / data loss | No backup/PITR on a demo instance that turns out to matter | Backup + point-in-time recovery + deletion-protection configured even for the demo/staging profile | ⚠️ planned |

## 4. Billing / cost (Phase 1, 4)

| Threat | Vector | Mitigation | Status |
|---|---|---|---|
| Resource exhaustion → real spend | An unbounded BigQuery query, an unmonitored 20k-scale run, an idle-but-running Cloud SQL/Cloud Run instance past the funded window | Dry-run → reserve → cap BigQuery flow (ADR-adjacent, Phase 1c); full-stack cost estimate before any large run (Phase 4c); explicit teardown checkpoint at the verified end of the funded window (Phase 0) | ⚠️ planned |
| False sense of safety | Treating the Cloud Billing Budget alert as a spending cap | Documented explicitly in `docs/GOVERNANCE.md`: a budget is an alert, not an enforcement mechanism — it does not stop any service from continuing to bill | ✅ documented |

## Residual risk accepted for this deployment window

This is a trial-credit-funded demo/staging deployment for a fixed ~5-week window, not a
production launch. Explicitly accepted, not silently ignored:
- Cloud SQL runs a shared-core, single-zone profile — no HA. Acceptable for a demo window; would
  need revisiting for real production use (noted in `docs/ARCHITECTURE.md`).
- No formal penetration test — the injection-corpus regression suite and this threat model
  substitute for one at this scale.
- Secret rotation is policy-documented (Phase 5) but not automated on a schedule within this
  window.
