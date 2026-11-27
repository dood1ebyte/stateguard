/**
 * The server side's durable record of keys: one SQLite file, the **same
 * schema and the same SQL** as Python's `IdempotencyStore`
 * (`stateguard.adapters.mcp.idempotency`), so a store file is readable,
 * releasable and resolvable from either language. Semantics, crash windows,
 * ownership and retention are the contract's; see
 * docs/runtime/SERVER_IDEMPOTENCY_CONTRACT.md and TYPESCRIPT_IDEMPOTENCY_ADAPTER.md.
 *
 * `node:sqlite`'s `DatabaseSync` is synchronous: every store call blocks the
 * event loop for its duration -- normally a short transaction, up to the busy
 * timeout when another process holds the write lock.
 */

import { createHash, randomUUID } from "node:crypto";
import { DatabaseSync } from "node:sqlite";

import type { Execution } from "./executor.ts";
import { Outcome } from "./contract.ts";

export const BeginStatus = {
  EXECUTE: "execute", // new key: recorded as started; execute, then complete()
  REPLAY: "replay", // done before: answer the recorded result
  IN_PROGRESS: "in_progress", // started by this instance and still executing
  IN_DOUBT: "in_doubt", // started, no result: execute nothing, ever
  EXPIRED: "expired", // done before, result no longer retained
  MISMATCH: "mismatch", // same key, other tool or arguments
  RECOVER: "recover", // adopt(): an abandoned attempt is now yours -- check before acting
} as const;
export type BeginStatus = (typeof BeginStatus)[keyof typeof BeginStatus];

export interface Begin {
  readonly status: BeginStatus;
  /** The recorded result, for REPLAY. */
  readonly result?: unknown;
}

/** The store cannot be read or written (unavailable, locked, corrupted). Fails closed. */
export class StoreUnavailable extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "StoreUnavailable";
  }
}

const STARTED = "started";
const DONE = "done";
const IN_DOUBT = "in_doubt";
const EXPIRED = "expired";
/** The owner-id prefix of an exclusive instance (shared with Python). */
const EXCLUSIVE_OWNER = "x-";

const SCHEMA =
  "CREATE TABLE IF NOT EXISTS stateguard_keys (" +
  " scope TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL," +
  " tool TEXT, state TEXT NOT NULL, owner TEXT, started_at REAL NOT NULL," +
  " finished_at REAL, result TEXT, note TEXT, PRIMARY KEY (scope, key))";

export interface StoreOptions {
  /** Keys are unique per scope (e.g. server name and tenant). */
  scope?: string;
  /** How long a done result is replayed (default 7 days). */
  retentionSeconds?: number;
  /** How long an expired key's tombstone is kept (default 30 days, >= retention). */
  tombstoneSeconds?: number;
  /** SQLite busy timeout, and how long an exclusive instance waits for a dying holder. */
  timeoutSeconds?: number;
  /** Seconds since the epoch (as Python's `time.time()`). */
  clock?: () => number;
  /**
   * Hold the scope exclusively: an SQLite EXCLUSIVE lock on `lockPath`, held
   * for this instance's lifetime and dropped by the kernel when the process
   * dies -- the same lock, on the same file, as Python's exclusive store.
   * Local to one host; not distributed coordination.
   */
  exclusive?: boolean;
}

interface Row {
  fingerprint: string;
  state: string;
  owner: string | null;
  finished_at: number | null;
  result: string | null;
}

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
}

function deferred(): Deferred {
  let resolve: () => void = () => {};
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function isStoreError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const code = (error as { code?: unknown }).code;
  // node:sqlite raises ERR_SQLITE_ERROR (and friends); the filesystem raises errno codes.
  return typeof code === "string" && (code.startsWith("ERR_SQLITE") || /^E[A-Z]+$/.test(code));
}

export class IdempotencyStore {
  readonly path: string;
  readonly scope: string;
  readonly retentionSeconds: number;
  readonly tombstoneSeconds: number;
  readonly exclusive: boolean;
  /** This instance's identity: whose started keys are "in progress", not "in doubt". */
  readonly owner: string;
  readonly #timeoutSeconds: number;
  readonly #clock: () => number;
  readonly #active = new Map<string, Deferred>();
  #lockDb: DatabaseSync | undefined;

