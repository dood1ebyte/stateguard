/**
 * The store and executor: the contract's semantics, and its crash windows
 * with real process deaths (a child SIGKILLs itself at a chosen point).
 * Ground truth is the effect file or the effect table, read directly.
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { join } from "node:path";
import { describe, test } from "node:test";

import {
  BeginStatus,
  IdempotencyStore,
  KeyedExecutor,
  NotExecuted,
  Outcome,
  Recovery,
  StoreUnavailable,
  executionFor,
  fingerprint,
} from "../src/index.ts";
import { CRASH_CHILD, runChild, tmp } from "./helpers.ts";

const FP = fingerprint("pay", { amount: 5 });
const lines = (path: string): string[] => (existsSync(path) ? readFileSync(path, "utf8").split("\n") : []);

function payments(db: string): number {
  const conn = new DatabaseSync(db);
  try {
    return (conn.prepare("SELECT COUNT(*) AS n FROM payments").get() as { n: number }).n;
  } catch {
    return 0;
  } finally {
    conn.close();
  }
}

/** A restarted server (a new process's instance) receives the resend. */
async function resend(dir: string, options: { recover?: boolean; checks?: string[] } = {}) {
  const effects = join(dir, "effects.txt");
  const ran: number[] = [];
  if (options.recover) {
    const store = new IdempotencyStore(join(dir, "keys.db"), { exclusive: true });
    if (existsSync(effects)) {
      const data = readFileSync(effects);
      writeFileSync(effects, data.subarray(0, data.lastIndexOf(0x0a) + 1)); // cut a torn line
    }
    try {
      const execution = await new KeyedExecutor(store).execute(
        "k1",
        "pay",
        { amount: 5 },
        () => {
          ran.push(1);
          writeFileSync(effects, "paid\tk1\n", { flag: "a" });
          return { paid: 5 };
        },
        {
          recover: () => {
            options.checks?.push("checked");
            return lines(effects).slice(0, -1).includes("paid\tk1") ? Recovery.found({ paid: 5 }) : Recovery.absent();
          },
        },
      );
      return { outcome: execution.outcome, ran };
    } finally {
      store.close();
    }
  }
  const store = new IdempotencyStore(join(dir, "keys.db"));
  const execution = await new KeyedExecutor(store).execute("k1", "pay", { amount: 5 }, () => {
    ran.push(1);
    writeFileSync(effects, "paid\n", { flag: "a" });
    return { paid: 5 };
  });
  return { outcome: execution.outcome, ran };
}

function crashChild(dir: string, mode: string, point: string): void {
  const out = runChild(CRASH_CHILD, [join(dir, "keys.db"), join(dir, "effects.txt"), mode, point, "k1"]);
  assert.equal(out.signal, "SIGKILL", `the child really died at ${point}: ${out.stderr}`);
}

