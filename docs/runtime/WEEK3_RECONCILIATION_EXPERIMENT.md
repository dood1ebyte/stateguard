# Week 3 — Can a read-only probe safely reconcile a lost response?

**Status:** experiment report for review. Not committed. No runtime code changed.
**Date:** 2026-10-16.
**Base:** `main` at `b6f7ba0`, which includes PR #18 (merged 2026-10-16 09:59Z).
**Evidence:** private, under `/root/stateguard-captures/week3-reconcile/run-final/`.
- `results.json`, sha256 `24304f58c991964f`
- 164 runs, from 16:05:42Z to 16:08:23Z

**Short answer.**
- **Observed:** a probe can safely establish *"the intended postcondition holds now."* It cannot establish *"the operation did not execute."*
- **Observed:** the one policy that issued `not_executed` — the Week 2 handoff policy, which trusts `exclusive: true` — was right in 8 runs. In 3 runs (an execution undone by another actor) it was wrong, and its resend caused a duplicate execution.
- **Conclusion:** the evidence does **not** justify a resend path. The next runtime package should implement *"verified satisfied, or escalate"*, with no resend.

---

## 1. Hypothesis and assumptions

**Hypothesis.** After a response is lost, a declared read-only probe, taken once, can separate four cases:

1. **satisfied** — the intended postcondition holds;
2. **not executed** — the operation definitely did not run;
3. **inconclusive** — the evidence cannot decide;
4. **unsatisfied, open** — the postcondition is absent, and the original executor may still run the operation.

**Five concepts are kept apart.** The verdict logic is in `reconcile.py`, and every assessment records all five:

| Concept | Value in this experiment |
|---|---|
| Execution state | Always `unknown`. A probe never changes it. **0 relabels in 164 runs.** |
| Result validity | `no_result`: the response was lost. |
| Postcondition | `satisfied` / `absent` / `contradictory` / `unknown`, from the probe. |
| Attribution | Always `unknown`. A probe sees the state, not who produced it. |
| Resend eligibility | Only after `not_executed`; only if declared; at most once; never while the executor is alive. |

**Assumptions** (stated, not verified, unless marked otherwise):

- **A1 — executor termination is observable.** It is: the harness reaps the shim process. The shim kills and reaps the server, and the server's pid is checked to be gone. A single-process stdio server cannot act after it is reaped. Detached child processes are assumed absent, not checked.
- **A2 — the probe reads the truth.** The probe uses the server's own `read_text_file` through a fresh server process. Ground truth is checked independently from the OS.
- **A3 — the pre-image is captured just before the call.** The harness does this with a separate read-only session.
- **A4 — exclusivity is declared, never verified.** Every operation was declared `exclusive: true, resend_if_not_executed: true`, the most permissive declaration, so the verdict depends only on the policy.

**Policies compared.**

- `declared` is the Week 2 handoff policy. It accepts `exclusive: true` as proof of non-execution, provided the executor is confirmed stopped and a fresh probe sees the pre-state.
- `strict` follows the Week 3 rules: a declaration is not evidence. `not_executed` requires *verified* exclusivity, which no probe provides, so `strict` can never reach it in this experiment. Unit tests confirm the branch exists, and that `strict` can resend in no combination.

## 2. Scenarios and fault injection

**Shared method.**

- **Testbed.** Week 2's filesystem testbed (`@modelcontextprotocol/server-filesystem` 2026.8.31, pinned). The Week 2 files are unchanged.
- **Target operations.**
  - S3 `move_file`: inbox → done.
  - S4 `edit_file`: insert the log line before `END`, a non-idempotent append.
