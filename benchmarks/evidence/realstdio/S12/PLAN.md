# S12: the stdio matrix on S4's final relay code (written before any row)

**Why.** S4.3 (`572c74e`) adds `--serialize-tools` to the stdio relay. With the flag unset
(every default cell), the lane is never used:
- calls take the same path;
- the server pump checks one empty dict per answer;
- one lock is taken per server loss.
S12 checks that the matrix does not change on the final code `0dd8604`.

**Setup.** S10's parts and commands exactly, on `0dd8604`, through `stateguard relay stdio`:
- sqlite, memory and filesystem: TRIAL_OFFSET 22000;
- memory_idempotent: TRIAL_OFFSET 22000;
- the slow parts: TRIAL_OFFSET 23000;
- keys_auto: TRIAL_OFFSET 24000.

**Prediction:** S10, cell for cell, in every StateGuard arm (filesystem against
`filesystem_r2`), with 0 unsafe outcomes.

Synthetic fault injection on pinned servers; not production traffic.
