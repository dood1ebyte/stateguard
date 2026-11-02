/**
 * End to end: the TypeScript reference server as a real process, the official
 * SDK client over stdio, real process deaths (SIGKILL at a chosen point), a
 * restart, and a resend with the same key. Ground truth is the server's own
 * effects, read directly: order rows, ledger lines, outbox lines, payments.
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { after, before, describe, test } from "node:test";

import { KEY, connect, errorOutcome, outcomeOf, tmp } from "./helpers.ts";

const lines = (path: string): string[] => (existsSync(path) ? readFileSync(path, "utf8").split("\n").slice(0, -1) : []);
const entries = (path: string): string[] => lines(path).map((line) => line.split("\t")[0] ?? "");

function orders(db: string, item: string): number {
  const conn = new DatabaseSync(db);
  try {
    return (conn.prepare("SELECT COUNT(*) AS n FROM orders WHERE item = ?").get(item) as { n: number }).n;
  } finally {
    conn.close();
  }
}

/** A downstream payment service that honours idempotency keys (native idempotency). */
const receipts = new Map<string, string>();
let applied = 0;
const payments = createServer((req, res) => {
  let body = "";
  req.on("data", (chunk) => (body += chunk));
  req.on("end", () => {
    const lookup = req.url?.match(/^\/payments\/(.+)$/);
    if (req.method === "GET" && lookup) {
      const receipt = receipts.get(decodeURIComponent(lookup[1] ?? ""));
      res.writeHead(receipt ? 200 : 404, { "content-type": "application/json" });
      res.end(JSON.stringify(receipt ? { receipt } : {}));
      return;
    }
    const key = String(req.headers["idempotency-key"]);
    let receipt = receipts.get(key);
    const replayed = receipt !== undefined;
    if (receipt === undefined) {
      receipt = `r-${++applied}`; // the effect: one charge per key
      receipts.set(key, receipt);
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ receipt, replayed, body: JSON.parse(body || "{}") }));
  });
});
let paymentsUrl = "";
before(async () => {
  await new Promise<void>((done) => payments.listen(0, "127.0.0.1", done));
  paymentsUrl = `http://127.0.0.1:${(payments.address() as AddressInfo).port}`;
});
after(() => payments.close());

function serverArgs(dir: string, ...extra: string[]): string[] {
  return [
    "--db", join(dir, "orders.db"),
    "--ledger", join(dir, "ledger.txt"),
    "--outbox", join(dir, "outbox.txt"),
    "--payments", paymentsUrl,
    "--fault-file", join(dir, "fault.fired"),
    ...extra,
  ];
}

async function callOnce(dir: string, extra: string[], name: string, args: Record<string, unknown>, key?: string) {
  const client = await connect(serverArgs(dir, ...extra));
  try {
    return { ok: true as const, result: await client.callTool({ name, arguments: args, ...(key ? { _meta: { [KEY]: key } } : {}) }) };
  } catch (error) {
    return { ok: false as const, error };
  } finally {
    await client.close().catch(() => {});
  }
}

/** First call meets the fault (the server dies); the supervisor restarts it; the client resends. */
async function crashThenResend(dir: string, point: string, name: string, args: Record<string, unknown>, extra: string[] = [], key = "client-key") {
  const first = await callOnce(dir, [...extra, "--fault", point], name, args, key);
  assert.equal(first.ok, false, "the first call dies with the server");
  assert.ok(existsSync(join(dir, "fault.fired")), "the fault fired");
  return callOnce(dir, extra, name, args, key); // the fault fires once per file
}

describe("transactional create_order: exactly one order at every crash point", () => {
  for (const point of ["before_begin", "after_begin", "mid_effect", "after_effect", "after_record"]) {
    test(`death at ${point}`, async () => {
      const dir = tmp();
      const resent = await crashThenResend(dir, point, "create_order", { item: "widget", qty: 2 });
      assert.ok(resent.ok, String(!resent.ok && resent.error));
      assert.equal(outcomeOf(resent.result), point === "after_record" ? "replayed" : "executed");
      assert.equal(orders(join(dir, "orders.db"), "widget"), 1);
    });
  }
});

