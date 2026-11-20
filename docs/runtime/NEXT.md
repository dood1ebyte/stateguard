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
- approvals A1–A7 (A6, recruitment, withdrawn) and the rule that there is no external contact
  or data sharing (section 8);
- the two validation tracks (section 10, Track B method; section 11, evidence integrity).

**Validation strategy, amended by the founder on 2026-11-17:**
- **No external developer recruitment, contact or data sharing.** Phase 2 does not depend on
  outside developers, and their absence is not a blocker.
- **Track A:** controlled engineering validation, unchanged, including the fixed S6 protocol.
  It is the only basis for measured reliability claims.
- **Track B:** public failure intelligence. MCP failure reports are collected from GitHub
  first, then Reddit, Hacker News, Indie Hackers and others; public information only.
  - Its workflow is built in S5 (S5.5), and its analysis and reproductions are done in S7.
  - It is never used for reliability rates, failure-reduction percentages, a sub-1% claim, or
    the S6 fault mix.

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
- **CI is green on the S1 code.** Run 53 (head `def9cc6`) passed all 11 jobs: Ubuntu and
  macOS × Python 3.11–3.14, mypy (both configurations), ruff, and import isolation.
  - S1.3 root-caused and fixed the three failures of the first runs (`d486a58`):
    - an unreadable `/root` path in a test's probe;
    - `prctl`, which is Linux-only;
    - a macOS 3.11 timing margin.
    Run 50 on `d486a58` was green.
  - Run 51 (`0e606f3`) hung in a new CLI test, which started a real HTTP relay (fixed in
    `85f5c97`). I cancelled it, and also run 52, which run 53 superseded.
- **Evidence-only commits carry `[skip ci]`**, so data commits do not each queue the macOS
  matrix. Every code commit, and the session's last commit, runs full CI.
- **S2 (2026-11-20):**
  - run 55's macOS auth flake was root-caused and fixed (`ca30096`);
  - run 57 (S2.1) was green;
  - run 59 (S2.3) passed 10 of 11 jobs. macOS 3.13 died in the runner's action download, with
    no step of ours run, and was re-run once;
  - see "S2 result".

