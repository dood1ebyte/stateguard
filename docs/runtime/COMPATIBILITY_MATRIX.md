# StateGuard compatibility matrix (Phase 2 S4.2, 2026-11-25)

**What this table shows.** Ten public MCP servers that need no credentials, each installed at
a pinned version, run through StateGuard's relay. Each server was checked for:
- the handshake and listing (shadow and auto, against a direct listing);
- calls with no fault;
- whether `--keys auto` can start it with keys;
- three faults on a declared read and on a write.

**Evidence:**
- rows, plan and results: `benchmarks/evidence/compat/M1/`;
- harness: `benchmarks/runtime/compat/`;
- server-memory under concurrency: `benchmarks/evidence/realstdio/SER1/`.

These are synthetic faults on pinned servers, with 3 trials per cell, not production traffic. A
status says what was observed in these cells. It does not certify the server.

| # | Server (pinned) | Transport | Status | Limitation |
|---|---|---|---|---|
| 1 | @modelcontextprotocol/server-filesystem 2026.8.31 | stdio | **Pass** | none |
| 2 | @modelcontextprotocol/server-memory 2026.8.31 | stdio | **Pass with a documented limitation** | Loses concurrent writes, each told success (a server defect, the same without StateGuard). The opt-in `--serialize-tools add_observations` removes the loss (SER1). |
| 3 | @modelcontextprotocol/server-everything 2026.8.31 | stdio | **Pass with a documented limitation** | No tool with a lasting effect: writes untested. |
| 4 | mcp-server-sqlite 2025.4.25 (mcp 1.9.4) | stdio | **Pass with a documented limitation** | Reports a failed query as an ordinary result, so a failure looks like success to StateGuard and to the client. |
| 5 | mcp-server-git 2026.8.18 (mcp 1.30.0) | stdio | **Pass** | none |
| 6 | mcp-server-fetch 2026.10.10 | stdio | **Pass with a documented limitation** | No write. Tested against a local site only. |
| 7 | mcp-server-time 2026.10.10 | stdio | **Pass with a documented limitation** | No write. |
| 8 | @modelcontextprotocol/server-sequential-thinking 2026.8.31 | stdio | **Pass with a documented limitation** | State lives only in the process: no ground truth outside it, nothing to recover after a fault. |
| 9 | FastMCP example (MCP Python SDK README quickstart, mcp 1.30.0) | stdio | **Pass with a documented limitation** | No write. An SDK example, not a production server. |
| 10 | TypeScript SDK `simpleStreamableHttp` example (@modelcontextprotocol/sdk 1.32.2) | Streamable HTTP | **Pass with a documented limitation** | `--keys auto` does not apply over HTTP. No write. An SDK example. |
| — | a browser server (puppeteer or playwright) | — | **Not attempted** | Optional and last in the plan. Out of time in S4. |

## The current releases (M2, 2026-11-27; `benchmarks/evidence/compat/M2/`)

On 2026-11-18 the reference servers moved to the TypeScript SDK v2 (Node) and mcp 2.3.0
(Python). M2 re-ran these cells on the current releases, with 0 unsafe outcomes:
- **Listing, no-fault calls and faulted reads:** as above on every server.
- **The Python servers** (git, fetch and time 2026.11.18): keyed by `--keys auto`, as before.
- **The Node servers on the SDK v2** (filesystem 2.4.0, memory 2.2.1, everything 2.3.1,
  sequential-thinking 2.2.0):
  - `--keys auto` runs them **unkeyed**, with the reason (since `e76475c`), because the preload
    wraps the TypeScript SDK 1.x only;
  - before `e76475c`, `--keys auto` stopped them from starting;
  - keying them needs a preload for the SDK v2, which is future work.
- **server-memory 2.2.1:** lost 0 of 80 concurrent writes with no relay, so the limitation in
  row 2 is the pinned 2026.8.31's.
- **The FastMCP quickstart:** a 1.x example; it does not run on mcp 2.x.

## What held on every attempted server

- **0 unsafe outcomes in 315 rows:** no duplicate effect, false success, false failure, wrong
  content, or duplicate send of a tool not declared safe to repeat.
- **Listing:** the tool list through the relay was identical to the direct one, in shadow and
  in auto.
- **`--keys auto`:** it started every stdio server with keys (the preload for Node, the
  injector for Python), and the keyed tool was advertised with keys.
- **A declared read survived each fault** (killed before it was sent, answer lost, hang):
  81/81 recovered.
- **A write was never claimed or re-run after a fault.** It ended UNKNOWN or as a client
  timeout, counted as a residual failure.
  - A write hung under the default deadline scope is answered only after `--answer-timeout`
    (300 s). Clients that stop waiting sooner see a timeout.
  - `--deadline-scope all` answers "unknown" at the deadline instead.

## What this matrix does not show

- **Crash-window closure for any of these servers.** That holds only for cooperating servers
  whose key record and side effect commit atomically (`SERVER_IDEMPOTENCY_CONTRACT.md` §4.5).
  On these servers a crash between the effect and the key's record stays in doubt or UNKNOWN.
- **Behaviour under sustained concurrency, or for tools other than the one read and one write
  per server.** The exception is server-memory's concurrency, which SER1 covers.
- **Any credentialed server.** None was attempted, because A7 supplied no credentials.
