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

## State at handoff (2026-11-05)

- 8 commits on the branch since PR #18 (2026-11-02 to 11-04): idempotency
  keys (Python and TypeScript adapters), Python key injection, the Streamable
  HTTP relay, per-invocation incidence records. No CI run exists for them
  (CI ran last on `ec339ab`, 2026-10-16). No open PR.
- Latest results: M16–M20 (controlled fault injection only). Real fault
  incidence is unmeasured.

## Queue (take the first item that is not done or blocked)

1. **Get CI on the branch.** Open a draft PR `claude/serene-bohr-o28snr` →
   `main` titled "MCP idempotency keys, key injection, HTTP relay, incidence
   records". Read CI; fix only genuine failures introduced by these commits.
2. **Stale pooled connection** (KEY_INJECTION §7, "unmeasured"). Add a fault
   case where the server closes an idle keep-alive connection as the relay
   reuses it; measure keyed and unkeyed outcomes; then set the relay's pool
   idle timeout below the server's (httpx and Node both default to 5 s) and
   re-measure. Record as M21.
3. **Real capture for fault incidence.** Follow `CAPTURE_1_PLAN.md` and
   `REAL_CAPTURE.md`: shadow-only, mask-only capture against local real
   servers through both relays, then `stateguard incidence`. Label it as
   local real-stack evidence, never a production rate. If any step needs the
   owner (traffic, accounts, consent), stop and record the exact action here.
4. **T9 circuit breaker** (TASKMAP "Next task").
5. **Second MCP domain** with `outputSchema`, enums/bounds, closed schemas and
   the SDK 2.x lifecycle (TASKMAP "Next task").

## Session log

One line per session, newest first:
`YYYY-MM-DD · task · result (tests: N passed / failures) · next`.
