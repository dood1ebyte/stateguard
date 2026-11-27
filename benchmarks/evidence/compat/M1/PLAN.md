# M1: the compatibility matrix (Phase 2 S4.2; written before any recorded row)

**What.** Ten public MCP servers, none needing credentials. Each one is run from a pinned install
through StateGuard's relay. This follows the cells of PHASE2_PLAN §7. The harness is
`benchmarks/runtime/compat/matrix.py`; the scorer is `compat/summary.py`.

**Before this plan,** single trials were run outside the record to check the harness:
- a handshake and one read on each server;
- a few fault and `--keys auto` cells on sqlite, sequential-thinking, the FastMCP example and
  the TypeScript SDK example.
No row from those checks is in this batch.

## Servers and pinned installs

| # | Server | Install (pinned) | Transport |
|---|---|---|---|
| 1 | filesystem | @modelcontextprotocol/server-filesystem 2026.8.31 (TS SDK 1.32.2) | stdio |
| 2 | memory | @modelcontextprotocol/server-memory 2026.8.31 | stdio |
| 3 | everything | @modelcontextprotocol/server-everything 2026.8.31, `stdio` | stdio |
| 4 | sqlite | mcp-server-sqlite 2025.4.25 (Python, mcp 1.9.4) | stdio |
| 5 | git | mcp-server-git 2026.8.18 (Python, mcp 1.30.0) | stdio |
| 6 | fetch | mcp-server-fetch 2026.10.10, against a local site (`--allow-private-ips --ignore-robots-txt`) | stdio |
| 7 | time | mcp-server-time 2026.10.10 (mcp 1.30.0), `--local-timezone UTC` | stdio |
| 8 | sequential-thinking | @modelcontextprotocol/server-sequential-thinking 2026.8.31 | stdio |
| 9 | FastMCP example | the MCP Python SDK README quickstart (`add`, a resource), on mcp 1.30.0 (`compat/fastmcp_quickstart.py`) | stdio |
| 10 | TypeScript SDK example | `simpleStreamableHttp.js` from @modelcontextprotocol/sdk 1.32.2's examples | Streamable HTTP |

Runtime: Node v22.23.4, Python 3.13.18. The relay is this repository's code, run from a clean
worktree of the commit named in `codestate_*.json`.

## The calls

| Server | Read (declared read_only) | Write (declared non_idempotent); ground truth |
|---|---|---|
| filesystem | read_text_file | move_file; the destination exists |
| memory | search_nodes | add_observations; the marker in the memory file |
| everything | echo | none: its tools have no lasting effect |
| sqlite | read_query | write_query (INSERT); the rows |
| git | git_log | git_commit (a staged file); commits with the marker as subject |
| fetch | fetch (the local page) | none |
| time | convert_time | none |
| sequential-thinking | none | `sequentialthinking`, declared non_idempotent: it appends to the process's own history, so there is no ground truth outside the process. It is scored by its answer and by how many times it reached the server. |
| FastMCP example | add | none |
| TypeScript SDK example | greet | none |

## Cells, all 3 trials each (TRIAL_OFFSET 40000)

- **`list/direct`:** the reference listing, with no relay.
- **`list/shadow`, `list/auto`:** the same through the relay; the tool list must be identical.
- **No fault:** `read/none/shadow`, `read/none/auto`, and `write/none/auto` where there is a
  write.
- **`keys/auto`** (stdio only): the relay starts the plain server command with `--keys auto`. The
  cell records:
  - the launch plan (adapter, or why the server is unkeyed);
  - whether the keyed tool is advertised with keys;
  - one call's idempotency outcome.
  On a server with no write, the read is keyed as a probe of the wrapping only.
- **Faults, auto, on the read and on the write:**
  - stdio, injected by `realstdio/shim.py`: `kill_before`, `lost_exit` (the answer is lost and
    the server exits) and `hang`;
  - HTTP, injected by `http_fault_proxy.py`: `kill_before`, `lost_response` and `hang_after`.

The relay is configured as the `sg` arm of realstdio and realhttp:
- `--retry auto`, with the declarations above and `--trust-read-only-hints`;
- `--call-deadline 2`;
- on stdio, also `--restart 2 --restart-policy after-unknown`.

## Predictions

- **Listing:** identical through the relay on every server, in shadow and in auto (30/30 per
  mode).
- **No fault:** every call succeeds. Every write runs once.
- **`--keys auto`:**
  - Node servers (filesystem, memory, everything, sequential-thinking) start with the preload;
  - Python servers started by a console script or `python script` (sqlite, git, fetch, time,
    the FastMCP example) start under the injector;
  - each server advertises keys for its keyed tool, and the call's outcome is `executed`.
  - **Not certain:** git, fetch and time, whose SDK path for registering tools the injector has
    not been run against before. A server that does not advertise keys is recorded as such:
    a limitation, not a failure.
- **Faults on a declared read:** recovered (the relay resends a declared read): 3/3 per cell.
- **Faults on a write:** never success claimed without the effect.
  - `kill_before`, `lost_exit`, `hang`: surfaced UNKNOWN or an honest error, with the write's
    effect consistent with what the client is told.
  - `kill_before` on stdio: also UNKNOWN. The relay cannot tell that the shim dropped the call.
- **0 unsafe outcomes** in every cell: no duplicate effect, false success, false failure, wrong
  content, or duplicate send of a tool not declared safe to repeat.

## Status rule (one per server)

- **Pass:** every listing identical, every no-fault call succeeds, every declared read recovers
  from the three faults, `--keys auto` wraps the server and keys are advertised, 0 unsafe.
- **Pass with a documented limitation:** 0 unsafe and every listing and no-fault call correct,
  but something above does not hold or cannot be shown. The limitation is named. Examples:
  - no write to test;
  - no ground truth outside the process;
  - `--keys auto` cannot wrap the server or keys are not advertised;
  - an HTTP server, for which `--keys auto` does not apply.
- **Unsupported:** the handshake, the listing or a no-fault call fails through the relay. The
  reason is given.
- **Not attempted:** with the reason.

Any unsafe outcome stops feature work until it is fixed (operating rules).

Synthetic fault injection on pinned public servers; not production traffic.
