/**
 * For the cross-language tests (tests/examples/test_typescript_adapter.py):
 *   contract_probe.ts contract              -> the wire contract as JSON
 *   contract_probe.ts fingerprints < rows   -> one fingerprint per {"tool","args"} JSON line
 *   contract_probe.ts store <db> <scope> <op> [key] [json]
 *       ops: begin-complete | replay | state | hold-exclusive <seconds>
 */

import { readFileSync } from "node:fs";

import {
  IDEMPOTENCY_CAPABILITY,
  IDEMPOTENCY_KEY,
  IDEMPOTENCY_OUTCOME,
  IdempotencyStore,
  KeyedExecutor,
  Outcome,
  VERSION,
  canonicalJson,
  errorCode,
  errorMessage,
  fingerprint,
  isNotExecutedOutcome,
} from "../../src/index.ts";

const [command, ...rest] = process.argv.slice(2);

if (command === "contract") {
  const outcomes = Object.values(Outcome);
  process.stdout.write(
    JSON.stringify({
      capability: IDEMPOTENCY_CAPABILITY,
      key: IDEMPOTENCY_KEY,
      outcome: IDEMPOTENCY_OUTCOME,
      version: VERSION,
      outcomes,
      errors: Object.fromEntries(
        outcomes.filter(isNotExecutedOutcome).map((o) => [o, { code: errorCode(o), message: errorMessage(o) }]),
      ),
    }),
  );
} else if (command === "fingerprints") {
  const rows = readFileSync(0, "utf8").split("\n").filter(Boolean);
  for (const row of rows) {
    const { tool, args } = JSON.parse(row) as { tool: string; args: unknown };
    process.stdout.write(`${JSON.stringify({ fingerprint: fingerprint(tool, args), canonical: canonicalJson([tool, args]) })}\n`);
  }
} else if (command === "store") {
  const [db = "", scope = "", op = "", key = "", json = "null"] = rest;
  if (op === "hold-exclusive") {
    const store = new IdempotencyStore(db, { scope, exclusive: true, timeoutSeconds: 0.5 });
    process.stdout.write("held\n");
    setTimeout(() => store.close(), Number(key) * 1000);
  } else {
    const store = new IdempotencyStore(db, { scope });
    const executor = new KeyedExecutor(store);
    const args = JSON.parse(json) as unknown;
    if (op === "begin-complete") {
      const out = await executor.execute(key, "pay", args, () => ({ receipt: `ts-${key}` }));
      process.stdout.write(JSON.stringify(out));
    } else if (op === "replay") {
      const out = await executor.execute(key, "pay", args, () => {
        throw new Error("must not run");
      });
      process.stdout.write(JSON.stringify(out));
    } else if (op === "state") {
      process.stdout.write(JSON.stringify({ state: store.stateOf(key) ?? null, lockPath: store.lockPath }));
    }
  }
}
