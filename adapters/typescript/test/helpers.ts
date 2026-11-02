import { spawnSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
export const SERVER = join(ROOT, "examples", "keyed_reference_server.ts");
export const CRASH_CHILD = join(ROOT, "test", "fixtures", "crash_child.ts");

export function tmp(prefix = "sg-ts-"): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

/** Run a node child to completion; its exit status (null + signal when killed). */
export function runChild(script: string, args: string[]): { status: number | null; signal: NodeJS.Signals | null; stdout: string; stderr: string } {
  const out = spawnSync(process.execPath, [script, ...args], { encoding: "utf8", timeout: 60_000 });
  return { status: out.status, signal: out.signal, stdout: out.stdout, stderr: out.stderr };
}

/** An official SDK client talking to the reference server as a real stdio process. */
export async function connect(serverArgs: string[]): Promise<Client> {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [SERVER, ...serverArgs],
    stderr: "ignore",
  });
  const client = new Client({ name: "stateguard-ts-tests", version: "0" });
  await client.connect(transport);
  return client;
}

export const KEY = "io.stateguard/idempotencyKey";
export const OUTCOME = "io.stateguard/idempotencyOutcome";

export function outcomeOf(result: unknown): unknown {
  const meta = (result as { _meta?: Record<string, unknown> } | undefined)?._meta;
  return meta?.[OUTCOME];
}

/** The outcome named in a JSON-RPC error's data (what the client SDK throws). */
export function errorOutcome(error: unknown): unknown {
  const data = (error as { data?: Record<string, unknown> } | undefined)?.data;
  return data?.[OUTCOME];
}
