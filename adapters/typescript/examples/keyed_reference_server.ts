/**
 * A reference MCP server that honours StateGuard idempotency keys, built on the
 * TypeScript MCP SDK (1.32.1, `McpServer`) with this adapter. It mirrors the
 * Python reference (examples/mcp/keyed_reference_server.py).
 *
 *     node examples/keyed_reference_server.ts --db orders.db --ledger ledger.txt \
 *       [--payments http://127.0.0.1:PORT] [--outbox outbox.txt]
 *
 * Tools:
 *   create_order(item, qty)  mutating, transactional: the order row and its key's record commit
 *                            in one SQLite transaction (the store's database).
 *   append_ledger(entry)     mutating, two-phase, with recovery: appends `entry<TAB>key` in one
 *                            fsync'd write; a torn last line is cut at startup (exclusive store);
 *                            the recovery check looks for this key's own line.
 *   charge(account, amount)  mutating, two-phase; the downstream payment service (--payments)
 *                            dedupes by the key the call sends (native idempotency), and the
 *                            recovery check asks it, by key, what it recorded.
 *   send_notice(to)          mutating, two-phase, an external effect with NO key and NO check:
 *                            appends to --outbox. A crash after the append leaves it in doubt.
 *   read_ledger()            read-only.
 *
 * Every call without a key is an ordinary MCP tool call.
 *
 * TEST-ONLY faults (--fault POINT --fault-file F): SIGKILL once per F at POINT
 * on the first keyed call of a mutating tool that reaches it (in-handler points
 * also fire for unkeyed calls): before_begin, after_begin, mid_effect (ledger:
 * half a line), after_effect, after_record, hang_after_record (never answers).
 * --no-advertise keys nothing; --no-recover keeps abandoned keys in doubt.
 */

import { closeSync, existsSync, fsyncSync, openSync, readFileSync, truncateSync, writeSync, writeFileSync } from "node:fs";
import type { DatabaseSync } from "node:sqlite";
import { parseArgs } from "node:util";

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

import { IdempotencyStore, KeyedTools, NotExecuted, Recovery, currentKey } from "../src/index.ts";
import type { KeyedPoint } from "../src/index.ts";

const { values: args } = parseArgs({
  options: {
    db: { type: "string" },
    ledger: { type: "string" },
    payments: { type: "string" },
    outbox: { type: "string" },
    scope: { type: "string", default: "keyed-reference" },
    "no-advertise": { type: "boolean", default: false },
    "no-recover": { type: "boolean", default: false },
    fault: { type: "string" },
    "fault-file": { type: "string" },
    "effect-delay": { type: "string", default: "0" },
  },
});
if (args.db === undefined || args.ledger === undefined) throw new Error("--db and --ledger are required");
const ledger = args.ledger;
const outbox = args.outbox;
const effectDelay = Number(args["effect-delay"]) * 1000;
const MUTATING = new Set(["create_order", "append_ledger", "charge", "send_notice"]);

function hits(point: string): boolean {
  const flag = args["fault-file"];
  if (args.fault !== point || flag === undefined || existsSync(flag)) return false;
  writeFileSync(flag, point);
  return true;
}

async function crash(point: string): Promise<void> {
  if (!hits(point)) return;
  if (point === "hang_after_record") await new Promise<never>(() => {});
  process.kill(process.pid, "SIGKILL");
}

const sleep = (ms: number) => new Promise((done) => setTimeout(done, ms));
const text = (payload: unknown): CallToolResult => ({ content: [{ type: "text", text: JSON.stringify(payload) }] });

const recovering = !args["no-recover"];
const store = new IdempotencyStore(args.db, { scope: args.scope, exclusive: recovering });
if (recovering && existsSync(ledger)) {
  // Holding the lock: no other process is appending. A torn last line was never an entry.
  const data = readFileSync(ledger);
  const keep = data.lastIndexOf(0x0a) + 1;
  if (keep !== data.length) {
    truncateSync(ledger, keep);
    process.stderr.write(`keyed-reference: cut a torn ledger line (${data.length - keep} bytes)\n`);
  }
}
{
  const db = store.connect();
  db.exec("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, item TEXT, qty INT)");
  db.close();
}

function insertOrder(db: DatabaseSync, input: Record<string, unknown>): CallToolResult {
  if (hits("after_begin")) process.kill(process.pid, "SIGKILL");
  const qty = input["qty"];
  if (typeof qty !== "number" || !Number.isInteger(qty) || qty <= 0) throw new Error("qty must be a positive integer");
  const run = db.prepare("INSERT INTO orders (item, qty) VALUES (?, ?)").run(String(input["item"]), qty);
  if (hits("mid_effect") || hits("after_effect")) process.kill(process.pid, "SIGKILL");
  return text({ order_id: Number(run.lastInsertRowid), item: input["item"] });
}

