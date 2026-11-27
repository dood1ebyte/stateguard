# CW2 results: crash windows on the TypeScript cooperating server (HTTP and stdio)

**Code:** `e22ce8e` (S3.3), run through `run_batch.py` on a clean worktree on 2026-11-25. The
plan, `PLAN.md`, was committed in the same commit, before any row, in both `realhttp/CW2/` and
`realstdio/CW2/`.
- **Server:** `adapters/typescript/examples/cooperating_notes_server.ts`, on the TypeScript SDK
  with this repository's adapter. Its note and the key's record commit in one SQLite
  transaction.
- **Faults:** synthetic fault injection (TEST-ONLY SIGKILL at the point); not production
  traffic.
- **Trials:** 12 per cell.

| Relay | Arm | after_begin | after_effect | after_record | Fault-free | Unsafe |
|---|---|---|---|---|---|---|
| HTTP | sg_keyed | 0/12 | 0/12 | 0/12 | 0/12 | 0 |
| HTTP | sg_keyed_deadline | 0/12 | 0/12 | 0/12 | 0/12 | 0 |
| stdio | sg_keyed | 0/12 | 0/12 | 0/12 | 0/12 (read and write) | 0 |
| HTTP | sg (the same server, `--no-keys`) | 12/12 surfaced UNKNOWN | 12/12 | 12/12 | 0/12 | 0 |
| stdio | sg (`--no-keys`) | 12/12 surfaced UNKNOWN | 12/12 | 12/12 | 0/12 | 0 |

**As predicted, the same as CW1's Python server:**
- **Keyed arms:** 0/36 unresolved and 0 duplicates, on each relay.
  - The resend's outcome is `executed` after a crash before the commit (the transaction rolled
    back), and `replayed` after it.
  - The note exists exactly once in every row (ground truth: the server's `notes` table).
- **Unkeyed StateGuard arm:** never success; every crash is surfaced as UNKNOWN.

**Baselines (not StateGuard configurations):**
- **stdio:** as CW1. raw gives 36/36 false failures; naive gives 36/36 duplicates.
- **HTTP:** naive has 36/36 calls with no answer (the client saw its connection drop), as CW1.
  raw differs from CW1:
  - 36/36 had no answer at all, where CW1's Python server gave error answers (false failures);
  - this is how the client experienced the TypeScript server's death;
  - neither arm runs StateGuard.

**What this does and does not show.** With CW1, S3's acceptance holds for both languages
delivered: 0/36 on each relay. That is crash-window closure **for the cooperating servers'
own effect only**: the note, in the key store's database, in the same transaction as the key's
record. It is not shown for any unmodified server or for any other effect; those keep their
floor (in doubt or UNKNOWN, never re-run).
