# S3: retrying a JSON-RPC -32603 where it is safe (written before any row)

**Change under test.** A JSON-RPC internal error (`-32603`) becomes its own failure class
(`SERVER`, observed `server_error`) instead of a protocol error. `decide_retry` treats it as a
known failure:
- retried once if the call is keyed, or the tool is declared `read_only` or `idempotent`;
- otherwise delivered as the server sent it, because an internal error may come after the
  effect.

The other protocol errors (-32600, -32601, -32602 and the rest) are still never retried.

**Batch.** S2's harness, on the commit named in `codestate*.json`:
- servers sqlite and memory;
- cells read/transient and write/transient (the shim answers `-32603` once, without forwarding);
- arms sg and sg_keyed;
- 12 trials; fault-free controls read/none and write/none, 12 trials per arm.

**Expectations, against S2 (where all four cells failed 12/12):**
- read/transient: sg 0/12, sg_keyed 0/12 (a declared read, retried).
- write/transient: sg_keyed 0/12, with the resend executed once. sg 12/12: an unkeyed
  non-idempotent write is never retried, and the client gets the server's error.
- 0 duplicates and 0 false successes; fault-free controls 0 failures.
