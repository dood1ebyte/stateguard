# CW2: crash windows on the TypeScript cooperating server (HTTP and stdio); written before any row

**Why (S3.3, deferred from S3 and delivered in S4 on 2026-11-25).** The TypeScript twin of the
cooperating notes server is `adapters/typescript/examples/cooperating_notes_server.ts`, built on
the TypeScript SDK and this repository's adapter (`KeyedTools.transactional`). Its note and its
key's record commit in one SQLite transaction. S3's acceptance asks for 0/36 on each relay and
each language delivered. CW1 covered Python; CW2 covers TypeScript.

**Setup.** The code is the commit named in `codestate_*.json`, the one this plan is committed
in, run with CW1's cells and arms:
- **realhttp** (`driver.py`), server `tscoop`, run with `--http`, answering with event streams:
  - `write/after_begin,after_effect,after_record`, 12 trials, TRIAL_OFFSET 33000;
  - `write/none`, TRIAL_OFFSET 33500;
  - arms raw, naive, sg, sg_keyed, sg_keyed_deadline.
  - Unkeyed arms run the server with `--no-keys`.
- **realstdio** (`../realstdio/driver.py`), server `tscoop`:
  - the crash cells, TRIAL_OFFSET 34000;
  - `read/none,write/none`, TRIAL_OFFSET 34500;
  - arms raw, naive, sg, sg_keyed.

**Prediction:** CW1's results for the Python server, cell for cell.
- **sg_keyed and sg_keyed_deadline:** 0/12 in every crash cell. The resend executes after a
  crash before the commit and replays after it. 0 duplicates.
- **sg (`--no-keys`):** 12/12 surfaced UNKNOWN in each crash cell, never success.
- **Fault-free:** 0/12.
- **0 unsafe outcomes** in every StateGuard arm.
- **Baselines:** raw gives false failures; naive gives duplicates on stdio and unanswered calls
  on HTTP, as in CW1.

**Boundary.** As for CW1: closure for this server's own effect, in the key store's
transaction, only. Not for any unmodified server or any other effect.

Synthetic fault injection (TEST-ONLY SIGKILL at the point); not production traffic.
