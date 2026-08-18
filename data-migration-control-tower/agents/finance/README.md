# Finance Reporting Impact Agent (Block C, §20.3)

The cross-department discovery proof: owned by "Finance Systems" (not
Technology, which owns the migration fleet), published and approved by
distinct Finance-Systems identities
(`infrastructure/seed_finance_agent.py`), and discovered/invoked by
`agents/orchestrator/orchestrator.py` purely via an
`impact.assessment.*` wildcard capability query — the orchestrator has
no import of, or hardcoded knowledge about, this module.

`assess_impact(run_id)` uses real Lineage data: it finds discovered SQL
views whose name looks like a finance report (contains "REVENUE",
"FINANCE", "P&L", or "LEDGER" — the real match today is
`SH.V_QUARTERLY_REVENUE_BY_CHANNEL`) and reports which upstream source
tables feed them.

```bash
python infrastructure/seed_finance_agent.py             # publish + approve
python infrastructure/seed_finance_agent.py --deprecate  # the negative proof
```

After deprecating, re-running the orchestrator logs "no approved
provider" and continues rather than crashing — see the root README's
Block C section.
