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
