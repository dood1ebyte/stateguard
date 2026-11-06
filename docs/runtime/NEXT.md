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
- Known sandbox-only failure (VERIFIED 2026-11-05, head fb49bba):
  `tests/examples/test_cli_executor_scope.py::test_on_a_platform_without_pid_namespaces_the_scope_is_refused`
  fails because the venv's `python3` path is not found by the executor scope.
  Everything else passed. Do not count it as a regression; do not "fix" it
  by weakening the test.
- Also run `ruff check src/ tests/`, `ruff format --check src/ tests/` and
  `mypy src/` before pushing (CI runs them).

## Phase 1 (2026-11-05 to 11-18): MCP reliability, the founder's mandate

The founder's Phase 1 mandate supersedes "one task per session". Work continuously: take the
next item, finish it, take the next. The git rules above still hold. Report only at completion
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
`(cd adapters/typescript && npm ci)`; server-everything 2026.8.31 into `/opt/sg-servers/everything`.
Full suite: `STATEGUARD_MCP1_PYTHON=/tmp/mcp1/bin/python .venv/bin/pytest tests/`.

## STOPPED by the founder (2026-11-05, ~21:50 UTC): resume Monday 2026-11-09 09:00 IST

Phase 1 work began before the approved schedule; the founder stopped it. The schedule is
mandatory: no engineering outside the approved windows (2026-11-09, 11, 13, 16, 18, 09:00 IST).

- **Done and pushed:** `237a332`, the in-repo realhttp harness, measurement spec, CI pydantic pin
  for mcp 1.x, a loud failure for a broken `STATEGUARD_MCP1_PYTHON`, and this queue.
- **Committed locally, NOT pushed:** `4017672`, the B1 plan
  (`benchmarks/evidence/realhttp/B1/PLAN.md`). The branch is 1 ahead of `origin`.
- **Stopped mid-run (uncommitted, kept as is):** the B1 batch, after 70 of 960 rows of its
  first part (`B1/rows_notes.jsonl`, `rows_notes.err`). There is no `codestate_notes.json` or
  summary. This part is **incomplete and must not be reported as a result**. Every B1 process
  was stopped; none remains.
- **Left in place:** the git worktree `/tmp/sg-code-237a3326cfc7` (detached at `237a332`,
  used by `run_batch.py`), the venvs `.venv` and `/tmp/mcp1` (pydantic 2.11.7), TypeScript
  `node_modules`, and server-everything at `/opt/sg-servers/everything`. All of these are lost
  if the container is replaced; rebuild them per "Environment" above.
- **Baseline suite on this container** (Python 3.13, head `3c44f3e` plus setup): 6337 passed,
  16 skipped, 1 failed. The failure is the known sandbox-only executor-scope test. 13 of the
  skips were the mcp 1.x injector tests (pydantic too new). They pass, 15/15, with
  pydantic 2.11.7.
- **Queue item 2 (stale pooled connection) was not started:** nothing written yet.
- **Exact next action (2026-11-09 09:00 IST):**
  1. Push `4017672`, unless `origin` has moved.
  2. Decide on the partial B1 rows: keep them as `rows_notes_aborted.jsonl`, with a note in
     `B1/PLAN.md` that the run was stopped.
  3. Re-run B1 in full with new part names (`run_batch.py` refuses to overwrite).
  4. Then queue item 2.

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

## Handoff (2026-11-05, stopped by the founder: work began before the first window)

- **Done and pushed:**
  - `237a332`: the in-repo harness (`benchmarks/runtime/realhttp/`), the CI pin of pydantic
    2.11.7 for mcp 1.9.4, and a loud error when `STATEGUARD_MCP1_PYTHON` is unusable.
  - `4017672`: the B1 plan (`benchmarks/evidence/realhttp/B1/PLAN.md`).
  - Session baseline before any change: 6337 passed, 16 skipped, 1 known sandbox failure. The 13
    mcp 1.x skips were the pydantic defect, fixed in `237a332` (15/15 injector tests pass).
- **Stopped (not committed):**
  - B1 part `notes`: killed after 70 of 960 rows.
  - Kept as they were: `benchmarks/evidence/realhttp/B1/rows_notes.jsonl` (70 rows, partial),
    the empty `rows_notes.err`, no `codestate_notes.json`, the temp run dir
    `/tmp/realhttp-B1-52fcakil`, and the git worktree `/tmp/sg-code-237a3326cfc7`.
  - The partial rows are not evidence. `run_batch.py` refuses to overwrite them, so the next
    session moves them aside (e.g. `rows_notes.partial-killed.jsonl`) before re-running that part.
- **Uncommitted:** this file only.
- **Started, not implemented:** stale pooled connection (queue item 2). Design:
  - the relay gets `--pool-idle-timeout` (httpx `keepalive_expiry`), default about 1 s, below
    gunicorn's 2 s and uvicorn's and Node's 5 s;
  - a delay-injecting TCP proxy (40 ms each way) between the relay and the notes server, which
    widens the race window to about 80 ms;
  - unkeyed `add_note` calls spaced 4.9–5.1 s apart;
  - arms: the old default (5 s) against the new, plus a keyed arm;
  - scored with the ground truth and the relay's invocation records.
- **Exact next action (Fri 11-06, 09:00 IST):**
  - Rebuild the environment if the container is new.
  - Rename the partial B1 rows aside, run B1 (all six parts; the commands are in the
    background job of 2026-11-05, see `run_batch.py` usage), then queue item 2.

## Phase 1 queue (first not done or blocked; mark done here with its commit)

1. In-repo measurement harness and spec (`benchmarks/runtime/realhttp/`). Re-run the HTTP
   baseline (notes, tsnotes, everything; all arms; controls) on the current commit as batch
   B1, so the M17–M19 evidence exists in the repository again.
2. Stale pooled connection (old queue item 2): fault case, measure, fix (relay pool idle
   timeout below the server's), re-measure.
3. Concurrency adversarial validation for the HTTP relay: concurrent sessions and calls under
   session loss, lost responses and deadlines. Each write exactly once; no cross-session leak.
4. stdio relay batch in-repo: Python sqlite (keys via injector) and the TypeScript servers
   (filesystem, memory; keys via preload). The M16 and TS evidence was lost with the container.
5. Crash-window residual (in doubt, 24/204 per stdio server): bound it, or close it where a
   sound recovery check exists.
6. Real capture for incidence (old queue item 3). Owner-only steps go to the session log.
7. Phase Completion Report (`docs/runtime/PHASE1_COMPLETION_REPORT.md`), then notify the founder.

## Session log

One line per session, newest first:
`YYYY-MM-DD · task · result (tests: N passed / failures) · next`.

- 2026-11-06 · Session 1 started 09:00 IST · partial B1 rows set aside in B1/aborted/ · running B1
- 2026-11-05 · (before the schedule; stopped by the founder) harness, CI pin, B1 plan · pushed 237a332, 4017672; B1 killed at 70/960 rows (tests: 6337 passed / 1 known sandbox failure, 16 skipped before the pin) · B1, then stale pool

2026-11-05 · Phase 1 queue 1 (harness, CI pin, B1 started) · stopped by the founder before schedule; 237a332 pushed, 4017672 local, B1 partial 70/960 uncommitted (tests: 6337 passed / 1 known sandbox failure) · resume 2026-11-09 09:00 IST per the STOPPED section
