# C1 results: real servers, scripted workloads, through `stateguard capture`

**Label: real server, scripted workload.** These are pinned public MCP servers run unmodified
on this machine and driven by a scripted client. This is not production traffic, and no rate
here is a real-world failure rate.
- **Code:** `9de0a26`, run through `run_batch.py` on a clean worktree; code state in
  `codestate_*.json`.
- **Plan:** `PLAN.md`, written before any row.
- **Rows:** `rows_*.jsonl`, one per session.
- **Summaries:** `summary_*.txt` / `.json`.
- **Bundles:** `bundles_*.tar.gz`, 24 of them, the redacted bundles exactly as `stateguard
  capture` wrote them.

## Counts per server and per arm (never pooled across arms)

| Server | Arm | Sessions | Client calls | Relay calls = records = answered | Unfinished | Succeeded | Tool error | Failed | UNKNOWN | Resends made | Would resend |
|---|---|---|---|---|---|---|---|---|---|---|---|
| git 2026.8.18 | shadow | 4 | 200 | 200 | 0 | 192 | 8 | 0 | 0 | 0 | 8 |
| git 2026.8.18 | auto | 4 | 200 | 200 | 0 | 192 | 8 | 0 | 0 | 8 | 0 |
| filesystem 2026.8.31 | shadow | 4 | 200 | 200 | 0 | 193 | 7 | 0 | 0 | 0 | 7 |
| filesystem 2026.8.31 | auto | 4 | 200 | 200 | 0 | 193 | 7 | 0 | 0 | 7 | 0 |
| fetch 2026.10.10 (local site) | shadow | 4 | 200 | 200 | 0 | 192 | 8 | 0 | 0 | 0 | 8 |
| fetch 2026.10.10 (local site) | auto | 4 | 200 | 200 | 0 | 192 | 8 | 0 | 0 | 8 | 0 |

- **Total:** 1,200 tool calls across 3 real servers, 600 in shadow and 600 in auto. 0 missing
  captures, 0 invalid sessions.
- **UNKNOWN:** 0/200 in every group (Wilson 95% upper bound 1.9%).
- **Denominators:** in every session, client tool calls = relay tool calls = invocation records
  = answers, with 0 unfinished. Every check held.
- **Rebuild:** all 24 reports rebuild identically from their bundles alone. This was re-verified
  from the tarballs after the run: 24/24.

## Paired comparison (same server, seed and workload; shadow vs auto)

All 12 pairs ran the same workload, the same tools in the same order. Each pair's outcome
counts are equal: succeeded, tool error, failed and UNKNOWN, seed by seed (`summary_*.txt`).

## Against the plan's prediction

- **Held:**
  - every denominator check holds;
  - 0 unfinished, failed or UNKNOWN;
  - the tool errors are the deliberate misses, equal within each pair;
  - every report rebuilds;
  - relay overhead of a few milliseconds.
- **Did not hold: "0 retries in auto, would_retry 0 in shadow".** Auto resent exactly the calls
  that returned a tool error (git 8, filesystem 7, fetch 8), and shadow would have resent the
  same calls. Every deliberate miss is a call to a tool the effect file declares `read_only`
  (`git_show`, `read_text_file`, `fetch`). The relay's retry rule (`effects.decide_retry`)
  repeats a known failure only of a tool declared safe to repeat. That is designed behaviour,
  not a defect:
  - each resend got the same tool error back;
  - no effect was repeated;
  - the client's answer was unchanged;
  - the cost was one extra call for each of the 23.
  The prediction overlooked that rule.

## Latency and relay overhead (milliseconds; this machine, this run's load)

| Server | Arm | Latency p50 (median of sessions) | Latency p95 (max of sessions) | Overhead p50 | Overhead p95 (max) | Overhead calls |
|---|---|---|---|---|---|---|
| git | shadow | 7.0 | 15.0 | 1.0 | 2.0 | 200 |
| git | auto | 7.0 | 16.0 | 1.0 | 3.8 | 200 |
| filesystem | shadow | 2.0 | 5.0 | 1.0 | 2.1 | 200 |
| filesystem | auto | 2.0 | 5.0 | 1.0 | 2.7 | 200 |
| fetch | shadow | 495.0 | 570.0 | 1.2 | 2.9 | 200 |
| fetch | auto | 492.0 | 563.0 | 1.3 | 11.7 | 200 |

- **What overhead measures:** the client-side time, minus the server's own time from forward to
  answer, for each call forwarded exactly once. In auto, the resent calls make the p95 maximum
  larger.
- **fetch:** its latency is the server's own work of fetching and converting a page.

## Privacy check (the bundles as committed)

The 24 bundles were searched for values the workload typed or the servers returned:
- local URLs (`127.0.0.1`), page names and words from the site;
- note text, edit tokens and commit messages;
- the git author email, file names and the deliberate-miss names.
None was found. "scripted" appears only in the operator's label and the report's "not
claimed" text, and "initial" only in the protocol method `notifications/initialized`. No
recording carries either.

## Limitations

- One machine, one run; scripted workloads; local servers only, with no external network.
- No fault was injected, so this says nothing about recovery.
- The deliberate misses are the only tool errors.
- Keys were not used (the arms differ only in `--shadow`). The key-store duplicate detection is
  verified separately (`tests/test_capture.py`).
