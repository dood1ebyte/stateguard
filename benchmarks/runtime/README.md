# Runtime failure measurement

```bash
python benchmarks/runtime/measure.py                      # full run, 8 invocations per fault
python benchmarks/runtime/measure.py --per-fault 1        # quick
python benchmarks/runtime/measure.py --json report.json   # every invocation, every arm
```

This measures how many MCP invocations end correctly, before and after
StateGuard, per failure category (A–K and Z; see
[`docs/runtime/TASKMAP.md`](../../docs/runtime/TASKMAP.md)).

Each scenario is one `tools/call` with its ground truth decided up front. It
runs in a fresh in-process rig: client ↔ stdio relay ↔ scripted server, over
OS pipes. The scenario runs once in each of four arms:

| Arm | What it is |
|---|---|
| `raw` | The relay with StateGuard off (`--passthrough`). This is the baseline. |
| `naive` | Raw, plus a client that retries once on an error or timeout. It shows what blind retry does, and is not a recovery strategy. |
| `shadow` | StateGuard observing. It must match raw exactly. |
| `auto` | StateGuard acting. |

The scripted server keeps a ledger of what it **executed**. Duplicate
executions and phantom successes are counted where they happen, not
inferred from what the client saw.

## Files

- **`harness.py`** — the rig, the scripted server and its fault actions.
  - Faults before execution: hang, transient error, auth error, pipe close.
  - Faults after execution: tool error, lost response, truncated response,
    duplicate response, pipe close.
- **`scenarios.py`** — seeded scenarios per category. The corpus is
  pluggable: anything with the drift corpus's shape (`tools` and labelled
  `calls`) can replace the synthetic one, including recorded real traffic.
- **`measure.py`** — runs the arms, judges each invocation against its ground
  truth, and prints the per-category table plus latency.

## Reading the numbers

**The default corpus is synthetic.** It is 14 tool definitions modelled on
real servers, plus 37 hand-labelled calls and generated faults. The numbers
describe that corpus; they are not production failure rates. The report
prints its provenance, its size, the injected-fault distribution and its
limitations every time.

## Session recovery

```bash
python benchmarks/runtime/recovery.py                 # 5 sessions per kind
python benchmarks/runtime/recovery.py --per-type 1    # quick
```

`measure.py` follows the one call that meets a fault. A server death also
decides the fate of every call *after* it, so `recovery.py` runs multi-call
sessions instead. It has nine session kinds:

- control (no fault);
- idle death;
- death after dispatch;
- death before execute;
- a request racing the restart;
- restart that always fails;
- restart that dies on start;
- a listing that changed after the restart;
- two requests in flight.

Each kind runs in five arms: raw, shadow, auto with restart off, restart
idle-only, and restart after-unknown.

Every invocation gets exactly one outcome. Those outcomes are succeeded,
recovered (ran once on a restarted server), unknown, not sent, connection
closed, silent, and other error.

These are counted separately and never folded into those outcomes:

- replays;
- duplicate executions;
- StateGuard-induced failures (category Z);
- server deaths;
- reconnects attempted, succeeded and failed.

A fall in failures therefore shows whether it was recovered, contained
(unknown or not sent), made visible (connection closed), or merely hidden.

## Record and replay

```bash
python benchmarks/runtime/record_corpus.py            # re-record tapes/ (A-H)
python benchmarks/runtime/replay.py                   # replay every tape under every arm
python benchmarks/runtime/replay.py --tapes DIR --json out.json
```

The relay can record what crosses it (`stdio_proxy.py --record FILE`; library:
`stateguard.adapters.mcp.recording`). A recording is versioned JSON lines.
It holds every line in all four directions, the relay's decisions and audit
events, and the server generation. Fault-injection recordings also hold the
injected faults and per-request ground-truth labels. Everything is
**redacted before it is written**:

- credentials are removed by key name and by value shape;
- free-form text is masked shape-preservingly;
- only protocol structure, and argument values the tool's own schema vouches
  for, are kept verbatim.

`replay.py` drives the recorded client lines through the relay. The server
side is a `TapeServer`, an in-process scripted server that answers each call
exactly as the recording says the real one did. Arms:

| Arm | What it is |
|---|---|
| `raw` | StateGuard off. |
| `shadow` | Observing; must equal raw. |
| `auto` | Acting, default settings. |
| `auto_ops` | Acting, with a 0.3 s deadline, declared effects, retry on, and restart (idle-only). |
| `as_recorded` | The configuration the tape was recorded with: the **fidelity** check. Replay must reproduce every recorded answer and decision. |

Reported separately, never folded into one "failures solved" number:

- raw failures, plus failures prevented, recovered and safely escalated;
- UNKNOWN outcomes, duplicate executions and StateGuard-induced failures (Z);
- reconnects and retries, latency, and recording overhead;
- **counterfactual answers**: what the TapeServer predicted for traffic the
  recording never saw, such as repaired arguments or a retry. Every
  "prevented" or "recovered" figure says how many of its cases rest on one.

**`tapes/` is fault injection on a synthetic workload** (`record_corpus.py`,
sessions A–H). The report says so on its first line. Record/replay
infrastructure enables real-traffic measurement but does not itself
constitute real-traffic evidence. To measure real traffic, record with
`--record-provenance real_capture` against a real deployment and point
`--tapes` at those files. Replay never contacts the original server.