- **Starting state.** Before every run, the sandbox is rebuilt and brought by plain file operations to the checkpoint before the target step, and asserted equal to it.
- **Faults.** `reconcile_shim.py` injects them deterministically in front of the real server. It also writes a delivery ledger (`started`, `delivered`, `answered`, `held`, `swallowed`, `server_reaped`) holding envelopes only, never content.
- **Ground truth:**
  - Did the original execute?
    - If the executor stopped: a sandbox snapshot taken immediately after its reaping, *before* any other actor, compared with the checkpoints.
    - If the executor stayed alive: the ledger's `answered` record.
  - State at probe time and final state: snapshots against the checkpoints.
  - Duplicate effects: effective executions counted from the ground truth plus the ledger.

**Configurations.** All four run the identical scenario, fault and starting checkpoint.

| Config | What it does |
|---|---|
| `raw` | Stop at the unknown outcome. |
| `naive` | Reconnect and re-send once, without probing. |
| `declared` | Probe, then resend if eligible, then verify with a second probe. |
| `strict` | Same as `declared`, under the strict policy. |

**Scenarios.** 3 repetitions each; R10 has 5.

| ID | Required case | Op | Fault / actor | Ground truth (observed) |
|---|---|---|---|---|
| R1 | 1 never reaches the server; executor stopped | move | `swallow_request` | not executed (12/12) |
| R2 | 2 executes; response lost | move | `drop_response` | executed (12/12) |
| R3 | 3 append executes; a blind retry would duplicate | append | `drop_response` | executed (12/12) |
| R4 | 4 server executes after the probe saw the pre-state | append | `delay_request` 3.0 s; client gives up at 1.0 s | executed late (12/12); pre-state at probe time |
| R5 | 5 a concurrent writer produces the desired post-state | move | `swallow_request`; actor performs the move | **not** executed (12/12); post-state present |
| R6 | 6 a concurrent writer produces a conflicting state | move | `swallow_request`; actor writes a different destination | not executed (12/12) |
| R6b | 6 variant: an execution undone by another actor (ABA) | append | `drop_response`; actor restores the pre-image | **executed** (12/12); pre-state at probe time |
| R7a | 7 stale probe evidence | move | `drop_response`; probe = the pre-loss view | executed (12/12) |
| R7b | 7 inconsistent probe | move | `swallow_request`; actor moves the file between probe reads | not executed (12/12) |
| R7c | 7 incomplete probe | move | `drop_response`; the probe server dies on its 2nd read | executed (12/12) |
| R7d | 7 probe unavailable | move | `drop_response`; the probe server cannot start | executed (12/12) |
| R8 | 8 postcondition absent; executor alive | append | `hang_request`, never delivered | not executed (12/12) |
| R9 | 9 satisfied but attribution unknown | — | R2 vs R5, compared | see §3 |
| R10 | 10 server restarts between invocation and probe | move | `kill_after_deliver` (SIGKILL), probe on a new process | not executed (20/20) |

## 3. Results against ground truth (observed)

| Scenario | raw | naive | declared | strict |
|---|---|---|---|---|
| R1 not executed, stopped | unknown | resend → ok | `not_executed` → resend → verified ✔ | `inconclusive` → escalate |
| R2 executed, lost | unknown | resend → *"Destination already exists"* ✘ false failure | `satisfied` ✔ | `satisfied` ✔ |
| R3 append executed, lost | unknown | resend → ok ✘ **duplicate** | `satisfied` ✔ | `satisfied` ✔ |
| R4 executes late | unknown | resend → ok ✘ **duplicate** (late original) | `unsatisfied_open` → escalate | `unsatisfied_open` → escalate |
| R5 other actor made the post-state | unknown | resend → fails ✘ false failure | `satisfied` (attribution unknown) | `satisfied` (attribution unknown) |
| R6 conflicting state | unknown | resend → fails | `inconclusive` | `inconclusive` |
| R6b ABA | unknown | resend → ok ✘ **duplicate** | ✘ **`not_executed` (false)** → resend ✘ **duplicate** | `inconclusive` → escalate |
| R7a stale | unknown | ✘ false failure | `inconclusive` | `inconclusive` |
| R7b inconsistent | unknown | resend → ok | `inconclusive` | `inconclusive` |
| R7c incomplete | unknown | ✘ false failure | `inconclusive` | `inconclusive` |
| R7d unavailable | unknown | ✘ false failure | `inconclusive` | `inconclusive` |
| R8 alive, never executes | unknown | resend → ok | `unsatisfied_open` → escalate | `unsatisfied_open` → escalate |
| R10 killed after delivery | unknown | resend → ok | `not_executed` → resend → verified ✔ | `inconclusive` → escalate |

