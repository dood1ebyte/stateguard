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

## Handoff (end of Session 1, Fri 2026-11-06)

- **Pushed:** everything up to the commit that adds this section. `origin` and the local branch
  are level. The 2026-11-05 state (STOPPED) was resolved in Session 1: `4017672` pushed, the
  partial B1 rows set aside in `B1/aborted/`, B1 re-run in full.
- **Uncommitted:** nothing. **Running:** nothing; every batch, relay and server process ended.
- **Left in place (lost if the container is replaced):** the venvs `.venv` and `/tmp/mcp1`,
  TypeScript `node_modules`, `/opt/sg-servers/*`, Python 3.14 in `/opt/uvpython`, and the
  `run_batch.py` worktrees `/tmp/sg-code-<sha12>` (detached, read-only use). Rebuild per
  "Environment" if they are gone.
- **Results and reasoning:** `docs/runtime/PHASE1_ENGINEERING_RECORD.md`; evidence in
  `benchmarks/evidence/{realhttp,realstdio}/`.
- **Exact next action (Mon 2026-11-09, 09:00 IST):** check the environment, then queue item 1
  below (incidence records for requests open when a relay stops).

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

Next, in order (Session 2, Mon 2026-11-09, is metrics and measurement):
1. **Incidence records for requests still open when a relay stops.** B1 and B2 show `sg`
   `hang_after` rows with no `invocation` record: a relay writes one when a request finishes.
   On shutdown (and, for HTTP, when the client disconnects), write a record for each open
   request as unresolved, so the incidence denominator is complete. Test it; check
   records == rows on the hang cells.
2. **One Phase 1 residual-failure table from the evidence** (B2 and S4): RFR =
   (failed + unresolved) / eligible per server, family and arm, Wilson 95%, with unsafe and
   induced counts. A script under `benchmarks/runtime/`, so the Completion Report cites
   generated numbers.
3. **Per-call overhead of a running stdio relay**: many calls in one session, so the relay's
   start is separated from the per-call cost (P1 could not).
4. **P2b: the event-stream connection cost with a kernel delay** (iproute2 netem in a network
   namespace; this container has neither `tc` nor `ip`), then decide whether to read event
   streams to their end so the connection can be reused. Session 3 or 5.
5. **Real capture for incidence** (owner-dependent; see the old queue item 3).
6. Phase Completion Report (Session 6).

## Session log

One line per session, newest first:
`YYYY-MM-DD · task · result (tests: N passed / failures) · next`.

- 2026-11-06 · Session 1 (09:00–12:30 IST) · B1, S1–S4, B2, P1, P2; D1–D8 fixed (stale pool, `-32603` retry where safe in both relays, executor-scope order, CI pin, harness); head pushed and level with origin (tests: Python 3.13, 6567 passed / 0 failed / 0 skipped; ruff, format, mypy clean) · Session 2: queue item 1, incidence records for requests open when a relay stops
- 2026-11-05 · (before the schedule; stopped by the founder) harness, CI pin, B1 plan · pushed 237a332, 4017672; B1 killed at 70/960 rows (tests: 6337 passed / 1 known sandbox failure, 16 skipped before the pin) · B1, then stale pool

2026-11-05 · Phase 1 queue 1 (harness, CI pin, B1 started) · stopped by the founder before schedule; 237a332 pushed, 4017672 local, B1 partial 70/960 uncommitted (tests: 6337 passed / 1 known sandbox failure) · resume 2026-11-09 09:00 IST per the STOPPED section
