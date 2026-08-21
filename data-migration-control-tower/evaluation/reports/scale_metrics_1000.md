# Control-plane planning and scheduling benchmark (master doc §32.12)

Generated 2026-08-21T20:38:54.877160+00:00 · 1000 synthetic pipeline definitions.

**This is a 1,000-object CONTROL-PLANE planning and scheduling benchmark — it moves zero rows and zero bytes.** Never cite this as an "1,000-object migration"; see evaluation/data_plane_scale_test.py for real rows/bytes moved and evaluation/load_test.py for concurrent operational load — three distinct measurements (docs/EVALUATION.md), not one number.

Model calls: **0** (control-plane-only — this harness never calls a model at any scale).

| Operation | Measure | Value |
|---|---|---|
| Schema validation (1000 records) | p50 latency | 7.306 ms |
| Schema validation | p95 latency | 11.04 ms |
| Schema validation | total | 7410.13 ms |
| Schema validation | throughput | 135.0 records/sec |
| Wave Manager scheduling (1000 items, one pass) | total duration | 76.17 ms |
| Wave Manager scheduling | avg per item | 0.0762 ms |
| Wave Manager scheduling | throughput | 13128.4 items/sec |
| Wave Manager scheduling | admitted / held | 6 / 994 |
| Wave Manager scheduling | backlog-escalated | 765 |
| Policy decisions (real Firestore writes, sampled) | sample size | 100 |
| Policy decisions | p50 latency | 343.1 ms |
| Policy decisions | p95 latency | 1169.48 ms |
