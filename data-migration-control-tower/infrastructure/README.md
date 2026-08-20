# Infrastructure

`gcp_setup.sh` — idempotent gcloud bootstrap for Day 1 (see root README
for prerequisites and usage). Enables APIs, creates the Firestore
database, BigQuery dataset, the §8.1 Pub/Sub event topics, and the
`sa-orchestrator` service account.

## Agent framework and model runtime

The deployment pins `google-adk==2.7.1` and `google-genai==2.19.0`.
Reasoning V2 calls Vertex AI through `google-genai`; the removed
`vertexai.generative_models` and legacy `google-generativeai` packages are
not used. Discovery, Lineage and Planner require a validated structured
Gemini result when the feature is enabled. Deterministic adapters, policy,
state, reconciliation and approval controls remain authoritative.

`gcp_setup.sh` also creates a private, uniform-access report bucket with a
30-day retention policy, grants the control-tower identity create/read-only
object access, and enables Firestore TTL for assistant sessions, messages and
safety events.

## Region

All resources default to `us-central1` to keep Cloud Run, Firestore, and
BigQuery co-located and avoid cross-region latency/egress. Override via
`GCP_REGION` in `.env` if needed.

## Public-release feature gates

Keep `ENABLE_AGENT_REASONING_V2`, `ENABLE_REPORTS`, and
`ENABLE_AI_ASSISTANT` disabled until staging has a configured report bucket,
Vertex AI access, Firebase roles and a successful live acceptance run. These
flags default off.
