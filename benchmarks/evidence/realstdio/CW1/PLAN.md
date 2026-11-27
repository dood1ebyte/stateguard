# CW1: crash windows on a cooperating server, over HTTP and stdio (written before any row)

**Why (Phase 2 S3.4).** The cooperating notes server (`examples/mcp/cooperating_notes_server.py`)
commits a keyed note and its key's record in one SQLite transaction. Its crash window should be
closed: a crash before the commit leaves nothing, and the relay's keyed resend executes once; a
crash after it is replayed. This holds **only for this cooperating server**. The
non-cooperating servers must be unchanged from Phase 1 (in doubt or unknown, never success).

**Setup.** The code is this commit. TEST-ONLY crash faults (SIGKILL at the point), 12 trials.
The server is restarted after it dies: a supervisor on HTTP, `--restart 2
--restart-policy after-unknown` on stdio.

realhttp (`driver.py`):
- `coopnotes_crash`: `coopnotes 12 write/after_begin,write/after_effect,write/after_record`,
  every default arm (raw, naive, sg, sg_keyed, sg_keyed_deadline), TRIAL_OFFSET 31000;
- `coopnotes_none`: `coopnotes 12 write/none`, every default arm, TRIAL_OFFSET 31500;
- non-cooperating, unchanged: `notes` and `tsnotes`, the crash cells, arms sg_keyed and
  sg_keyed_deadline, TRIAL_OFFSET 11000 (B6's commands). Compared with B6 cell for cell.

realstdio (`../realstdio/driver.py`):
- `coop_crash`: `coop 12 write/after_begin,write/after_effect,write/after_record`, every
  default arm (raw, naive, sg, sg_keyed), TRIAL_OFFSET 32000;
- `coop_none`: `coop 12 read/none,write/none`, every default arm, TRIAL_OFFSET 32500;
- non-cooperating, unchanged: `sqlite`, `memory` and `filesystem`, the crash cells, arm sg_keyed,
  TRIAL_OFFSET 22000 (S8/S9's offset). Compared with S10's sg_keyed crash cells.

The cooperating server runs keyed in keyed arms and with `--no-keys` in the others, from the
same code. Ground truth is the count of the marker's rows in its notes table.

**Prediction:**
- **Cooperating server, keyed arms:** **0/36 unresolved and 0 duplicates**, on each relay.
  `after_begin` and `after_effect` are answered `executed` (the first transaction rolled back);
  `after_record` is answered `replayed`. Fault-free: 0/12.
- **Cooperating server, unkeyed arms:**
  - sg: 36/36 failed, surfaced unknown, 0 unsafe;
  - raw: 36/36 failed;
  - naive: duplicates where its blind resend re-runs a committed note. These baselines are not
    StateGuard configurations.
- **Non-cooperating servers:** identical to Phase 1:
  - `after_begin` and `after_effect` 12/12 unresolved (in doubt or unknown, never success);
  - `after_record` 0/12;
  - 0 unsafe.

Any other difference is a regression to explain or fix.

Synthetic fault injection on controlled and pinned servers; not production traffic. The closure
claimed here is for this cooperating server's effect only (its note in the key store's own
database). An unmodified server, or an effect outside that transaction, keeps its floor.
