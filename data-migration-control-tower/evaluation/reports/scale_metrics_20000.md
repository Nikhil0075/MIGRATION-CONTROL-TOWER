# Control-plane planning and scheduling benchmark (master doc §32.12)

Generated 2026-08-21T20:44:29.314355+00:00 · 20000 synthetic pipeline definitions.

**This is a 20,000-object CONTROL-PLANE planning and scheduling benchmark — it moves zero rows and zero bytes.** Never cite this as an "20,000-object migration"; see evaluation/data_plane_scale_test.py for real rows/bytes moved and evaluation/load_test.py for concurrent operational load — three distinct measurements (docs/EVALUATION.md), not one number.

Model calls: **0** (control-plane-only — this harness never calls a model at any scale).

| Operation | Measure | Value |
|---|---|---|
| Schema validation (20000 records) | p50 latency | 7.85 ms |
| Schema validation | p95 latency | 11.9 ms |
| Schema validation | total | 157564.88 ms |
| Schema validation | throughput | 126.9 records/sec |
| Wave Manager scheduling (20000 items, one pass) | total duration | 150.81 ms |
| Wave Manager scheduling | avg per item | 0.0075 ms |
| Wave Manager scheduling | throughput | 132613.5 items/sec |
| Wave Manager scheduling | admitted / held | 6 / 19994 |
| Wave Manager scheduling | backlog-escalated | 15350 |
| Policy decisions (real Firestore writes, sampled) | sample size | 100 |
| Policy decisions | p50 latency | 457.88 ms |
| Policy decisions | p95 latency | 1116.42 ms |
