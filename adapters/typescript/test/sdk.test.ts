/**
 * The SDK adapter on a real `McpServer` (SDK 1.32.1), driven by the official
 * `Client` over the SDK's in-memory transport.
 */

import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, test } from "node:test";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { CallToolRequestSchema, ListToolsRequestSchema, UrlElicitationRequiredError } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

import { IDEMPOTENCY_CAPABILITY, IdempotencyStore, KeyedTools, NotExecuted, Outcome, currentKey } from "../src/index.ts";
import { KEY, errorOutcome, outcomeOf, tmp } from "./helpers.ts";

interface Rig {
  client: Client;
  store: IdempotencyStore;
  effects: string[];
  keys: (string | undefined)[];
}

async function rig(options: { delayMs?: number; fail?: () => never } = {}): Promise<Rig> {
  const store = new IdempotencyStore(join(tmp(), "keys.db"), { scope: "sdk-tests" });
  const effects: string[] = [];
  const keys: (string | undefined)[] = [];
  const mcp = new McpServer({ name: "rig", version: "0" });
  new KeyedTools(store).twoPhase(["pay", "confirm_then_pay", "declined"]).install(mcp.server);
  mcp.registerTool("pay", { inputSchema: { amount: z.number() } }, async ({ amount }) => {
    keys.push(currentKey());
    if (options.delayMs) await new Promise((done) => setTimeout(done, options.delayMs));
    effects.push(`pay ${amount}`);
    return { content: [{ type: "text", text: `paid ${amount} #${effects.length}` }] };
  });
  let confirmed = false;
  mcp.registerTool("confirm_then_pay", { inputSchema: { amount: z.number() } }, async ({ amount }) => {
    if (!confirmed) {
      confirmed = true; // ask once: SDK 1.x's "input required"
      throw new UrlElicitationRequiredError(
        [{ mode: "url", message: "confirm", url: "https://example.invalid/confirm", elicitationId: "e1" }],
        "confirmation required",
      );
    }
    effects.push(`confirmed ${amount}`);
    return { content: [{ type: "text", text: "ok" }] };
  });
  mcp.registerTool("declined", { inputSchema: { amount: z.number() } }, async ({ amount }) => {
    effects.push(`declined ${amount}`);
    throw new Error("card declined"); // McpServer answers isError; the outcome is recorded
  });
  mcp.registerTool("read", { inputSchema: {}, annotations: { readOnlyHint: true } }, async () => ({
    content: [{ type: "text", text: "r" }],
  }));
  const [serverSide, clientSide] = InMemoryTransport.createLinkedPair();
  await mcp.connect(serverSide);
  const client = new Client({ name: "sdk-tests", version: "0" });
  await client.connect(clientSide);
  return { client, store, effects, keys };
}

const call = (r: Rig, name: string, args: Record<string, unknown>, key?: string) =>
  r.client.callTool({ name, arguments: args, ...(key ? { _meta: { [KEY]: key } } : {}) });

