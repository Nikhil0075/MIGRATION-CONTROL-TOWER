# Control-plane planning and scheduling benchmark (master doc §32.12)

Generated 2026-08-21T20:40:38.186206+00:00 · 5000 synthetic pipeline definitions.

**This is a 5,000-object CONTROL-PLANE planning and scheduling benchmark — it moves zero rows and zero bytes.** Never cite this as an "5,000-object migration"; see evaluation/data_plane_scale_test.py for real rows/bytes moved and evaluation/load_test.py for concurrent operational load — three distinct measurements (docs/EVALUATION.md), not one number.

Model calls: **0** (control-plane-only — this harness never calls a model at any scale).

| Operation | Measure | Value |
|---|---|---|
| Schema validation (5000 records) | p50 latency | 6.128 ms |
| Schema validation | p95 latency | 10.148 ms |
| Schema validation | total | 32783.4 ms |
| Schema validation | throughput | 152.5 records/sec |
| Wave Manager scheduling (5000 items, one pass) | total duration | 64.63 ms |
| Wave Manager scheduling | avg per item | 0.0129 ms |
| Wave Manager scheduling | throughput | 77368.2 items/sec |
| Wave Manager scheduling | admitted / held | 6 / 4994 |
| Wave Manager scheduling | backlog-escalated | 3791 |
| Policy decisions (real Firestore writes, sampled) | sample size | 100 |
| Policy decisions | p50 latency | 300.11 ms |
| Policy decisions | p95 latency | 1182.15 ms |
