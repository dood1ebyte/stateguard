# Phase 1 reliability metrics and failure taxonomy (final, Session 2, 2026-11-09)

This document is the definition the Phase 1 Completion Report uses. Its only implementation is
`benchmarks/runtime/phase1_table.py`. `summarize.py`'s per-kind counts are diagnostics, not the
metric.

**What a number here is.**
- Every Phase 1 rate comes from **synthetic fault injection** on named, pinned servers, with
  one fault per invocation, made to happen.
- It is a property of the fault matrix, **never a production failure rate**.
- Production rates need real traffic (see "Traffic-weighted rate" below), which Phase 1 does not
  have.

## Unit and denominator

- **Eligible logical invocation:** one client session (`initialize`, `tools/list`) followed by
  one `tools/call` that the workload issues, with at most one injected fault.
- **One row is one invocation.** A resend, by the client or by StateGuard, never adds a row.
- **No row is excluded.** A harness error is a row (`driver_error`), and it is a failure.
- **The workload's calls are valid and succeed when no fault is injected.** So in the matrix,
  any error answer is a failure of the invocation.
  - In real traffic a tool's own error is an answer, not an invocation failure (`incidence.py`,
    `answered_error`). The two settings differ on purpose.

## Outcome of a row (exactly one)

| Outcome | Definition |
|---|---|
| **success** | Read: answered with the right content. Write: told success **and** the effect exists exactly once in ground truth, which is read outside StateGuard. |
| **unresolved** | The client cannot tell whether the call ran: told "outcome unknown" (`executionState: unknown`), a keyed server's "in doubt" (`-32010`) or "still running" (`-32011`), or no answer at all (timeout, or the connection or process died). |
| **failed, resolved** | Any other failure. Told "not sent" (a resolved failure: it did not run), an error answer, wrong content, or told success wrongly. |

**Residual failure rate (RFR) = (unresolved + failed, resolved) / eligible.**
- Reported with a Wilson 95% interval and its sample size.
- **UNKNOWN is a failure.** Detection is never counted as recovery.
- A retry counts only through its row's verified outcome.

## Counted alongside the RFR, never folded into it

| Measure | Definition |
|---|---|
| **unsafe** | `duplicate_effect` (effect exists more than once), `false_success` (told success, no effect), `false_failure` (told failure, not "unknown", yet the effect exists), `wrong_content`. A StateGuard arm must show 0. Any unsafe outcome is a defect, whatever the RFR. |
| **StateGuard-induced (Z)** | Fails in an arm while the same (op, mode, trial) succeeds in `raw`. Must be 0. |
| **recovered vs raw** | Succeeds in an arm while the same (op, mode, trial) fails in `raw`. |
| **overhead** | Fault-free session latency against `raw` on the same trial, from paired single-worker batches (`paired_overhead.py`). Contended means are indicative only. |
| **record agreement** | The relay's own `invocation` record (answered or recovered, against anything else) agrees with ground truth. Any disagreement makes the incidence report wrong. |

## Reporting rules

- **Never pooled.** Rates are reported per suite (HTTP, stdio), server, arm and family:
  `fault_free`, `transport`, `session`, `crash`, `drift`. `faulted` is all injected faults of
  one server and arm together.
- Every cell (op × fault) has the same number of trials, so `faulted` weights faults equally.
  That weighting is a choice of the matrix, not of traffic.
- **A named arm, not "StateGuard".** Each arm is a configuration: `sg` (relay, retry auto),
  `sg_keyed` (the server keyed by the injector or preload), and `*_deadline`
  (`--call-deadline`). A claim names the arm and the configuration it needs.

## Failure taxonomy: what the Phase 1 matrix covers

The categories are `TASKMAP.md`'s (A–K, Z). Each injected fault maps to one of them:

| Cat | Name | Faults in the Phase 1 matrix | Covered by |
|---|---|---|---|
| A | Contract / argument schema | `drift` (a field renamed): in the HTTP driver, but **no Phase 1 batch has run it yet** | library suites only, so far |
| B | Tool discovery / staleness | — | library and relay tests only |
| C | Protocol / conformance | — | relay tests only |
| D | Session / capability | `session_loss` | HTTP matrix |
| E | Transport / connection | `kill_before` (not executed) | HTTP and stdio matrices |
| F | Timeout / transient / rate limit | `http_503_before`, stdio `transient` (`-32603`), both not executed | HTTP and stdio matrices |
| G | Tool execution failure | — (a tool's own error is an answer) | not measured |
| H | Result integrity | — | not measured |
| I | Ambiguous completion | `lost_response`, `http_502_after`, `cut_stream`, `hang_after`; stdio `lost_exit`, `hang`; server crashes `after_begin`, `after_effect`, `after_record` | HTTP and stdio matrices |
| J | Runtime state inconsistency | duplicates, as an unsafe outcome | every row |
| K | Auth / authorization | — | not measured |
| Z | StateGuard-induced | induced vs raw | every row |

`phase1_table.py --by-category` prints the RFR per category and fault. **The Phase 1 RFR
speaks only for D, E, F and I** (A once a drift batch runs). B, C, G, H and K are outside it.

## Traffic-weighted rate and the sub-1% target

The matrix gives a **conditional** residual `r(c)` for each fault class `c`: the RFR given that
the fault happened. A rate for real traffic also needs the **incidence** `p(c)`, the share of
invocations that meet `c`:

    RFR_traffic = p(none) * r(none) + sum over c of p(c) * r(c)

- `r(none)` is the fault-free RFR (0 in every Phase 1 batch, with an upper bound from its sample).
- `p(c)` can only come from relay `invocation` records on real traffic (`stateguard incidence`).
  None exist yet.
- Until they do, **no traffic-weighted RFR is claimed**, and the sub-1% target cannot be shown
  met or missed.
- Phase 1 can state what the target needs. For each class, a residual `r(c)` low enough that
  plausible incidences keep the sum under 1%. Classes where `r(c)` is high must stay rare. An
  example is the crash windows that remain in doubt on an unmodified server.

## Generate

    .venv/bin/python benchmarks/runtime/phase1_table.py \
        benchmarks/evidence/realhttp/B2 benchmarks/evidence/realstdio/S4 [--by-category] \
        [--json OUT.json]

## Changes from earlier definitions

- `surfaced_not_sent` was listed under "unresolved" in `realhttp/README.md`. It is a resolved
  failure: the client knows the call did not run.
- A keyed server's `-32010` and `-32011` answers are unresolved; `summarize.py` counted them as
  plain error answers.
- Neither change moves a number in B2 or S4: neither batch has such a row (checked
  2026-11-09).
