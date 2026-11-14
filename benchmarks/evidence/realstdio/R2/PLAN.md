# R2: repeated faults on stdio, calls in flight together (written before any row)

**Question.** R1 asked this over HTTP; R2 asks it over stdio. One client session keeps several
writes in flight, and faults keep coming, including on the relay's resends and on restarted
servers. Do StateGuard's arms stay safe (no duplicate, no false success, no false failure)?
What residual is left?

Every earlier stdio batch (S2–S8, A1) faulted one attempt of one call. D15 and D16 changed the
stdio relay's handling of later attempts, and only unit tests have exercised it there so far.

**Setup** (`benchmarks/runtime/realstdio/repeated.py`, `multi_client.py`; the shim's new
`repeat` mode).
- **Per trial:** one client session (the official Python SDK, mcp 2.x, stdio) makes 12
  writes, each with its own marker, with up to 4 in flight at once. memory runs one at a time:
  server-memory's `add_observations` loads, changes and saves its file with no lock, so
  concurrent calls can lose each other's writes with no fault at all. That is the server's own
  behaviour, outside this question.
- **Faults:** the shim faults every 3rd write that reaches it, a relay's resends included, with
  the count kept across restarts. It cycles through all eight stdio modes:
  - `kill_before`: the shim and server exit, nothing ran;
  - `lost_exit`: the call ran, its answer was lost, and they exit;
  - `hang`: the call ran and never answers;
  - `transient`: a `-32603` from the shim, nothing ran;
  - `duplicate`, `garbage`, `truncated`, `nonconformant`: the call ran and its answer was
    altered.
- **If the session breaks** (its process exits), every call in flight on it ends as the client
  saw it, and the calls not yet started go on a new session.

**Arms**, as in `driver.py`:
- `raw`: client to shim to server.
- `naive`: raw, plus one blind resend of any failed call.
- `sg`: the stdio relay with `--retry auto`, `--restart 10 --restart-policy after-unknown`,
  `--call-deadline 2`, and the write declared non-idempotent. It gets 10 restarts, not the
  matrices' 2: a trial kills the server several times.
- `sg_keyed`: sg, with the server keyed by the injector (Python) or the preload (TypeScript).

**Servers:**
- `sqlite`: mcp-server-sqlite, Python; `write_query`, an INSERT.
- `memory`: server-memory, TypeScript; `add_observations`.
- `filesystem`: server-filesystem, TypeScript; `move_file`, a distinct file per call.

**Size:** 8 trials per arm per server: 96 calls per arm per server, 1,152 rows.

**Scoring.**
- One row is one call, scored by `driver.score`, the stdio suite's rule, by its marker's
  ground truth: rows, the memory file, or whether the destination exists.
- RFR with a Wilson 95% interval per server and arm.
- Nothing is excluded: a call lost with a broken session is a failure, and so is a driver
  error.
- Not paired with raw (as R1). Each row lists the faults its call met.

**Agreement checks, per trial** (StateGuard arms): the relay's `tools/call` records equal the
calls made, and the calls the relay said ran equal the successes.

**Code:** D17, D16 and D15. They are the code under test.
- D16: an attempt budget of 3 for keyed calls.
- D17: in the default `recoverable` scope, a keyed resend with attempts left is itself tried
  again at its deadline, and a resend's answer that comes after its own deadline is delivered.

**Smoke run before this plan** (1 trial per arm per server, then 3 sqlite sg_keyed trials, from
the working tree; not evidence). It found D17. A keyed call's first answer was garbage and its
resend's answer truncated. Neither line can be matched to a request, so the call waited until
the client gave up, and its third attempt was never sent. The same run showed two expected
residuals in sg_keyed, both unresolved and never unsafe:
- calls whose attempts were all lost when *other* calls' faults killed the shared server;
- the crash window (a server killed between a call's effect and its key's record).

**Prediction:**
- raw and naive: high failure rates. A server killed by one call takes every call in flight
  with it.
- sg: mostly unresolved (unknown outcomes of non-idempotent calls are never resent).
- sg_keyed: a low residual, all unresolved.
- StateGuard arms: 0 unsafe outcomes.

**Decision rule:** any duplicate, false success or false failure in a StateGuard arm is a defect
to fix and re-measure.

Synthetic fault injection on three pinned servers on one VM; not production traffic.
