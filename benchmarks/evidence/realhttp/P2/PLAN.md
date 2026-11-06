# P2: the cost of event-stream answers on the relay's upstream connections (before any row)

**Question.** After an answer that is an event stream, the relay never reuses its upstream
connection: it stops reading at the answer, and httpx discards the connection (record, D4
finding). What does a new connection per call cost when the path has latency?

**Setup.** `stale_pool.py`:
- one SDK session; 60 unkeyed `add_note` calls, each after a gap drawn from 0.2–0.3 s (below
  the relay's 1 s pool idle timeout, so a JSON connection stays reusable);
- 40 ms one way between the relay and the server;
- relay at its default `--pool-idle-timeout` (1 s).

**Arms:**
- `json`: the server answers JSON (connections reused);
- `sse`: the server answers event streams (`--sse`).

Same seed. The measure is per-call latency, as the client sees it, and the upstream connection
count.

**Expectation:** the `sse` arm opens about one connection per call and is slower per call by
about one round trip (a TCP handshake through the delay: about 80 ms). 0 failures in both arms.
This sizes the cost; it does not decide a change.
