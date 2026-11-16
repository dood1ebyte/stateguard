# StateGuard Phase 2: the approved plan

**Status:** approved by the founder on 2026-11-17 (final authorisation, after review with
ChatGPT). This file is the source of truth for Phase 2 execution. `NEXT.md` holds execution
state; scheduled tasks point here.

**Strategic objective:** StateGuard as commercially viable AI runtime reliability
infrastructure, MCP first.

**Engineering north star:** below 1% residual MCP invocation failure rate on clearly defined,
representative, supported workloads, with safe execution and UNKNOWN never hidden. Synthetic
improvements alone are not enough: developer adoption and real-world evidence are part of the
goal.

---

## 1. Schedule (founder-approved; do not move, extend or compress)

Monday, Wednesday or Friday, 09:00–14:00 IST, five hours at most. One session per window:
the same Claude Code conversation (`session_01NykZHSv2cJyQrzsT2GQbv8`), the same repository,
the branch `claude/serene-bohr-o28snr`.

| Session | Date | Window (IST) | Scheduled task |
|---|---|---|---|
| S1 | Wed 2026-11-18 | 09:00–14:00 | see `NEXT.md` |
| S2 | Fri 2026-11-20 | 09:00–14:00 | see `NEXT.md` |
| S3 | Mon 2026-11-23 | 09:00–14:00 | see `NEXT.md` |
| S4 | Wed 2026-11-25 | 09:00–14:00 | see `NEXT.md` |
| S5 | Fri 2026-11-27 | 09:00–14:00 | see `NEXT.md` |
| S6 | Wed 2026-12-02 | 09:00–14:00 | see `NEXT.md` |
| S7 | Mon 2026-12-07 | 09:00–14:00 | see `NEXT.md` |

S7 is ten days after S5, giving outside developers time to use the kit.

---

## 2. Operating rules for every session

**Window:**
- Check the clock (`TZ=Asia/Kolkata date`) at the start and regularly.
- Work only inside the window.
- Start no new task after 13:30 IST.
- Stop engineering by 14:00 IST.
- If the session is delivered after 14:00, do no engineering. Record it as missed and stop.

**Commits and state:**
- Commit and push each finished piece, about every 60 minutes.
- Update `NEXT.md` during the session as well as at the end.
- Preserve experiment state and logs.

**Closing a session:**
- Stop every background process.
- Update `NEXT.md` with what is done and unfinished, blockers, and the exact next action.
- Commit, push, and confirm that origin is level.

**Interruptions:**
- If usage limits interrupt a session, preserve the state and resume at the next authorised
  session.
- Never compensate by working outside the schedule.

**Early finish:**
- If a session's agenda is done early, report and stop.
- Do not invent work, and do not start the next session's agenda.
- The only exception is the deferral rule in section 4: deferred items from earlier sessions,
  in list order.

**Autonomy:** no routine founder approvals. Escalate only for:
- a consequential architectural decision outside the approved scope;
- a security-sensitive or destructive operation;
- missing credentials or permissions that genuinely block an approved deliverable;
- an irrecoverable blocker;
- a decision that changes product direction.

**Git:**
- Work on `claude/serene-bohr-o28snr` only, with plain fast-forward pushes.
- Never merge, rebase, force-push, reset, rewrite history or clean up destructively.
- If local and origin diverge, stop pushing, record the conflict and notify the founder.

**Never:**
- publish packages (A5 is not approved);
- merge the draft PR or mark it ready;
- contact people or share data outside the consent process (A4, A6);
- use credentials that were not supplied (A7);
- create, change or delete scheduled tasks;
- expose private outreach or research documents;
- fabricate metrics;
- present synthetic results as production evidence.

**Safety outranks everything:** an unsafe outcome (a safety violation, section 6) found in any
run stops the session's feature work until it is fixed.

---

## 3. Sessions: deliverables, dependencies, acceptance, fallback

Each session plans 4.0–4.5 hours of work and keeps 0.5–1.0 hours in reserve. About 29.5 hours
are planned and about 5.5 are reserve, out of 35. Long runs (suites, experiments) go in the
background while code is written.

### S1: product surface and keys by default (Wed 18 Nov)

**Depends on:** A2 (draft PR, for CI only).