describe("two-phase append_ledger with recovery: exactly one line at every crash point", () => {
  const expected: Record<string, string> = {
    before_begin: "executed",
    after_begin: "executed", // abandoned, the key's line absent: executed now, once
    mid_effect: "executed", // the torn half line is cut at startup, then absent
    after_effect: "recovered", // the key's own line is there: recorded from it
    after_record: "replayed",
  };
  for (const [point, outcome] of Object.entries(expected)) {
    test(`death at ${point}: ${outcome}`, async () => {
      const dir = tmp();
      const resent = await crashThenResend(dir, point, "append_ledger", { entry: "pay-7" });
      assert.ok(resent.ok, String(!resent.ok && resent.error));
      assert.equal(outcomeOf(resent.result), outcome);
      assert.ok(readFileSync(join(dir, "ledger.txt"), "utf8").endsWith("\n"), "nothing torn is left");
      assert.deepEqual(entries(join(dir, "ledger.txt")), ["pay-7"]);
    });
  }

  test("without recovery, a death after the effect stays in doubt: never re-run, never a duplicate", async () => {
    const dir = tmp();
    const resent = await crashThenResend(dir, "after_effect", "append_ledger", { entry: "pay-8" }, ["--no-recover"]);
    assert.equal(resent.ok, false);
    assert.equal(errorOutcome(!resent.ok && resent.error), "in_doubt");
    assert.deepEqual(entries(join(dir, "ledger.txt")), ["pay-8"]);
  });

  test("a restarted server replays its persisted result", async () => {
    const dir = tmp();
    const first = await callOnce(dir, [], "append_ledger", { entry: "persist" }, "k-persist");
    const again = await callOnce(dir, [], "append_ledger", { entry: "persist" }, "k-persist");
    assert.ok(first.ok && again.ok);
    assert.equal(outcomeOf(again.result), "replayed");
    assert.deepEqual(again.result.content, first.result.content);
    assert.deepEqual(entries(join(dir, "ledger.txt")), ["persist"]);
  });

  test("a hang after recording: the client times out, resends, and the record answers", async () => {
    const dir = tmp();
    const client = await connect(serverArgs(dir, "--fault", "hang_after_record"));
    try {
      const keyed = { name: "append_ledger", arguments: { entry: "hang" }, _meta: { [KEY]: "k-hang" } };
      await assert.rejects(client.callTool(keyed, undefined, { timeout: 500 }));
      const resent = await client.callTool(keyed);
      assert.equal(outcomeOf(resent), "replayed");
    } finally {
      await client.close().catch(() => {});
    }
    assert.deepEqual(entries(join(dir, "ledger.txt")), ["hang"]);
  });
});

describe("native idempotency: charge, through a payment service that honours the key", () => {
  for (const [point, outcome] of [
    ["after_begin", "executed"], // the service never saw it: absent, charged now
    ["after_effect", "recovered"], // the service charged it: its record of the key answers
    ["after_record", "replayed"],
  ] as const) {
    test(`death at ${point}: ${outcome}, one charge`, async () => {
      const dir = tmp();
      const key = `pay-${point}-${Date.now()}`;
      const before = applied;
      const resent = await crashThenResend(dir, point, "charge", { account: "acme", amount: 10 }, [], key);
      assert.ok(resent.ok, String(!resent.ok && resent.error));
      assert.equal(outcomeOf(resent.result), outcome);
      assert.equal(applied - before, 1, "exactly one charge downstream");
      assert.equal(receipts.has(key), true);
    });
  }
});

describe("an external effect without native idempotency: send_notice", () => {
  test("a death after the effect stays in doubt; repeated resends never re-send it", async () => {
    const dir = tmp();
    const resent = await crashThenResend(dir, "after_effect", "send_notice", { to: "ops@example.invalid" });
    assert.equal(resent.ok, false);
    assert.equal(errorOutcome(!resent.ok && resent.error), "in_doubt");
    for (let i = 0; i < 2; i++) {
      const again = await callOnce(dir, [], "send_notice", { to: "ops@example.invalid" }, "client-key");
      assert.equal(errorOutcome(!again.ok && again.error), "in_doubt");
    }
    assert.deepEqual(lines(join(dir, "outbox.txt")), ["ops@example.invalid"]); // sent once; not claimed exactly-once
  });
});

describe("negative control: a server that does not adopt the contract", () => {
  test("a blind resend after a death after the effect duplicates it", async () => {
    const dir = tmp();
    const resent = await crashThenResend(dir, "after_effect", "append_ledger", { entry: "dup" }, ["--no-advertise"]);
    assert.ok(resent.ok);
    assert.equal(outcomeOf(resent.result), undefined); // no keys honoured
    assert.deepEqual(entries(join(dir, "ledger.txt")), ["dup", "dup"]); // the hazard the contract removes
  });

  test("the non-adopting server advertises nothing", async () => {
    const client = await connect(serverArgs(tmp(), "--no-advertise"));
    try {
      const tools = (await client.listTools()).tools;
      assert.ok(tools.every((t) => t._meta?.["io.stateguard/idempotency"] === undefined));
    } finally {
      await client.close();
    }
  });
});
