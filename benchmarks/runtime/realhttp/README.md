# realhttp: baseline-versus-StateGuard over Streamable HTTP (TEST-ONLY)

Controlled fault injection on named, pinned MCP servers. Nothing here is production traffic, and
no number from it is a production failure rate.

## Measurement specification

The final Phase 1 definitions, the failure taxonomy and the generated table are in
`docs/runtime/PHASE1_METRICS.md` (`benchmarks/runtime/phase1_table.py`).

**Eligible logical invocation.** One client session (`initialize`, `tools/list`) followed by
one `tools/call` that the workload issues, with at most one injected fault.
- Every row is one invocation. Resends by the client (`naive`) or by StateGuard never add a row.
- No row is excluded. A harness failure is a row with kind `driver_error`, and it counts as
  failed.
- Cancellation and unsupported operations are not part of this workload.

**Ground truth** is read outside StateGuard:
- for writes (`add_note`): how many times the note appears in the server's own notes file;
- for reads: whether the answer contains the seeded content;
- the fault proxy logs every `tools/call` that reached the server.

**Outcome of a row** (`summarize.py`):

| Term | Definition |
|---|---|
| success | read: the client was answered with the right content. Write: the client was told success **and** the note exists exactly once |
| failed | anything else: the residual failure rate counts failed (including unresolved) / eligible |
| unresolved | the client cannot tell whether it ran: told "outcome unknown" (`surfaced_unknown`), a keyed server's `-32010`/`-32011`, or no answer (`no_answer_timeout`, `no_answer_crash`). "Not sent" (`surfaced_not_sent`) is a resolved failure |
| recovered | a row that succeeds in an arm while the same (op, mode, trial) fails in `raw`. Detection is never recovery: "unknown" is a failure |
| duplicate effect | the note exists more than once |
| false success | the client was told success, and the note does not exist |
| false failure | the client was told failure (not "unknown"), and the note exists |
| StateGuard-induced | a row that fails in an arm while the same (op, mode, trial) succeeds in `raw` |
| overhead | fault-free session latency, from client process start to exit, compared with raw. Use paired, single-worker batches for a precise comparison |

Rates use Wilson 95% intervals. Fault-free, faulted (transport and session) and schema drift are
reported apart, and every cell has the same weight. The rate is a property of this matrix,
never of real traffic.

**Consistency check.** Each StateGuard arm's relay writes an `invocation` record
(`stateguard.adapters.mcp.incidence`). `summarize.py` counts records whose resolution
(answered or recovered, against anything else) disagrees with ground truth. A disagreement
would mean the relay's own incidence report is wrong.

## Reproduce

Set up the toolchain as CI does, plus the pinned third-party server:

    python3 -m venv .venv && .venv/bin/pip install -e ".[pydantic,mcp,dev]"
    (cd adapters/typescript && npm ci)
    mkdir -p /opt/sg-servers/everything && (cd /opt/sg-servers/everything && \
      npm install @modelcontextprotocol/server-everything@2026.8.31)

Write `benchmarks/evidence/realhttp/BATCH/PLAN.md`, then run:

    .venv/bin/python benchmarks/runtime/realhttp/run_batch.py BATCH HEAD -- notes 12
    TRIAL_OFFSET=1000 .venv/bin/python benchmarks/runtime/realhttp/run_batch.py BATCH HEAD \
      --part control -- notes 60 read/none,write/none

`driver.py SERVER N [cells] [arms] [workers]` runs one batch without recording, against the
working tree, for development only.