describe("crash windows (real process deaths)", () => {
  // point -> [resend outcome, resend executes?, complete "paid" lines after]
  const twoPhase: Record<string, [string, boolean, number]> = {
    before_begin: [Outcome.EXECUTED, true, 1], // nothing claimed: the resend is the execution
    after_begin: [Outcome.IN_DOUBT, false, 0], // claimed, never acted: conservatively unknown
    mid_effect: [Outcome.IN_DOUBT, false, 0], // half an effect: unknown, never re-run
    after_effect: [Outcome.IN_DOUBT, false, 1], // the documented window: acted, unknown
    after_record: [Outcome.REPLAYED, false, 1], // recorded: replayed, not re-run
  };
  for (const [point, [expected, executes, count]] of Object.entries(twoPhase)) {
    test(`two-phase, death at ${point}: ${expected}, never a duplicate`, async () => {
      const dir = tmp();
      crashChild(dir, "two_phase", point);
      const { outcome, ran } = await resend(dir);
      assert.equal(outcome, expected);
      assert.equal(ran.length > 0, executes);
      assert.equal(lines(join(dir, "effects.txt")).filter((l) => l === "paid").length, count);
    });
  }

  const recovered: Record<string, [string, boolean]> = {
    before_begin: [Outcome.EXECUTED, true],
    after_begin: [Outcome.EXECUTED, true], // abandoned, the key's line absent: executed once
    mid_effect: [Outcome.EXECUTED, true], // the torn line cut, then absent
    after_effect: [Outcome.RECOVERED, false], // the key's own line: recorded from it
    after_record: [Outcome.REPLAYED, false],
  };
  for (const [point, [expected, executes]] of Object.entries(recovered)) {
    test(`two-phase with recovery, death at ${point}: ${expected}, exactly one effect`, async () => {
      const dir = tmp();
      crashChild(dir, "marked", point);
      const { outcome, ran } = await resend(dir, { recover: true });
      assert.equal(outcome, expected);
      assert.equal(ran.length > 0, executes);
      assert.equal(readFileSync(join(dir, "effects.txt"), "utf8"), "paid\tk1\n");
      const checks: string[] = [];
      assert.equal((await resend(dir, { recover: true, checks })).outcome, Outcome.REPLAYED);
      assert.deepEqual(checks, []); // settled for good
    });
  }

  for (const point of ["before_begin", "after_begin", "mid_effect", "after_effect", "after_record"]) {
    test(`transactional, death at ${point}: exactly one payment`, () => {
      const dir = tmp();
      crashChild(dir, "transactional", point);
      const store = new IdempotencyStore(join(dir, "keys.db"));
      let ran = 0;
      const execution = store.transact("k1", FP, (tx) => {
        ran++;
        tx.prepare("INSERT INTO payments (amount) VALUES (5)").run();
        return { paid: 5 };
      }, { tool: "pay" });
      assert.equal(execution.outcome, point === "after_record" ? Outcome.REPLAYED : Outcome.EXECUTED);
      assert.equal(ran, point === "after_record" ? 0 : 1);
      assert.equal(payments(join(dir, "keys.db")), 1);
    });
  }

  test("a death during recovery leaves the key abandoned again", async () => {
    const dir = tmp();
    const store = new IdempotencyStore(join(dir, "keys.db"));
    assert.equal(store.begin("k1", FP).status, BeginStatus.EXECUTE);
    store.markInDoubt("k1", "cut off");
    crashChild(dir, "recover_crash", "none");
    assert.equal(store.stateOf("k1"), "started");
    const { outcome, ran } = await resend(dir, { recover: true });
    assert.equal(outcome, Outcome.EXECUTED);
    assert.deepEqual(ran, [1]);
  });

  test("a dead non-exclusive owner is never presumed dead", async () => {
    const dir = tmp();
    crashChild(dir, "two_phase", "after_begin");
    const checks: string[] = [];
    const { outcome, ran } = await resend(dir, { recover: true, checks });
    assert.equal(outcome, Outcome.IN_DOUBT);
    assert.deepEqual(ran, []);
    assert.deepEqual(checks, []);
  });
});

