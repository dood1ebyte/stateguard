# StateGuard — scheduled-session handoff

Read this first in every scheduled engineering session. It is the short
handoff; `TASKMAP.md` is the durable record and
`KEY_INJECTION_AND_HTTP_RELAY.md` holds the latest results. Code and tests are
ground truth. Labels: VERIFIED / INFERRED / UNVERIFIED.

## Phase 2 (2026-11-18 to 12-07): approved by the founder on 2026-11-17

**Source of truth:** `docs/runtime/PHASE2_PLAN.md`. It holds:
- the plan, and each session's deliverables, acceptance and fallbacks;
- the deferral list;
- clarifications A–D;
- the fixed S6 protocol;
- approvals A1–A7 and the consent process.

Where it differs from the Phase 1 rules below, it wins. Each session runs from its scheduled
task's prompt plus that file.

**Schedule** (09:00–14:00 IST, five hours at most; do not move, extend or compress). Every task
fires into this conversation (`session_01NykZHSv2cJyQrzsT2GQbv8`), on the repository
`dood1ebyte/stateguard`, branch `claude/serene-bohr-o28snr`. Verified with `list_triggers` on
2026-11-17.

| Session | Date (IST) | Fires (UTC) | Scheduled task |
|---|---|---|---|
| S1 | Wed 2026-11-18 | 03:30 | `trig_01JBfioVdCgAAaNB85oA8n9C` (the Phase 1 Session 6 task, repurposed: same ID, new name and prompt) |
| S2 | Fri 2026-11-20 | 03:30 | `trig_013DogfP9exuQ7k4LauoKrtC` |
| S3 | Mon 2026-11-23 | 03:30 | `trig_0157stVvT3YaY66MTtGw8zSD` |
| S4 | Wed 2026-11-25 | 03:30 | `trig_01B3yXNEdKm8VqBNTLZdNYv7` |
| S5 | Fri 2026-11-27 | 03:30 | `trig_01JNxhLzF7ggD1XhnTbXavY3` |
| S6 | Wed 2026-12-02 | 03:30 | `trig_01TffaUVKQLkTE3TVX1cxePN` |
| S7 | Mon 2026-12-07 | 03:30 | `trig_01GtcNizdnuWpP4CyzBgk2km` |

**Not part of Phase 2, left untouched:** "StateGuard outreach: daily reply check"
(`trig_013ZparK8VtEKmw6YVh4ZRtR`) and every Venture Lab task.

**Draft PR #19** (https://github.com/dood1ebyte/stateguard/pull/19), opened 2026-11-17 under A2,
for CI only.
- Never merge it, and never mark it ready, without the founder's separate approval.
- Every push to the branch updates it.
- First CI run (head `eb8da57`, run 43955222895), as it stood at setup:
  - lint, import isolation and mypy passed;
  - the Ubuntu test jobs for 3.11, 3.12 and 3.13 **failed within 40 s**;
  - macOS 3.11–3.14 and Ubuntu 3.14 were still running.
  - Not investigated at setup: that is S1.3.

**Approvals:**
- A1, A2, A3, A4 and A6: approved.
- **A5: no publishing.**
- **A7: no credentials.**

### S1 starting checklist (Wed 2026-11-18, 09:00 IST)

1. Run `TZ=Asia/Kolkata date` and confirm the time is inside 09:00–14:00. Note the start time.
2. Check git:
   - `git status`, the branch, `git log -3`;
   - `git fetch origin claude/serene-bohr-o28snr`;
   - `git rev-list --left-right --count HEAD...origin/claude/serene-bohr-o28snr` must be `0 0`.
3. Check the toolchain. On 2026-11-17 it was present:
   - `.venv` and `/tmp/mcp1` (3.13.18; mcp 2.3.0; mcp 1.9.4 with mcp-server-sqlite 2025.4.25);
   - `/tmp/venv311` (3.11.18), `/tmp/venv312` (3.12.3), `/tmp/venv314b` (3.14.6);
   - node v22.23.4;
   - `/opt/sg-servers/{everything,memory,filesystem}`;
   - `adapters/typescript/node_modules`.
   If the container was replaced, rebuild it per "Environment" below.
4. CI: read PR #19's check runs and the failed jobs' logs (`mcp__github__get_job_logs`). List
   each failure for S1.3.
