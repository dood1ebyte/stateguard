# CW1 results: crash windows on a cooperating server (HTTP and stdio)

**Code:** `7022ff3`, run through `run_batch.py` on a clean worktree. The relays and the
cooperating server are unchanged from the plan's commit `6b0de13`; only tests changed between
them. The plan, `PLAN.md`, was written before any row, in both `realstdio/CW1/` and
`realhttp/CW1/`. This is synthetic fault injection (TEST-ONLY SIGKILL at the point) on
controlled and pinned servers, not production traffic. Run on 2026-11-25. It was planned in S3
(2026-11-23), which stopped before running it.

## The cooperating notes server (`examples/mcp/cooperating_notes_server.py`)

The note and its key's record commit in one SQLite transaction. Each cell is 12 trials.

| Relay | Arm | after_begin | after_effect | after_record | Fault-free | Unsafe |
|---|---|---|---|---|---|---|
| HTTP | sg_keyed | 0/12 | 0/12 | 0/12 | 0/12 | 0 |
| HTTP | sg_keyed_deadline | 0/12 | 0/12 | 0/12 | 0/12 | 0 |
| stdio | sg_keyed | 0/12 | 0/12 | 0/12 | 0/12 | 0 |
| HTTP | sg (the same server, `--no-keys`) | 12/12 surfaced unknown | 12/12 | 12/12 | 0/12 | 0 |
| stdio | sg (`--no-keys`) | 12/12 surfaced unknown | 12/12 | 12/12 | 0/12 | 0 |

- **Keyed arms:**
  - **0/36 unresolved and 0 duplicates, on each relay**, as predicted.
  - Every relay record agrees with ground truth: the note's rows in the server's table.
  - The resend's outcome is `executed` after a crash before the commit (the first transaction
    rolled back) and `replayed` after it.
- **Unkeyed StateGuard arm (sg):** never success. Every crash is surfaced as UNKNOWN.
- **Baselines (not StateGuard configurations):** they fail as expected.
  - raw: 36/36 false failures on each relay (the client is told it failed; the note may have
    committed).
  - naive: 36/36 duplicates on stdio (its blind resend runs a committed note again), and 36/36
    unanswered crashes on HTTP.

## The non-cooperating servers: identical to Phase 1

| Relay | Server | Arm | Crash cells | Compared with | Result |
|---|---|---|---|---|---|
| HTTP | notes | sg_keyed, sg_keyed_deadline | 24/36 each | B6 | identical, cell for cell |
| HTTP | tsnotes | sg_keyed, sg_keyed_deadline | 24/36 each | B6 | identical |
| stdio | sqlite | sg_keyed | 24/36 | S10 | identical |
| stdio | memory | sg_keyed | 24/36 | S10 | identical |
| stdio | filesystem | sg_keyed | 24/36 | S10 (`filesystem_r2`) | identical |

- `after_begin` and `after_effect` are 12/12 unresolved: in doubt or unknown, never success.
- `after_record` is 0/12.
- 0 unsafe outcomes.
- The per-part comparisons are in `summary_*.compare.txt`.

## What this does and does not show

- **Shown:** crash-window closure, **for the cooperating server's effect only**: its note, in the
  key store's own database, committed in the same transaction as the key's record.
- **Not shown, and not claimed:**
  - closure for any unmodified third-party server;
  - closure for any effect outside that transaction (files, remote APIs, other databases).
  Those keep their floor: in doubt or UNKNOWN, never re-run, and counted as residual failures.
- **The late-original-attempt fencing (replay rule)** is tested in
  `tests/examples/test_cooperating_notes_server.py`: threads and a killed process racing a
  resend. These cells do not test it.
