/**
 * Operator injection of the adapter into an **unmodified** TypeScript MCP
 * server (SDK 1.x), at process start:
 *
 *     STATEGUARD_IDEMPOTENCY_DB=/var/lib/sg/keys.db \
 *     STATEGUARD_IDEMPOTENCY_SCOPE=filesystem \
 *     STATEGUARD_KEYED_TOOLS=move_file \
 *       node --import /abs/path/adapters/typescript/src/preload.ts \
 *         node_modules/@modelcontextprotocol/server-filesystem/dist/index.js /data
 *
 * The server's files are not touched. The preload finds the SDK copy the
 * server itself will load (from the server entry's own `node_modules`, through
 * that package's `exports`), imports the same module, and wraps
 * `Server.prototype.setRequestHandler` -- the SDK's public handler
 * registration, which `McpServer.registerTool` uses -- so the server's
 * `tools/list` advertises the keyed tools and its `tools/call` honours keys.
 * This is an *adopting deployment*, not an unmodified server.
 *
 * Keyed tools are two-phase, with no recovery check: the tools' own effects
 * (a rename, a rewritten file) carry no key, so a crash between the effect and
 * its record leaves the key **in doubt** -- reported as unknown, never re-run.
 * The store need not be exclusive (exclusivity only serves recovery), but
 * `STATEGUARD_IDEMPOTENCY_EXCLUSIVE=1` makes it so.
 *
 * TEST-ONLY fault injection (labelled faults, never in production):
 *   STATEGUARD_FAULT=before_begin|after_begin|after_effect|after_record|hang_after_record
 *   STATEGUARD_FAULT_FILE=<path>   the fault fires once per file (whatever the process generation)
 *   STATEGUARD_FAULT_TOOLS=<list>  tools it applies to (default: the keyed tools)
 * The process kills itself (SIGKILL, as a crash would) at the point; hang_after_record
 * never answers. Without STATEGUARD_IDEMPOTENCY_DB the same points wrap the
 * unmodified handler with no keys at all (before_* = before the handler,
 * after_* = after it): the same faults, for arms without the adapter.
 */

import { existsSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

import { IdempotencyStore } from "./store.ts";
import { KeyedTools, methodOf } from "./sdk.ts";
import type { KeyedPoint, RequestHandler } from "./sdk.ts";

const FAULTS = ["before_begin", "after_begin", "after_effect", "after_record", "hang_after_record"] as const;
type Fault = (typeof FAULTS)[number];

function list(value: string | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

/** The server's own copy of `@modelcontextprotocol/sdk`: the nearest one up from its entry. */
export function findSdk(entry: string): string {
  let dir = dirname(realpathSync(entry));
  for (;;) {
    const candidate = join(dir, "node_modules", "@modelcontextprotocol", "sdk");
    if (existsSync(join(candidate, "package.json"))) return candidate;
    const parent = dirname(dir);
    if (parent === dir) throw new Error(`stateguard preload: no @modelcontextprotocol/sdk above ${entry}`);
    dir = parent;
  }
}

/** Resolve a subpath of the SDK the way its `exports` map does for `import` (the `./*` pattern). */
export function sdkModule(sdkRoot: string, subpath: string): string {
  const pkg = JSON.parse(readFileSync(join(sdkRoot, "package.json"), "utf8")) as {
    exports?: Record<string, unknown>;
  };
  const exact = pkg.exports?.[`./${subpath}`];
  const pattern = pkg.exports?.["./*"];
  const target = (exact ?? pattern) as { import?: unknown } | string | undefined;
  const mapped = typeof target === "string" ? target : target?.import;
  if (typeof mapped !== "string") throw new Error(`stateguard preload: cannot resolve ${subpath} in ${sdkRoot}`);
  return join(sdkRoot, exact ? mapped : mapped.replace("*", subpath));
}

const fault = process.env["STATEGUARD_FAULT"] as Fault | undefined;
if (fault !== undefined && !FAULTS.includes(fault)) throw new Error(`stateguard preload: unknown fault ${fault}`);
const faultFile = process.env["STATEGUARD_FAULT_FILE"];
const db = process.env["STATEGUARD_IDEMPOTENCY_DB"];
const keyedTools = list(process.env["STATEGUARD_KEYED_TOOLS"]);
const faultTools = new Set(list(process.env["STATEGUARD_FAULT_TOOLS"]).length ? list(process.env["STATEGUARD_FAULT_TOOLS"]) : keyedTools);

function fires(point: Fault, tool: string): boolean {
  if (fault !== point || faultFile === undefined || !faultTools.has(tool) || existsSync(faultFile)) return false;
  writeFileSync(faultFile, point);
  return true;
}

async function atPoint(point: Fault, tool: string): Promise<void> {
  if (!fires(point, tool)) return;
  if (point === "hang_after_record") await new Promise<never>(() => {}); // never answer
  process.kill(process.pid, "SIGKILL"); // as a crash would: no cleanup, no answer
}

if (db !== undefined || fault !== undefined) {
  const entry = process.argv[1];
  if (entry === undefined) throw new Error("stateguard preload: no server entry in argv");
  const sdkRoot = findSdk(entry);
  const { Server } = (await import(pathToFileURL(sdkModule(sdkRoot, "server/index.js")).href)) as {
    Server: { prototype: { setRequestHandler: (schema: unknown, handler: RequestHandler) => void } };
  };
  let wrap: (schema: unknown, handler: RequestHandler) => RequestHandler;
  if (db !== undefined) {
    if (keyedTools.length === 0) throw new Error("stateguard preload: STATEGUARD_KEYED_TOOLS is empty");
    const store = new IdempotencyStore(db, {
      scope: process.env["STATEGUARD_IDEMPOTENCY_SCOPE"] ?? "default",
      exclusive: process.env["STATEGUARD_IDEMPOTENCY_EXCLUSIVE"] === "1",
    });
    const keyed = new KeyedTools(store, {
      onPoint: async (point: KeyedPoint, tool: string) => {
        await atPoint(point, tool);
        if (point === "after_record") await atPoint("hang_after_record", tool);
      },
    }).twoPhase(keyedTools);
    wrap = (schema, handler) => keyed.wrapFor(schema, handler);
    process.stderr.write(`stateguard preload: keyed ${keyedTools.join(",")} (store ${db}, scope ${store.scope})\n`);
  } else {
    // Fault-only: the same crash points around the unmodified handler, no keys.
    wrap = (schema, handler) =>
      methodOf(schema) !== "tools/call"
        ? handler
        : async (request, extra) => {
            const params = (request as { params?: { name?: unknown } } | undefined)?.params;
            const tool = typeof params?.name === "string" ? params.name : "";
            await atPoint("before_begin", tool);
            await atPoint("after_begin", tool);
            const result = await handler(request, extra);
            await atPoint("after_effect", tool);
            await atPoint("after_record", tool);
            await atPoint("hang_after_record", tool);
            return result;
          };
  }
  const original = Server.prototype.setRequestHandler;
  Server.prototype.setRequestHandler = function (this: unknown, schema: unknown, handler: RequestHandler) {
    return original.call(this, schema, wrap(schema, handler));
  };
}
