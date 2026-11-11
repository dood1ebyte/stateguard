/**
 * Two-phase execution of a keyed call, and recovery of abandoned attempts --
 * the contract's executor (Python: `KeyedExecutor`, `recover_abandoned`),
 * framework-free. The MCP SDK adapter (`sdk.ts`) builds on it.
 */

import { Outcome, fingerprint } from "./contract.ts";
import { BeginStatus, IdempotencyStore, StoreUnavailable, executionFor } from "./store.ts";
import type { Begin } from "./store.ts";

export interface Execution {
  readonly outcome: Outcome;
  /** The result (EXECUTED, EXECUTED_UNRECORDED, REPLAYED, RECOVERED). */
  readonly result: unknown;
  /** The handler's error (ROLLED_BACK, IN_DOUBT after a failure, NotExecuted). */
  readonly error?: unknown;
}

/**
 * Thrown by a keyed handler that can show it executed nothing (it failed
 * before acting). The key is freed; a resend may execute. Anything else a
 * handler throws leaves the key in doubt.
 */
export class NotExecuted extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NotExecuted";
  }
}

/**
 * What a server's recovery check found for an abandoned attempt of a key:
 * `found(result)`, `absent()`, `rerun(note)` or `unknown(note)`. The check is sound only
 * under the contract's two conditions -- the attempt is over (an exclusive
 * store), and the check looks for *this key's own* effect (the effect carries
 * the key, or a downstream that honours the key answers for it), never a
 * postcondition another request could have produced.
 */
export class Recovery {
  /**
   * true: this key's effect is present. false: run it now -- absent with nothing partial, or
   * (`rerun`) the operator declared the tool idempotent, so running it again leaves the same
   * state whether or not the attempt took effect. null: cannot tell.
   */
  readonly applied: boolean | null;
  /** For `found`: the result to record and answer (JSON). */
  readonly result: unknown;
  readonly note: string;

  private constructor(applied: boolean | null, result: unknown, note: string) {
    this.applied = applied;
    this.result = result;
    this.note = note;
  }

  static found(result: unknown): Recovery {
    return new Recovery(true, result, "");
  }

  static absent(): Recovery {
    return new Recovery(false, undefined, "");
  }

  /**
   * Cannot tell whether the attempt took effect, and need not: the operator declared the tool
   * idempotent. The declaration, not a check, is the evidence -- the operator's promise.
   */
  static rerun(note = "declared idempotent: run again"): Recovery {
    return new Recovery(false, undefined, note);
  }

  static unknown(note = ""): Recovery {
    return new Recovery(null, undefined, note);
  }
}

export type RecoveryCheck = () => Recovery | Promise<Recovery>;

function describe(error: unknown): string {
  return error instanceof Error ? `${error.name}: ${error.message}` : String(error);
}

function quietly(action: () => void): void {
  try {
    action();
  } catch (error) {
    // On a store error the key stays as it was: started, so in doubt -- never free.
    if (!(error instanceof StoreUnavailable)) throw error;
  }
}

/**
 * Settle a key `adopt` returned as RECOVER, from the server's *check*:
 * found -> recorded as done, RECOVERED; absent -> null: the caller executes
 * now (it holds the key) and completes it; cannot tell, the check threw, or it
 * returned no `Recovery` -> in doubt again, IN_DOUBT.
 */
export async function recoverAbandoned(
  store: IdempotencyStore,
  key: string,
  check: RecoveryCheck,
): Promise<Execution | null> {
  let found: unknown;
  try {
    found = await check();
  } catch (error) {
    quietly(() => store.markInDoubt(key, `recovery check failed: ${describe(error)}`));
    return { outcome: Outcome.IN_DOUBT, result: undefined, error };
  }
  if (found instanceof Recovery && found.applied === false) return null;
  if (found instanceof Recovery && found.applied === true) {
    // On a store error the key stays started and abandoned: recovered again next time.
    quietly(() => store.complete(key, found.result));
    return { outcome: Outcome.RECOVERED, result: found.result };
  }
  const note = found instanceof Recovery ? found.note : `not a Recovery: ${String(found)}`;
  quietly(() => store.markInDoubt(key, `recovery check could not tell: ${note}`));
  return { outcome: Outcome.IN_DOUBT, result: undefined };
}

/** Claim a key: begin; wait for a duplicate running here; adopt an abandoned one if recovering. */
export async function claim(
  store: IdempotencyStore,
  key: string,
  fp: string,
  tool: string,
  options: { waitSeconds: number; recovers: boolean },
): Promise<Begin> {
  let begun = store.begin(key, fp, { tool });
  if (begun.status === BeginStatus.IN_PROGRESS && (await store.wait(key, options.waitSeconds))) {
    begun = store.begin(key, fp, { tool });
  }
  if (begun.status === BeginStatus.IN_DOUBT && options.recovers) {
    begun = store.adopt(key, fp, { tool });
    // Another request is recovering it here: wait for its verdict, never a second check.
    if (begun.status === BeginStatus.IN_PROGRESS && (await store.wait(key, options.waitSeconds))) {
      begun = store.begin(key, fp, { tool });
    }
  }
  return begun;
}

/**
 * Two-phase execution of a keyed call over an `IdempotencyStore`: `run()` at
 * most once per (key, tool, arguments). See the contract for the crash windows.
 */
export class KeyedExecutor {
  readonly store: IdempotencyStore;
  readonly waitSeconds: number;

  constructor(store: IdempotencyStore, options: { waitSeconds?: number } = {}) {
    this.store = store;
    this.waitSeconds = options.waitSeconds ?? 5;
  }

  async execute(
    key: string,
    tool: string,
    args: unknown,
    run: () => unknown,
    options: { recover?: RecoveryCheck } = {},
  ): Promise<Execution> {
    const fp = fingerprint(tool, args);
    let begun: Begin;
    try {
      begun = await claim(this.store, key, fp, tool, {
        waitSeconds: this.waitSeconds,
        recovers: options.recover !== undefined,
      });
    } catch (error) {
      if (error instanceof StoreUnavailable) return { outcome: Outcome.REJECTED_STORE, result: undefined };
      throw error;
    }
    if (begun.status === BeginStatus.RECOVER && options.recover !== undefined) {
      const settled = await recoverAbandoned(this.store, key, options.recover);
      if (settled !== null) return settled;
    } else if (begun.status !== BeginStatus.EXECUTE) {
      return executionFor(begun);
    }
    let result: unknown;
    try {
      result = await run();
    } catch (error) {
      if (error instanceof NotExecuted) {
        quietly(() => this.store.abort(key));
        return { outcome: Outcome.NOT_EXECUTED, result: undefined, error };
      }
      quietly(() => this.store.markInDoubt(key, describe(error)));
      return { outcome: Outcome.IN_DOUBT, result: undefined, error };
    }
    try {
      this.store.complete(key, result);
    } catch (error) {
      if (error instanceof StoreUnavailable) return { outcome: Outcome.EXECUTED_UNRECORDED, result };
      throw error;
    }
    return { outcome: Outcome.EXECUTED, result };
  }
}

export { describe as describeError, quietly };