  constructor(path: string, options: StoreOptions = {}) {
    const retention = options.retentionSeconds ?? 7 * 24 * 3600;
    const tombstone = options.tombstoneSeconds ?? 30 * 24 * 3600;
    if (!(retention > 0) || tombstone < retention) {
      throw new RangeError("need 0 < retentionSeconds <= tombstoneSeconds");
    }
    this.path = path;
    this.scope = options.scope ?? "default";
    this.retentionSeconds = retention;
    this.tombstoneSeconds = tombstone;
    this.#timeoutSeconds = options.timeoutSeconds ?? 5;
    this.#clock = options.clock ?? (() => Date.now() / 1000);
    this.exclusive = options.exclusive ?? false;
    if (this.exclusive) this.#holdExclusively();
    this.owner = (this.exclusive ? EXCLUSIVE_OWNER : "") + randomUUID().replaceAll("-", "");
    try {
      this.#run((db) => db.exec(SCHEMA));
    } catch (error) {
      this.close(); // an instance that never existed holds no lock
      throw error;
    }
  }

  // -- plumbing -----------------------------------------------------------------------

  /** The lock file an exclusive instance holds (one per store file and scope; Python's too). */
  get lockPath(): string {
    return `${this.path}.${createHash("sha256").update(this.scope, "utf8").digest("hex").slice(0, 16)}.lock`;
  }