describe("KeyedTools on McpServer", () => {
  test("tools/list advertises exactly the keyed tools", async () => {
    const r = await rig();
    const tools = Object.fromEntries((await r.client.listTools()).tools.map((t) => [t.name, t]));
    assert.deepEqual(tools["pay"]?._meta?.[IDEMPOTENCY_CAPABILITY], { version: 1 });
    assert.equal(tools["read"]?._meta?.[IDEMPOTENCY_CAPABILITY], undefined);
  });

  test("duplicate requests with one key execute once and replay the recorded result", async () => {
    const r = await rig();
    const first = await call(r, "pay", { amount: 5 }, "k1");
    const again = await call(r, "pay", { amount: 5 }, "k1");
    assert.equal(outcomeOf(first), Outcome.EXECUTED);
    assert.equal(outcomeOf(again), Outcome.REPLAYED);
    assert.deepEqual(again.content, first.content);
    assert.deepEqual(r.effects, ["pay 5"]);
    assert.deepEqual(r.keys, ["k1"]); // currentKey() inside the handler
  });

  test("concurrent duplicates with one key execute once", async () => {
    const r = await rig({ delayMs: 150 });
    const answers = await Promise.all([1, 2, 3].map(() => call(r, "pay", { amount: 7 }, "same")));
    assert.deepEqual(answers.map(outcomeOf).sort(), [Outcome.EXECUTED, Outcome.REPLAYED, Outcome.REPLAYED].sort());
    assert.deepEqual(r.effects, ["pay 7"]);
  });

  test("concurrent calls with different keys each execute once", async () => {
    const r = await rig({ delayMs: 50 });
    const answers = await Promise.all(["a", "b", "c", "d"].map((key, i) => call(r, "pay", { amount: i }, key)));
    assert.deepEqual(answers.map(outcomeOf), Array(4).fill(Outcome.EXECUTED));
    assert.deepEqual(r.effects.sort(), ["pay 0", "pay 1", "pay 2", "pay 3"]);
  });

  test("a key reused with other arguments is rejected; nothing executes", async () => {
    const r = await rig();
    await call(r, "pay", { amount: 5 }, "k");
    await assert.rejects(call(r, "pay", { amount: 6 }, "k"), (error) => {
      assert.equal((error as { code?: number }).code, -32602);
      assert.equal(errorOutcome(error), Outcome.REJECTED_MISMATCH);
      return true;
    });
    assert.deepEqual(r.effects, ["pay 5"]);
  });

  test("a tool error is recorded and replayed consistently, never re-run", async () => {
    const r = await rig();
    const first = await call(r, "declined", { amount: 5 }, "k");
    const again = await call(r, "declined", { amount: 5 }, "k");
    assert.equal(first.isError, true);
    assert.equal(again.isError, true);
    assert.deepEqual(again.content, first.content);
    assert.equal(outcomeOf(again), Outcome.REPLAYED);
    assert.deepEqual(r.effects, ["declined 5"]);
  });

  test("an input-required answer is not recorded: the retry with its input executes", async () => {
    const r = await rig();
    await assert.rejects(call(r, "confirm_then_pay", { amount: 5 }, "k"), (error) => {
      assert.equal((error as { code?: number }).code, -32042); // passed through as the SDK sent it
      return true;
    });
    assert.equal(r.store.stateOf("k"), undefined); // not "done": a retry must not replay it
    const retried = await call(r, "confirm_then_pay", { amount: 5 }, "k");
    assert.equal(outcomeOf(retried), Outcome.EXECUTED);
    assert.equal(outcomeOf(await call(r, "confirm_then_pay", { amount: 5 }, "k")), Outcome.REPLAYED);
    assert.deepEqual(r.effects, ["confirmed 5"]);
  });

  test("unkeyed calls, unkeyed tools and reads pass through untouched", async () => {
    const r = await rig();
    const plain = await call(r, "pay", { amount: 1 });
    assert.equal(outcomeOf(plain), undefined);
    assert.equal(outcomeOf(await call(r, "read", {}, "k-read")), undefined); // not a keyed tool
    await call(r, "pay", { amount: 1 }); // a plain resend is a new call
    assert.deepEqual(r.effects, ["pay 1", "pay 1"]);
    assert.deepEqual(r.keys, [undefined, undefined]);
  });

  test("a store failure before execution executes nothing", async () => {
    const r = await rig();
    for (const suffix of ["", "-wal", "-shm"]) writeFileSync(`${r.store.path}${suffix}`, Buffer.alloc(4096, 7));
    await assert.rejects(call(r, "pay", { amount: 5 }, "k"), (error) => {
      assert.equal(errorOutcome(error), Outcome.REJECTED_STORE);
      return true;
    });
    assert.deepEqual(r.effects, []);
    assert.equal(outcomeOf(await call(r, "pay", { amount: 5 })), undefined); // unkeyed still works
  });

  test("install fails closed if the tool handlers already exist", () => {
    const mcp = new McpServer({ name: "late", version: "0" });
    mcp.registerTool("x", { inputSchema: {} }, async () => ({ content: [] }));
    assert.throws(() => new KeyedTools(new IdempotencyStore(join(tmp(), "k.db"))).install(mcp.server), /already exists/);
  });
});

describe("KeyedTools on a low-level Server", () => {
  async function lowLevel(handler: () => unknown) {
    const store = new IdempotencyStore(join(tmp(), "keys.db"));
    const server = new Server({ name: "low", version: "0" }, { capabilities: { tools: {} } });
    new KeyedTools(store).twoPhase("op").install(server);
    server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: "op", inputSchema: { type: "object" } }] }));
    server.setRequestHandler(CallToolRequestSchema, async () => handler() as never);
    const [a, b] = InMemoryTransport.createLinkedPair();
    await server.connect(a);
    const client = new Client({ name: "c", version: "0" });
    await client.connect(b);
    return { client, store };
  }

  test("NotExecuted frees the key; any other throw leaves it in doubt", async () => {
    let mode = "refuse";
    const { client, store } = await lowLevel(() => {
      if (mode === "refuse") throw new NotExecuted("invalid input");
      if (mode === "boom") throw new Error("gateway timed out");
      return { content: [{ type: "text", text: "done" }] };
    });
    const keyed = (key: string) => client.callTool({ name: "op", arguments: {}, _meta: { [KEY]: key } });
    await assert.rejects(keyed("a"), (e) => errorOutcome(e) === Outcome.NOT_EXECUTED);
    assert.equal(store.stateOf("a"), undefined);
    mode = "boom";
    await assert.rejects(keyed("b"), (e) => errorOutcome(e) === Outcome.IN_DOUBT);
    mode = "ok";
    await assert.rejects(keyed("b"), (e) => errorOutcome(e) === Outcome.IN_DOUBT); // never re-run
    assert.equal(outcomeOf(await keyed("a")), Outcome.EXECUTED);
  });

  test("an input_required result passes through unrecorded and frees the key", async () => {
    const store = new IdempotencyStore(join(tmp(), "keys.db"));
    const keyed = new KeyedTools(store).twoPhase("op");
    const ask = { resultType: "input_required", inputRequests: { q: {} } };
    const wrapped = keyed.wrapCallHandler(async () => ask);
    const request = { method: "tools/call", params: { name: "op", arguments: {}, _meta: { [KEY]: "k" } } };
    assert.deepEqual(await wrapped(request, {}), ask);
    assert.equal(store.stateOf("k"), undefined);
  });
});