5. Start the Python 3.13 suite in the background as a baseline before any change (about 10
   minutes).
6. **S1.1**, the relays as package commands:
   - move the relays into the package;
   - add a `relay` subcommand to `stateguard` (`src/stateguard/cli.py`);
   - make `examples/mcp/stdio_proxy.py` and `http_proxy.py` thin wrappers;
   - check which optional dependencies the HTTP relay needs (extras in `pyproject.toml`).
7. **S1.2**, `--keys auto`:
   - a Python server (`python`, `python -m`, `uv`/`uvx`) runs as
     `python -m stateguard.adapters.mcp.inject …`;
   - a Node server (`node`/`npx`) runs with `--import <preload>`, and the preload must be
     reachable from an installed wheel;
   - choose a default key-store path, and which tools are keyed (non-read tools), and record
     both decisions;
   - otherwise run unkeyed, and log why.
8. **Acceptance tests:**
   - a new end-to-end test:
     - build the wheel (`pip wheel . --no-deps`);
     - install it into a fresh virtual environment;
     - run `stateguard relay stdio --keys auto -- <pinned server-memory>`, with
       `STATEGUARD_FAULT=after_record` on `add_observations`;
     - assert the write tool is keyed, executed once and replayed once;
   - the HTTP and stdio keyed and unkeyed matrices on the S1 commit through the new commands
     (new batches, plans written before any rows), compared cell by cell with B5/B6 and
     S8/S9;
   - Python 3.11–3.14 suites, TypeScript `tsc` and `npm test`, ruff, format, mypy.
9. **S1.3:** fix or root-cause each CI failure, time-boxed to 1 hour.
10. Commit and push each finished piece (about every 60 minutes). No new task after 13:30. By
    14:00:
    - stop background processes;
    - update NEXT.md;
    - push and confirm `0 0`;
    - give the short report.

## Rules for scheduled sessions (owner, 2026-11-05)

- Branch `claude/serene-bohr-o28snr` (the head of PRs #14–#18; VERIFIED
  2026-11-05: 0 behind and 9 ahead of `main`). Push to that branch only, with a
  plain fast-forward `git push`. Never push to `main`, merge, rebase,
  force-push or rewrite history. If the local branch and `origin` have
  diverged, or a push is rejected, stop: record the conflict in the session
  log and leave it for the owner.
- One draft PR from the branch to `main` may be open for review; keep it
  current. Never mark it ready or merge it.
- One task per session, within the time cap set in the session prompt. No new
  scope, no new projects, no long reports: a few lines here and in TASKMAP.
- Install like CI: `python3 -m venv /tmp/sgvenv && /tmp/sgvenv/bin/pip install -e ".[pydantic,mcp,dev]"`.
  `uv run` does not install the package.
- Tests: run the targeted tests while working; run the full suite once before
  the push (about 9 min wall time in the cloud sandbox, measured 2026-11-05).
- **Corrected 2026-11-06:** `test_on_a_platform_without_pid_namespaces_the_scope_is_refused`
  was misdiagnosed as sandbox-only. It failed on Python 3.13+, which CI tests, because
  `shutil.which` follows a patched `sys.platform` ("win32") and the relay resolved the program
  before checking the platform. The relay now checks the platform first. Expect 0 failures.
- Also run `ruff check src/ tests/`, `ruff format --check src/ tests/` and
  `mypy src/` before pushing (CI runs them).

## Phase 1 (2026-11-05 to 11-18): MCP reliability, the founder's mandate

The founder's Phase 1 mandate supersedes "one task per session". Within the scheduled windows
below, work continuously: take the next item, finish it, take the next. The git rules above
still hold. Report only at completion
(Phase Completion Report, then stop and wait for Phase 2 authorization) or on a genuine blocker.

**Facts (VERIFIED 2026-11-05):**
- The container was replaced between sessions. Everything outside the repository was lost:
  `/root/stateguard-captures` held the raw rows, plans, snapshots and drivers behind M16–M20,
  and the three untracked outreach notes (never committed, so not recoverable).
- From now on, measurement code lives in `benchmarks/runtime/realhttp/` and evidence in
  `benchmarks/evidence/realhttp/<batch>/`, both committed.
- The toolchain must be rebuilt per container (see "Environment" below).
- No open PR, and no CI run on the branch since `ec339ab`. Opening a PR needs the founder.

