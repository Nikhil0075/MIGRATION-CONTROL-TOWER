# tools

Shared, deterministic tool functions called by agents (never the other
way around — tools have no agent-framework dependency, so they're
directly unit-testable, per master doc §9).

| File | Used by | Purpose |
|---|---|---|
| `firestore_client.py` | all agents | thin Firestore connection + read/write helper |
| `sqlserver_client.py` | hello-agent, source_catalog | SQL Server connection + simple table listing |
| `source_catalog.py` | Discovery Agent | full estate introspection: SQL Server, Oracle corpus, DAG artifacts, validated against `contracts/metadata_model.json` |
| `data_classifier.py` | Risk Agent | deterministic PII/classification rules against `policies/data_classification.yaml` |
| `policy_engine.py` | every agent's tool boundary | the §5.1 ALLOW/DENY/REQUIRE_APPROVAL decision point against `policies/agent_permissions.yaml`, audited to Firestore |
| `lineage_graph.py` | Lineage Agent | derives Dependency edges from DAG refs + regex SQL view parsing — never seeded |
| `events.py` | orchestrator | real Pub/Sub publish/pull |
| `bigquery_tools.py` | migration executor, Validation, Cutover | BigQuery load/query helpers |
| `migration_executor.py` | Planner's first pass, failure injector, recovery | the single real source->BigQuery copy path |
| `reconciliation.py` | Validation Agent | five deterministic checks: schema, row_count, aggregate, null_profile, hash |
| `plan_builder.py` | Migration Planner | deterministic execution-order + plan-hash logic |
| `approval_service.py` | Cutover Agent (request/consume) + a human script (approve) | the separation-of-duties human approval gate |
| `registry.py` | orchestrator | Agent Registry: publish/approve/discover/resolve/deprecate; `invoke_capability()` dynamically dispatches to a resolved card's handler — no hardcoded agent names |
| `memory_bank.py` | recovery loop | cross-run remediation facts, global collection (not run-scoped) keyed by defect signature |
| `multimodal_discovery.py` | Risk Agent | Gemini vision/file extraction of documented schemas (ERD image, PDF data dictionary) with a deterministic fallback; diffs them against the real catalog into drift findings |
| `fast_pii_screen.py` | Risk Agent | cheap, broad sensitivity screen (documented Gemma substitution); disagreements with the careful classifier are recorded, not resolved |
| `untrusted_content.py` | anywhere estate content reaches a model prompt | the §23.1 envelope shape, a deterministic injection-pattern scan, and containment-event recording |

Later days add `sql_parser.py` (richer dialect translation for the
Migration Planner) per the master doc's repository structure (§14).
