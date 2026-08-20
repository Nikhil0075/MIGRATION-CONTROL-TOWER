# Bounded control-plane scale demonstration (master doc §32.12)

Generated 2026-08-20T15:38:07.888141+00:00 · 250 synthetic pipeline definitions.

**This is a control-plane-scale demonstration at 100-500 definitions, not the master doc's 20,000-definition bulk-data claim** — that would need a live estate that large to prove honestly, out of this phase's declared scope.

| Operation | Measure | Value |
|---|---|---|
| Schema validation (250 records) | p50 latency | 8.772 ms |
| Schema validation | p95 latency | 11.737 ms |
| Schema validation | total | 2161.12 ms |
| Wave Manager scheduling (250 items, one pass) | total duration | 5.71 ms |
| Wave Manager scheduling | avg per item | 0.0228 ms |
| Wave Manager scheduling | admitted / held | 6 / 244 |
| Wave Manager scheduling | backlog-escalated | 190 |
| Policy decisions (real Firestore writes, sampled) | sample size | 100 |
| Policy decisions | p50 latency | 386.31 ms |
| Policy decisions | p95 latency | 1216.4 ms |
