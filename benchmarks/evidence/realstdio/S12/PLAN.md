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

## Commit changed before any row of this batch: `d429817`, not `0dd8604`

Recorded at 10:05 IST on 2026-11-25, before any row of this batch.
- After the plan was written, S3.2 changed the injector and the preload: the recovery-check
  hook, inert unless `STATEGUARD_RECOVERY_CHECK` is set. S3.3 added the TypeScript cooperating
  server, a new file.
- The keyed arms here run those adapters, so the batch runs on the session's final code,
  `d429817`.
- The relays are unchanged since `0dd8604`.
- The prediction is unchanged.