Every scenario and configuration gave the same outcome on every repetition (3 each, R10 5).

**R9 (attribution).**
- The probe evidence in R2 (the original executed) and in R5 (another actor made the state; the original never ran) is **byte-identical**: the same observed paths and digests.
- Both get `satisfied` with attribution `unknown`.
- The probe therefore cannot attribute state to the original call, and the harness never claims it did.

**Verdict correctness.** These are verdicts judged against ground truth at the moment of the probe.

| | declared | strict |
|---|---|---|
| Verdicts | 9 satisfied, 11 not_executed, 6 unsatisfied_open, 15 inconclusive | 9 satisfied, 0 not_executed, 6 unsatisfied_open, 26 inconclusive |
| Correct | 38 / 41 | 41 / 41 |
| Wrong verdicts | 3 false `not_executed` (R6b) | none |

`inconclusive` makes no claim, so it is never counted as false.

**Determinism.** For every scenario and policy, all repetitions produced the same verdict, reasons and evidence fingerprint: 0 non-deterministic groups.

**Cost.** Medians across reconciliation runs:

- Each probe takes about 0.23 s, and that includes starting a server process.
- The pre-image costs the same again.
- Extra MCP calls per reconciliation:
  - move: 7 (pre-image initialize + 2 reads; probe initialize + 3 reads);
  - append: 5.
- Extra server processes: 2 per reconciliation, or 4 when `declared` resends and verifies.

## 4. False-safety claims and duplicate effects

Each configuration had 41 runs.

| Metric | raw | naive | declared | strict |
|---|---|---|---|---|
| False `satisfied` | — | — | 0 | **0** |
| False `not_executed` | — | — | **3** | **0** |
| Execution state relabelled "executed" | 0 | 0 | 0 | **0** |
| Resends attempted | 0 | 41 | 11 | **0** |
| — eligible under the contract | — | 0 | 11 (3 caused duplicates) | — |
| — sent when the original had executed | — | 21 | 3 | 0 |
| Duplicate side effects | 0 | **9** | **3** | **0** |
| False completion claims | 0 | 9 | 3 | **0** |
| False failure claims | 0 | 15 | 0 | 0 |
| Completed correctly | 0 | 14 | 17 | 9 |
| Unresolved (escalated or unknown) | 41 | 0 | 21 | 32 |
| Sentinel outside the sandbox changed | 0 | 0 | 0 | 0 |

**Observed:** the safety target was met only by `strict`: zero false claims, zero unauthorized resends, zero duplicates. `declared` met it everywhere except the ABA scenario, where its single permitted resend executed the operation a second time.

## 5. Limitations and untested cases

- **One probe per loss.** R4 resolves itself later (the late execution lands), but a single probe cannot see that. Repeated probing was not tested.
- **Fencing a live executor was not tested as a policy.** Killing and reaping it on a deadline would turn R4/R8 into a stopped-executor case. R10 suggests what happens: SIGKILL immediately after delivery left the operation **unexecuted in 20/20 runs**, so the branch where the kill lands *after* execution was **not observed**.
- **No live run had a satisfied postcondition with the executor still alive.** That rule is covered by unit tests only.
- **Actor timing differs by configuration.** In R7b the actor runs *during* the probe, so it does not happen in `raw`/`naive`; naive's success there reflects that asymmetry.
- **Narrow scope.** One server, two operations on one or two paths, one host. Probes go through the same server software they are checking (A2). There were no partial writes, no multi-file transactions and no symlinks.
- **Not tested at all:** detached child processes and other server implementations.
- **The probe ran outside the relay**, through a separate server process. Pre-image capture inside the relay would add a round trip on the same connection instead; that latency was not measured.
- **No claim** is made about other MCP servers, production prevalence or demand.

