# Injection defense corpus (master doc §23.2)

Twelve self-authored adversarial strings across the four required
families, in `payloads.json`. `tests/test_injection_defense.py` is the
evaluation harness: for each case it (1) confirms the deterministic
scan (`tools/untrusted_content.py::scan_for_injection_patterns`)
detects it, (2) exercises a real, family-specific containment invariant
against this codebase's actual functions — not a mocked stand-in — and
(3) records a `containment_event` (`tools/untrusted_content.py::record_containment_event`).

| Family | Real invariant asserted |
|---|---|
| `direct_instruction_override` | The payload survives only as an inert string field (e.g. a Pipeline's `owner`); `tools/policy_engine.py::evaluate()` takes no free-text estate content as input at all, so its decision is structurally unaffected. |
| `tool_poisoning` | The fabricated tool name is never a capability any `tools/registry.py` card advertises — `discover()`/`resolve_capability_handler()` finds nothing, because capabilities only ever come from explicit `publish()` calls, never parsed estate content. |
| `privilege_escalation_by_assertion` | Calling `policy_engine.evaluate()` for the exact action the payload claims to grant (e.g. `production.write`, `approval.self_issue`) still returns `DENY`/`REQUIRE_APPROVAL` — permissions come only from `policies/agent_permissions.yaml` / the registry card. |
| `exfiltration_prompt` | The narrative/extraction functions that would embed this text in a Gemini prompt (`agents/orchestrator/recovery.py`, `tools/multimodal_discovery.py`) only ever call the one fixed Vertex AI endpoint — there is no code path that derives an outbound URL from estate content. |

Run: `pytest tests/test_injection_defense.py -v`
