# Risk & Compliance Agent (Day 3, extended Day 8)

Scores PII exposure, SQL-dialect incompatibility, and critical-dependency
risk across a run's discovered catalog. As of Block C/Day 8, also runs a
cheap first-pass sensitivity screen and a multimodal documentation-drift
check. Tools (`agent.py`): `classify_estate`, `run_fast_pii_prescreen`,
`assess_documentation_drift`, `verify_pii_access_boundary`.

Classification itself is deterministic (`tools/data_classifier.py`
against `policies/data_classification.yaml`) and policy decisions are
deterministic (`tools/policy_engine.py` against
`policies/agent_permissions.yaml`) — this agent orchestrates those calls
and records findings; it does not make the PII/permission judgment call
itself (master doc §9).

```bash
python agents/risk/run_risk.py [run_id]   # defaults to the most recent run
```

Updates `migration_runs/{run_id}/catalog/*` classifications in place and
writes `migration_runs/{run_id}/risk_findings/*`. Transitions run state
to `RISK_ASSESSED`.

## Day 8 additions (master doc §22)

- **`run_fast_pii_prescreen`** (`tools/fast_pii_screen.py`) — a cheap,
  broad, deliberately imprecise screen standing in for a self-hosted
  Gemma model (documented substitution — see that module's docstring
  for why). Disagreements with the careful classifier, in *either*
  direction, are recorded as `SENSITIVITY_SCREEN_DISAGREEMENT` findings
  rather than silently resolved.
- **`assess_documentation_drift`** (`tools/multimodal_discovery.py`) —
  extracts the documented schema from `simulator/documentation/`'s ERD
  image and PDF data dictionary (real Gemini vision/file call attempted
  first, deterministic fallback if unavailable) and diffs it against
  the run's real catalog: `MISSING_IN_ACTUAL`, `MISSING_IN_DOCUMENTED`,
  `TYPE_DIVERGENCE`, `CLASSIFICATION_GAP` findings, each with evidence
  tracing back to the source artifact.
