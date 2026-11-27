# NR1: connection reuse after S4.4's event-stream drain, and its network cost (netem)

**Run.**
- **Where:** GitHub Actions, workflow `.github/workflows/netem.yml`, run 45544981209, job
  138972296475, on `ubuntu-latest` (kernel 7.0.0-1020-azure, Python 3.13.18), 2026-11-25
  04:11 UTC.
- **Code:**
  - "before" is the relay at `290ba5d` (a worktree);
  - "after" is `0dd8604`;
  - the harness `benchmarks/runtime/realhttp/conn_reuse.py` is the same for both.
- **Rows:** copied verbatim from the job log into `rows_netem.jsonl`. The job also uploaded
  them as artifact 14991496303.
- **No plan was committed before this run.** The design is in the workflow and in
  `0dd8604`'s message, both committed before the job ran. No numeric prediction was written
  down beforehand, so none is claimed here.

**Setup.**
- **Server:** the cooperating notes server, MCP Python SDK 2.x, Streamable HTTP, unkeyed. It
  answers `tools/call` with an event stream, and the rows confirm this: answer type
  `text/event-stream`.
- **Topology:** a counting TCP proxy (TCP_NODELAY set, as asyncio sets it) sits in front of
  the server, and `stateguard relay http` in front of the proxy.
- **Workload:** one client session, 40 sequential `list_notes` calls.
- **Delay:** `tc qdisc add dev lo root netem delay D` with one-way D of 0, 5 and 25 ms on
  loopback, applied to every hop.

| One-way delay | Relay | Calls OK | New connections during the 40 calls | p50 per call | p95 |
|---|---|---|---|---|---|
| 0 ms | before | 40/40 | 40 | 4.29 ms | 4.92 ms |
| 0 ms | after | 40/40 | 1 | 3.94 ms | 5.68 ms |
| 5 ms | before | 40/40 | 40 | 54.64 ms | 55.01 ms |
| 5 ms | after | 40/40 | 0 | 34.31 ms | 34.85 ms |
| 25 ms | before | 40/40 | 40 | 255.61 ms | 256.22 ms |
| 25 ms | after | 40/40 | 0 | 154.77 ms | 155.34 ms |

**Measured.**
- **Before:** one new upstream connection per call (40 of 40).
- **After:** 0 or 1 in 40, because the drained connection is reused.
- **Saved per call:** about 20 ms at 5 ms one-way (10 ms round trip), and about 101 ms at
  25 ms (50 ms round trip). That is about two round trips: in this topology each new relay
  connection also made the proxy open a new connection to the server.
- **Direct connection (expected, not measured):** with the relay connected straight to its
  server, a new connection costs one round trip. Over https it also costs the TLS handshake,
  which is not measured here.

**Limitations.**
- Loopback with emulated delay; one server; sequential calls only.
- **Servers that do not set TCP_NODELAY.** A server that writes its answer in more than one
  segment stalls on a reused connection: Nagle's algorithm waits for the relay's delayed ACK,
  about 40 ms on Linux.
  - Seen locally with Python's `http.server` and with a proxy that lacked TCP_NODELAY: p50 rose
    to 47–88 ms.
  - Not seen with uvicorn (the server here) or Node, which set it.
  - Such a server was better off with a new connection per call. That trade is documented, not
    fixed (the relay cannot set the server's socket options).
- The 0 ms rows are loopback times on a shared runner; treat their difference as noise.

Local exploratory check (outside the record, the container, 30 calls, no netem): 30 → 1
connection; p50 6.8 → 4.0 ms.
