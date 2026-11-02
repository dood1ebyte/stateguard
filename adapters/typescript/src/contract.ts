/**
 * The wire contract -- names, outcomes, error codes, fingerprints -- exactly as
 * `stateguard.adapters.mcp.idempotency` (Python) defines them. Both sides of a
 * keyed call, in either language, must agree on every string here; the
 * cross-language tests (tests/examples/test_typescript_adapter.py) check it.
 */

import { createHash, randomUUID } from "node:crypto";

export const IDEMPOTENCY_CAPABILITY = "io.stateguard/idempotency";
export const IDEMPOTENCY_KEY = "io.stateguard/idempotencyKey";
export const IDEMPOTENCY_OUTCOME = "io.stateguard/idempotencyOutcome";
/** The only contract version. */
export const VERSION = 1;

export const Outcome = {
  EXECUTED: "executed",
  EXECUTED_UNRECORDED: "executed_unrecorded",
  REPLAYED: "replayed",
  RECOVERED: "recovered",
  ROLLED_BACK: "rolled_back",
  NOT_EXECUTED: "not_executed",
  IN_PROGRESS: "in_progress",
  IN_DOUBT: "in_doubt",
  EXPIRED: "expired",
  REJECTED_MISMATCH: "rejected_mismatch",
  REJECTED_STORE: "rejected_store",
} as const;
export type Outcome = (typeof Outcome)[keyof typeof Outcome];

/** Outcomes answered as a JSON-RPC error: this request executed nothing. */
export type NotExecutedOutcome =
  | typeof Outcome.NOT_EXECUTED
  | typeof Outcome.IN_PROGRESS
  | typeof Outcome.IN_DOUBT
  | typeof Outcome.EXPIRED
  | typeof Outcome.REJECTED_MISMATCH
  | typeof Outcome.REJECTED_STORE;

const ERROR_CODES: Readonly<Record<NotExecutedOutcome, number>> = {
  not_executed: -32014,
  in_doubt: -32010,
  in_progress: -32011,
  expired: -32012,
  rejected_store: -32013,
  rejected_mismatch: -32602,
};

const ERROR_MESSAGES: Readonly<Record<NotExecutedOutcome, string>> = {
  not_executed:
    "The tool failed before acting: nothing was executed, and the idempotency key is free.",
  in_doubt:
    "Idempotency key in doubt: an earlier request with this key started and recorded " +
    "no result. Nothing was executed now; whether it executed earlier is unknown.",
  in_progress:
    "Idempotency key in progress: an earlier request with this key is still executing. " +
    "Nothing was executed now.",
  expired:
    "Idempotency key expired: a request with this key executed earlier and its result is " +
    "no longer retained. Nothing was executed now.",
  rejected_store:
    "Idempotency store unavailable: the server cannot record keys, so it executed nothing.",
  rejected_mismatch:
    "Idempotency key reused with another tool or other arguments: nothing was executed.",
};

export function isNotExecutedOutcome(value: unknown): value is NotExecutedOutcome {
  return typeof value === "string" && Object.hasOwn(ERROR_CODES, value);
}

export function errorCode(outcome: NotExecutedOutcome): number {
  return ERROR_CODES[outcome];
}

export function errorMessage(outcome: NotExecutedOutcome): string {
  return ERROR_MESSAGES[outcome];
}

export function errorData(outcome: Outcome, key: string): Record<string, string> {
  return { [IDEMPOTENCY_OUTCOME]: outcome, [IDEMPOTENCY_KEY]: key };
}

/**
 * The error a keyed request that executed nothing is answered with. The SDK's
 * `Protocol` turns any thrown error with an integer `code` and a `data` into the
 * JSON-RPC error `{code, message, data}`.
 */
export class OutcomeError extends Error {
  readonly code: number;
  readonly data: Record<string, string>;
  readonly outcome: NotExecutedOutcome;

  constructor(outcome: NotExecutedOutcome, key: string, detail?: string) {
    super(detail ? `${ERROR_MESSAGES[outcome]} (${detail})` : ERROR_MESSAGES[outcome]);
    this.name = "OutcomeError";
    this.code = ERROR_CODES[outcome];
    this.data = errorData(outcome, key);
    this.outcome = outcome;
  }
}

/** The JSON-RPC error message for a not-executed outcome (relay-side helpers use this). */
export function outcomeError(
  id: unknown,
  outcome: NotExecutedOutcome,
  key: string,
): { jsonrpc: "2.0"; id: unknown; error: { code: number; message: string; data: unknown } } {
  return {
    jsonrpc: "2.0",
    id,
    error: { code: ERROR_CODES[outcome], message: ERROR_MESSAGES[outcome], data: errorData(outcome, key) },
  };
}

// -- wire helpers ---------------------------------------------------------------------

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The `_meta` entry a server puts on a tool definition that honours keys. */
export function advertisement(): Record<string, { version: number }> {
  return { [IDEMPOTENCY_CAPABILITY]: { version: VERSION } };
}

