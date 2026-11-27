# SER1: server-memory under concurrency, with and without `--serialize-tools` (Phase 2 S4.3; written before any recorded row)

**Why.**
- @modelcontextprotocol/server-memory 2026.8.31 keeps its graph in one file. Each call reads
  the file, changes the graph and rewrites the file, and the server runs calls concurrently. So
  two overlapping `add_observations` calls can each be told success while one of the two
  observations is lost.
- S4.3 adds `--serialize-tools LIST` to the stdio relay. Calls to the listed tools go to the
  server one at a time: each is sent when the server has answered the previous one (or its
  process is reaped) and the previous client request has its answer.
- **Exploratory check (one trial per arm, outside the record):** 16 concurrent calls lost 15
  writes with no relay and 15 with the relay without the flag; with the flag they lost 0.

**Setup.**
- The harness is `benchmarks/runtime/realstdio/concurrent_writes.py`.
- Each trial:
  - starts a fresh memory file holding one entity;
  - opens one client session (`realstdio/multi_client.py`, the official Python SDK);
  - sends 16 `add_observations` calls, at most 16 in flight at once, each adding its own unique
    observation.
- **Ground truth:** after the session, each observation's presence in the memory file.
- **Arms**, 10 trials each, TRIAL_OFFSET 50000:
  - `raw`: client → server;
  - `sg`: through `stateguard relay stdio --retry auto`;
  - `sg_serial`: the same plus `--serialize-tools add_observations`.

**Measures, per arm:**
- calls told success;
- observations present;
- **lost writes:** calls told success whose observation is absent;
- duplicates (an observation present more than once);
- calls not told success;
- the time per trial.

**Predictions.**
- `raw` and `sg`: lost writes in most trials, about equal between the two arms. This is the
  server's own defect; the relay neither causes it nor can see it.
- `sg_serial`: **0 lost writes in 160 calls**, 160/160 told success and present, 0 duplicates.
- `sg_serial` is slower per trial: 16 round trips one after another, not overlapped.

**Classification.** A lost write is a false success: the client is told success and the effect
is absent.
- In `raw` it is the server's.
- In `sg` (the default relay, no flag) it is the same server defect, passed through unchanged:
  StateGuard does not cause it and cannot detect it.
- That is why the flag exists. It is opt-in, because the relay cannot know that a server
  loses concurrent writes.
The results report it as measured; it is not hidden.

Real pinned server, synthetic concurrent workload; not production traffic.
