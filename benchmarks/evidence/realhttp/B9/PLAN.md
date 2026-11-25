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