describe("the store contract", () => {
  test("duplicates with one key run once and replay the recorded result", async () => {
    const executor = new KeyedExecutor(new IdempotencyStore(join(tmp(), "keys.db")));
    let runs = 0;
    const first = await executor.execute("k", "pay", { amount: 5 }, () => ({ receipt: ++runs }));
    const again = await executor.execute("k", "pay", { amount: 5 }, () => ({ receipt: ++runs }));
    assert.equal(first.outcome, Outcome.EXECUTED);
    assert.equal(again.outcome, Outcome.REPLAYED);
    assert.deepEqual(again.result, { receipt: 1 });
    assert.equal(runs, 1);
  });

  test("concurrent duplicates in one process run once; the rest wait and replay", async () => {
    const executor = new KeyedExecutor(new IdempotencyStore(join(tmp(), "keys.db")), { waitSeconds: 5 });
    let runs = 0;
    const slow = async () => {
      runs++;
      await new Promise((done) => setTimeout(done, 200));
      return { paid: 5 };
    };
    const outcomes = await Promise.all([1, 2, 3, 4].map(() => executor.execute("k", "pay", { amount: 5 }, slow)));
    assert.equal(runs, 1);
    assert.deepEqual(outcomes.map((e) => e.outcome).sort(), [Outcome.EXECUTED, Outcome.REPLAYED, Outcome.REPLAYED, Outcome.REPLAYED].sort());
  });

  test("concurrent duplicates across instances: one executes, the other is in doubt", () => {
    const dir = tmp();
    const a = new IdempotencyStore(join(dir, "keys.db"));
    const b = new IdempotencyStore(join(dir, "keys.db"));
    assert.equal(a.begin("k", FP).status, BeginStatus.EXECUTE);
    assert.equal(a.begin("k", FP).status, BeginStatus.IN_PROGRESS); // the same instance: in progress
    assert.equal(b.begin("k", FP).status, BeginStatus.IN_DOUBT); // another: cannot tell live from dead
  });

  test("concurrent calls with different keys each execute once", async () => {
    const executor = new KeyedExecutor(new IdempotencyStore(join(tmp(), "keys.db")));
    const effects: string[] = [];
    const results = await Promise.all(
      ["k1", "k2", "k3", "k4", "k5"].map((key) =>
        executor.execute(key, "pay", { amount: 5 }, async () => {
          await new Promise((done) => setTimeout(done, 50));
          effects.push(key);
          return { key };
        }),
      ),
    );
    assert.deepEqual(results.map((e) => e.outcome), Array(5).fill(Outcome.EXECUTED));
    assert.deepEqual(effects.sort(), ["k1", "k2", "k3", "k4", "k5"]);
  });

  test("a key reused with other arguments or another tool is rejected, nothing runs", async () => {
    const executor = new KeyedExecutor(new IdempotencyStore(join(tmp(), "keys.db")));
    await executor.execute("k", "pay", { amount: 5 }, () => 1);
    for (const [tool, args] of [["pay", { amount: 6 }], ["refund", { amount: 5 }]] as const) {
      const rejected = await executor.execute("k", tool, args, () => assert.fail("ran"));
      assert.equal(rejected.outcome, Outcome.REJECTED_MISMATCH);
    }
  });

  test("a restarted server replays a persisted result", async () => {
    const db = join(tmp(), "keys.db");
    await new KeyedExecutor(new IdempotencyStore(db)).execute("k", "pay", { amount: 5 }, () => ({ receipt: 7 }));
    const replay = await new KeyedExecutor(new IdempotencyStore(db)).execute("k", "pay", { amount: 5 }, () => assert.fail("ran"));
    assert.equal(replay.outcome, Outcome.REPLAYED);
    assert.deepEqual(replay.result, { receipt: 7 });
  });

  test("an unreachable store cannot be opened (fails closed)", () => {
    assert.throws(() => new IdempotencyStore(join(tmp(), "missing", "dir", "keys.db")), StoreUnavailable);
  });

  test("a corrupted store executes nothing", async () => {
    const dir = tmp();
    const store = new IdempotencyStore(join(dir, "keys.db"));
    for (const suffix of ["", "-wal", "-shm"]) {
      if (existsSync(join(dir, `keys.db${suffix}`))) writeFileSync(join(dir, `keys.db${suffix}`), Buffer.alloc(4096, 7));
    }
    const out = await new KeyedExecutor(store).execute("k", "pay", { amount: 5 }, () => assert.fail("ran"));
    assert.equal(out.outcome, Outcome.REJECTED_STORE);
  });

  test("a store locked past its timeout executes nothing", async () => {
    const dir = tmp();
    const store = new IdempotencyStore(join(dir, "keys.db"), { timeoutSeconds: 0.2 });
    const holder = new DatabaseSync(join(dir, "keys.db"));
    holder.exec("BEGIN EXCLUSIVE");
    try {
      const out = await new KeyedExecutor(store).execute("k", "pay", { amount: 5 }, () => assert.fail("ran"));
      assert.equal(out.outcome, Outcome.REJECTED_STORE);
    } finally {
      holder.exec("ROLLBACK");
      holder.close();
    }
  });

  test("a result that cannot be recorded leaves the key in doubt, never free", async () => {
    const dir = tmp();
    const store = new IdempotencyStore(join(dir, "keys.db"));
    const out = await new KeyedExecutor(store).execute("k", "pay", { amount: 5 }, () => {
      // The store is corrupted after the effect, before its record.
      writeFileSync(join(dir, "keys.db"), Buffer.alloc(4096, 9));
      return { paid: 5 };
    });
    assert.equal(out.outcome, Outcome.EXECUTED_UNRECORDED);
    assert.throws(() => store.stateOf("k"), StoreUnavailable); // nothing claims it is free
  });

  test("a handler that proves it did nothing frees the key; any other failure leaves it in doubt", async () => {
    const store = new IdempotencyStore(join(tmp(), "keys.db"));
    const executor = new KeyedExecutor(store);
    const rejected = await executor.execute("a", "pay", { amount: 5 }, () => {
      throw new NotExecuted("invalid amount");
    });
    assert.equal(rejected.outcome, Outcome.NOT_EXECUTED);
    assert.equal((await executor.execute("a", "pay", { amount: 5 }, () => 1)).outcome, Outcome.EXECUTED);
    const failed = await executor.execute("b", "pay", { amount: 5 }, () => {
      throw new Error("gateway timed out");
    });
    assert.equal(failed.outcome, Outcome.IN_DOUBT);
    assert.equal(store.stateOf("b"), "in_doubt");
    for (let i = 0; i < 3; i++) {
      assert.equal((await executor.execute("b", "pay", { amount: 5 }, () => assert.fail("ran"))).outcome, Outcome.IN_DOUBT);
    }
  });

  test("operator release and resolve are the ways out of doubt", async () => {
    const store = new IdempotencyStore(join(tmp(), "keys.db"));
    const executor = new KeyedExecutor(store);
    const boom = () => {
      throw new Error("cut off");
    };
    await executor.execute("a", "pay", { amount: 5 }, boom);
    await executor.execute("b", "pay", { amount: 5 }, boom);
    assert.deepEqual(store.purge(), { expired: 0, deleted: 0 }); // doubt never expires
    assert.equal(store.release("a"), true);
    assert.equal((await executor.execute("a", "pay", { amount: 5 }, () => 1)).outcome, Outcome.EXECUTED);
    assert.equal(store.resolve("b", { receipt: 7 }), true);
    const replay = await executor.execute("b", "pay", { amount: 5 }, () => assert.fail("ran"));
    assert.deepEqual([replay.outcome, replay.result], [Outcome.REPLAYED, { receipt: 7 }]);
  });

  test("expiry, tombstones and the documented reuse horizon", async () => {
    let now = 1000;
    const store = new IdempotencyStore(join(tmp(), "keys.db"), { retentionSeconds: 10, tombstoneSeconds: 100, clock: () => now });
    const executor = new KeyedExecutor(store);
    assert.equal((await executor.execute("k", "pay", { amount: 5 }, () => 1)).outcome, Outcome.EXECUTED);
    now += 11; // past retention: executed earlier, result gone
    assert.equal((await executor.execute("k", "pay", { amount: 5 }, () => assert.fail("ran"))).outcome, Outcome.EXPIRED);
    assert.deepEqual(store.purge(), { expired: 1, deleted: 0 });
    assert.equal(store.stateOf("k"), "expired");
    now += 100; // past the tombstone horizon: the key is forgotten
    assert.deepEqual(store.purge(), { expired: 0, deleted: 1 });
    // Documented hazard: a key reused after the horizon executes as new.
    assert.equal((await executor.execute("k", "pay", { amount: 5 }, () => 2)).outcome, Outcome.EXECUTED);
  });

  test("scopes are separate: the same key in another scope is another key", async () => {
    const db = join(tmp(), "keys.db");
    const a = new KeyedExecutor(new IdempotencyStore(db, { scope: "tenant-a" }));
    const b = new KeyedExecutor(new IdempotencyStore(db, { scope: "tenant-b" }));
    assert.equal((await a.execute("k", "pay", { amount: 5 }, () => 1)).outcome, Outcome.EXECUTED);
    assert.equal((await b.execute("k", "pay", { amount: 5 }, () => 2)).outcome, Outcome.EXECUTED);
    assert.equal((await b.execute("k", "pay", { amount: 6 }, () => 3)).outcome, Outcome.REJECTED_MISMATCH);
    assert.equal((await a.execute("k", "pay", { amount: 5 }, () => 4)).outcome, Outcome.REPLAYED);
  });

  test("retention must not exceed the tombstone horizon", () => {
    assert.throws(() => new IdempotencyStore(join(tmp(), "keys.db"), { retentionSeconds: 10, tombstoneSeconds: 5 }), RangeError);
  });

  test("an exclusive store has one live instance; another scope is another lock", () => {
    const db = join(tmp(), "keys.db");
    const first = new IdempotencyStore(db, { exclusive: true });
    assert.throws(() => new IdempotencyStore(db, { exclusive: true, timeoutSeconds: 0.2 }), /exclusively/);
    new IdempotencyStore(db, { scope: "other", exclusive: true }).close();
    assert.equal(first.begin("k", FP).status, BeginStatus.EXECUTE);
    assert.throws(() => first.close(), /still executing/);
    first.complete("k", 1);
    first.close();
    new IdempotencyStore(db, { exclusive: true, timeoutSeconds: 0.2 }).close();
  });

  test("adopt has one taker and never takes a live attempt", () => {
    const store = new IdempotencyStore(join(tmp(), "keys.db"), { exclusive: true });
    assert.equal(store.adopt("k", FP).status, BeginStatus.EXECUTE);
    assert.equal(store.adopt("k", FP).status, BeginStatus.IN_PROGRESS);
    store.markInDoubt("k", "handler raised");
    assert.equal(store.adopt("k", fingerprint("pay", { amount: 6 })).status, BeginStatus.MISMATCH);
    assert.equal(store.adopt("k", FP).status, BeginStatus.RECOVER);
    assert.equal(store.adopt("k", FP).status, BeginStatus.IN_PROGRESS);
    assert.equal(new IdempotencyStore(store.path).adopt("k", FP).status, BeginStatus.IN_DOUBT);
    store.complete("k", { paid: 5 });
    assert.equal(store.adopt("k", FP).status, BeginStatus.REPLAY);
    store.close();
  });

  for (const [name, verdict, outcome, runs, state] of [
    ["found", () => Recovery.found({ receipt: 9 }), Outcome.RECOVERED, 0, "done"],
    ["absent", () => Recovery.absent(), Outcome.EXECUTED, 1, "done"],
    ["rerun (declared idempotent)", () => Recovery.rerun(), Outcome.EXECUTED, 1, "done"],
    ["unknown", () => Recovery.unknown("cannot tell"), Outcome.IN_DOUBT, 0, "in_doubt"],
    ["a failing check", () => { throw new Error("boom"); }, Outcome.IN_DOUBT, 0, "in_doubt"],
    ["not a Recovery", () => "yes" as unknown as Recovery, Outcome.IN_DOUBT, 0, "in_doubt"],
  ] as const) {
    test(`a recovery check that says ${name}`, async () => {
      const store = new IdempotencyStore(join(tmp(), "keys.db"));
      const executor = new KeyedExecutor(store);
      await executor.execute("k", "pay", { amount: 5 }, () => {
        throw new Error("cut off");
      });
      let ran = 0;
      const run = () => {
        ran++;
        return { paid: 5 };
      };
      const settled = await executor.execute("k", "pay", { amount: 5 }, run, { recover: verdict });
      assert.equal(settled.outcome, outcome);
      assert.equal(ran, runs);
      assert.equal(store.stateOf("k"), state);
      if (state === "in_doubt") {
        const again = await executor.execute("k", "pay", { amount: 5 }, run, { recover: verdict });
        assert.equal(again.outcome, Outcome.IN_DOUBT); // re-checked, never run
        assert.equal(ran, 0);
      }
    });
  }

  test("concurrent resends of an abandoned key check and run once", async () => {
    const store = new IdempotencyStore(join(tmp(), "keys.db"));
    const executor = new KeyedExecutor(store, { waitSeconds: 5 });
    assert.equal(store.begin("k", FP).status, BeginStatus.EXECUTE);
    store.markInDoubt("k", "cut off");
    let checks = 0;
    let runs = 0;
    const outcomes = await Promise.all(
      [1, 2, 3, 4].map(() =>
        executor.execute("k", "pay", { amount: 5 }, () => ({ run: ++runs }), {
          recover: async () => {
            checks++;
            await new Promise((done) => setTimeout(done, 150));
            return Recovery.absent();
          },
        }),
      ),
    );
    assert.equal(checks, 1);
    assert.equal(runs, 1);
    assert.deepEqual(outcomes.map((e) => e.outcome).sort(), [Outcome.EXECUTED, Outcome.REPLAYED, Outcome.REPLAYED, Outcome.REPLAYED].sort());
  });

  test("transactional: the effect's own failure rolls back; an async effect is refused", () => {
    const store = new IdempotencyStore(join(tmp(), "keys.db"));
    const conn = store.connect();
    conn.exec("CREATE TABLE payments (id INTEGER PRIMARY KEY, amount INT)");
    conn.close();
    const failed = store.transact("k", FP, (tx) => {
      tx.prepare("INSERT INTO payments (amount) VALUES (5)").run();
      throw new Error("declined");
    });
    assert.equal(failed.outcome, Outcome.ROLLED_BACK);
    const asynchronous = store.transact("k2", FP, async () => ({ paid: 5 }));
    assert.equal(asynchronous.outcome, Outcome.ROLLED_BACK);
    assert.equal(payments(store.path), 0);
    assert.equal(store.stateOf("k"), undefined); // the key is free again
  });

  test("a claimed key is not an outcome", () => {
    for (const status of [BeginStatus.EXECUTE, BeginStatus.RECOVER]) {
      assert.throws(() => executionFor({ status }), RangeError);
    }
  });
});