## 6. Is a safe resend path justified?

**No, not on probe evidence.**

- **Observed.** In R1 and R10 (not executed) and in R6b (executed, then undone by another actor), the probe sees the same thing: the pre-state, with the executor confirmed stopped.
  - Trusting a declaration of exclusivity resends correctly in the first two and duplicates the execution in the third.
- **Conclusion.** Nothing available to a stdio MCP relay can verify exclusivity. The resend path therefore rests entirely on an unverified operator assertion, and the rules forbid that.
- **Hypothesis, untested.** Non-execution could be established only by evidence from outside the probe, for example:
  - a server-side idempotency key or execution journal;
  - a fencing token or lease on the resource.
  Both need server or deployment cooperation that this testbed does not have.

**What is justified (observed):**
- Reporting the intended outcome as verified, with attribution and execution left unknown, when a fresh, consistent, complete probe matches the post-state captured from the pre-image.
- In this testbed, that resolved 9/41 runs (R2, R3, R5) with zero false claims.
- It removed every duplicate and every false failure that the naive retry produced in those runs.

## 7. Proposed runtime contract and acceptance criteria (next package)

**Contract: "verified satisfied, or escalate".**

1. **Ledger.**
   - Execution state stays UNKNOWN for ever after a loss.
   - Add separate fields for result validity, postcondition (`satisfied` / `absent` / `contradictory` / `unknown`) and attribution (always `unknown` from a probe).
   - Fix the latent issue: `answered_locally` returns whether it applied, and "not sent" is answered only if it did.
2. **Declaration.** The operator declares, per tool: the probe reads, the pre-image reads and the post-state predicate, built from the pre-image. `exclusive` and `resend_if_not_executed` are **not** part of the contract.
3. **Evidence rules:**
   - Executor liveness comes from process reaping.
   - The probe must run on a new server generation and must be taken after the loss.
   - Repeated reads must agree.
   - Any missing, error or unavailable read makes the verdict `inconclusive`.
4. **Verdicts and actions:**

   | Verdict | Action |
   |---|---|
   | `satisfied` | Report "intended outcome verified; execution and attribution unknown". If the executor may be alive, add `late_execution_possible`. |
   | `unsatisfied_open` / `inconclusive` | Escalate "unknown". |
   | `not_executed` | Not reachable. |

   **No resend in any case.**
5. **Mode.** Opt-in per tool. Shadow mode logs proposals only and sends no probes.

**Acceptance criteria for the next package.** Run through the relay against this testbed, with identical seeds:

- R1–R10 reproduce the `strict` verdicts:
  - 0 false `satisfied`, 0 false `not_executed`;
  - 0 resends, 0 duplicate effects, 0 execution-state relabels;
  - deterministic across 3 repetitions.
- A regression test covers the ledger fix: a SENT request reaching the not-sent path gets "unknown".
- Shadow fidelity is re-verified: byte-identical, and no probe traffic in shadow mode.
- Probe overhead is reported per tool: calls and latency.
- The existing suite stays green, and `decide_retry` is unchanged.
- Untested cases (§5) are stated in the PR, never implied as covered.

**Status-board changes I propose.** The roadmap is not in the repository, so none was edited.

| Week 3 item | Proposed status |
|---|---|
| Test execution-unknown / no-replay behaviour | done for the harness; relay pending |
| Independently verify postconditions | done for the harness |
| Compare against baseline | done (raw / naive / declared / strict) |
| Demonstrate safe bounded recovery | **partial**: the verified-satisfied path only; resend not justified |
