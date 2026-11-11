# StateGuard — scheduled-session handoff

Read this first in every scheduled engineering session. It is the short
handoff; `TASKMAP.md` is the durable record and
`KEY_INJECTION_AND_HTTP_RELAY.md` holds the latest results. Code and tests are
ground truth. Labels: VERIFIED / INFERRED / UNVERIFIED.

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
- **The 14:00 IST end** is enforced by each session's own clock checks, not by the platform.

## Handoff (end of Session 3, Wed 2026-11-11)

- **Pushed:** everything up to the commit that adds this section; `origin` and the local branch
  are level. **Uncommitted:** nothing. **Running:** nothing.
- **Left in place (lost if the container is replaced):** the venvs `.venv` and `/tmp/mcp1`,
  TypeScript `node_modules`, `/opt/sg-servers/*`, Python 3.14 in `/opt/uvpython`, and the
  `run_batch.py` worktrees `/tmp/sg-code-<sha12>`. Rebuild per "Environment" if they are gone.
- **Results and reasoning:** `docs/runtime/PHASE1_ENGINEERING_RECORD.md` (Sessions 1–3).
  Metrics: `docs/runtime/PHASE1_METRICS.md`. Evidence: `benchmarks/evidence/{realhttp,realstdio}/`.
- **Rules learned:** never edit `benchmarks/runtime/` while a batch runs. Crash cells bypass the
  fault proxy (record, C1).
- **Exact next action (Fri 2026-11-13, 09:00 IST):** check the environment and any founder
  decision on items 1–2, then queue item 3.

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

Next, in order (Session 4, Fri 2026-11-13, is hardening and compatibility):
1. **Founder decision needed: the relays' deadlines differ.**
   - HTTP `--call-deadline` is when to resend (the first answer wins).
   - stdio `--call-deadline` is a promised answer time: "unknown" after about two deadlines
     for a recoverable call, by its documented contract.
   - Do not change stdio's contract without that decision (record, D10).
2. **Founder decision needed: a default deadline for recoverable requests (HTTP).** E1–E3
   (record) show the HTTP relay's deadline never cuts short a slow keyed write or a slow
   declared read after D10, and it recovers `hang_after`. Costs: a second request per call
   slower than the deadline, and slow calls counted as deadline faults in incidence.
3. **Hardening and compatibility (Session 4):**
   - the multi-Python regression (3.11, 3.12, 3.14) on the Session 2–3 code;
   - the declaration's refusal paths under a real misconfiguration;
   - (done in Session 3: the TypeScript store tests cover `Recovery.rerun`).
4. **P2b: the event-stream connection cost with a kernel delay** (iproute2 netem in a network
   namespace; this container has neither `tc` nor `ip`). Session 5 (overhead).
5. **The crash window of a non-idempotent tool** stays unresolved without a key-bound check or
   a transactional effect. An operator-supplied check for the injector and preload is possible
   but tool-specific; decide whether it is in Phase 1 scope.
6. **Real capture for incidence** (owner-dependent; see the old queue item 3).
7. Phase Completion Report (Session 6).

## Session log

One line per session, newest first:
`YYYY-MM-DD · task · result (tests: N passed / failures) · next`.

- 2026-11-11 · Session 3 (09:00–10:15 IST, recovery engineering) · D11 declared-idempotent crash windows recover (S5: memory 24/84 -> 0/84, not exactly-once); HTTP crash cells (C1: 0 unsafe); D12 resend backoff (C1 keyed after_record 5/24 -> 0/24); B3 = B2 cell for cell, every relay row recorded · pushed, level with origin (tests: Python 3.13, 6585 passed / 0 failed / 0 skipped; TypeScript 74/74; ruff, format, mypy clean) · Session 4: founder items 1-2, then queue item 3 (hardening)
- 2026-11-09 · Session 2, continued with the founder's go-ahead (11:32–11:52 IST) · stdio half of D10 found to be stdio's documented deadline contract (change discarded, decision queued for the founder); E2 (TypeScript keyed, write/slow) and E3 (read/slow) 0/12 with the deadline · pushed, level with origin (tests: Python 3.13, 6575 passed / 0 failed / 0 skipped; TypeScript 73/73; ruff, format, mypy clean) · founder: items 1 and 2; else P2b
- 2026-11-09 · Session 2 (09:00–09:50 IST) · metrics and taxonomy final (PHASE1_METRICS.md, phase1_table.py); D1 drift 0/60 per relay arm; D9 incidence records for unfinished requests (I1: 96/96 recorded); P3 per-call stdio overhead <1 ms (keyed write 6–7 ms); D10 HTTP keyed call still running is waited for (E1: 12/12 → 0/12 induced) · pushed, level with origin (tests: Python 3.13, 6575 passed / 0 failed / 0 skipped; ruff, format, mypy clean) · Session 3: queue item 1, the stdio half of D10
- 2026-11-06 · Session 1 (09:00–12:30 IST) · B1, S1–S4, B2, P1, P2; D1–D8 fixed (stale pool, `-32603` retry where safe in both relays, executor-scope order, CI pin, harness); head pushed and level with origin (tests: Python 3.13, 6567 passed / 0 failed / 0 skipped; ruff, format, mypy clean) · Session 2: queue item 1, incidence records for requests open when a relay stops
- 2026-11-05 · (before the schedule; stopped by the founder) harness, CI pin, B1 plan · pushed 237a332, 4017672; B1 killed at 70/960 rows (tests: 6337 passed / 1 known sandbox failure, 16 skipped before the pin) · B1, then stale pool

2026-11-05 · Phase 1 queue 1 (harness, CI pin, B1 started) · stopped by the founder before schedule; 237a332 pushed, 4017672 local, B1 partial 70/960 uncommitted (tests: 6337 passed / 1 known sandbox failure) · resume 2026-11-09 09:00 IST per the STOPPED section