  #holdExclusively(): void {
    const deadline = Date.now() + this.#timeoutSeconds * 1000;
    for (;;) {
      let db: DatabaseSync;
      try {
        db = new DatabaseSync(this.lockPath);
      } catch (error) {
        throw new StoreUnavailable(String((error as Error).message ?? error), { cause: error });
      }
      try {
        db.exec("PRAGMA busy_timeout = 50");
        db.exec("PRAGMA locking_mode=EXCLUSIVE");
        db.exec("BEGIN EXCLUSIVE");
        db.exec("COMMIT"); // the lock stays: exclusive locking mode
        this.#lockDb = db;
        return;
      } catch (error) {
        db.close();
        const message = String((error as Error).message ?? error);
        if (!/locked|busy/i.test(message)) throw new StoreUnavailable(message, { cause: error });
        if (Date.now() >= deadline) {
          throw new StoreUnavailable(`another live instance holds ${this.lockPath} exclusively`);
        }
        // A dying holder's lock goes with its process; wait a moment (blocking, as Python's).
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50);
      }
    }
  }

  /** Let go of the exclusive lock. Only when nothing is executing here. */
  close(): void {
    if (this.#active.size > 0) throw new Error("keys are still executing in this instance");
    this.#lockDb?.close();
    this.#lockDb = undefined;
  }

  /** A connection to the store's database (for `transact` effects and inspection). */
  connect(): DatabaseSync {
    const db = new DatabaseSync(this.path);
    db.exec(`PRAGMA busy_timeout = ${Math.round(this.#timeoutSeconds * 1000)}`);
    db.exec("PRAGMA journal_mode=WAL");
    db.exec("PRAGMA synchronous=FULL");
    return db;
  }

  #run<T>(work: (db: DatabaseSync) => T): T {
    let db: DatabaseSync;
    try {
      db = this.connect();
    } catch (error) {
      throw new StoreUnavailable(String((error as Error).message ?? error), { cause: error });
    }
    try {
      return work(db);
    } catch (error) {
      if (error instanceof StoreUnavailable || !isStoreError(error)) throw error;
      throw new StoreUnavailable(String((error as Error).message), { cause: error });
    } finally {
      db.close();
    }
  }

  #immediate<T>(db: DatabaseSync, work: () => T): T {
    db.exec("BEGIN IMMEDIATE");
    try {
      const out = work();
      db.exec("COMMIT");
      return out;
    } catch (error) {
      try {
        db.exec("ROLLBACK");
      } catch {
        // already rolled back
      }
      throw error;
    }
  }

  #row(db: DatabaseSync, key: string): Row | undefined {
    return db
      .prepare(
        "SELECT fingerprint, state, owner, finished_at, result FROM stateguard_keys WHERE scope = ? AND key = ?",
      )
      .get(this.scope, key) as Row | undefined;
  }

  #insertStarted(db: DatabaseSync, key: string, fp: string, tool: string | undefined): void {
    db.prepare(
      "INSERT INTO stateguard_keys (scope, key, fingerprint, tool, state, owner, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
    ).run(this.scope, key, fp, tool ?? null, STARTED, this.owner, this.#clock());
  }

  #claim(key: string): void {
    this.#active.set(key, deferred());
  }

  #finish(key: string): void {
    const waiting = this.#active.get(key);
    this.#active.delete(key);
    waiting?.resolve();
  }

  /** The attempt behind this row can no longer act (see the contract's "Recovery"). */
  #abandoned(key: string, state: string, owner: string | null): boolean {
    if (state === IN_DOUBT) return true; // marked after its handler failed
    if (state !== STARTED) return false;
    if (owner === this.owner) return !this.#active.has(key); // finished here; recording failed
    // Another instance's: only provably gone if it held the lock we now hold.
    return this.exclusive && typeof owner === "string" && owner.startsWith(EXCLUSIVE_OWNER);
  }

  #judge(key: string, fp: string, row: Row): Begin {
    if (row.fingerprint !== fp) return { status: BeginStatus.MISMATCH };
    if (row.state === DONE) {
      if (row.finished_at !== null && this.#clock() - row.finished_at > this.retentionSeconds) {
        return { status: BeginStatus.EXPIRED };
      }
      return { status: BeginStatus.REPLAY, result: JSON.parse(row.result ?? "null") };
    }
    if (row.state === EXPIRED) return { status: BeginStatus.EXPIRED };
    if (row.state === STARTED && row.owner === this.owner && this.#active.has(key)) {
      return { status: BeginStatus.IN_PROGRESS };
    }
    return { status: BeginStatus.IN_DOUBT };
  }

  // -- two-phase ----------------------------------------------------------------------

  /** Claim *key* (EXECUTE: committed as started, by this instance) or judge it. */
  begin(key: string, fp: string, options: { tool?: string } = {}): Begin {
    return this.#run((db) => {
      const begun = this.#immediate(db, (): Begin | Row => {
        const row = this.#row(db, key);
        if (row === undefined) {
          this.#insertStarted(db, key, fp, options.tool);
          return { status: BeginStatus.EXECUTE };
        }
        return row;
      });
      if ("status" in begun) {
        this.#claim(key);
        return begun;
      }
      return this.#judge(key, fp, begun);
    });
  }

  /**
   * Take over an **abandoned** attempt of *key* (RECOVER: now started by this
   * instance), atomically -- one taker. The caller must find out what the
   * attempt did before acting (`recoverAbandoned`).
   */
  adopt(key: string, fp: string, options: { tool?: string } = {}): Begin {
    return this.#run((db) => {
      const begun = this.#immediate(db, (): Begin | Row => {
        const row = this.#row(db, key);
        if (row === undefined) {
          this.#insertStarted(db, key, fp, options.tool);
          return { status: BeginStatus.EXECUTE };
        }
        if (row.fingerprint === fp && this.#abandoned(key, row.state, row.owner)) {
          db.prepare(
            "UPDATE stateguard_keys SET state = ?, owner = ?, started_at = ?," +
              " note = substr(? || coalesce(note, ''), 1, 500) WHERE scope = ? AND key = ?",
          ).run(STARTED, this.owner, this.#clock(), `adopted from ${row.owner} (${row.state}); `, this.scope, key);
          return { status: BeginStatus.RECOVER };
        }
        return row;
      });
      if ("status" in begun) {
        this.#claim(key);
        return begun;
      }
      return this.#judge(key, fp, begun);
    });
  }

  /** Record *key*'s result (done). `StoreUnavailable` leaves the key in doubt. */
  complete(key: string, result: unknown): void {
    try {
      this.#run((db) => {
        db.prepare(
          "UPDATE stateguard_keys SET state = ?, result = ?, finished_at = ?" +
            " WHERE scope = ? AND key = ? AND state = ? AND owner = ?",
        ).run(DONE, JSON.stringify(result ?? null), this.#clock(), this.scope, key, STARTED, this.owner);
      });
    } finally {
      this.#finish(key);
    }
  }

  /** The handler proved it executed nothing: free the key (this instance's own started key). */
  abort(key: string): void {
    try {
      this.#run((db) => {
        db.prepare(
          "DELETE FROM stateguard_keys WHERE scope = ? AND key = ? AND state = ? AND owner = ?",
        ).run(this.scope, key, STARTED, this.owner);
      });
    } finally {
      this.#finish(key);
    }
  }

  /** The handler failed after it may have acted: the key stays in doubt, with why. */
  markInDoubt(key: string, note: string): void {
    try {
      this.#run((db) => {
        db.prepare(
          "UPDATE stateguard_keys SET state = ?, note = ? WHERE scope = ? AND key = ? AND state = ? AND owner = ?",
        ).run(IN_DOUBT, note.slice(0, 500), this.scope, key, STARTED, this.owner);
      });
    } finally {
      this.#finish(key);
    }
  }

  /** Wait until *key* is no longer executing in this instance. False on timeout. */
  async wait(key: string, timeoutSeconds: number): Promise<boolean> {
    const waiting = this.#active.get(key);
    if (waiting === undefined) return true;
    let timer: NodeJS.Timeout | undefined;
    const timedOut = new Promise<false>((done) => {
      timer = setTimeout(() => done(false), timeoutSeconds * 1000);
    });
    try {
      return await Promise.race([waiting.promise.then(() => true as const), timedOut]);
    } finally {
      clearTimeout(timer);
    }
  }

  isActive(key: string): boolean {
    return this.#active.has(key);
  }

  // -- transactional ------------------------------------------------------------------

  /**
   * Run *effect(db)* and record its result in **one** transaction on the
   * store's database. The effect must be synchronous: a Promise would let the
   * transaction commit before the effect finished, so it is refused (rolled
   * back). EXECUTED, ROLLED_BACK (the effect threw: nothing committed), or a
   * not-executed outcome for a key seen before.
   */
  transact(key: string, fp: string, effect: (db: DatabaseSync) => unknown, options: { tool?: string } = {}): Execution {
    try {
      return this.#run((db): Execution => {
        db.exec("BEGIN IMMEDIATE");
        try {
          const row = this.#row(db, key);
          if (row !== undefined) {
            db.exec("COMMIT");
            return executionFor(this.#judge(key, fp, row));
          }
          let result: unknown;
          try {
            result = effect(db);
            if (result instanceof Promise) {
              result.catch(() => {}); // refused below; never let it reject unhandled
              throw new TypeError("a transactional effect must be synchronous");
            }
          } catch (error) {
            db.exec("ROLLBACK");
            return { outcome: Outcome.ROLLED_BACK, result: undefined, error };
          }
          const now = this.#clock();
          db.prepare(
            "INSERT INTO stateguard_keys (scope, key, fingerprint, tool, state, owner, started_at, finished_at, result)" +
              " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
          ).run(this.scope, key, fp, options.tool ?? null, DONE, this.owner, now, now, JSON.stringify(result ?? null));
          db.exec("COMMIT");
          return { outcome: Outcome.EXECUTED, result };
        } catch (error) {
          try {
            db.exec("ROLLBACK");
          } catch {
            // already rolled back or committed
          }
          throw error;
        }
      });
    } catch (error) {
      if (error instanceof StoreUnavailable) {
        // Nothing was committed -- the effect is in the same transaction, so it did not happen either.
        return { outcome: Outcome.REJECTED_STORE, result: undefined };
      }
      throw error;
    }
  }

  // -- retention and operator actions -------------------------------------------------

  /** Expire old results (keep a tombstone), delete old tombstones. In-doubt keys stay. */
  purge(): { expired: number; deleted: number } {
    const now = this.#clock();
    return this.#run((db) => {
      const expired = db
        .prepare("UPDATE stateguard_keys SET state = ?, result = NULL WHERE scope = ? AND state = ? AND finished_at < ?")
        .run(EXPIRED, this.scope, DONE, now - this.retentionSeconds).changes;
      const deleted = db
        .prepare("DELETE FROM stateguard_keys WHERE scope = ? AND state = ? AND finished_at < ?")
        .run(this.scope, EXPIRED, now - this.tombstoneSeconds).changes;
      return { expired: Number(expired), deleted: Number(deleted) };
    });
  }

  /** Operator: an in-doubt key did **not** execute -- free it. True if one was released. */
  release(key: string): boolean {
    if (this.#active.has(key)) return false;
    return this.#run(
      (db) =>
        Number(
          db
            .prepare("DELETE FROM stateguard_keys WHERE scope = ? AND key = ? AND state IN (?, ?)")
            .run(this.scope, key, IN_DOUBT, STARTED).changes,
        ) > 0,
    );
  }

  /** Operator: an in-doubt key **did** execute, with *result* -- record it as done. */
  resolve(key: string, result: unknown): boolean {
    if (this.#active.has(key)) return false;
    return this.#run(
      (db) =>
        Number(
          db
            .prepare(
              "UPDATE stateguard_keys SET state = ?, result = ?, finished_at = ? WHERE scope = ? AND key = ? AND state IN (?, ?)",
            )
            .run(DONE, JSON.stringify(result ?? null), this.#clock(), this.scope, key, IN_DOUBT, STARTED).changes,
        ) > 0,
    );
  }

  /** The recorded state of *key* (started/done/in_doubt/expired) or undefined. */
  stateOf(key: string): string | undefined {
    return this.#run((db) => {
      const row = db.prepare("SELECT state FROM stateguard_keys WHERE scope = ? AND key = ?").get(this.scope, key) as
        | { state: string }
        | undefined;
      return row?.state;
    });
  }
}

/** What a judged (not claimed) key means for this request. */
export function executionFor(begun: Begin): Execution {
  switch (begun.status) {
    case BeginStatus.REPLAY:
      return { outcome: Outcome.REPLAYED, result: begun.result };
    case BeginStatus.IN_PROGRESS:
      return { outcome: Outcome.IN_PROGRESS, result: undefined };
    case BeginStatus.IN_DOUBT:
      return { outcome: Outcome.IN_DOUBT, result: undefined };
    case BeginStatus.EXPIRED:
      return { outcome: Outcome.EXPIRED, result: undefined };
    case BeginStatus.MISMATCH:
      return { outcome: Outcome.REJECTED_MISMATCH, result: undefined };
    default:
      throw new RangeError(`${begun.status}: the key is claimed; execute or recover it`);
  }
}
