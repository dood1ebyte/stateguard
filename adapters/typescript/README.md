# @stateguard/mcp-idempotency (TypeScript)

This is the server side of StateGuard's MCP idempotency-key contract, for the TypeScript MCP SDK 1.x (tested with **1.32.1**).
- It uses the same wire contract, the same outcomes and the same SQLite store schema as the Python reference (`stateguard.adapters.mcp.idempotency`).
- A store file and its lock are shared between the two languages.
- Contract: `docs/runtime/SERVER_IDEMPOTENCY_CONTRACT.md`. This adapter's compatibility matrix and evaluation: `docs/runtime/TYPESCRIPT_IDEMPOTENCY_ADAPTER.md`.

Requires Node ≥ 22.18, which runs the TypeScript sources directly through type stripping. `npm run build` emits JavaScript to `dist/`.

## Adopting it in a server you write

```ts
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { IdempotencyStore, KeyedTools, currentKey } from "@stateguard/mcp-idempotency";

const store = new IdempotencyStore("/var/lib/orders/keys.db", { scope: "orders" });
const keyed = new KeyedTools(store)
  .twoPhase("send_invoice")                     // any effect: in doubt if cut off mid-way
  .transactional("create_order", insertOrder);  // effect in the store's own SQLite: exactly once
const mcp = new McpServer({ name: "orders", version: "1.0.0" });
keyed.install(mcp.server);                      // before any registerTool
```

- `install` wraps the server instance's public `setRequestHandler`. SDK 1.x has no middleware layer.
- `currentKey()` gives the running call's key. A handler can pass it to a downstream that honours it (native idempotency), or write it into its own effect; either makes a recovery check exact: `twoPhase(name, { recover })`.

## Adopting it in a server you cannot edit

```
STATEGUARD_IDEMPOTENCY_DB=/var/lib/sg/keys.db STATEGUARD_IDEMPOTENCY_SCOPE=filesystem \
STATEGUARD_KEYED_TOOLS=move_file \
  node --import /path/to/adapters/typescript/src/preload.ts \
    node_modules/@modelcontextprotocol/server-filesystem/dist/index.js /data
```

- **How it attaches.** The preload patches the server's own copy of the SDK (`Server.prototype.setRequestHandler`); the server's files are not touched. The result is an adopting *deployment*, not an unmodified server.
- **No recovery.** Keyed tools are two-phase with no recovery check, because their effects carry no key. A crash between the effect and its record leaves the key **in doubt**, and it is never re-run.

## What it guarantees, and what it does not

- **Guaranteed:**
  - The same key and the same request never execute twice.
  - Concurrent duplicates are serialised.
  - A key reused with other arguments is rejected.
  - Recorded results and recorded tool errors replay.
  - A store failure executes nothing.
  - "Input required" (`UrlElicitationRequired`) is never recorded as a completion.
- **Exactly-once** holds only for `transactional` effects in the store's database, and for two-phase effects with a sound recovery check: an exclusive store, plus a key-carrying or natively keyed effect.
- **Not guaranteed:**
  - Effects that carry no key (a rename, a rewritten file, an e-mail) are *at most once*. After a crash they are reported unknown.
  - The exclusive lock is an SQLite `EXCLUSIVE` lock on one host. It is not distributed coordination.
  - `node:sqlite` is synchronous: a store call blocks the event loop while it runs.
  - Fingerprints equal Python's, except for integral values written as floats (`1.0`, `1e16`) and integers beyond ±2^53.

## Gates

```
npm run typecheck   # tsc 6.0.2, --strict, against SDK 1.32.1's own .d.ts
npm test            # node --test: store, crash windows (real SIGKILL), SDK, end-to-end server
```

The cross-language tests are `tests/examples/test_typescript_adapter.py`, run with pytest.
