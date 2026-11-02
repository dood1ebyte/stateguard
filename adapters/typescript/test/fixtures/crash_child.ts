/**
 * A child that runs one keyed call and dies (SIGKILL, as a crash) at POINT.
 * argv: db effects mode point key
 *   mode two_phase      KeyedExecutor, effect line "paid\n"
 *        marked         exclusive store, effect line "paid\t<key>\n" (recoverable)
 *        transactional  store.transact, effect row in the store's database
 *        recover_crash  exclusive store: adopt the key and die inside the recovery check
 */

import { closeSync, fsyncSync, openSync, writeSync } from "node:fs";

import { IdempotencyStore, KeyedExecutor, fingerprint } from "../../src/index.ts";

const [db = "", effects = "", mode = "", point = "", key = ""] = process.argv.slice(2);
const crash = (here: string): void => {
  if (here === point) process.kill(process.pid, "SIGKILL");
};

function append(line: string, upTo?: number): void {
  const fd = openSync(effects, "a");
  const bytes = Buffer.from(line, "utf8");
  writeSync(fd, upTo === undefined ? bytes : bytes.subarray(0, upTo));
  fsyncSync(fd);
  closeSync(fd);
}

const store = new IdempotencyStore(db, { exclusive: mode === "marked" || mode === "recover_crash" });

if (mode === "recover_crash") {
  await new KeyedExecutor(store).execute(key, "pay", { amount: 5 }, () => 1, {
    recover: () => {
      process.kill(process.pid, "SIGKILL");
      throw new Error("unreachable");
    },
  });
} else if (mode === "two_phase" || mode === "marked") {
  const line = mode === "marked" ? `paid\t${key}\n` : "paid\n";
  crash("before_begin");
  const execution = await new KeyedExecutor(store).execute(key, "pay", { amount: 5 }, () => {
    crash("after_begin");
    if (point === "mid_effect") {
      append(line, Math.floor(line.length / 2));
      process.kill(process.pid, "SIGKILL");
    }
    append(line);
    crash("after_effect");
    return { paid: 5 };
  });
  crash("after_record");
  process.stdout.write(JSON.stringify({ outcome: execution.outcome }));
} else {
  const conn = store.connect();
  conn.exec("CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, amount INT)");
  conn.close();
  crash("before_begin");
  const execution = store.transact(key, fingerprint("pay", { amount: 5 }), (tx) => {
    crash("after_begin");
    tx.prepare("INSERT INTO payments (amount) VALUES (5)").run();
    crash("mid_effect");
    crash("after_effect");
    return { paid: 5 };
  }, { tool: "pay" });
  crash("after_record");
  process.stdout.write(JSON.stringify({ outcome: execution.outcome }));
}