/** True if the tool definition's `_meta` says the server honours keys (version 1). */
export function toolSupportsKeys(definition: unknown): boolean {
  if (!isRecord(definition)) return false;
  const meta = definition["_meta"];
  const entry = isRecord(meta) ? meta[IDEMPOTENCY_CAPABILITY] : undefined;
  return isRecord(entry) && entry["version"] === VERSION;
}

/** A new random key: 32 lowercase hex digits, as Python's `uuid4().hex`. */
export function newKey(): string {
  return randomUUID().replaceAll("-", "");
}

/** The key a `tools/call` request's params carry, if any. */
export function paramsKey(params: unknown): string | undefined {
  const meta = isRecord(params) ? params["_meta"] : undefined;
  const key = isRecord(meta) ? meta[IDEMPOTENCY_KEY] : undefined;
  return typeof key === "string" && key ? key : undefined;
}

/** For an error answer that says "this keyed request executed nothing": which outcome. */
export function notExecutedOutcome(message: unknown): NotExecutedOutcome | undefined {
  const error = isRecord(message) ? message["error"] : undefined;
  const data = isRecord(error) ? error["data"] : undefined;
  const value = isRecord(data) ? data[IDEMPOTENCY_OUTCOME] : undefined;
  return isNotExecutedOutcome(value) ? value : undefined;
}

// -- fingerprints: Python's canonical JSON ---------------------------------------------------

/**
 * What a key must keep meaning: the tool and its arguments, as the sha256 of
 * the same canonical JSON Python computes with
 * `json.dumps([tool, arguments], sort_keys=True, separators=(",", ":"))`.
 *
 * Equal to Python's fingerprint for strings, booleans, null, object key order,
 * integers within +-2^53 and non-integral floats. Not for an integral number
 * written as a float (`1.0`, `1e16`) or an integer beyond +-2^53: JavaScript's
 * `JSON.parse` erases the difference, so here `1.0` and `1` are one request,
 * while Python keeps them apart. See docs/runtime/TYPESCRIPT_IDEMPOTENCY_ADAPTER.md.
 */
export function fingerprint(tool: string, args: unknown): string {
  return createHash("sha256").update(canonicalJson([tool, args]), "utf8").digest("hex");
}

/** `json.dumps(value, sort_keys=True, separators=(",", ":"))` (ensure_ascii, allow_nan). */
export function canonicalJson(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "number") return pythonNumber(value);
  if (typeof value === "string") return pythonString(value);
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (isRecord(value)) {
    const keys = Object.keys(value)
      .filter((key) => value[key] !== undefined)
      .sort(compareCodePoints);
    return `{${keys.map((key) => `${pythonString(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  throw new TypeError(`not JSON: ${typeof value}`);
}

/** Python sorts str keys by code point; JavaScript's default sort compares UTF-16 units. */
function compareCodePoints(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = (x[i]?.codePointAt(0) ?? 0) - (y[i]?.codePointAt(0) ?? 0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

const SHORT_ESCAPES: Readonly<Record<string, string>> = {
  '"': '\\"',
  "\\": "\\\\",
  "\n": "\\n",
  "\r": "\\r",
  "\t": "\\t",
  "\b": "\\b",
  "\f": "\\f",
};

/** Python's `ensure_ascii` string encoding: everything outside ' '..'~' is escaped. */
function pythonString(text: string): string {
  let out = '"';
  for (let i = 0; i < text.length; i++) {
    const unit = text.charCodeAt(i); // UTF-16 code units, as Python escapes astral chars
    const char = text[i] ?? "";
    const short = SHORT_ESCAPES[char];
    if (short !== undefined) out += short;
    else if (unit < 0x20 || unit > 0x7e) out += `\\u${unit.toString(16).padStart(4, "0")}`;
    else out += char;
  }
  return `${out}"`;
}

/** Python's `repr` for a JSON number (int, or float with 'r' formatting). */
function pythonNumber(value: number): string {
  if (Number.isNaN(value)) return "NaN";
  if (value === Infinity) return "Infinity";
  if (value === -Infinity) return "-Infinity";
  if (Number.isInteger(value)) return Object.is(value, -0) ? "0" : String(value);
  // Shortest round-trip digits, as both languages compute them; Python's layout.
  const [mantissa = "", exp = "0"] = value.toExponential().split("e");
  const negative = mantissa.startsWith("-");
  const digits = mantissa.replace("-", "").replace(".", "");
  const exponent = Number(exp);
  let body: string;
  if (exponent < -4 || exponent >= 16) {
    const rest = digits.slice(1);
    const sign = exponent < 0 ? "-" : "+";
    body = `${digits[0] ?? "0"}${rest ? `.${rest}` : ""}e${sign}${String(Math.abs(exponent)).padStart(2, "0")}`;
  } else if (exponent < 0) {
    body = `0.${"0".repeat(-exponent - 1)}${digits}`;
  } else {
    const whole = digits.slice(0, exponent + 1).padEnd(exponent + 1, "0");
    const fraction = digits.slice(exponent + 1);
    body = `${whole}.${fraction || "0"}`;
  }
  return negative ? `-${body}` : body;
}

export type { Json };
