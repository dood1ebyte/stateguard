# StateGuard developer demonstration — lost-response on a mutating MCP call

**Date:** 2026-10-19. **Audience:** MCP server/client developers who have hit (or worry about) a mutating tool call whose response is lost. **Runtime:** ~3 minutes. **Status:** this walkthrough replays an existing, recorded experiment; it makes **no** claims beyond what that experiment showed.

> **What this demo is, and is not.** It reproduces **one** failure class (a mutating `tools/call` whose response is lost after the effect) against **one** pinned third-party server (`@modelcontextprotocol/server-filesystem` 2026.8.31) under an **injected** transport fault, in a **deterministic test harness**. It is **not** evidence of general MCP reliability, production readiness, broad coverage, or that this failure is common in the wild. Trials are repetitions of one mock, not independent deployments. See "Honest boundaries" at the end — present them alongside the result, not after it.

## The problem in one sentence
When a mutating MCP tool call executes on the server but its response never reaches the client (timeout / dropped connection), the client cannot tell whether it happened — and the two obvious reactions are both wrong: **blind retry** can duplicate the effect or report a false failure, while **giving up** leaves the outcome unknown.

This is documented in public, independently, by third parties (not us):
- [modelcontextprotocol#3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394) — a retry re-runs a mutating call, "2 external effects for 1 logical operation" (reproduced by two other people).
- [microsoft/agent-framework#9204](https://github.com/microsoft/agent-framework/issues/9204) — `MCPTool` reissues the call after a connection loss; "the customer may be charged twice."

## What the demo shows (all from recorded results)
Source artifacts (reproducible): harness `/root/stateguard-captures/public-failure-evidence/thirdparty/tp_driver.py`, raw results `…/thirdparty/results.json`, lifecycle trace `…/thirdparty/lifecycle_trace_fs_armD.txt`; the mutating op is `move_file a.txt → b.txt` on the real filesystem server; **ground truth is the actual files on disk**, checked outside StateGuard.

### Step 1 — the mutating operation executes, then its response is lost
A test-only transport shim lets the real server complete `move_file` (so `b.txt` really exists on disk), then **drops that response and closes the connection**. This is an *injected* fault that reproduces the #3394 sequence — labeled as injected, not a bug we found in the server.

### Step 2 — ordinary retry produces a misleading failure (the status quo)
Reconnect and resend the same `move_file` (what a naive client/framework does). The source is already gone, so the server returns an error ("destination already exists"). **The client is told the move failed — but it succeeded.**
Recorded, 10/10 trials (`results.json` → `S1-B`): `client_false_failure: 10`, `unsafe_resend: 10`, moved-once-on-disk `10/10`.

### Step 3 — StateGuard does not resend the uncertain mutation
Through the real StateGuard stdio relay (`--executor-scope pid-namespace --restart 2 --restart-policy after-unknown --effects …`), the lost response is classified as an unknown outcome and the mutating call is **never resent**.
Recorded, 10/10 trials (`S1-D`): `unsafe_resend: 0`, `duplicate_effect: 0`, mutating calls that reached a server `= 1`.

### Step 4 — the executor is really terminated and restarted (not assumed)
From the audit trace (`lifecycle_trace_fs_armD.txt`):
```
server_lost
executor_termination  kind=scope_ended  applies=True  executor=pidns-…
server_restarted generation=1
reconcile_probe tool=read_text_file status=satisfied   ← READ-ONLY, not a resend
reconcile_verdict postcondition=observed_satisfied
outcome_unknown
```
Recorded: `executor_termination_confirmed: 10/10`, `server_restarted: 10/10`, `probe_ran: 10/10`.

### Step 5 — a read-only probe confirms the observable postcondition
StateGuard runs the operator-declared read-only probe `read_text_file(path=$destination)`. It observes that `b.txt` exists — correctly, matching disk ground truth on `10/10` trials (`reconcile_correct: 10`).

### Step 6 — StateGuard does NOT overclaim
The client receives an explicit error, not a fake success:
> *"Outcome unknown … the declared predicate held when a read-only probe read it. That does not show this request executed or caused it, that it still holds, or that the operation succeeded. Do not resend it."*
with `treatAsSuccess: false`, and `executionState`, `attribution`, `operationOutcome` = **unknown**, `durability` = **not_established**. Recorded: `keeps_concepts_separate: 10/10`, `false_exactly_once_success: 0`, StateGuard-induced failures `0`.

## Side-by-side (recorded, 10 trials each; `results.json`)
| | no retry | ordinary retry | StateGuard relay (scope+reconcile) |
|---|---|---|---|
| duplicate effect | 0 | 0 | 0 |
| unsafe resend | 0 | **10** | **0** |
| false-failure claim | 0 | **10** | 0 |
| outcome made explicit (UNKNOWN) | 10 | 0 | 10 |
| postcondition reconciled correctly | — | — | **10** |
| false success / attribution / durability claim | 0 | 0 | 0 |
| latency p50 | 0.29 s | 0.69 s | 1.22 s |

## How to run it yourself (≈3 min)
```
# pinned third-party server already installed; Node 22; StateGuard repo venv
cd /home/user/stateguard
.venv/bin/python /root/stateguard-captures/public-failure-evidence/thirdparty/tp_driver.py 10 5
# prints per-arm aggregates + per-trial rows; ground truth is the filesystem state
```
The `#3394` author's own in-memory reproduction can also be run verbatim (`/root/stateguard-captures/public-failure-evidence/experiment/` — `b1_lost_response_repro.py`, baseline prints "2 external effects").

## Honest boundaries (say these out loud)
- **Recovery needs a read-only predicate.** StateGuard resolves the ambiguity only when the operator declares a read-only probe for the mutating tool. With no such predicate it degrades to a **safe UNKNOWN** (never a resend) — safe, but not resolved.
- **Injected fault, one server, one failure class, deterministic mock trials.** This is not prevalence, not breadth, not production proof.
- **Privileged scope, as root, in this environment.** A non-root relay in a real container end-to-end is not yet validated.
- **Concurrency not fully exercised.** A second call genuinely in-flight at the exact instant of connection loss was not deterministically forced (the concurrent read resolved first in every trial).
- **StateGuard never claims the call executed, caused the state, or is durable** — only that a read-only probe saw the declared predicate, and that the call must not be blindly resent.
