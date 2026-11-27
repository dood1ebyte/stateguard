/**
 * Idempotency keys for a server built on the TypeScript MCP SDK (1.x; tested
 * with 1.32.1). The contract is the Python reference's
 * (docs/runtime/SERVER_IDEMPOTENCY_CONTRACT.md); this is the same semantics on
 * the SDK's own boundary.
 *
 * SDK 1.x has no middleware. Its public boundary is `Server.setRequestHandler`,
 * which `McpServer` calls (lazily, on the first `registerTool`) to install its
 * `tools/list` and `tools/call` handlers. `KeyedTools.install(server)` wraps
 * that method on the server *instance*, so it must run before any tool is
 * registered (it fails closed otherwise):
 *
 *     const store = new IdempotencyStore("orders.db", { scope: "orders" });
 *     const keyed = new KeyedTools(store).twoPhase("append_ledger");
 *     const mcp = new McpServer({ name: "orders", version: "1.0.0" });
 *     keyed.install(mcp.server);          // before registerTool
 *     mcp.registerTool("append_ledger", ...);
 *
 * What a handler's answer means for its key (as in Python):
 * - a result -- success or `isError` -- is recorded and replayed. `McpServer`
 *   turns a tool's exceptions (even argument validation) into `isError`
 *   results, so under `McpServer` those are recorded errors too;
 * - `NotExecuted` thrown (reaching the adapter: a low-level `Server` handler)
 *   frees the key;
 * - "input required" -- SDK 1.x's thrown `UrlElicitationRequired` (-32042), or
 *   a `resultType: "input_required"` result -- frees the key and passes the
 *   answer through unrecorded: the client retries with its input, and a keyed
 *   tool must not act before it has it;
 * - anything else thrown leaves the key in doubt.
 * A client's cancellation sends no answer, but the handler runs on and its
 * result is recorded, so a resend replays it.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import type { DatabaseSync } from "node:sqlite";

import { IDEMPOTENCY_KEY, IDEMPOTENCY_OUTCOME, Outcome, OutcomeError, advertisement, fingerprint, isNotExecutedOutcome, paramsKey } from "./contract.ts";
import { NotExecuted, Recovery, claim, describeError, quietly, recoverAbandoned } from "./executor.ts";
import type { Execution } from "./executor.ts";
import { BeginStatus, IdempotencyStore, StoreUnavailable, executionFor } from "./store.ts";
import type { Begin } from "./store.ts";

const keyContext = new AsyncLocalStorage<string>();

/** The idempotency key of the keyed call this handler is executing, or undefined. */
export function currentKey(): string | undefined {
  return keyContext.getStore();
}

/** SDK 1.x `ErrorCode.UrlElicitationRequired`: the client must supply input and retry. */
export const URL_ELICITATION_REQUIRED = -32042;

export type RequestHandler = (request: unknown, extra: unknown) => unknown;
/** The SDK result shape this adapter records and answers (a JSON object). */
export type ToolResult = Record<string, unknown>;
export type Effect = (db: DatabaseSync, args: Record<string, unknown>) => ToolResult;
/** `check(key, arguments)`: did this key's own effect happen? */
export type Recover = (key: string, args: Record<string, unknown>) => Recovery | Promise<Recovery>;
/** TEST-ONLY: named points of a keyed call, for fault injection. */
export type KeyedPoint = "before_begin" | "after_begin" | "after_effect" | "after_record";
export type PointHook = (point: KeyedPoint, tool: string) => void | Promise<void>;

/** The part of an SDK `Server` (or `Protocol`) the adapter uses -- all public API. */
export interface HandlerRegistry {
  setRequestHandler(schema: unknown, handler: RequestHandler): void;
  assertCanSetRequestHandler(method: string): void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The JSON form of a handler's answer: what the store records and a resend replays. */
function toJson(value: unknown): ToolResult {
  return JSON.parse(JSON.stringify(value)) as ToolResult;
}

/**
 * The request method a zod request schema is for: its `method` literal. Read
 * through zod's public `shape` and literal `value` (zod 4, as SDK 1.32.1 uses;
 * zod 3's `_def.value` as a fallback).
 */
export function methodOf(schema: unknown): string | undefined {
  const shape = isRecord(schema) || typeof schema === "function" ? (schema as { shape?: unknown }).shape : undefined;
  const literal = isRecord(shape) ? shape["method"] : undefined;
  if (!isRecord(literal) && typeof literal !== "function") return undefined;
  try {
    const value = (literal as { value?: unknown }).value ?? (literal as { _def?: { value?: unknown } })._def?.value;
    return typeof value === "string" ? value : undefined;
  } catch {
    return undefined; // a multi-value literal has no single value
  }
}

function isInputRequiredError(error: unknown): boolean {
  return isRecord(error) || error instanceof Error ? (error as { code?: unknown }).code === URL_ELICITATION_REQUIRED : false;
}

function isInputRequiredResult(value: unknown): boolean {
  return isRecord(value) && value["resultType"] === "input_required";
}

export class KeyedTools {
  readonly store: IdempotencyStore;
  readonly waitSeconds: number;
  readonly #twoPhase = new Set<string>();
  readonly #recover = new Map<string, Recover>();
  readonly #transactional = new Map<string, Effect>();
  readonly #onPoint: PointHook | undefined;

