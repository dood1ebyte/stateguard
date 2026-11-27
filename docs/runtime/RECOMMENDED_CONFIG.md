# The recommended configuration: frozen for S6 (Phase 2 S5.4, 2026-11-27)

**This is the configuration S6 tests, exactly** (PHASE2_PLAN.md §3, S5.4, and §6).
- It is the StateGuard arm the Phase 1/2 matrices ran: `sg_keyed` on stdio and
  `sg_keyed_deadline` over HTTP.
- It adds S4.3's serialisation for server-memory, which §6 requires ("memory (TypeScript,
  serialised)").
- **Frozen at:** the commit that adds this file.
- **Changing it:** after this point, only through a new protocol version committed before any
  S6 run (§6).

## stdio relay (W1, W1-coop)

```
stateguard relay stdio \
  --retry auto \
  --restart 2 --restart-policy after-unknown \
  --effects EFFECTS.json \
  --trust-read-only-hints \
  --call-deadline 2 \
  [--keys auto --keys-db KEYS.db --keys-scope SERVER]          # sqlite, memory, filesystem
  [--serialize-tools add_observations,create_entities,create_relations,delete_entities,delete_observations,delete_relations]   # memory only
  -- SERVER COMMAND...
```

- **`--effects`:** each server's writes `non_idempotent` and its reads `read_only`, as the
  realstdio driver declares them. Tools not declared are never resent.
- **`--keys auto`:**
  - the Python injector for sqlite;
  - the TypeScript preload for memory and filesystem;
  - the store is not exclusive;
  - no `STATEGUARD_IDEMPOTENT_TOOLS` and no `STATEGUARD_RECOVERY_CHECK`: both are operator
    declarations or code, outside the default.
- **The cooperating server (W1-coop)** keys itself natively, so it runs without `--keys auto`.
- **`--call-deadline 2`:** the matrices' value, and S6's.
  - The default `--deadline-scope recoverable` applies: the deadline is when to try again, and
    only resendable calls (declared reads, honoured keys) are resent at it.
  - Other calls wait for their answer up to `--answer-timeout` (300 s), then "outcome unknown".
  - **Outside S6:** set it above the slowest tool's normal latency.
- **`--serialize-tools`** lists all six of server-memory's writing tools, because each one
  rewrites the graph file. SER1 measured the loss without it.

## HTTP relay (W2, W2-coop)

```
stateguard relay http \
  --retry auto \
  --effects EFFECTS.json \
  --trust-read-only-hints \
  --call-deadline 2 \
  --upstream http://HOST:PORT/mcp
```

- **Keys:**
  - notes runs under the Python injector, and tsnotes under the TypeScript preload (§6 W2);
  - the cooperating server keys itself.
- **Defaults otherwise:** `--pool-idle-timeout 1`, and the event-stream drain (S4.4), which is
  always on.

## What is not in it

- `--no-listing-refresh`: refresh stays on, the default.
- `--keys auto` over HTTP: it does not exist; servers are keyed by the adapter at start.
- `STATEGUARD_IDEMPOTENCY_EXCLUSIVE`, `STATEGUARD_IDEMPOTENT_TOOLS`, `STATEGUARD_RECOVERY_CHECK`.
- `--passthrough`, `--shadow`.

**Scope.** This configuration covers the defined supported scope (the pinned servers and
fixtures of §6), not production traffic. Every S6 report says so.
