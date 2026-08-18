# Infrastructure

`gcp_setup.sh` — idempotent gcloud bootstrap for Day 1 (see root README
for prerequisites and usage). Enables APIs, creates the Firestore
database, BigQuery dataset, the §8.1 Pub/Sub event topics, and the
`sa-orchestrator` service account.

## Agent framework note (Rung-2 substitution, §19)

The master doc specifies Google ADK as the agent framework. If
`google-adk` is not installable/available in a given environment, the
agent code in `agents/orchestrator/hello_agent/` and `agents/discovery/`
falls back to a thin wrapper directly over `google-generativeai`
(Gemini) with the same tool-call interface — a documented Rung-2
substitution, not a silent one. Check each agent's `agent.py` docstring
for which path is active.

## Region

All resources default to `us-central1` to keep Cloud Run, Firestore, and
BigQuery co-located and avoid cross-region latency/egress. Override via
`GCP_REGION` in `.env` if needed.

## Deferred to later build days

- Per-agent service accounts + Agent Identity (Block C, 24 Aug, §17.2)
- Agent Registry service (Block C, 24 Aug, §20)
- Agent Gateway / centralized policy enforcement (Block C)
- Model Armor / guardrails (Block C)
