# S2: stdio baseline versus StateGuard on pinned real servers (written before any row)

**Why.** The real-server stdio evidence (M16 for sqlite; the TypeScript servers) was lost with
its container. S2 re-establishes it with the in-repo harness (`benchmarks/runtime/realstdio/`).

**Servers** (third-party, unmodified; package versions are in `codestate*.json`):
- mcp-server-sqlite 2025.4.25 (Python, mcp 1.9.4, pydantic 2.11.7). Keys via the injector.
  Write: `write_query` INSERT, ground truth is the row count.
- @modelcontextprotocol/server-memory 2026.8.31 (TypeScript; installed SDK 1.32.2). Keys via
  the preload. Write: `add_observations`. The tool drops a duplicate observation, so duplicates
  cannot appear there.

**Arms:** raw, naive (blind resend on a new process), sg (relay: retry auto, restart 2
after-unknown, write declared non_idempotent, call deadline 2 s), sg_keyed.

**Cells (13):**
- read/none and write/none;
- read and write × the shim faults kill_before, lost_exit, hang, transient;
- write × the server crash points after_begin, after_effect, after_record.

12 trials per cell; fault-free controls 60 more per fault-free cell (TRIAL_OFFSET=1000).

**Expectations (before running):**
- raw fails every faulted cell.
- naive duplicates sqlite rows where the write ran before the fault: lost_exit, hang,
  after_effect, after_record. It cannot duplicate on memory.
- sg_keyed recovers every transport cell and after_record. after_begin and after_effect stay
  in doubt (failed, never success): 24/132 faulted per server.
- sg recovers reads. Unkeyed writes whose outcome is unknown stay unknown, and none is resent.
- 0 duplicates and 0 false successes in sg and sg_keyed; fault-free 0 in every arm; 0
  invocation-record mismatches.
