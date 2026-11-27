# T1: `stateguard trial` on three real servers, scripted (Phase 2 S5.1; written before any recorded row)

**What.** `stateguard trial` runs one scripted list of calls twice through
`stateguard capture stdio`: shadow first, then auto (`--retry auto`).
- Each run works on a fresh copy of the server's data (`--sandbox`).
- It writes a before/after report and two redacted bundles, kept locally.
- The driver is `benchmarks/runtime/trial_runs.py`. Its calls are written deterministically
  for each server, a mix of reads and writes with one deliberate miss.

**Servers (pinned, as in M1):**
- server-filesystem 2026.8.31: sandbox is the root directory;
- mcp-server-sqlite 2025.4.25: sandbox is the database's directory;
- mcp-server-git 2026.8.18: sandbox is the repository.

Effects are declared: the reads `read_only` and the writes `non_idempotent`.

**Runs, per server:**
- **`plain`:** no fault.
- **`fault_read`:** TEST-ONLY `--fault lost_answer` on the first call of the server's main
  read tool, against the sandbox copy.

**Prediction.**
- **`plain`:**
  - shadow and auto answer every call alike: no changed calls;
  - the deliberate miss is a tool error in both. On a declared read, auto may resend it once,
    as C1 found;
  - 0 UNKNOWN; the denominators hold; both bundles rebuild from themselves.
  - **sqlite:** its failed query is an ordinary result (M1's documented limitation), so the
    miss is counted as ok in both runs.
- **`fault_read`:**
  - **shadow:** the faulted call and every later one get no answer. Shadow never restarts and
    never answers for the server, so the client sees the session end, as without StateGuard;
  - **auto:** every call answered, the faulted read resent to the restarted server, 0 UNKNOWN;
  - **changed calls:** exactly the faulted call and the calls after it.
- **0 unsafe outcomes:** no write runs twice (each run's sandbox copy is checked), and nothing
  is told success without its effect.

Real pinned servers, scripted workloads, TEST-ONLY faults on sandbox copies; not production
traffic. The counts are for these scripted calls only.
