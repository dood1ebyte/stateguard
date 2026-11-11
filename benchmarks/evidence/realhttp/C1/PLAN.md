# C1: server crashes under the HTTP relay (written before any row)

**Why.** Phase 1's HTTP evidence (B1, B2) covers transport faults and session loss, never a
server process that dies mid-call. The stdio matrix (S2, S4) covers crashes. This batch adds
them for HTTP: recovery after partial execution, and duplicate prevention.

**Setup.** `driver.py`:
- notes (Python, injector) and tsnotes (TypeScript, preload);
- cells `write/after_begin`, `write/after_effect`, `write/after_record`. The server kills
  itself (SIGKILL) at that point inside `add_note`: keyed arms through the key's two phases,
  unkeyed arms around the unmodified handler (fault-only mode). A supervisor restarts it on the
  same port, as systemd's `Restart=` would.
- The client and relay reach the server directly: no fault proxy. A proxy in front of a dead
  server turns "refused" into "lost", which the dry run showed distorts the relay's recovery.
- every default arm (raw, naive, sg, sg_keyed, sg_keyed_deadline); 12 trials,
  TRIAL_OFFSET=10000, 4 workers; the code is this commit.

**Harness change in this commit:** a keyed server's `-32010`/`-32011` answer is no longer
scored `false_failure`, as already in the stdio driver. It is unresolved. No recorded HTTP row
had one with that kind (checked).

**Expectations:**
- raw and naive fail every crash row; naive may duplicate `after_effect` and `after_record`
  if its resend reaches the restarted server;
- sg (unkeyed): every row unresolved (`unknown`), never resent, no duplicate;
- sg_keyed and sg_keyed_deadline: `after_record` replayed (0/12); `after_begin` and
  `after_effect` in doubt (`-32010`), the floor without a key-bound check (the notes tool is
  not idempotent);
- 0 unsafe outcomes in StateGuard arms; 0 record mismatches.

## Addendum (before the `*_d12` parts' rows): the same cells on D12

The first parts (code `9692d4d`) replayed only 9/12 and 10/12 keyed `after_record` rows. The
immediate keyed resend reached the dying server and was reset, spending the last attempt. D12
(`51f6fdf`) waits `--resend-backoff` (0.2 s) before a resend after a transport loss.

Parts `notes_d12` and `tsnotes_d12` repeat the same cells, arms and trials (TRIAL_OFFSET=11000)
on `51f6fdf`.

**Expectations:**
- keyed `after_record` 0/12 per server;
- keyed `after_begin` and `after_effect` still in doubt (the floor);
- unkeyed arms unchanged;
- 0 unsafe outcomes, 0 record mismatches.