| ID | Deliverable | Hours |
|---|---|---|
| S1.1 | The relays as package commands: `stateguard relay stdio [opts] -- <server cmd>` and `stateguard relay http --upstream URL [opts]`. `examples/mcp/stdio_proxy.py` and `http_proxy.py` stay as thin wrappers, so every existing test and driver keeps working. | 2.25 |
| S1.2 | `--keys auto`: wraps a Python MCP server with the injector and a Node server with the preload, detected from the command. Anything else runs unkeyed, and the relay logs why. | 1.0 |
| S1.3 | CI on the draft PR: Ubuntu and macOS × Python 3.11–3.14. Failures are fixed or root-caused within a one-hour time-box. | 1.0 |
| — | Reserve | 0.75 |

**Acceptance:**
- An end-to-end test builds a wheel and installs it in a fresh virtual environment.
- It runs `stateguard relay stdio --keys auto -- <pinned server-memory>` and shows two things:
  - the write tool is keyed;
  - a crash after the key's record is replayed exactly once.
- The HTTP and stdio matrices run through the new commands match B5/B6 and S8/S9 cell for cell.
- Python 3.11–3.14 suites, the TypeScript checks, ruff, format and mypy all pass.
- CI is green on Ubuntu and macOS, or each remaining failure is root-caused in `NEXT.md`.

**Fallback, in order:**
1. Leave macOS-only CI failures as tracked items.
2. Ship only the stdio command; the HTTP command moves to S4's reserve, and the HTTP relay
   stays at its example path.

**Never cut:** S1.2, the end-to-end test, the regression matrices.

### S2: capture, measurement and denominator integrity (Fri 20 Nov)

**Depends on:** S1.1; A3 (pinned public servers).

| ID | Deliverable | Hours |
|---|---|---|
| S2.1 | `stateguard capture` and `stateguard report`, built on the recording format, incidence records and `evidence_report.py`. They report: observed failure rate per call, UNKNOWN, retries, latency percentiles, relay overhead, and a redacted shareable bundle (mask redaction required, local only). | 2.0 |
| S2.2 | Denominator integrity: client calls = relay records, and `unfinished` requests are counted. Duplicate detection from the key store (executions per key) on keyed servers, the only ground truth real traffic has. | 0.75 |
| S2.3 | `--record` for the HTTP relay, in shadow mode, under the same privacy rules as stdio. | 1.25 |
| S2.4 | Real servers driven by scripted, agent-like workloads: git (`CAPTURE_1_PLAN.md`), filesystem, and fetch against a local site. Labelled **"real server, scripted workload"**. | 0.5 |
| — | Reserve | 0.5 |

**Capture acceptance (clarification C):**
- The target is **at least 1,000 captured calls in total across at least 3 real servers**.
- **The same scripted workloads run against the same servers in shadow and in auto** wherever
  possible.
- Call counts are reported **separately for each mode and each server**. The report says
  whether a shadow/auto comparison is paired (same workload, seed and server) or not.
- **Every denominator is reported, and so is every missing or invalid capture.**
- **Shadow and auto results are never combined in a way that hides a difference between
  them.**
- The 1,000 target stands unless a real technical blocker makes it impossible. Any shortfall
  is reported as such.

**Other acceptance:**
- The report can be rebuilt from the bundle alone.
- Privacy tests pass: masking is enforced and real captures carry no ground-truth labels.
- The denominator checks hold.
- Duplicate detection is verified against a deliberately duplicated keyed call.

**Fallback:** S2.3 (HTTP falls back to its incidence records).

**Never cut:** S2.1, S2.2, the privacy guarantees.

### S3: crash-window closure on cooperating servers only (Mon 23 Nov)

**Depends on:** S1.2.

| ID | Deliverable | Hours |
|---|---|---|
| S3.1 | A Python helper that commits the key's record **in the same SQLite transaction as the side effect**, plus a cooperating Python reference server (notes held in SQLite). | 1.75 |
| S3.2 | A recovery-check hook in the injector and the preload (`STATEGUARD_RECOVERY_CHECK=module:function`, and the TypeScript equivalent), **under the replay rule below**. | 1.0 |
| S3.3 | The same atomic helper in TypeScript, plus a cooperating TypeScript reference server. | 1.0 |
| S3.4 | Crash cells on the cooperating servers over HTTP and stdio (background); `SERVER_IDEMPOTENCY_CONTRACT.md` updated. | 0.5 |
| — | Reserve | 0.75 |

