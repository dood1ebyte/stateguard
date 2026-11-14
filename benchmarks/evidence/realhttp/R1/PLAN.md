# R1: concurrency under repeated faults (written before any row)

**Question.** Several client sessions call one relay at once. Each session makes several
writes, and faults keep coming, including on the relay's own resends. Under those conditions,
do StateGuard's arms stay safe (no duplicate, no false success, no false failure)? And what
residual is left? This is the queue's "concurrency under repeated faults (several sessions,
several faults each)". Every earlier batch put one fault on one call of one session.

**Setup** (`benchmarks/runtime/realhttp/repeated.py`, `multi_client.py`; the fault proxy's new
`repeat` mode). Each trial runs:
- one notes server;
- one fault proxy in front of it;
- in StateGuard arms, one HTTP relay in front of that.

Four client processes (the official Python SDK, mcp 2.x) start together. Each makes six
`add_note` calls, one after another, on its own session, and every note text is unique.

The proxy faults every 3rd `add_note` request that reaches it, from any session (a relay's
resends included). It cycles through: `kill_before`, `http_503_before`, `lost_response`,
`http_502_after`, `cut_stream`, `rpc_internal_error`. No hang modes are used, so no deadline is
involved; the relay runs with `--call-deadline 0`, as in the matrix arms.

If a session breaks, the call it broke on is recorded as the client saw it, and the next call
opens a new session.

**Arms:**
- `raw`: client to proxy to server.
- `naive`: raw, plus one blind resend of any failed call, with no key.
- `sg`: the relay with `--retry auto`, and `add_note` declared non-idempotent.
- `sg_keyed`: sg, with the server keyed by the injector (Python) or the preload (TypeScript).

**Servers:** `notes` (Python mcp 2.x) and `tsnotes` (TypeScript SDK), reported separately: this
is the Python and TypeScript side-by-side item too.

**Size:** 10 trials per arm per server, so 240 logical invocations per arm per server and 1,920
rows. Two trials run at a time (4 CPUs, and each trial has 4 clients and up to 3 servers).

**Scoring.**
- One row is one call a client made, scored by `driver.score` as every write is scored, by its
  note's count in the notes file (ground truth outside StateGuard).
- RFR = (failed + unresolved) / eligible, with a Wilson 95% interval, per server and arm
  (`phase1_table.py`, family `concurrent`).
- Nothing is excluded: a call lost to a broken session, and a driver error, are failures.
- Rows are **not paired with raw**: which call a fault hits depends on the order requests reach
  the proxy. Resends use up fault slots, so StateGuard arms get *more* faults per trial than
  raw. Each row records the faults its call met (`faults_hit`).

**Agreement checks, per trial** (the relay's records carry the session and JSON-RPC id, not
the note, so they are checked per trial, not per call):
- relay `tools/call` records = calls made;
- calls the relay said ran (`answered` + `recovered`) = calls scored successful.

**Prediction** (from a smoke run of 1 trial per arm per server, from the working tree, before
this plan; it is not evidence):
- raw about 1/3 failed, with some false failures;
- naive: duplicates;
- sg about 1/3, nearly all unresolved (an unknown outcome of a non-idempotent call is never
  resent);
- sg_keyed: only calls whose every allowed attempt was faulted. The relay allows 2 attempts
  (`DEFAULT_MAX_ATTEMPTS`), so these are unresolved.
- StateGuard arms: 0 unsafe outcomes.

**Decision rule.** Any unsafe outcome in a StateGuard arm is a defect, to be fixed and
re-measured. If sg_keyed's residual is only calls faulted on both attempts, a larger attempt
budget for keyed calls is the candidate change. It would run as a separate part, on its own
commit, with this batch's parts as its baseline.

Synthetic fault injection on two controlled servers on one VM; not production traffic.
