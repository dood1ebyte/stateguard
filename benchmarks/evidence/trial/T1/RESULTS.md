# T1 results: `stateguard trial` on three real servers, scripted

**Code:** `c504e38` (S5.1), run through `run_batch.py` on a clean worktree on 2026-11-27. The
plan, `PLAN.md`, was committed in the same commit, before any row.
- **Servers and workload:** real pinned servers, scripted workloads. 10 calls per run.
- **Faults:** TEST-ONLY faults on sandbox copies; not production traffic.
- **Records:** each row holds a trial report (`trial_report.json`) and the ground truth read
  from each run's sandbox copy. The redacted bundles stayed in the trial directory and were
  checked: each rebuilds from itself.

| Server | Run | Shadow: client saw | Auto: client saw | UNKNOWN shadow / auto | Resends proposed / made | Changed calls | Writes in each run's copy |
|---|---|---|---|---|---|---|---|
| filesystem | plain | 9 ok, 1 tool error | 9 ok, 1 tool error | 0 / 0 | 1 / 1 | none | b.md written and moved to c.md once; a.md edited once (both runs) |
| filesystem | fault_read | 10 no answer | 9 ok, 1 tool error | 1 / 0 | 0 / 2 | 0–9 | shadow: none (the session ended at call 0); auto: as plain |
| sqlite | plain | 10 ok | 10 ok | 0 / 0 | 0 / 0 | none | rows seed, t-0, t-1, t-2; table tags (both runs) |
| sqlite | fault_read | 1 ok, 9 no answer | 10 ok | 1 / 0 | 0 / 1 | 1–9 | shadow: seed only; auto: as plain |
| git | plain | 9 ok, 1 tool error | 9 ok, 1 tool error | 0 / 0 | 1 / 1 | none | one commit, "trial change" (both runs) |
| git | fault_read | 10 no answer | 9 ok, 1 tool error | 1 / 0 | 0 / 2 | 0–9 | shadow: none; auto: one commit |

Denominator checks held, and each bundle rebuilt from itself, in all 12 runs (6 trials ×
shadow and auto).

**Against the plan: as predicted.**
- **`plain`:**
  - shadow and auto answered every call alike, with 0 UNKNOWN;
  - the deliberate miss on a declared read (filesystem, git) was proposed for a resend in
    shadow and resent once in auto, with the same answer (C1's behaviour);
  - **sqlite:** the failed query (`SELECT nope FROM missing`) came back as an ordinary result
    in both runs. This is M1's documented limitation: the server reports errors as text.
- **`fault_read`:**
  - **shadow:** the faulted read and every later call got no answer. Shadow never restarts a
    server and never answers for one, so the client saw what it would see without StateGuard.
    The relay's own record counts the call as UNKNOWN.
  - **auto:** the relay restarted the server, resent the declared read, and answered every
    call, with 0 UNKNOWN.
- **0 unsafe outcomes:** every write ran exactly once in each run's copy, and nothing was told
  success without its effect. Each run started from a fresh copy, and the original sandbox was
  untouched.

**Limits.** One scripted workload per server, 10 calls, one machine. The trial report's
counts are for these calls only.