**The replay rule (clarification A):**
- A recovery check that answers "not found" is **not** proof that the original operation cannot
  still execute.
- A replay is allowed only when the server-side contract **establishes authoritative execution
  state and prevents concurrent or late execution of the original attempt**. That needs one of:
  - the atomic record-and-effect transaction;
  - an atomic claim or fencing token checked on the effect's write path;
  - an exclusive store that proves the original executor is gone (D11's mechanism).
- **If that guarantee cannot be established, the call stays UNKNOWN and is not replayed.**
- A duplicate side effect is unacceptable in every case.
- Tests must include a late original attempt racing a recovery decision: it must be fenced off,
  or the call must stay UNKNOWN.

**Acceptance:**
- **Only on cooperating reference servers** (key record and side effect committed atomically):
  crash cells (`after_begin`, `after_effect`, `after_record` × 12) are **0/36 unresolved with 0
  duplicates**, on each relay and each language delivered.
- Every new test fails without its change.
- **Non-cooperating servers** (sqlite, memory, filesystem, notes) give results identical to
  Phase 1: still in doubt or unknown, never success.
- The late-attempt and fencing tests pass.

**Fallback:** S3.3. TypeScript keeps the recovery-check hook only, and 0/36 is claimed for
Python only.

**Never cut:**
- S3.1 (the Python cooperating server's crash-window closure);
- the replay rule;
- unchanged UNKNOWN on non-cooperating servers;
- the contract update.

**Scope of the claim:** no document or report says "crash-window recovery" without naming this
boundary. Unmodified third-party servers, and effects outside the key store's transaction
(files, remote APIs, other databases), keep their floor: in doubt, never re-run, counted as a
failure.

### S4: runtime hardening (Wed 25 Nov)

**Depends on:** S1. A2 for the netem job.

| ID | Deliverable | Hours |
|---|---|---|
| S4.1 | Schema-repair safety: fall back to the original call (recorded) when the server rejects a repaired call; refuse repair from a listing known to be stale when refresh is off. | 1.5 |
| S4.2 | The compatibility matrix, attempted and documented (section 7). | 2.0 |
| S4.3 | An optional per-tool serialisation flag (server-memory writes without a lock). | 0.5 |
| S4.4 | Event-stream draining so connections are reused, plus a netem measurement job on a GitHub runner. | 0.5 |
| — | Reserve | 0.5 |

**Acceptance:**
- A1/A2 stale-listing cells re-run with refresh off: **0 false successes, 0 false failures**.
- The compatibility table is published with a status per server and 0 unsafe outcomes.
- server-memory under concurrency: 0 lost writes with serialisation on, and the loss measured
  with it off.
- The regression matrices match.
- S4.4, if shipped: connection counts, plus the network cost measured on a netem runner (or
  reported as not measured).

**Fallback, in order:** S4.4; compatibility servers beyond the first 6; S4.3 (it becomes a
documented limitation).

**Never cut:** S4.1, zero unsafe outcomes across the attempted matrix.

### S5: developer kit and hand-off (Fri 27 Nov)

**Depends on:** S1, S2; A4 and A6 (the consent process).

| ID | Deliverable | Hours |
|---|---|---|
| S5.1 | `stateguard trial`: runs a developer's own server in shadow, then in auto, and produces a before/after report and a redacted bundle. Optional fault injection runs only against a sandbox copy or a fixture. | 2.0 |
| S5.2 | Onboarding: `mcpServers` snippets for Claude Desktop, Cursor and Claude Code; `stateguard doctor`; a scripted clean-machine timing from install to first protected call. | 1.0 |
| S5.3 | Release candidates (wheel, sdist, npm tarball, CHANGELOG), built and verified. **Not published (A5 is not approved).** | 0.5 |
| S5.4 | The developer hand-off pack and the consent and data-handling process (section 8). **The recommended configuration is frozen at this commit; S6 tests exactly this.** | 0.5 |
| — | Reserve, also for friction found by the clean-machine run | 1.0 |

**Acceptance:**
- `stateguard trial` produces a before/after report on 3 real servers, scripted.
- Clean-machine time from install to first protected call is **5 minutes or less**, scripted.
- Release candidates install cleanly.
- The recommended configuration is written down and frozen.
- The hand-off pack is ready for the founder to send. **Claude contacts no one.**

**Fallback:** `doctor` (the snippets stay).

**Never cut:** S5.1, the frozen configuration, no publishing.

### S6: validation under the fixed statistical protocol (Wed 2 Dec)

**Depends on:** S5's frozen configuration; S3's cooperating servers.

| ID | Deliverable | Hours |
|---|---|---|
| S6.0 | Commit section 6 of this file **unchanged** as the S6 batch's `PLAN.md`, before any trial. Only commit hashes and server versions are filled in. | 0.25 |
| S6.1 | Harness reusing the R1/R2 drivers: independent per-attempt random faults with fixed seeds; crash points that can fire repeatedly; the read/write mix. | 1.25 |
| S6.2 | Runs in the fixed order (background, about 2–2.5 h of compute). | 0.5 |
| S6.3 | Analysis: failure rate per call (Wilson and session-level bootstrap), the safety-violation dimension, UNKNOWN, latency. | 0.75 |
| — | Reserve, for fixing a defect found mid-run | 1.0 |

**Acceptance:**
- Every run is done or reported "not run".
- Every claim follows the claim rule (section 6).
- Safety-gate failures are reported as such.

**Fallback:** unfinished runs are reported "not run", in the fixed order, never swapped.

**Never cut:** the committed protocol, the fixed order, reporting every completed run.

### S7: real-user data, fixes, final validation and the Phase 2 report (Mon 7 Dec)

**Depends on:** S5's hand-off, then the developers' calendar time.

| ID | Deliverable | Hours |
|---|---|---|
| S7.1 | Analyse the outside developers' redacted bundles and before/after reports. Every figure is labelled with its source and sample size. | 1.5 |
| S7.2 | Fix the defects and friction developers hit, with tests. | 1.5 |
| S7.3 | Final regression: Python 3.11–3.14, TypeScript, the matrices, CI (background). | 0.5 |
| S7.4 | The Phase 2 report (section 9). | 0.5 |
| — | Reserve | 1.0 |

**If no outside data arrived:**
- Product validation is reported **incomplete**. Nothing is substituted for it.
- S7.1–S7.2 time goes to deferred items, in list order (section 4).
- S7.3 and S7.4 are unchanged.

**Never cut:** S7.3, S7.4, honest labels.

---

## 4. Contingency: the deferral list

**Rules:**
1. Sessions are never extended.
2. A session's reserve covers its own overruns first.
3. Beyond the reserve, items are deferred **in this order** and recorded in `NEXT.md` with
   what remains.
4. A deferred item can be picked up later only after that session's own must-haves are done.
5. A safety defect displaces deferrable items. It never displaces validation or the final
   regression.

**Deferred first to last:**
1. Event-stream draining and the netem measurement (S4.4).
2. SDK version checks beyond the pinned versions.
3. Compatibility servers beyond the first 6.
4. The TypeScript atomic helper (S3.3); the TypeScript recovery-check hook stays.
5. Per-tool serialisation (S4.3).
6. HTTP `--record` (S2.3); HTTP evidence falls back to incidence records.
7. `stateguard doctor`.
8. S6's secondary per-fault statistics.

**Never cut:**
- the safety requirements (zero unsafe outcomes, the S4.1 repair fallback, the S3 replay rule);
- packaged relays and `--keys auto`;
- capture and reporting;
- the Python cooperating server's crash-window closure;
- fixed-protocol validation;
- the final regression and completion reporting.

---

## 5. Dependencies and parallel work

- **Order:** S1 → S2 → S5 → (a wait of at least 7 days; 10 as scheduled) → S7.
- **S3 and S4** depend only on S1. S3 comes first because S6 needs it.
- **S6** needs S3 and S5, and falls inside the wait before S7.
- **Background in every session:** the suites (about 40 minutes for four Pythons, back to
  back), the matrices, the capture runs, the compatibility runs, and S6's compute.
- **The founder's track** (recruitment and consent, A6) runs alongside S1–S5.

---

## 6. The statistical protocol, fixed (S6 commits this section unchanged, before any trial)

No fault probability, sample size, run order or claim rule may be adjusted after any result is
seen. A change made before any run needs a new protocol version, committed first and reported
alongside the original.

1. **Unit and denominator.**
   - One call is one `tools/call` sent by the workload's client.
   - The denominator is **every** call the client sent in the run.
   - Retries never add to it.
   - Calls lost to a broken session, client timeouts, and driver or harness errors all count
     as failures.
   - **No call is excluded.**
   - A whole run is invalid only if a pre-run check fails: wrong code commit, or uncommitted
     harness changes. An invalid run is re-run in full, and both are reported.
2. **Two reporting dimensions, always kept apart (clarification B).**
   - **Residual failure rate** = (failed, resolved + unresolved) ÷ all client calls.
     - *Success:* the client was told success, and ground truth shows exactly one effect
       (correct content, for a read).
     - *Unresolved:* unknown, in doubt, in progress, no answer, or a timeout.
     - *Failed, resolved:* every other failure.
     - **UNKNOWN always counts as a residual failure.**
   - **Safety violations:** duplicate effects, false successes, false failures and wrong
     content, each counted.
     - **Any safety violation in a StateGuard configuration fails the safety gate, whatever
       the residual failure rate.**
     - A failed safety gate makes that configuration's run a failure of the protocol, reported
       as such.
3. **Workloads.**
   - **W1, local stdio:** sqlite (Python), memory (TypeScript, serialised), filesystem
     (TypeScript). Sessions of 12 calls, 4 in flight (memory: 1). Reads and writes 1:1.
   - **W2, HTTP:** notes (Python) and tsnotes (TypeScript), keyed by the injector and the
     preload. 4 concurrent sessions × 6 calls. Reads and writes 1:1.
   - **W1-coop and W2-coop:** the S3 cooperating reference servers, under the same shapes.
   - **Configurations:** the recommended configuration frozen in S5, and raw (no StateGuard).
   - These represent **the defined supported scope, not production traffic**, and every report
     says so.
4. **The fault model.**
   - Each attempt (first attempts **and** resends) is faulted independently with probability
     *q*.
   - The class is drawn **uniformly** from the transport's injectable faults:
     - **W1:** kill_before, lost_exit, hang, transient, duplicate, garbage, truncated,
       nonconformant, and the three crash points (after_begin, after_effect, after_record);
     - **W2:** kill_before, http_503_before, lost_response, http_502_after, cut_stream,
       hang_after, rpc_internal_error, session_loss, dup_answer, garbage_answer,
       truncated_answer, nonconformant_answer, and the three crash points.
   - *q* ∈ {1%, 5%, 20%}. **All three are run and all three are reported.**
   - Seeds: the run index.
   - The uniform mix is **an assumption, not an observation**.
   - If real captures contain at least 30 observed faults, one extra "observed mix" run is
     added and reported **alongside** the others. Otherwise it is not run.
   - Nothing is re-weighted after results are seen.
5. **Sample size, fixed.**
   - 1,200 calls per (workload, server, configuration, *q*) for StateGuard.
   - 600 calls per cell for raw.
   - No early stopping, and no extension to rescue a result.
6. **Intervals and the claim rule.**
   - Wilson 95% interval on calls.
   - A session-level bootstrap (10,000 resamples, seed 20261202, 95% percentile interval),
     because calls in a session are not independent.
   - **"Below 1%" may be claimed for a (workload, server, *q*) only if both upper bounds are
     below 1% and the safety gate passed.** With 1,200 calls that allows at most about 5
     failures.
   - Workload-level figures that pool servers equally are secondary.
   - Per-fault (conditional) rates are secondary, and may claim at most 1% only if at least 381
     faulted calls were observed.
7. **Run order:** *q* = 5%, then 20%, then 1%. Within each: W1, W2, W1-coop, W2-coop. Raw follows
   each StateGuard run. Unfinished runs are reported "not run".
8. **Reporting:**
   - every run and every *q*;
   - raw against StateGuard;
   - the safety dimension and UNKNOWN, separately;
   - latency p50/p95;
   - invalid runs.
9. **Prediction, recorded before any run:** with the uniform mix, crash windows dominate what
   is left on non-cooperating servers.
   - W1 and W2 are expected below 1% at *q* = 1%, possibly at 5%, **not at 20%**.
   - The cooperating workloads are expected below 1% at all three levels.
   - Results are reported as they come out.

---

## 7. The compatibility matrix (attempted and documented)

**Candidates, in priority order, none needing credentials:**
1. filesystem
2. memory
3. everything
4. sqlite
5. git
6. fetch (against a local site)
7. time
8. sequential-thinking
9. a FastMCP (Python) example server
10. a TypeScript SDK Streamable HTTP example server

A browser server (puppeteer or playwright) is optional, last.

**Per server, time-boxed to about 15 minutes of attention:**
- a pinned install;
- handshake and listing through the relay, in shadow and in auto;
- calls with no fault;
- whether `--keys auto` can wrap it;
- three faults where safe: kill_before, a lost answer, a hang.

**Status, one per server:**
- **Pass**;
- **Pass with a documented limitation**;
- **Unsupported**, with the reason;
- **Not attempted**, with the reason (out of time, or needs credentials).

**Hard requirement:** 0 unsafe outcomes across every attempted server. The target is 8–10
servers **attempted**, not 8–10 passing. Credentialed servers are not attempted: A7 provided
no accounts and the core does not need them.

---

## 8. Founder approvals (2026-11-17) and the consent process

| # | Decision | Status |
|---|---|---|
| A1 | The final seven-session plan and schedule | **Approved** |
| A2 | Open a draft PR so CI can run. **Never merge** without separate approval. | **Approved** |
| A3 | Install pinned public MCP servers from npm and PyPI (no credentials) | **Approved** |
| A4 | Redacted developer data kept **outside the repository**; only aggregate figures committed; consent, privacy and access controls enforced | **Approved** |
| A5 | PyPI and npm publishing | **Not approved.** Build and verify release candidates only. |
| A6 | Prepare recruitment and hand-off for three outside developers. External contact and data sharing follow the consent process. | **Approved** (preparation) |
| A7 | Credentialed test accounts | **Not provided; not required for core work** |

None of these is permission to publish packages, merge the PR, contact anyone, or use
credentials that were not supplied.

**Consent process (A4, A6), prepared in S5:**
- The founder makes every external contact. Claude prepares the pack and contacts no one.
- **Before any data is shared, the developer gets a written description of:**
  - what the bundle contains: redacted recordings, incidence records, the trial report;
  - what is masked;
  - where it is kept: outside the repository, with access limited to the founder;
  - that sharing is opt-in.
- Bundles are produced locally by `stateguard capture` / `trial`, redacted with mask mode.
  The developer reviews a bundle before sending it.
- Only aggregate figures are committed, labelled with source and sample size. No bundle, raw
  recording or identifying detail goes into the repository.
- A developer can withdraw their data. Withdrawn data is deleted, and its aggregates are
  removed at the next report.

---

## 9. Completion: engineering and product validation, judged separately

**Engineering completion:**
1. Packaged relays and `--keys auto`, proven by the clean-machine end-to-end test.
2. CI green on Ubuntu and macOS, or every remaining failure root-caused.
3. Capture and report work; at least 1,000 real-server calls under the acceptance in S2; the
   denominator checks hold; privacy tests pass.
4. Crash cells 0/36 on cooperating servers under the replay rule; non-cooperating servers
   unchanged and still UNKNOWN.
5. The repair fallback in place; A1/A2 re-run with 0 safety violations.
6. The compatibility matrix documented, with 0 safety violations.
7. S6 run under the fixed protocol, with every result reported on both dimensions.
8. Final regression passes; overhead re-measured; every deferral and limitation listed.

**Product validation** (reported separately; may be incomplete without blocking engineering):
1. Outside developers who installed StateGuard and ran `stateguard trial` (target 3, minimum 1).
2. Before/after reports received, with sample sizes.
3. Onboarding friction measured with real users: time to first protected call, issues hit.
4. Real-world captured call counts, by source.
5. Real recoveries or prevented failures, **only if actually observed**.
6. Feedback and what it means for the product.

**The Phase 2 report** (S7) separates the two and covers:
- productisation and installation;
- runtime and recovery changes;
- safety results;
- the compatibility matrix;
- capture and measurement;
- statistical validation;
- performance overhead;
- regression and CI;
- deferred work and remaining limitations;
- then the product-validation items above.

It never claims production-wide sub-1% reliability from synthetic workloads or a small pilot.
A later field-validation phase is needed for any production claim.
