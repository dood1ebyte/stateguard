# C1: real servers, scripted workloads, through `stateguard capture` (written before any row)

**Label: real server, scripted workload.** Pinned public MCP servers run unmodified on this
machine, driven by a scripted, agent-like client. This is not production traffic and not a
real user's session. No credentials, no external network: the fetch server reads a local site
on 127.0.0.1 only.

**Why (Phase 2 S2.4, capture acceptance C).** It exercises `stateguard capture` and
`stateguard report` on real servers, in shadow and in auto, with every denominator reported. It
measures nothing about real-world failure rates.

**Servers (pinned; approval A3):**
- `mcp-server-git` 2026.8.18 (PyPI, mcp 1.30.0), on a fresh local repository per session, with an
  isolated HOME and git config;
- `@modelcontextprotocol/server-filesystem` 2026.8.31 (npm, Node v22.23.4), on a fresh root
  directory per session;
- `mcp-server-fetch` 2026.10.10 (PyPI), `--allow-private-ips --ignore-robots-txt`, against a local
  `http.server` site built per session; proxy variables removed from its environment.

**Workload** (`benchmarks/runtime/capture_workloads.py`). One call at a time, as an agent does.
Each session is a deterministic sequence from its seed: reads, writes and listings in fixed
proportions. About 5% are deliberate misses (a missing file, revision or page), which give a
realistic share of tool errors. Writes to the repository's files are made by the client, as an
editor would; `git_add` and `git_commit` go through the relay.

**Arms, paired.** The same seed, workload and server run in both arms:
- **shadow:** `stateguard capture stdio --shadow --retry auto --effects E --trust-read-only-hints
  --call-deadline 30`;
- **auto:** the same command without `--shadow`.
Both use provenance `local_development` and label "real server, scripted workload". The effect
declarations E (the same in both arms) are:
- reads declared `read_only`;
- `git_commit`, `write_file`, `edit_file` and `move_file` declared `non_idempotent`;
- `git_add` and `create_directory` declared `idempotent`.
No keys: the arms differ only in `--shadow`.

**Size.** 3 servers × 2 arms × 4 sessions × 50 tool calls = **1,200 tool calls** (target: at least
1,000 across at least 3 real servers). Seeds 1–4, the same in both arms.

**Reported, per server and per arm, and never pooled across arms:**
- sessions;
- client tool calls (counted by the client), relay records, answered, unfinished;
- the denominator checks;
- succeeded, tool error, failed and UNKNOWN, with Wilson 95% intervals;
- retries and would-retry;
- latency and relay-overhead percentiles;
- whether every report rebuilds from its bundle;
- every missing or invalid capture.
Paired comparison per (server, seed): the outcome counts in shadow against auto.

**Prediction:**
- every denominator check holds: client calls = records = answers;
- 0 unfinished, 0 failed, 0 UNKNOWN; the tool errors are the deliberate misses, the same count in
  both arms of a pair;
- 0 retries made in auto; would_retry 0 in shadow (nothing fails);
- every report rebuilds identically from its bundle;
- relay overhead of a few milliseconds per call.

Any other result is reported as it is.
