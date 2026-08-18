# Migration Control Tower UI

The production console is an Oracle JET 20.1.3 VDOM application using Preact,
TypeScript, Core Pack responsive drawers, and the native Redwood theme.
FastAPI remains the same-origin API and static runtime. The prior vanilla
interface is temporarily retained at `/legacy` for one acceptance release.

## Run locally

```bash
cd frontend/client
npm ci
npm run generate:api
npm run typecheck
npm test
npm run build
cd ../..
uvicorn frontend.app:app --reload --port 8080
```

Open <http://localhost:8080/overview>. Deep links are served by FastAPI's SPA
fallback. `index.html` is never cached; fingerprinted release assets use an
immutable one-year cache policy.

## Authentication and roles

Configure the modular Firebase web SDK with `FIREBASE_API_KEY`,
`FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, and `FIREBASE_APP_ID` (the
gitignored legacy `static/firebase-config.js` remains a local compatibility
source). Every `/api/v1` data endpoint verifies the Firebase ID token.

Roles are Firebase custom claims:

- `viewer` reads console data.
- `operator` also starts assessments and supported runs, retries eligible
  work, and manages time-bounded wave overrides.
- `approver` also approves a cutover that is ready for approval.

`operator` and `approver` imply viewer access. A token with no recognized role
is authenticated but receives `403`. `APPROVER_ALLOWLIST` remains a one-release
compatibility fallback and grants only approver/viewer access; remove it after
custom claims are populated.

All writes require an `Idempotency-Key`, a typed justification, a role check,
and an append-only audit record. They create a durable Firestore operation
request and publish the corresponding Pub/Sub command, returning `202`.

## Console workspaces

The responsive shell provides Overview, Estates, Assessments, Waves, Runs,
Lineage, Reconciliation, Policies & Approvals, Agents, Evaluations, and System
Health. Desktop layouts use navigation + workspace + inspector; tablet uses
overlay drawers; mobile uses single-column content and a bottom inspector.
Statuses use icon/shape and text in addition to color.

The API never exposes source credentials. Source adapters record sanitized
connection-health snapshots. Cost and volume values are shown only when
measured; unavailable values carry an explicit `not_configured` or `stale`
state. Billing remains unavailable until `CLOUD_BILLING_EXPORT_TABLE` and a
durable billing snapshot are configured.

## API and client contract

New functionality uses typed `/api/v1` envelope endpoints with RFC 3339
timestamps, integer byte counts, millisecond durations, explicit freshness,
and cursor pagination. Existing `/api/*` endpoints remain for one compatibility
release. `npm run generate:api` exports FastAPI OpenAPI and generates
`src/generated/api-schema.ts`; `src/generated/client.ts` is the typed
same-origin client.

## Build and test

- `npm run typecheck` — strict TypeScript validation.
- `npm test` — Vitest + Testing Library route and evidence-presentation tests.
- `npm run test:e2e` — Playwright snapshots for all eleven routes, responsive
  checks at 1440/1024/768/390 px, keyboard navigation, axe, table controls,
  and status semantics. The command compiles a test-only authenticated shell;
  a normal `npm run build` hard-disables and tree-shakes that fixture.
- `npm run build` — hashed Oracle JET release bundle with lazy Lineage and
  Evaluations modules.
- `pytest tests/test_frontend_v1.py` — API contract, RBAC, idempotency-header,
  CSP, deep-link, and asset-cache coverage.

`frontend/Dockerfile` is a multi-stage Node 22 / Python 3.11 build. Deploy it
from the repository root so the backend can import sibling `agents/` and
`tools/` packages. The image needs Firestore read access and scoped Pub/Sub
publisher access; it does not need SQL Server or source credentials.