function appendLine(path: string, line: string, tornAt?: number): void {
  const fd = openSync(path, "a");
  try {
    const bytes = Buffer.from(line, "utf8");
    writeSync(fd, tornAt === undefined ? bytes : bytes.subarray(0, tornAt));
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

function ledgerHasKey(key: string, input: Record<string, unknown>): Recovery {
  // Exact: this key's own line. A crash's torn line was cut at startup; one now means
  // an append in this process failed part-way, and an append can't be ruled out.
  const lines = (existsSync(ledger) ? readFileSync(ledger, "utf8") : "").split("\n");
  if (lines.at(-1)) return Recovery.unknown("the ledger ends in a torn line");
  if (lines.slice(0, -1).includes(`${String(input["entry"])}\t${key}`)) {
    return Recovery.found(text({ appended: input["entry"] }));
  }
  return Recovery.absent();
}

async function payments(path: string, init?: RequestInit): Promise<Response> {
  if (args.payments === undefined) throw new Error("--payments is not configured");
  return fetch(`${args.payments}${path}`, init);
}

async function chargeHasKey(key: string): Promise<Recovery> {
  // The downstream honours the key itself: ask it what it recorded for this key.
  const response = await payments(`/payments/${encodeURIComponent(key)}`);
  if (response.status === 404) return Recovery.absent();
  if (!response.ok) return Recovery.unknown(`payment service answered ${response.status}`);
  const { receipt } = (await response.json()) as { receipt: string };
  return Recovery.found(text({ receipt }));
}

const keyed = new KeyedTools(store, {
  onPoint: async (point: KeyedPoint, tool: string) => {
    if (!MUTATING.has(tool)) return;
    if (point === "before_begin" || point === "after_record") await crash(point);
    if (point === "after_record") await crash("hang_after_record");
  },
});
if (!args["no-advertise"]) {
  keyed.transactional("create_order", insertOrder);
  keyed.twoPhase("append_ledger", recovering ? { recover: ledgerHasKey } : {});
  keyed.twoPhase("charge", recovering ? { recover: (key) => chargeHasKey(key) } : {});
  keyed.twoPhase("send_notice"); // no key on the effect, no check: in doubt after a crash
}

const mcp = new McpServer({ name: "keyed-reference-ts", version: "0.1.0" });
keyed.install(mcp.server);

mcp.registerTool(
  "create_order",
  { description: "Create an order.", inputSchema: { item: z.string(), qty: z.number() } },
  async (input) => {
    const db = store.connect(); // reached without a key: an ordinary call
    try {
      return insertOrder(db, input);
    } finally {
      db.close();
    }
  },
);

mcp.registerTool(
  "append_ledger",
  { description: "Append an entry to the ledger.", inputSchema: { entry: z.string() } },
  async ({ entry }) => {
    if (!entry || entry.includes("\n") || entry.includes("\t")) throw new NotExecuted("entry must be one line without tabs");
    await crash("after_begin");
    const key = currentKey();
    const line = `${key ? `${entry}\t${key}` : entry}\n`;
    if (effectDelay) await sleep(effectDelay);
    if (hits("mid_effect")) {
      appendLine(ledger, line, Math.floor(Buffer.byteLength(line) / 2));
      process.kill(process.pid, "SIGKILL");
    }
    appendLine(ledger, line);
    await crash("after_effect");
    return text({ appended: entry });
  },
);

mcp.registerTool(
  "charge",
  { description: "Charge an account (downstream payment service).", inputSchema: { account: z.string(), amount: z.number() } },
  async ({ account, amount }) => {
    await crash("after_begin");
    const key = currentKey() ?? `unkeyed-${crypto.randomUUID()}`;
    const response = await payments("/payments", {
      method: "POST",
      headers: { "content-type": "application/json", "idempotency-key": key },
      body: JSON.stringify({ account, amount }),
    });
    const { receipt } = (await response.json()) as { receipt: string };
    await crash("after_effect");
    return text({ receipt });
  },
);

mcp.registerTool(
  "send_notice",
  { description: "Send a notice (an external effect with no idempotency).", inputSchema: { to: z.string() } },
  async ({ to }) => {
    if (outbox === undefined) throw new Error("--outbox is not configured");
    await crash("after_begin");
    appendLine(outbox, `${to}\n`);
    await crash("after_effect");
    return text({ sent: to });
  },
);

mcp.registerTool(
  "read_ledger",
  { description: "Read the ledger.", inputSchema: {}, annotations: { readOnlyHint: true } },
  async () => {
    const lines = (existsSync(ledger) ? readFileSync(ledger, "utf8") : "").split("\n").filter(Boolean);
    return text({ lines: lines.map((line) => line.split("\t")[0]) });
  },
);

await mcp.connect(new StdioServerTransport());
