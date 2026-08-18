# Cutover Agent (Day 5)

Executes the controlled final migration step. Cannot self-approve —
`approval.self_issue` is denied by the policy engine, and
`tools/approval_service.approve()` is only ever called by
`approve_cutover.py`, a script standing in for a human, never by
`agent.py`.

```bash
python agents/cutover/run_cutover.py [run_id]     # request approval (state PASSED)
python agents/cutover/approve_cutover.py [run_id]  # the human step
python agents/cutover/run_cutover.py [run_id]     # perform cutover + monitoring (state APPROVED)
```

`run_cutover.py` dispatches on the run's current state, so it's the same
command before and after the human approves. Every invocation also
re-proves the self-approval denial (§12's Phase-5 exit condition).
Transitions `PASSED -> READY_FOR_APPROVAL -> APPROVED -> CUTOVER ->
MONITORING -> COMPLETE`.
