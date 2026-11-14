# Phase 1 evidence index (generated)

Every batch under `benchmarks/evidence/`: its plan (written before any row), the code
commit(s) it ran, its parts and rows. Results and reasoning: `PHASE1_ENGINEERING_RECORD.md`.
Every row is synthetic fault injection on named, pinned servers, never production traffic.
Generated while B5 and S8 were running (their rows are partial here); regenerated when they end.

| Suite | Batch | Plan title | Code | Parts | Rows |
|---|---|---|---|---|---|
| realhttp | A2 | A2: adversarial cells J, H, C and B under the HTTP relay (written before any row) | 69079ed, fccee7c | 6 | 816 |
| realhttp | B1 | B1: HTTP baseline versus StateGuard, re-established in the repository (written before any row) | 237a332 | 6 | 3744 |
| realhttp | B2 | B2: the HTTP matrix again, on the current code (written before any row) | 1852365 | 6 | 3744 |
| realhttp | B3 | B3: the HTTP matrix again, on the Session 2–3 code (written before any row) | 51f6fdf | 6 | 3744 |
| realhttp | B4 | B4: the deadline arms on the full HTTP matrix, after decision 2 (written before any row) | f5af9a8 | 3 | 480 |
| realhttp | B5 | B5: the HTTP matrix on the final Phase 1 code (written before any row) | — | 1 | 39 |
| realhttp | C1 | C1: server crashes under the HTTP relay (written before any row) | 51f6fdf, 9692d4d | 4 | 720 |
| realhttp | D1 | D1: schema drift, category A (written before any row) | 7b3c4d2 | 2 | 600 |
| realhttp | E1 | E1: a slow keyed call under a deadline, before and after D10 (written before any row) | 1852365, 68b5594 | 3 | 84 |
| realhttp | E2 | E2: D10 with the TypeScript keyed server (written before any row) | ece85e7 | 1 | 36 |
| realhttp | E3 | E3: a slow declared read under a deadline (written before any row) | 72fc67b | 1 | 36 |
| realhttp | E4 | E4: the HTTP relay's default deadline recovers a hung keyed call (written before any row) | 570d262 | 2 | 36 |
| realhttp | I1 | I1: every relayed request is counted in the incidence records (written before any row) | a00a269 | 2 | 288 |
| realhttp | P1 | P1: HTTP relay overhead, paired and uncontended (written before any row) | 1852365 | 3 | 300 |
| realhttp | P2 | P2: the cost of event-stream answers on the relay's upstream connections (before any row) | 1852365 | 2 | 122 |
| realhttp | P4 | P4: HTTP relay overhead on the final Phase 1 code, paired and uncontended (written before any row) | 16e26b9 | 3 | 280 |
| realhttp | S1 | S1: the stale pooled connection race, relay to server (written before any row) | 8de3735 | 4 | 244 |
| realstdio | A1 | A1: adversarial cells for categories J, H, C and B (written before any row) | 87f43d4, c255b44 | 6 | 1320 |
| realstdio | P1 | P1 (stdio): relay overhead, paired and uncontended (written before any row) | 1852365 | 2 | 240 |
| realstdio | P3 | P3 (stdio): per-call overhead of a running relay (written before any row) | 0cee55c | 2 | 6000 |
| realstdio | P4 | P4 (stdio): per-call overhead of a running relay on the final Phase 1 code (written before any row) | 16e26b9 | 2 | 6000 |
| realstdio | S2 | S2: stdio baseline versus StateGuard on pinned real servers (written before any row) | 6181cc2 | 4 | 2208 |
| realstdio | S3 | S3: retrying a JSON-RPC -32603 where it is safe (written before any row) | a8ed63d | 2 | 192 |
| realstdio | S4 | S4: the stdio matrix in one batch on the current commit (written before any row) | 180fae6 | 4 | 2208 |
| realstdio | S5 | S5: crash windows of a declared idempotent tool (written before any row) | bc03223 | 1 | 288 |
| realstdio | S6 | S6: the stdio matrix on the new deadline default, and slow answers (written before any row) | ba310ac | 4 | 1176 |
| realstdio | S7 | S7: the stdio matrix on a third pinned server, server-filesystem (written before any row) | 8469759 | 1 | 624 |
| realstdio | S8 | S8: the stdio matrix on the final Phase 1 code, three servers (written before any row) | — | 0 | 0 |
