# Domain 2 testbed: `@modelcontextprotocol/server-filesystem`

This is the Week 2 second MCP ecosystem and multi-step workflow testbed. Results and
rationale are in `docs/runtime/WEEK2_DOMAIN2_FILESYSTEM.md`.

## Pinned environment

| Component | Version | Pinned by |
|---|---|---|
| `@modelcontextprotocol/server-filesystem` | 2026.8.31 (serverInfo `secure-filesystem-server` 0.2.0) | `env/package.json` (exact) and `env/package-lock.json` (104 packages, integrity hashes) |
| `@modelcontextprotocol/sdk` (TypeScript) | 1.32.1 | lockfile |
| `zod` | 4.6.7 | lockfile |
| Node / npm | 22.22.0 / 10.9.4 | `engines` |
| Python client and relay | the repository `.venv` (Python 3.11), no MCP SDK in the path | — |

## Install

Install outside the repository, without running package scripts:

```sh
mkdir -p "$SERVER_DIR" && cp env/package.json env/package-lock.json "$SERVER_DIR"/
(cd "$SERVER_DIR" && npm ci --ignore-scripts)
SERVER="$SERVER_DIR/node_modules/.bin/mcp-server-filesystem"
```

## Isolation

- `WORKSPACE` must be a private scratch directory outside the repository.
- Everything mutable lives in `WORKSPACE/sandbox`. It is passed to the server as its
  only allowed directory and rebuilt by `fixture.reset` before every run.
- The fixture content is synthetic.
- `WORKSPACE/outside-sentinel.txt` sits next to the sandbox. Every snapshot hashes it,
  so an escape from the sandbox would show up.
- Child processes get only `PATH` and `HOME=WORKSPACE`. The server needs no credentials
  and its code makes no network calls.
- Do not send client roots (`roots/list`). A client that declares roots replaces the
  command-line sandbox (probe L10).

## Run

```sh
python probe.py    "$SERVER" "$WS/probe"    "$WS/probe-results.json"   # offline characterization (39 probes)
python workload.py "$SERVER" "$WS/workload" "$WS/workload/results.json" # 20 tests x configs = 59 runs
# Shadow capture: the same live tests, three passes in ONE workspace (byte-comparable)
python workload.py "$SERVER" "$WS/cap" "$WS/cap/results-raw.json"    --configs raw    --live-only
python workload.py "$SERVER" "$WS/cap" "$WS/cap/results-shadow.json" --configs shadow --live-only --record real_capture
python workload.py "$SERVER" "$WS/cap" "$WS/cap/results-tee.json"    --configs shadow --live-only --tee
python fidelity.py "$WS/cap" "$WS/cap/fidelity.json"
```

To stop: every session ends when the client closes stdin, and the server exits with
status 0 (probe L8). The shim and the relay exit with it.

To reset: `fixture.reset(sandbox)` removes the sandbox and rebuilds it. It never
touches anything outside the sandbox.

## Files

| File | Role |
|---|---|
| `fixture.py` | Sandbox build, reset and snapshot. Oracle checkpoints 0–4 are computed from the fixture text with plain string operations, never from the server. |
| `rawclient.py` | Newline-delimited JSON-RPC client with exact wire control. It keeps per-line sha256 digests. |
| `workflow.py` | The four-step approval workflow, its pre- and postconditions, and the step and workflow state taxonomy. |
| `shim.py` | Simulated faults between the client (or relay) and the real server. It also has a tee mode that writes digests only. |
| `workload.py` | The tests, with their expected outcomes written before execution, and the configurations `raw`, `shadow`, `enforce` and `restart`. |
| `fidelity.py` | Byte-fidelity check (client view with and without the relay, and both directions through it) and privacy scan. |
| `probe.py` | Offline characterization: lifecycle, contract and effect/replay probes. |

Unit tests that need no Node are in `tests/test_domain2_filesystem_testbed.py`.