**Environment (each fresh container):**
`python3 -m venv .venv && .venv/bin/pip install -e ".[pydantic,mcp,dev]"`;
`python3 -m venv /tmp/mcp1 && /tmp/mcp1/bin/pip install mcp==1.9.4 mcp-server-sqlite==2025.4.25 pydantic==2.11.7`;
`(cd adapters/typescript && npm ci)`; pinned third-party servers into `/opt/sg-servers/` (`npm install` in `everything/`, `memory/`, `filesystem/`: `@modelcontextprotocol/server-everything@2026.8.31`, `server-memory@2026.8.31`, `server-filesystem@2026.8.31`). Other Pythons for CI's matrix: `/usr/bin/python3.11`, `/usr/bin/python3.12`; 3.14 with `uv python install 3.14 --install-dir /opt/uvpython` (not under /root: the executor-scope tests run a child as an unprivileged user).
Full suite: `STATEGUARD_MCP1_PYTHON=/tmp/mcp1/bin/python .venv/bin/pytest tests/`.

## Schedule (founder, 2026-11-05): mandatory

Engineering only inside the approved windows, each 5 h from 09:00 IST: Fri 2026-11-06, Mon 11-09,
Wed 11-11, Fri 11-13, Mon 11-16, Wed 11-18. Outside them: no engineering, installs,
benchmarks, tests, code changes, commits or pushes. Preserving state and status reports are
allowed. Early completion or idle capacity never advances the schedule.
- **Scheduled tasks:** each fires into session `session_01NykZHSv2cJyQrzsT2GQbv8`, created
  2026-11-05:
  - Session 1, `trig_01MBaCJ5CbraSn3h3YLyKek2`, 11-06;
  - Session 2, `trig_01Dh9zhF3bYhccgrpSsEaa7r`, 11-09;
  - Session 3, `trig_01Df14Kxh58VnHxQMsoovLmW`, 11-11;
  - Session 4, `trig_01EPT7D9dKwSkNjJozgNQnQ1`, 11-13;
  - Session 5, `trig_01JK1QRZxsKzfao7RfLfLDVA`, 11-16;
  - Session 6, `trig_01JBfioVdCgAAaNB85oA8n9C`, 11-18.
  - **Extra session** (the founder, 2026-11-13: "schedule an extra session today. use it at
    maximum capacity"): `trig_01HJUHp5EKmRt2h2Q2dPuHRW`, Fri 11-13, 17:15–22:15 IST. It
    advances the queue; Sessions 5 and 6 continue with what remains.
    It was cut off at about 17:53 IST (the turn ended without closing out: the A2 harness
    uncommitted, no NEXT.md update). On the founder's "Try again" (Sat 11-14, 20:21 IST),
    it resumed with the time it lost: Sat 20:22 to Sun 00:45 IST.
    It was cut off again at about 22:40 IST (the founder's usage limit; R1's harness and plan
    were pushed, its scorer uncommitted). On the founder's "Please continue from where you left
    off" (Sun 11-15, 01:22 IST), it resumed with the time it lost: to 03:20 IST, no new task
    after 02:50. It closed at 01:54 IST.
  - **Session 5, held early** (the founder, Sun 11-15: "complete the next session now itself
    and advance as per schedule in the following sessions after that … can advance to next
    sessions if you are done early … unless i tell you, you will not be allowed to work
    outside the schedule"): Sun 11-15, 04:15–09:15 IST, run under Session 5's stored prompt.
    It finished Session 5's agenda, then went on to Session 6's: the Phase Completion Report.
  - **Phase 1 is complete** (`PHASE1_COMPLETION_REPORT.md`), and the founder was notified at
    the end of Session 5.
  - **Superseded on 2026-11-17.**
    - The Monday task (`trig_01JK1QRZxsKzfao7RfLfLDVA`) fired on 11-16 and only logged a line.
    - The Wednesday task (`trig_01JBfioVdCgAAaNB85oA8n9C`) was repurposed, on the founder's
      instruction, as Phase 2 S1 (see "Phase 2" above).
    - Phase 2 is authorised and runs from `PHASE2_PLAN.md`.
- **The 14:00 IST end** is enforced by each session's own clock checks, not by the platform.

## Handoff (Phase 2 setup, Tue 2026-11-17 02:25–02:40 IST; no engineering)

- **Done:**
  - the approved plan committed (`PHASE2_PLAN.md`, `eb8da57`);
  - draft PR #19 opened;
  - seven scheduled tasks configured and verified: the Phase 2 table above, with S1 repurposed
    from the Phase 1 Session 6 task.
- **Pushed:** everything; origin and local are level. **Uncommitted:** nothing.
  **Running:** nothing locally.
- **Exact next action:** S1, Wed 2026-11-18, 09:00 IST: the S1 starting checklist above.

## Handoff (end of Session 5, Sun 2026-11-15; Phase 1 complete)

- **Pushed:** everything up to the commit that adds this section; `origin` and the local branch
  are level. **Uncommitted:** nothing. **Running:** nothing.
- **Behaviour changes in Session 5:**
  - D16: keyed calls get 3 attempts;
  - D17: on stdio, a keyed resend with attempts left meets its own deadline.
  - Other defaults are unchanged.
- **Phase 1 is complete:** `docs/runtime/PHASE1_COMPLETION_REPORT.md`. Product code is final
  at `93addf9`; later commits are evidence and documentation.
- **Exact next action:** none in Phase 1. Wait for the founder's Phase 2 authorization (see
  Schedule, on the scheduled tasks that still fire).

## Handoff (end of the extra session, Sun 2026-11-15 early morning IST)

- **Pushed:** everything up to the commit that adds this section; `origin` and the local branch
  are level. **Uncommitted:** nothing. **Running:** nothing.
- **Behaviour change in the extra session:** D13, D14 and D15 (relay answers to non-conformant
  answers, and to an internal error after an unknown attempt). Defaults are unchanged.
- **Exact next action (Mon 2026-11-16, 09:00 IST):** check the environment, then queue item 2
  (the attempt budget for keyed calls, measured with R1's harness).

## Handoff (end of Session 4, Fri 2026-11-13)

- **Pushed:** everything up to the commit that adds this section; `origin` and the local branch
  are level. **Uncommitted:** nothing. **Running:** nothing.
- **Left in place (lost if the container is replaced):** the venvs `.venv`, `/tmp/mcp1`,
  `/tmp/venv311`, `/tmp/venv312` and `/tmp/venv314b` (3.14 from `/opt/uvpython`); TypeScript
  `node_modules`; `/opt/sg-servers/*`; and the `run_batch.py` worktrees
  `/tmp/sg-code-<sha12>`. Rebuild per "Environment" if they are gone.
- **Behaviour changes this session (defaults):**
  - HTTP `--call-deadline` is 30 s;
  - stdio `--deadline-scope` is `recoverable`, with `--answer-timeout` 300 s.
- **Results and reasoning:** `docs/runtime/PHASE1_ENGINEERING_RECORD.md` (Sessions 1–4).
- **Exact next action (Mon 2026-11-16, 09:00 IST):** check the environment, then queue item 1.

## Phase 1 queue (first not done or blocked; mark done here with its commit)

Done in Session 1 (2026-11-06); details are in `PHASE1_ENGINEERING_RECORD.md`:
- in-repo harnesses and evidence; B1 (HTTP baseline, `6181cc2`);
- the stale pooled connection fix with S1 (`8de3735`, `cbb396a`);
- the concurrency crash test (`8de3735`);
- the stdio baseline S2 (`24a975c`);
- the `-32603` retry where safe, with S3 (`a8ed63d`, `dcf0b0c`);
- defects D2, D3, D5 and D6, and the regression on Python 3.11, 3.13 and 3.14;
- S4, the stdio matrix in one batch (`37a54cb`): sg_keyed 24/132 per server, only the two crash
  windows, in doubt;
- D8, HTTP parity for `-32603` (`1852365`);
- queue item 3 concluded: the crash-window residual is a floor without server cooperation
  (record, "Queue item 3");
- B2, the HTTP matrix on `1852365` (`40bbf4a`): matches B1 cell for cell;
- P1, paired uncontended overhead (`5721c12`): HTTP relay +0.006 to +0.066 s per session;
  stdio relay +0.20 to +0.37 s including the relay process start;
- P2, event-stream answers (`5721c12`): one upstream connection per call confirmed; the
  network cost was not measurable with the delay proxy (record, "P2").

Done in Session 2 (2026-11-09):
- metrics and taxonomy final: `PHASE1_METRICS.md`, `benchmarks/runtime/phase1_table.py`
  (`7b3c4d2`);
- D1, schema drift, category A (`504f109`): every relay arm 0/60, raw 60/60;
- D9, incidence records for requests a relay never finished (`a00a269`), confirmed by I1:
  96/96 relay rows recorded;
- P3, per-call overhead of a running stdio relay (`e0a090a`): under 1 ms per call; a keyed
  write about 6–7 ms;
- D10, HTTP relay: a keyed call still running past its deadlines is waited for (`68b5594`);
  E1 (`18395e8`): the deadline arm failed 12/12 slow keyed calls before the fix, 0/12 after.
- after the founder's go-ahead to continue inside the window: the stdio half of D10 found
  to be stdio's documented deadline contract (record); E2 (TypeScript keyed server) and E3
  (slow declared read) both 0/12 with the deadline.

Done in Session 3 (2026-11-11, recovery engineering):
- D11, the crash window of an operator-declared idempotent tool (`c4de122`):
  `Recovery.rerun`, and `STATEGUARD_IDEMPOTENT_TOOLS` with an exclusive store, in the injector
  and the preload. S5 (`bc03223`): memory keyed residual 24/84 -> 0/84. Not exactly-once:
  correct only if the declaration is true.
- HTTP server-crash cells in the harness, and in doubt is no longer a false failure there
  (`9692d4d`). C1: no StateGuard arm duplicated or misreported; crash windows unresolved (the
  floor for a non-idempotent tool).
- D12, a backoff before a resend after a broken connection (`51f6fdf`). C1 on D12: keyed
  `after_record` 5/24 -> 0/24.
- B3, the HTTP matrix on `51f6fdf` (results in the record).

Done in Session 4 (2026-11-13, hardening):
- Decision 2, the HTTP default deadline of 30 s for recoverable requests, with slow successes
  recorded `answered` (`4c2256d`). E4 (`570d262`): a hung keyed write recovered at about 30 s,
  12/12; a 10 s write answered with no resend.
- Decision 1, the stdio deadline is when to try again: `--deadline-scope recoverable` by
  default, and `--answer-timeout` (`2d8a676`). S6 (`ba310ac`): the matrix unchanged; the old
  hard bound cost 12/12 slow unkeyed writes per server, the new default 0.
- The TypeScript store tests cover `Recovery.rerun`.
- Python 3.11, 3.12 and 3.14 suites: 6,589 passed each (`8469759`).
- S7, the stdio matrix on server-filesystem (`8469759`): StateGuard's residual as on sqlite
  and memory; the blind resend's unsafe outcome there is 48 false failures.
- B4, the deadline arms on the HTTP matrix after decision 2 (results in the record).
- Decided (scope): a key-bound recovery check for non-idempotent tools is tool-specific
  operator integration, outside Phase 1. Their crash windows stay a documented limitation.

Done in the extra session (Fri 2026-11-13 17:15–17:53, resumed Sat 11-14 20:22 IST):
- A1, adversarial cells J, H, C and B on stdio (`5363974`). D13: a non-conformant answer to a
  call that can be resent is resent (`87f43d4`). C sg_keyed 24/24 -> 0/24; every record agrees.
- A2, the same on HTTP (`69079ed` and on D14). D14: a non-conformant answer that cannot be resent
  is answered "unknown" (`fccee7c`). A2 had 12 false failures per server in arm sg; after D14, 0.
- The `--no-listing-refresh` risk measured (B) and stated in both relays' help.
- P2b blocked: this VM's kernel has no netem. The reuse optimisation is deferred to Phase 2
  (a limitation).
- P4, overhead on the final code (`16e26b9`): HTTP +0.009 to +0.058 s per session; stdio under
  1 ms per call, keyed writes about 4.5–5 ms.
- Python 3.11, 3.12 and 3.14 regression. Two test fixes: a race in the concurrency test, and
  a check that each rig process is the one listening on its port.
- B5 (`4ee41bb`) and S8 (`077c046`): the HTTP and stdio matrices on the final code. They match
  B3 and S4–S7 cell for cell. The Completion Report's evidence base.
- The harness port race (`8568fa3`): one naive fault-free row in B5. No other batch was hit.
- The evidence index and its generator (`607b69a`).
- R1, concurrency under repeated faults (`2a3810b`, `4d27df8`, `ddcd6e6`):
  - It found 5 false failures in sg_keyed. D15 (`4969b89`) answers an internal error after an
    unknown attempt "unknown".
  - On D15, sg_keyed is 18/240 per server, all unresolved, 0 unsafe.
  - notes (Python) and tsnotes (TypeScript) behave alike.

Done in Session 5 (held early, Sun 2026-11-15, on the founder's instruction):
- D16, keyed calls get 3 attempts (`4e859c1`). R1 on D16: sg_keyed 18/240 → 1/240 and
  4/240, 0 unsafe.
- R2, repeated faults on stdio with calls in flight (`d72755a`). 0 unsafe in StateGuard arms
  on sqlite, memory and filesystem. sg_keyed 9/96, 0/96 and 11/96, all unresolved: crash
  windows and calls with three faulted attempts.
- D17, found by R2's smoke run (`93addf9`): on stdio, a keyed resend with attempts left is
  tried again at its own deadline.
- B6 and S9, the matrices' keyed arms on the final code (`60ad165`): identical to B5 and S8,
  cell for cell. Slow keyed writes on stdio are 0/12 per server.
- P5, overhead on the final code (`8368dfa`): within P4's range.
- The final regression on Python 3.11–3.14 and TypeScript (session log).
- **Session 6's agenda, done early: `PHASE1_COMPLETION_REPORT.md`.** Phase 1 is complete;
  waiting for the founder's Phase 2 authorization.

The queue as it stood before Session 5 (done: items 1–3, and 5):
1. ~~B5 and S8~~ (done in the extra session).
2. **The attempt budget for keyed calls (D16 candidate):**
   - The facts: `DEFAULT_MAX_ATTEMPTS` is 2. R1's whole remaining sg_keyed residual is calls
     faulted on both attempts.
   - The work: raise it for keyed calls only. In the stdio relay, check
     `_consider_retry`'s deferred resend first: it assumes only a first attempt is deferred.
   - Measure with R1's harness as a new part. Then the full suite and the matrices'
     keyed cells.
3. **Adversarial validation, remaining:**
   - repeated faults on stdio (the shim faults once per file today);
   - Python and TypeScript side by side on the A1 cells (tsnotes is in A2 and R1; memory is
     in A1).
   - Fix what is found.
4. **Real capture for incidence** (owner-dependent; see the old queue item 3). Not done:
   Phase 2 priority 1 in the report.
5. Phase Completion Report (Session 6). Limitations to state:
   - the non-idempotent crash windows;
   - G and K outside the measured RFR;
   - schema repair trusts the server's listing (A2 finding);
   - P2b unmeasured (no netem);
   - no traffic-weighted RFR;
   - the attempt budget, if D16 is not done (R1: 7.5% unresolved under a fault on every 3rd
     request).

## Session log

One line per session, newest first:
`YYYY-MM-DD · task · result (tests: N passed / failures) · next`.

- 2026-11-17 · Phase 2 setup on the founder's final authorisation (02:25–02:40 IST, no engineering) · PHASE2_PLAN.md committed; draft PR #19 opened (CI: lint, isolation, mypy passed; Ubuntu 3.11–3.13 test jobs failed fast, not investigated; macOS running); S1 task repurposed, S2–S7 created, all verified · pushed, level with origin (tests: none run) · S1 Wed 18 Nov 09:00 IST
- 2026-11-16 · Session 5's scheduled task (`trig_01JK1QRZxsKzfao7RfLfLDVA`, delivered 09:01 IST) · Phase 1 complete (2026-11-15); awaiting the founder's Phase 2 authorization. No founder instruction since the report, so no engineering (mandate §10) · pushed, level with origin (tests: none run) · wait for the founder
- 2026-11-15 · Session 5, held early on the founder's instruction (04:15–06:00 IST), then Session 6's agenda · D16 keyed attempt budget 3 (R1: sg_keyed 18/240 → 1/240 and 4/240); R2 repeated faults on stdio, 0 unsafe; D17 keyed resend meets its own deadline (stdio); B6 and S9 identical to B5 and S8 cell for cell; P5 overhead within P4's range; PHASE1_COMPLETION_REPORT.md written, Phase 1 complete · pushed, level with origin (tests: Python 3.11.18 / 3.12.3 / 3.13.18 / 3.14.6 each 6629 passed / 0 failed / 0 skipped; TypeScript tsc clean, 74/74; ruff, format, mypy clean) · wait for the founder's Phase 2 authorization
- 2026-11-14/15 · Extra session (Fri 17:15–17:53; Sat 20:22–22:40; Sun 01:22–about 02:10 IST,
  after two interruptions) · A1 and A2 adversarial cells, with D13 and D14; P4 overhead; B5 and
  S8 on the final code; the harness port race; R1, concurrency under repeated faults, which
  found D15 (5 false failures in sg_keyed, then 0; sg_keyed 18/240 per server, all unresolved)
  · pushed, level with origin (tests: Python 3.13 full suite on D15, 6618 passed / 0 failed /
  0 skipped; earlier in the session, 3.11, 3.12 and 3.14 suites after the two test fixes;
  TypeScript 74/74, unchanged since; ruff, format, mypy clean) · Session 5: queue item 2,
  the keyed attempt budget
- 2026-11-13 · Session 4 (09:00–10:35 IST, hardening) · decision 2: HTTP default deadline 30 s (E4: hung keyed write 12/12 recovered at ~30 s); decision 1: stdio deadline is when to try again (S6: matrix unchanged, old bound cost 12/12 slow writes per server); S7 server-filesystem (sg_keyed 24/132, 0 unsafe); B4 deadline arms = B3 · pushed, level with origin (tests: Python 3.11/3.12/3.13/3.14 6589 passed each, 0 failed / 0 skipped; TypeScript 74/74; ruff, format, mypy clean) · Session 5: queue item 1 (adversarial cells B/H/J/C)
- 2026-11-11 · Session 3 (09:00–10:15 IST, recovery engineering) · D11 declared-idempotent crash windows recover (S5: memory 24/84 -> 0/84, not exactly-once); HTTP crash cells (C1: 0 unsafe); D12 resend backoff (C1 keyed after_record 5/24 -> 0/24); B3 = B2 cell for cell, every relay row recorded · pushed, level with origin (tests: Python 3.13, 6585 passed / 0 failed / 0 skipped; TypeScript 74/74; ruff, format, mypy clean) · Session 4: founder items 1-2, then queue item 3 (hardening)
- 2026-11-09 · Session 2, continued with the founder's go-ahead (11:32–11:52 IST) · stdio half of D10 found to be stdio's documented deadline contract (change discarded, decision queued for the founder); E2 (TypeScript keyed, write/slow) and E3 (read/slow) 0/12 with the deadline · pushed, level with origin (tests: Python 3.13, 6575 passed / 0 failed / 0 skipped; TypeScript 73/73; ruff, format, mypy clean) · founder: items 1 and 2; else P2b
- 2026-11-09 · Session 2 (09:00–09:50 IST) · metrics and taxonomy final (PHASE1_METRICS.md, phase1_table.py); D1 drift 0/60 per relay arm; D9 incidence records for unfinished requests (I1: 96/96 recorded); P3 per-call stdio overhead <1 ms (keyed write 6–7 ms); D10 HTTP keyed call still running is waited for (E1: 12/12 → 0/12 induced) · pushed, level with origin (tests: Python 3.13, 6575 passed / 0 failed / 0 skipped; ruff, format, mypy clean) · Session 3: queue item 1, the stdio half of D10
- 2026-11-06 · Session 1 (09:00–12:30 IST) · B1, S1–S4, B2, P1, P2; D1–D8 fixed (stale pool, `-32603` retry where safe in both relays, executor-scope order, CI pin, harness); head pushed and level with origin (tests: Python 3.13, 6567 passed / 0 failed / 0 skipped; ruff, format, mypy clean) · Session 2: queue item 1, incidence records for requests open when a relay stops
- 2026-11-05 · (before the schedule; stopped by the founder) harness, CI pin, B1 plan · pushed 237a332, 4017672; B1 killed at 70/960 rows (tests: 6337 passed / 1 known sandbox failure, 16 skipped before the pin) · B1, then stale pool

2026-11-05 · Phase 1 queue 1 (harness, CI pin, B1 started) · stopped by the founder before schedule; 237a332 pushed, 4017672 local, B1 partial 70/960 uncommitted (tests: 6337 passed / 1 known sandbox failure) · resume 2026-11-09 09:00 IST per the STOPPED section
