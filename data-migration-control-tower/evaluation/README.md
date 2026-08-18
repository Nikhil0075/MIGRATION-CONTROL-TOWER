# evaluation/ (Day 10, master doc §17.2 Fri 28 Aug, Appendix D)

- `scenarios.py` — the fourteen S-01…S-14 scenario functions, each
  exercising real code against real infrastructure.
- `run_harness.py` — entrypoint; runs the full catalog, writes
  `reports/{harness_run_id}.{json,md}`.
- `baseline_timer.py` — real stopwatch for the six §25.1 manual
  activities; writes to Firestore's `operational_baseline` collection.
- `friction_report.py` — combines those manual timings with live
  fleet-measured values into `reports/friction_table.md` (§25.2).
- `reports/` — generated output only; nothing in this directory is
  written by hand.

See the root [README.md](../README.md)'s Day 10 section for how these
fit together and for the cost-control reasoning behind sharing one
expensive migration fixture across four of the fourteen scenarios.
