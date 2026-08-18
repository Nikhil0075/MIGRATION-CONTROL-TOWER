# Bounded control-plane scale demonstration (master doc §32.12)

Generated 2026-08-16T17:47:54.446031+00:00 · 250 synthetic pipeline definitions.

**This is a control-plane-scale demonstration at 100-500 definitions, not the master doc's 20,000-definition bulk-data claim** — that would need a live estate that large to prove honestly, out of this phase's declared scope.

| Operation | Measure | Value |
|---|---|---|
| Schema validation (250 records) | p50 latency | 5.465 ms |
| Schema validation | p95 latency | 8.77 ms |
| Schema validation | total | 1455.97 ms |
| Wave Manager scheduling (250 items, one pass) | total duration | 2.52 ms |
| Wave Manager scheduling | avg per item | 0.0101 ms |
| Wave Manager scheduling | admitted / held | 6 / 244 |
| Wave Manager scheduling | backlog-escalated | 190 |
| Policy decisions (real Firestore writes, sampled) | sample size | 100 |
| Policy decisions | p50 latency | 276.72 ms |
| Policy decisions | p95 latency | 1183.76 ms |