**Approvals:**
- A1, A2, A3 and A4: approved (A4's boundary is kept; no developer data is expected).
- **A6 (recruitment): withdrawn on 2026-11-17.**
- **A5: no publishing.**
- **A7: no credentials.**

### S1 result (Wed 2026-11-18, 09:02–11:15 IST): all deliverables done

- **S1.1, done** (`0e606f3`, test fix `85f5c97`):
  - the relays are package modules, `stateguard.relay.stdio` and `stateguard.relay.http`;
  - they run as `stateguard relay stdio [opts] -- <server>` and
    `stateguard relay http --upstream URL [opts]`, or as `python -m stateguard relay ...`;
  - `examples/mcp/stdio_proxy.py` and `http_proxy.py` are thin wrappers. Imported, they are
    the package modules themselves (a `sys.modules` alias), so every existing test, driver
    and monkeypatch works unchanged;
  - `stateguard relay http` without the `mcp` extra exits 2 and names the extra.
- **S1.2, done** (`0e606f3`, plus `def9cc6`: `--keys auto` requires `--retry auto`):
  - `--keys auto` (default off) starts:
    - a Python server (`python SCRIPT`, `python -m MODULE`, a console script whose `#!` names
      Python) under the injector;
    - a Node server (`node ENTRY`, or an npm bin with `#!/usr/bin/env node`) with the preload.
  - Everything else runs unkeyed, with the reason on stderr and in
    `relay_started.keys.unkeyed_reason`; that covers `npx`, `uvx`, `docker`, shell
    wrappers, binaries, and `python` with an option before the script.
  - The decisions are recorded in `KEY_INJECTION_AND_HTTP_RELAY.md` section 9:
    - keyed tools = `--keyed-tools` plus the tools declared `non_idempotent` or `idempotent`
      in `--effects` (no implicit "every non-read tool"; a wildcard in the adapters is
      deferred);
    - the store is `~/.stateguard/keys.db`, with a scope per server command;
    - the Python server imports stateguard from a directory that holds only it;
    - the preload ships in the wheel (`stateguard/relay/ts/`);
    - the advertisement is verified after each listing (`keys_advertised` /
      `keys_not_advertised`);
    - npx and uvx support was not attempted.
- **Acceptance:**
  - **End-to-end test, from the wheel** (`tests/examples/test_relay_keys_auto_e2e.py`):
    - builds the wheel and installs it alone in a fresh virtual environment;
    - runs `stateguard relay stdio --keys auto -- node <pinned server-memory>`;
    - `add_observations` is keyed;
    - a TEST-ONLY `after_record` crash is answered `replayed`, with one answer and one
      effect. **Passes locally.** In CI it skips, because the pinned Node server is not
      installed there.
  - **B7, the HTTP matrix through `stateguard relay http`:** every StateGuard cell of all 8
    parts equals B5/B6:
    - notes and tsnotes: sg 84/192, sg_keyed 24/192, sg_keyed_deadline 0/192;
    - everything: sg 12/96, sg_deadline 0/96;
    - crash cells: keyed 24/36, with `after_record` 0/12;
    - controls: 0;
    - 0 unsafe outcomes and 0 relay-record mismatches.
    The only difference anywhere is a baseline arm, which runs no relay: tsnotes naive
    `read/none` 0/12, against 1/12 in B5. The plan named that in advance.
  - **S10, the stdio matrix through `stateguard relay stdio`:** every StateGuard cell equals
    S8/S9:
    - sqlite, memory and filesystem: sg 84/156, sg_keyed 24/156;
    - memory_idempotent: 0/96;
    - slow: 0/12 per server;
    - 0 unsafe outcomes and 0 relay-record mismatches; the baselines were identical too.
  - **New `sg_keys_auto` arm**, where the relay wraps the plain server command itself:
    identical to sg_keyed on its 4 cells on all three servers:
    - `write/none` executed;
    - `after_record` replayed;
    - `after_begin` and `after_effect` surfaced as unknown, never success or failure;
    - 0 unsafe.
  - **Interruption:** a container restart cut S10 `filesystem` short at 240/624 rows. Those
    rows are kept as `rows_filesystem_interrupted.*` and are not counted. The part was re-run
    complete as `filesystem_r2` (run log in S10's `PLAN.md`).
  - **Comparison tool:** `benchmarks/runtime/compare_cells.py`. The per-part results are in
    `summary_*.compare.txt`.
  - **CI:** green on `def9cc6` (run 53, all 11 jobs).
  - **Suites, final code:** Python 3.11.18, 3.12.3, 3.13.18 and 3.14.6 each 6662 passed,
    0 failed, 0 skipped, with `STATEGUARD_MCP1_PYTHON` set. The first 3.14 run had 2
    errors: the end-to-end fixture called the interpreter's pip, and that venv has none. The
    fixture now uses the fresh venv's own pip (`e6bdd18`); the re-run was clean.
  - **TypeScript:** `tsc` clean, tests 74/74.
  - **Lint:** ruff, format and mypy (both configurations) clean.
- **Deferred or not attempted in S1:** nothing from the S1 agenda.
  - Out of scope and noted for later sessions: `npx`/`uvx` keying, a keyed-tools wildcard
    in the adapters, and `--keys auto` for the HTTP relay (it connects to a URL; it starts
    no server).
  - The deferral list (`PHASE2_PLAN.md` section 4) is unchanged; no earlier session had
    deferred anything yet.

### S2 result (Fri 2026-11-20, 09:01–10:20 IST): all deliverables done

- **Before S2's agenda:** run 55's macOS 3.13 failure was root-caused and fixed (`ca30096`). The
  auth test's late answer at 0.45 s raced the 0.3 s deadline: the relay acts on the deadline
  when its deadline thread next wakes. Reproduced: with a coarse tick, 0.45 s gave
  authentication 20/20 and 0.9 s gave unknown 20/20, and in both 0 calls were told success
  and 0 retried. The late cases now answer at 0.9 s. No safety issue.
- **S2.1, done** (`ddf8b9c`): `stateguard capture stdio|http --dir DIR [--label]` and
  `stateguard report [--check] BUNDLE`.
  - The bundle holds:
    - the mask-redacted recording, with numbers masked (`--record-mask-numbers`, new on both
      relays);
    - the invocation records, through an allowlist with no ids and no text;
    - key-store counts;
    - a manifest with SHA-256s;
    - the report.
  - The report rebuilds from the bundle alone. It covers:
    - outcomes per call (succeeded, tool error, failed, UNKNOWN, not successful), with Wilson
      intervals;
    - retries, and would-retry in shadow;
    - latency and relay-overhead percentiles.
  - The raw audit log and the key store never enter a bundle.
  - Documented in `REAL_CAPTURE.md`.
- **S2.2, done** (`7edf71c`):
  - **Denominator checks:** client calls = records, unfinished counted, answers = finished,
    none answered twice, recording complete.
  - **Key-store duplicate detection:** identical calls executed under more than one key, counts
    only. Verified on a deliberately duplicated keyed call:
    - at the store level;
    - end to end on the pinned server-memory with `--keys auto`: 5 executions, 1 repeated, and
      the server said "executed" five times. The file shows the marker once, because the
      server skips an observation it already holds.
- **S2.3, done** (`b291562`): `stateguard relay http --record`, shadow only.
  - It refuses `--passthrough` and `--retry auto`.
  - It has the same recorder and redactor as stdio, with taps in all four directions, JSON and
    event stream.
  - `stateguard capture http` writes the bundle when the relay is stopped.
  - The HTTP relay's `main()` now returns after a graceful SIGINT/SIGTERM (uvicorn raises the
    signal again).
- **S2.4, done: C1** (`benchmarks/evidence/capture/C1/`, plan before rows, code `9de0a26`).
  Labelled **real server, scripted workload**:
  - **Servers:** git 2026.8.18, server-filesystem 2026.8.31, and fetch 2026.10.10 against a
    local site.
  - **Size:** **1,200 tool calls**: 200 per server and arm, shadow and auto, paired by seed.
  - **Results:**
    - every denominator check holds;
    - 0 failed, 0 UNKNOWN;
    - tool errors (git 8, filesystem 7, fetch 8 per arm) are the deliberate misses, equal in
      all 12 pairs;
    - all 24 reports rebuild from their bundles (re-verified from the tarballs);
    - no typed value is in any bundle;
    - overhead p50 1.0–1.3 ms.
  - **A missed prediction, recorded in RESULTS.md:** auto resent the 23 tool errors, all on
    declared reads, by the retry rule. Each resend got the same answer.
- **Tests:**
  - Python 3.11.18, 3.12.3, 3.13.18 and 3.14.6: each 6677 passed, 0 failed, 0 skipped.
  - TypeScript: `tsc` clean, tests 74/74.
  - ruff, format and mypy: clean.
- **CI:** green on S2.1's code (run 57). On S2.3's code (run 59), 10 of 11 jobs passed. macOS
  3.13 failed in the runner's "Prepare all required actions": it could not parse its downloaded
  `actions/checkout` `action.yml`, and no step of ours ran. That job was re-run once. The
  session's last commit runs full CI.
- **Deferred or not attempted:** none of the S2 agenda.
  - **Noted, not fixed:** on HTTP, per-call overhead is not matched (upstream ids are the
    relay's own), and the report says so.
  - The deferral list is unchanged. No earlier session had deferred items, so none were taken,
    and S3 was not started.

### S3 starting checklist (Mon 2026-11-23, 09:00 IST)

1. Run `TZ=Asia/Kolkata date`; check git is `0 0` with origin; check the toolchain. On
   2026-11-20 these were present: `.venv`, `/tmp/mcp1`, `/tmp/venv311`, `/tmp/venv312`,
   `/tmp/venv314b`, node v22.23.4, `/opt/sg-servers/*`, `adapters/typescript/node_modules`,
   and `/tmp/sgpy` (git and fetch servers).
2. CI: confirm the last run on PR #19 is green. Re-run once only if a job died before any test
   body ran.
3. Start the Python 3.13 baseline suite in the background.
4. Do S3 per `PHASE2_PLAN.md` (it depends on S1.2, done):
   - **S3.1:** a Python helper that commits the key's record in the same SQLite transaction as
     the effect, and a cooperating reference server with notes in SQLite.
   - **S3.2:** the recovery-check hook (`STATEGUARD_RECOVERY_CHECK`) under the replay rule.
   - **S3.3:** the TypeScript atomic helper and its server.
   - **S3.4:** the crash cells, and `SERVER_IDEMPOTENCY_CONTRACT.md`.
5. **The acceptance:**
   - on cooperating servers only: `after_begin`, `after_effect` and `after_record`, × 12 each,
     0/36 unresolved with 0 duplicates;
   - non-cooperating servers unchanged from Phase 1;
   - late-attempt and fencing tests;
   - every new test fails without its change.
   Fallback: S3.3.
6. **Batch evidence:**
   - the plan is written before any row;
   - `run_batch.py` takes `--suite capture` and the capture driver;
   - evidence-only commits carry `[skip ci]`;
   - the last commit runs full CI.

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

## Handoff (end of S2, Fri 2026-11-20 about 10:20 IST)

- **Done:** S2.1–S2.4 (see "S2 result" above), and the root cause and fix of run 55's auth
  flake.
  - Commits: `ca30096`, `ddf8b9c`, `7edf71c`, `9de0a26`, C1 evidence, `b291562`, docs.
- **Unfinished:** nothing on the S2 agenda.
- **Blockers:** none.
- **Pushed:** everything; origin and local are level. **Uncommitted:** nothing.
  **Running:** nothing locally. CI's re-run of the macOS 3.13 job, and the last commit's run,
  are on GitHub.
- **Exact next action:** S3, Mon 2026-11-23, 09:00 IST: the S3 starting checklist above.

## Handoff (end of S1, Wed 2026-11-18 11:15 IST)

- **Done:** S1.1, S1.2 and S1.3, with every acceptance item (see "S1 result" above).
  - Code: `d486a58` (CI fixes), `0e606f3` (packaged relays, `--keys auto`), `85f5c97` (a CLI
    test that would hang), `def9cc6` (`--keys auto` requires `--retry auto`), `e6bdd18` (the
    end-to-end fixture's pip).
  - Evidence: B7 and S10, cell for cell against B5/B6 and S8/S9, all StateGuard cells equal.
- **Unfinished:** none of the S1 agenda. The agenda finished early. No earlier session has
  deferred items, so none were taken, and S2's agenda was not started.
- **Blockers:** none.
- **Pushed:** everything; origin and local are level. **Uncommitted:** nothing.
  **Running:** nothing (batches, suites and monitors all stopped).
- **Exact next action:** S2, Fri 2026-11-20, 09:00 IST: the S2 starting checklist above.

## Handoff (validation-strategy amendment, Tue 2026-11-17 04:18 IST; no engineering)

- **Changed:**
  - `PHASE2_PLAN.md` (recruitment removed; Track B in sections 10 and 11; S5, S6 and S7
    revised; completion criteria split into Track A and Track B);
  - this file;
  - the S5, S6 and S7 scheduled-task instructions (same IDs, same times).
- **Unchanged:** S1–S4, their instructions and tasks, every engineering deliverable, the
  safety gates, and the statistical protocol (one clarification added: public reports never
  weight the fault mix).
- **Exact next action:** S1, Wed 2026-11-18, 09:00 IST: the S1 starting checklist above.

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

- 2026-11-20 · Phase 2 S2 (09:01–about 10:20 IST) · run 55 auth flake root-caused and fixed (deadline-thread race, reproduced); `stateguard capture stdio|http` and `stateguard report` (masked bundle, allowlisted invocations, rebuilt from the bundle alone); denominator checks; key-store duplicate detection (verified on a deliberate duplicate); HTTP relay `--record` (shadow only); C1: 1,200 calls on 3 real servers, scripted workloads, shadow and auto paired, every check holding, 0 failed and 0 UNKNOWN, 23 designed resends of tool errors on declared reads · pushed, level with origin (tests: Python 3.11.18 / 3.12.3 / 3.13.18 / 3.14.6 each 6677 passed / 0 failed / 0 skipped; TypeScript tsc clean, 74/74; ruff, format, mypy clean; CI 10/11 on `b291562` plus a runner action-download failure re-run) · S3 Mon 23 Nov 09:00 IST
- 2026-11-18 · Phase 2 S1 (09:02–11:15 IST) · relays packaged (`stateguard relay stdio|http`); `--keys auto` (injector for Python, preload for Node, unkeyed with a reason otherwise); end-to-end from the wheel (keyed write, after_record crash replayed once); B7 (HTTP) and S10 (stdio) equal to B5/B6 and S8/S9 in every StateGuard cell; sg_keys_auto equal to sg_keyed on 3 servers; CI green on `def9cc6` (S1.3: 3 failures fixed; 1 hanging test fixed) · pushed, level with origin (tests: Python 3.11.18 / 3.12.3 / 3.13.18 / 3.14.6 each 6662 passed / 0 failed / 0 skipped; TypeScript tsc clean, 74/74; ruff, format, mypy clean) · S2 Fri 20 Nov 09:00 IST
- 2026-11-17 · Validation-strategy amendment (founder; 04:18 IST, no engineering) · recruitment removed; Track B (public failure intelligence) added; S5–S7 instructions updated, same IDs and times; S1–S4 unchanged · pushed, level with origin (tests: none run) · S1 Wed 18 Nov 09:00 IST
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