  constructor(store: IdempotencyStore, options: { waitSeconds?: number; onPoint?: PointHook } = {}) {
    this.store = store;
    this.waitSeconds = options.waitSeconds ?? 5;
    this.#onPoint = options.onPoint;
  }

  /**
   * Key these tools; their handler runs at most once per key (two-phase).
   * *recover* settles abandoned attempts; it is only sound under the
   * contract's two conditions (an exclusive store, and a check for this key's
   * own effect).
   */
  twoPhase(names: string | readonly string[], options: { recover?: Recover } = {}): this {
    for (const name of typeof names === "string" ? [names] : names) {
      this.#twoPhase.add(name);
      if (options.recover !== undefined) this.#recover.set(name, options.recover);
    }
    return this;
  }

  /**
   * Key *name*; with a key, *effect(db, arguments)* runs instead of the handler,
   * synchronously, in one transaction with the key's record (`db` is the store's
   * database -- write the effect there).
   */
  transactional(name: string, effect: Effect): this {
    this.#transactional.set(name, effect);
    return this;
  }

  get keyed(): ReadonlySet<string> {
    return new Set([...this.#twoPhase, ...this.#transactional.keys()]);
  }

  #isKeyed(name: string): boolean {
    return this.#twoPhase.has(name) || this.#transactional.has(name);
  }

  // -- listing --------------------------------------------------------------------------

  advertise(tools: readonly unknown[]): unknown[] {
    return tools.map((tool) =>
      isRecord(tool) && typeof tool["name"] === "string" && this.#isKeyed(tool["name"])
        ? { ...tool, _meta: { ...(isRecord(tool["_meta"]) ? tool["_meta"] : {}), ...advertisement() } }
        : tool,
    );
  }

  wrapListHandler(handler: RequestHandler): RequestHandler {
    return async (request, extra) => {
      const result = await handler(request, extra);
      return isRecord(result) && Array.isArray(result["tools"])
        ? { ...result, tools: this.advertise(result["tools"]) }
        : result;
    };
  }

  // -- calls ----------------------------------------------------------------------------

  wrapCallHandler(handler: RequestHandler): RequestHandler {
    return async (request, extra) => {
      const params = isRecord(request) && isRecord(request["params"]) ? request["params"] : {};
      const name = params["name"];
      const key = paramsKey(params);
      if (typeof name !== "string" || !this.#isKeyed(name) || key === undefined) {
        return handler(request, extra);
      }
      const args = isRecord(params["arguments"]) ? params["arguments"] : {};
      return this.#keyedCall(key, name, args, () => handler(request, extra));
    };
  }

  /**
   * Wrap the instance's `setRequestHandler`, so the `tools/list` and
   * `tools/call` handlers installed afterwards (by `McpServer.registerTool`, or
   * by hand) are keyed. Fails closed if they are already installed.
   */
  install(server: HandlerRegistry): void {
    server.assertCanSetRequestHandler("tools/call");
    server.assertCanSetRequestHandler("tools/list");
    const original = server.setRequestHandler.bind(server);
    server.setRequestHandler = (schema: unknown, handler: RequestHandler) => original(schema, this.wrapFor(schema, handler));
  }

  /** The handler to register for *schema*: keyed for tools/call, advertised for tools/list. */
  wrapFor(schema: unknown, handler: RequestHandler): RequestHandler {
    const method = methodOf(schema);
    if (method === "tools/call") return this.wrapCallHandler(handler);
    if (method === "tools/list") return this.wrapListHandler(handler);
    return handler;
  }

  async #point(point: KeyedPoint, tool: string): Promise<void> {
    if (this.#onPoint !== undefined) await this.#onPoint(point, tool);
  }

  async #keyedCall(key: string, name: string, args: Record<string, unknown>, run: () => unknown): Promise<unknown> {
    const fp = fingerprint(name, args);
    const effect = this.#transactional.get(name);
    if (effect !== undefined) {
      await this.#point("before_begin", name);
      const inTransaction = (db: DatabaseSync): unknown => {
        const result: unknown = effect(db, args);
        if (result instanceof Promise) {
          result.catch(() => {}); // refused: it would outlive the transaction
          throw new TypeError("a transactional effect must be synchronous");
        }
        return toJson(result);
      };
      const execution = this.store.transact(key, fp, inTransaction, { tool: name });
      if (execution.outcome === Outcome.EXECUTED) await this.#point("after_record", name);
      return answer(execution, key);
    }
    const recover = this.#recover.get(name);
    await this.#point("before_begin", name);
    let begun: Begin;
    try {
      begun = await claim(this.store, key, fp, name, { waitSeconds: this.waitSeconds, recovers: recover !== undefined });
    } catch (error) {
      if (error instanceof StoreUnavailable) return answer({ outcome: Outcome.REJECTED_STORE, result: undefined }, key);
      throw error;
    }
    if (begun.status === BeginStatus.RECOVER && recover !== undefined) {
      const settled = await recoverAbandoned(this.store, key, async () => recorded(await recover(key, args)));
      if (settled !== null) return answer(settled, key);
    } else if (begun.status !== BeginStatus.EXECUTE) {
      return answer(executionFor(begun), key);
    }
    await this.#point("after_begin", name);
    let result: unknown;
    try {
      result = await keyContext.run(key, run);
    } catch (error) {
      if (error instanceof NotExecuted) {
        quietly(() => this.store.abort(key));
        return answer({ outcome: Outcome.NOT_EXECUTED, result: undefined, error }, key);
      }
      if (isInputRequiredError(error)) {
        quietly(() => this.store.abort(key)); // not complete: the client retries with its input
        throw error;
      }
      quietly(() => this.store.markInDoubt(key, describeError(error)));
      return answer({ outcome: Outcome.IN_DOUBT, result: undefined, error }, key);
    }
    if (isInputRequiredResult(result)) {
      quietly(() => this.store.abort(key));
      return result;
    }
    if (!isRecord(result)) {
      quietly(() => this.store.markInDoubt(key, `not a tools/call result: ${typeof result}`));
      return answer({ outcome: Outcome.IN_DOUBT, result: undefined }, key);
    }
    const record = toJson(result);
    await this.#point("after_effect", name);
    try {
      this.store.complete(key, record);
    } catch (error) {
      if (error instanceof StoreUnavailable) return answer({ outcome: Outcome.EXECUTED_UNRECORDED, result: record }, key);
      throw error;
    }
    await this.#point("after_record", name);
    return answer({ outcome: Outcome.EXECUTED, result: record }, key);
  }
}

/** A found result, as the JSON the store records (anything that is not an object: unknown). */
function recorded(found: Recovery): Recovery {
  if (found instanceof Recovery && found.applied === true) {
    return isRecord(found.result)
      ? Recovery.found(toJson(found.result))
      : Recovery.unknown(`found a ${typeof found.result}, not a tools/call result`);
  }
  return found;
}

/** The answer to a keyed call: a result with the outcome in `_meta`, or an `OutcomeError` thrown. */
export function answer(execution: Execution, key: string): ToolResult {
  const { outcome } = execution;
  if (
    outcome === Outcome.EXECUTED ||
    outcome === Outcome.EXECUTED_UNRECORDED ||
    outcome === Outcome.REPLAYED ||
    outcome === Outcome.RECOVERED
  ) {
    const result = isRecord(execution.result) ? execution.result : {};
    const meta = isRecord(result["_meta"]) ? result["_meta"] : {};
    return { ...result, _meta: { ...meta, [IDEMPOTENCY_OUTCOME]: outcome, [IDEMPOTENCY_KEY]: key } };
  }
  if (outcome === Outcome.ROLLED_BACK) {
    const why = execution.error instanceof Error ? execution.error.message : String(execution.error);
    return {
      content: [{ type: "text", text: `Tool failed: ${why}` }],
      isError: true,
      _meta: { [IDEMPOTENCY_OUTCOME]: outcome, [IDEMPOTENCY_KEY]: key },
    };
  }
  if (!isNotExecutedOutcome(outcome)) throw new RangeError(`unhandled outcome ${outcome}`);
  const detail =
    outcome === Outcome.NOT_EXECUTED && execution.error instanceof Error ? execution.error.message : undefined;
  throw new OutcomeError(outcome, key, detail);
}

export { NotExecuted, Recovery };
