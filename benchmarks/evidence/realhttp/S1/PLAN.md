# S1: the stale pooled connection race, relay to server (written before any row)

**Question.** A server closes a kept-alive connection after its own idle timeout (uvicorn: 5 s).
If the relay writes a request to that connection as the server closes it, the request is lost
unread, and the relay cannot tell that from a crash after execution. Does this happen through
the relay, how often in this setup, and does a pool idle timeout below the server's remove it?

**Setup.**
- Harness: `stale_pool.py`. Code under test: the commit named in `codestate*.json`.
- Server: the controlled notes server (uvicorn; its keep-alive timeout is uvicorn's default, 5 s).
- Network: `delay_proxy.py` adds 40 ms one way between the relay and the server.
- Workload: one SDK client session makes 60 `add_note` calls, each after an idle gap drawn
  uniformly from [4.8, 5.1] s (seed 1, so every arm gets the same gaps).

**Amendment (before any row, 2026-11-06 09:45 IST).** A probe showed the relay never reuses
a connection after an answer that is an event stream: it stops reading at the answer, before
the stream's end, so httpx discards the connection. For event-stream answers the race cannot
occur. The server therefore answers JSON (`--json`), whose connections return to the pool, in
every arm. One extra arm, `old_sse`, keeps event-stream answers to show the race's absence there.

**Arms:**
- `old`: `--pool-idle-timeout 5` (httpx's default, which the relay used before this change), unkeyed.
- `new`: `--pool-idle-timeout 1` (the new default), unkeyed.
- `old_keyed`: `--pool-idle-timeout 5`, with the server keyed by the injector.
- `old_sse`: as `old`, with the server answering event streams (`--sse`). Expected: no race.

**Expectations (before running):**
- `old`: some calls meet the race; the relay records `transport_lost` and answers "unknown".
  Each such note is absent (lost unread).
- `new`: no `transport_lost`, 0 failures; more upstream connections are opened.
- `old_keyed`: the race occurs as in `old`, but each lost call is resent with its key and
  recovered, written exactly once.
- 0 duplicates and 0 false successes in every arm.

This is synthetic: the gaps are chosen to sit near the timeout, so the race rate measures this
setup only, never its incidence in real traffic.
