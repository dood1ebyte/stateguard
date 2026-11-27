# M2: version checks beyond the pinned versions (deferral-list item 2; S5, 2026-11-27; written before any row)

**Why.** M1 and every matrix ran pinned versions from August to October 2026. On 2026-11-18,
the reference servers moved:
- the Node servers to semantic versions on the **TypeScript SDK v2** (`@modelcontextprotocol/server`
  2.4.0, `@modelcontextprotocol/node` 2.1.2), no longer on `@modelcontextprotocol/sdk` 1.x;
- the Python servers to mcp 2.3.0.
M2 runs M1's harness and cells on the current releases. This is deferral-list item 2
(PHASE2_PLAN.md §4), taken after S5's agenda was done (window rule 3).

**Versions under test (installed 2026-11-27):**

| Server | M1 (pinned) | M2 (current) |
|---|---|---|
| server-filesystem | 2026.8.31 (SDK 1.32.2) | 2.4.0 (SDK v2) |
| server-memory | 2026.8.31 | 2.2.1 (SDK v2) |
| server-everything | 2026.8.31 | 2.3.1 (SDK v2) |
| server-sequential-thinking | 2026.8.31 | 2.2.0 (SDK v2) |
| mcp-server-git | 2026.8.18 (mcp 1.30.0) | 2026.11.18 (mcp 2.3.0) |
| mcp-server-fetch | 2026.10.10 | 2026.11.18 (mcp 2.3.0) |
| mcp-server-time | 2026.10.10 | 2026.11.18 (mcp 2.3.0) |
| FastMCP example | mcp 1.30.0 | mcp 2.3.0 |

sqlite (2025.4.25) and the TypeScript SDK 1.x HTTP example (1.32.2) have no newer release, so
they are not re-run.

**Cells:** M1's: listing in shadow and auto, no-fault calls, `keys/auto`, and three faults on the
read and on the write. 3 trials, TRIAL_OFFSET 41000. Same harness, same relay commit.

**Server-memory under concurrency on 2.2.1 (SER1's workload).** 16 concurrent `add_observations`
per trial, 5 trials, arms raw, sg and sg_serial, TRIAL_OFFSET 51000. It shows whether the
current release still loses concurrent writes.

**Prediction.**
- **Listing, no-fault calls and faulted reads:** as M1 on every server. The relay works on the
  wire, not with any SDK.
- **`--keys auto`:**
  - **Python servers on mcp 2.3.0:** the injector wraps them, and keys are advertised (it
    supports mcp 2.x).
  - **Node servers on TypeScript SDK v2: not keyed.** The preload wraps
    `@modelcontextprotocol/sdk` 1.x, which these servers no longer use. The cell records what
    happens (unkeyed with a reason, or the server failing to start under the preload): a
    compatibility limitation to document, not a safety outcome.
- **The FastMCP example on mcp 2.3.0:** not certain. The README quickstart's import may not
  exist in 2.x.
- **server-memory 2.2.1 under concurrency:** not predicted; measured.
- **0 unsafe outcomes** in every StateGuard cell.

Synthetic fault injection on public servers; not production traffic.
