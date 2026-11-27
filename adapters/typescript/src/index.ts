/**
 * StateGuard's MCP idempotency-key contract, server side, for the TypeScript
 * MCP SDK. Same wire contract and store schema as the Python reference
 * (`stateguard.adapters.mcp.idempotency`).
 */

export * from "./contract.ts";
export { BeginStatus, IdempotencyStore, StoreUnavailable, executionFor } from "./store.ts";
export type { Begin, StoreOptions } from "./store.ts";
export { KeyedExecutor, NotExecuted, Recovery, recoverAbandoned } from "./executor.ts";
export type { Execution, RecoveryCheck } from "./executor.ts";
export { KeyedTools, URL_ELICITATION_REQUIRED, answer, currentKey, methodOf } from "./sdk.ts";
export type { Effect, HandlerRegistry, KeyedPoint, PointHook, Recover, RequestHandler, ToolResult } from "./sdk.ts";
