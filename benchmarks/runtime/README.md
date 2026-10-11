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
