# StateGuard — four-week roadmap final closure audit

**Date:** 2026-10-18. **State:** working tree, uncommitted and unpushed. **HEAD:** `b6f7ba0f` (`Merge pull request #18 … serene-bohr`).
**Nature:** audit and decision only. No production code changed, no feature added, no experiment re-run, nothing committed/pushed/stashed/reset.
**Method:** every status below is checked against repository files, test output, git state, and evidence under `/root/stateguard-captures/`. Prior assistant summaries were not trusted where the underlying evidence could be read. Controlled technical evidence and external real-world evidence are kept separate; neither is treated as proof of market demand.

---

## Audit A — original exit criteria

Statuses: **COMPLETE** (evidence establishes the criterion), **PARTIAL** (established only on a controlled/limited basis), **MISSING** (no evidence), **UNVERIFIED** (claimed but not independently checkable here).

| # | Exit criterion | Status | Evidence (location → result) | What it establishes | Limitations / unresolved | Smallest action if incomplete |
|---|---|---|---|---|---|---|
| 1 | Git/MCP investigation closure; verified merge/CI | **COMPLETE** (controlled) | `WEEK2_DOMAIN2_FILESYSTEM.md §1`: `8f94504` contained in `main` (merge `467396b`); CI run #44 on `467396b` = `success`, re-checked via GitHub API that day; 4 private Git recordings intact | Week 1 Git/MCP work merged and green at that commit | CI was **not** re-queried live in this audit (GitHub tooling intermittent); HEAD has since advanced to `b6f7ba0` (a later merge), on top of which all Week 3–4 work is uncommitted | (optional) re-query CI for `b6f7ba0` when GitHub tooling is available |
| 2 | Invocation lifecycle; explicit no-replay / recovery-eligibility rules | **COMPLETE** | `RECONCILIATION_CONTRACT_V2.md §4.4`; `src/stateguard/adapters/mcp/reconcile.py` (`TerminationEvidence`: only `scope_ended` permits a probe); `WEEK3_REAL_RELAY_VERIFICATION.md §1`: 243 trials, **0 resends, 0 duplicate deliveries, 0 execution/attribution claims** | A lost call is never replayed on unknown outcome; a probe runs only after confirmed termination; otherwise `undetermined`/`treatAsSuccess:false` | Guarantees hold within a StateGuard-controlled scope only; external side effects are out of boundary (by design) | — |
| 3 | Second MCP ecosystem characterized; bounded multi-step workflow | **COMPLETE** (controlled) | `WEEK2_DOMAIN2_FILESYSTEM.md §1`: filesystem server 2026.8.31 pinned (lockfile, 104 pkgs); **39 offline probes**; 4-step workflow inspect→approve→file→log with 5 disk-oracle checkpoints | A second, independently-pinned ecosystem was characterized and driven through a bounded workflow with on-disk ground truth | Two servers only; `§489`: "two servers do not license claims about MCP servers in general" | — |
| 4 | Independent verification of ≥1 recovery outcome | **COMPLETE** (controlled) | `WEEK3_REAL_RELAY_VERIFICATION.md §1`: every `verified` matched the disk predicate; defects A/B/C found with reproducible xfail cases, then fixed and re-verified (`§13`, `WEEK3_AUDIT_FIXES.md`) | At least one recovery outcome independently verified against disk ground truth; the audit also caught and fixed real safety defects | Controlled testbed, not real traffic; "no prevalence or customer-value claim follows" (`§vi`) | — |
| 5 | Fair comparison vs raw execution and conventional retry | **COMPLETE** (controlled) | `WEEK2 §305` D1 (raw + naive-retry baseline told "success twice"/duplicate log line; StateGuard does not link to the unknown), `§651` (raw vs naive-retry vs reconciliation); `TASKMAP.md` T4: arms **raw / naive-retry / shadow / auto** × fault injectors, per-category incl. Z | On a controlled workload StateGuard avoids the duplicate-execution a naive retry causes, measured against both baselines | **Synthetic / fault-injection corpus; no real-traffic recording** (`TASKMAP §82`); small-N | real-traffic recording would strengthen it, but that needs real deployment (see #7) |
| 6 | Cross-domain taxonomy; capability/boundary matrix | **COMPLETE** | `TASKMAP.md` failure taxonomy A–K,Z with per-category own/observe/escalate verdicts; `WEEK2_DOMAIN2_FILESYSTEM.md` cross-domain section | A failure taxonomy and an owner-boundary matrix exist and the code keys on them | Grounded in 2 domains; taxonomy authored in-repo (no external reference taxonomy) | — |
| 7 | Public failure evidence collection; specific developer/maintainer outreach | **MISSING** | None. A repo-wide search for outreach / public-failure / maintainer-contact / market evidence found only dependency metadata and explicit disclaimers ("no prevalence or customer-value claim", `WEEK3 §vi`; `WEEK2` status line) | Nothing — no attributable public failure corpus and no outreach exist | External, real-world criterion; cannot be satisfied by engineering and must not be invented | **Bounded task (below):** collect ≥N attributable public MCP-runtime-failure examples (source/date/mechanism/impact/workaround/relevance) and draft a specific maintainer/developer outreach list + messages for the user to send. Responses cannot be fabricated. |
| 8 | Simplest integration path; setup friction, deps, config, latency overhead, failure modes | **PARTIAL** | Path/config: `WEEK4D_DEPLOYMENT_GUIDE.md` (container/systemd, caps, seccomp); latency: `WEEK2 §341` median per step — raw ≈19 ms / shadow ≈37 ms / enforce ≈34 ms total, "indicative only, 3 runs"; failure modes + packaging: `WEEK4E §3.2` (install in site-packages), 4D fail-closed modes | Integration path, the per-intervention overhead (~1 extra `tools/list` round trip), and failure modes are documented and measured on the tested env | Deployment examples are **[unverified]** except the one tested environment; latency is small-N indicative; no non-Docker runtime verified | measure latency at higher N and verify one deployment recipe end-to-end (deferrable; not blocking closure) |
| 9 | Current-vs-future architecture boundaries | **COMPLETE** | `WEEK4F_EXECUTOR_SCOPE_ARCHITECTURE_DECISION.md` (A/B/C against one contract); `WEEK4G`/`WEEK4H` (B prototype + hardening gate); "not a sandbox / not production-ready" stated throughout | The current privileged design, the unprivileged prototype, and the external-supervisor option are bounded, with explicit what-is-not-built | B remains experimental; deployment prerequisites open (Audit C) | — |
| 10 | Final evidence-based decision gate | **COMPLETE** (per line) | `WEEK4H_REGRESSION_AND_HARDENING_GATE.md §Phase 5`: executor-scope decision = option 2 (B needs specific hardening; green suite ≠ readiness) | The executor-scope line has an evidence-based gate; **this document is the overall four-week gate** | The overall gate must weigh the MISSING external criterion (#7) | — |

---

## Audit B — Week 4H and working-tree integrity

**Week 4H deliverables, independently checked (not taken from the handoff):**
- `uniqueItems` disposition: the Week 4G failure is a **reference-validator unsoundness** (`jsonschema._utils.uniq` sorts then compares only adjacent items; misses non-adjacent duplicates in bool/number-mixed arrays). StateGuard is correct. Reproduced directly and on committed baseline `b6f7ba0` in an isolated worktree (now removed). Evidence: `/root/stateguard-captures/week4h/phase2/uniqueitems_investigation.txt`. **Verified independently this audit.**
- Fix is test-only: `tests/adapters/jsonschema/test_2020_12_conformance.py` now asserts against a sound oracle with the counterexample pinned (`@example`) plus a regression test; `tests/adapters/mcp/test_executor_scope_unprivileged.py` adds two UID/GID mapping fail-closed tests. **No production code changed** (confirmed: `executor_scope.py`, `stdio_proxy.py`, `core/validator.py` byte-identical to the Week 4G post-state per `/root/stateguard-captures/week4h/baseline/changed_files_sha256.txt`).
- Full suite: `/root/stateguard-captures/week4h/gates/full_suite.txt` → **6075 passed, 0 failed**. Edge-case gate → 131 passed. Manifests self-verify; weeks 1–3 + 4A–4H re-verified in the prior package.

**Git status (unaltered):** HEAD `b6f7ba0`; working tree = **49 untracked + 5 modified**. Distinguishing 4H from inherited:
- **Week 4H's own changes:** `M tests/adapters/jsonschema/test_2020_12_conformance.py`; `?? tests/adapters/mcp/test_executor_scope_unprivileged.py` (new, never committed — created 4G, edited 4H); `?? docs/runtime/WEEK4H_REGRESSION_AND_HARDENING_GATE.md`.
- **Inherited Week 4A–4G (uncommitted):** the other `M` (`examples/mcp/stdio_proxy.py`, `src/stateguard/adapters/mcp/__init__.py`, `ledger.py`, `reconcile.py`) and `??` (the whole Week 4 doc set, `executor_scope.py`, prior test files, benchmarks).

**Discrepancies between handoff and repository state:** none found. The handoff's "6075 passed / 0 failed", "no production code changed", and "two test files changed" all match the repository and the saved gate output.

---

## Audit C — external evidence and integration friction

**External evidence (criterion #7) — what has actually been done:** nothing. There is no attributable public-failure corpus (no documented source/date/mechanism/impact/workaround/relevance for any real-world MCP failure) and no record of any developer or maintainer outreach. Every report explicitly disclaims prevalence/demand. **Observed fact:** the requirement is unsatisfied. **Not inferred, not invented.**
Minimum bounded task that would satisfy the *collection* half: gather ≥5 attributable public examples of MCP runtime failures (from public issue trackers / forums) and document each field; and prepare a specific, named outreach list with drafted messages. The *response* half (demand signal) depends on real people and time and cannot be produced or simulated here.

**Integration friction — measured vs unknown:**
- **Measured (controlled):** simplest path = run the stdio relay as a wrapper around the server command (`examples/mcp/stdio_proxy.py`), Shadow by default; per-step latency overhead small and dominated by a first-call/`tools/list` round trip (`WEEK2 §341`: raw ≈19 / shadow ≈37 / enforce ≈34 ms total, 3 runs, indicative); packaging requirement = install in site-packages (`WEEK4E §3.2`); fail-closed failure modes documented (4D).
- **Unknown / unverified:** any deployment recipe beyond the single tested environment (4D §7 examples are all [unverified]); latency at realistic scale and payload size; behaviour under a real client/agent; non-Docker runtimes. **A passing 6075-test suite is not production readiness** and is not claimed as such.

---

## Audit D — final decision

1. **Is the four-week roadmap complete against its original criteria?** **No — not fully.** 8 of 10 criteria are COMPLETE (controlled) or COMPLETE; #8 is PARTIAL; **#7 (public failure evidence + outreach) is MISSING.**
2. **Exact criteria incomplete:** #7 MISSING (external evidence/outreach); #8 PARTIAL (deployment recipes unverified beyond one env; latency small-N). #5 and #4 are complete but only on a controlled/synthetic basis (no real traffic).
3. **Which gaps block roadmap closure vs only a deployment experiment?**
   - **Block roadmap closure (its own stated terms):** #7 external evidence/outreach — and it is **not an engineering gap**.
   - **Block only a controlled deployment experiment:** the Week 4H executor-scope prerequisites (UID/GID remap decision; narrow seccomp proven on a non-Docker runtime; a direct "userns disabled" fail-closed test; B behind a non-root relay end-to-end) and #8's unverified deployment recipes. None of these block *closing the engineering roadmap*.
4. **Is further executor-scope hardening necessary now?** **No — defer.** It is sound, fail-closed, and bounded as an experimental prototype (Audit A#9, Week 4H). More hardening only pays off once a deployment experiment or a demand signal justifies it.
5. **Single highest-value next experiment:** the **external one** — a bounded public-failure-evidence collection plus a real, specific maintainer/developer outreach to test whether the problem StateGuard solves is one people will adopt a relay for. Every controlled technical question that can be answered without users has been answered; the unanswered question is demand, and it is not an engineering question.
6. **What StateGuard should explicitly NOT build next:** more executor-scope hardening; a third MCP domain; new repair/keyword features; resource-limit/cgroup machinery; any production-sandbox claim. None of these is blocked by missing capability — they are blocked by missing demand evidence.
7. **Credible vs unsupported claims:**
   - **Credible (from controlled evidence):** StateGuard detects and conservatively repairs contract/schema failures; reconciles lost calls with no replay and no false execution/attribution (243 trials, 0 resends); avoids the duplicate execution a naive retry causes; works across two pinned MCP ecosystems; adds small per-step latency on the tested env; the executor scope confines a server generation and fails closed; an unprivileged variant removes host `CAP_SYS_ADMIN` at the cost of a uid remap.
   - **Unsupported:** any statement about real-world failure prevalence, market demand, production readiness, general MCP-server behaviour, or multi-runtime deployment. No evidence exists for these.

**Outcome: CLOSE AFTER A SHORT, BOUNDED COMPLETION TASK.**
The engineering roadmap is complete and no engineering exit criterion is blocked (so not "continue engineering"); but one stated exit criterion (#7) is genuinely unmet, so it is not a clean "close now". The completion task is **non-engineering**: collect the attributable public-failure corpus and prepare the specific outreach, then let the demand signal — not more code — decide what comes after. If the user judges #7 out of scope for this repo (it is a go-to-market activity, not a code deliverable), the plan may instead be **CLOSED** as engineering-complete with #7 recorded as deliberately deferred. Either way: **do not open another engineering package now.**

---

## Handoff

**Decision:** CLOSE AFTER A SHORT, BOUNDED COMPLETION TASK — the engineering roadmap is complete and sound; the only genuinely unmet exit criterion (#7, public failure evidence + outreach) is external and must not be fabricated.

**Outstanding tasks, priority order:**
1. **(Non-engineering, highest value)** Collect ≥5 attributable public MCP-runtime-failure examples (source, date, mechanism, impact, workaround, StateGuard relevance) and draft a specific maintainer/developer outreach list + messages. This closes the collection half of #7; responses come from real people.
2. **(Deferred until demand)** The Week 4H executor-scope deployment prerequisites, only if a controlled deployment experiment is greenlit: UID/GID remap decision; narrow seccomp on a non-Docker runtime; direct "userns-disabled" fail-closed test; B behind a non-root relay end-to-end.
3. **(Optional)** Verify one deployment recipe end-to-end and measure latency at realistic scale (closes #8's unverified half).

**Next action for you:** decide whether #7 is in scope for this engineering repo. If yes, I can do the bounded public-failure-evidence collection (research + documentation) in a single focused pass and draft the outreach — without inventing any responses. If you consider #7 go-to-market and out of scope, say so and the four-week plan is CLOSED as engineering-complete with #7 recorded as deferred. Nothing further should be built until a demand signal exists.
