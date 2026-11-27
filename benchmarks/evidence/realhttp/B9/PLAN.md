# B9: the HTTP matrix on S4's final relay code (written before any row)

**Why.** S4.4 (`0dd8604`) changes how the HTTP relay uses its connections to the server. An
event stream answered by the server is now read to its end in the background, so the connection
goes back to the pool and is reused for the next request. Phase 1's matrix ran with one new
connection per event-stream call.
- **The risk:** after a server crash and restart on the same port, the next request is written
  to a pooled connection to the dead process.
- The relay already bounds a connection's idle reuse (`--pool-idle-timeout`, 1 s), and a
  request lost on a dead connection is resolved by the same rules as any lost request: a keyed
  or declared-repeatable call is resent, and an unkeyed write is UNKNOWN.
- B9 checks that the matrix does not change.

**Setup.** B7's parts and commands exactly, on `0dd8604`, through `stateguard relay http`:
- notes, tsnotes and everything: 12 trials, TRIAL_OFFSET 0;
- the controls: 60 trials, TRIAL_OFFSET 1000;
- crash_notes and crash_tsnotes: TRIAL_OFFSET 11000.

**Prediction:**
- B7, cell for cell, in every StateGuard arm, with 0 unsafe outcomes;
- a difference in a baseline arm is reported as run-to-run variation;
- any StateGuard-arm difference is reported, investigated, and either fixed before S4 closes
  or recorded as a blocker.

Synthetic fault injection; not production traffic.

## Run log

- **09:41 IST:** the `notes` part was started by mistake while B8 was still running: a queue
  script's wait matched no process. It was stopped about a minute later, after 51 rows, so
  that the two matrices would not load the machine together.
  - Those rows are kept as `rows_notes_interrupted.jsonl`, are not used in any comparison, and
    were run under double load.
  - The part is re-run from the start as `notes_r2` once B8 and S11 have finished.

## Commit changed before any row of this batch: `d429817`, not `0dd8604`

Recorded at 10:05 IST on 2026-11-25, before any row of this batch.
- After the plan was written, S3.2 changed the injector and the preload: the recovery-check
  hook, inert unless `STATEGUARD_RECOVERY_CHECK` is set. S3.3 added the TypeScript cooperating
  server, a new file.
- The keyed arms here run those adapters, so the batch runs on the session's final code,
  `d429817`.
- The relays are unchanged since `0dd8604`.
- The prediction is unchanged.

## Run log (continued)

- **2026-11-25, about 10:31 IST:** S4 stopped while the `notes_control` part was running, with
  223 rows written. The container was later replaced, and the cause was not recorded.
  - The rows are kept as `rows_notes_control_interrupted.jsonl` and are not used.
  - `notes_r2` had finished and is committed.
- **2026-11-27 (S5):** the remaining parts, from `notes_control` on, are re-run on the same
  commit `d429817`, in the background.
