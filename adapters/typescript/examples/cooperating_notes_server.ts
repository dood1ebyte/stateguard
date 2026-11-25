/**
 * A **cooperating** MCP server on the TypeScript SDK (1.x `McpServer`) with this adapter: its
 * effect and its idempotency key's record commit in **one SQLite transaction**
 * (`KeyedTools.transactional`, `IdempotencyStore.transact`), so a keyed call's crash window is
 * closed -- for this effect, and only because the server cooperates. It is the TypeScript twin
 * of `examples/mcp/cooperating_notes_server.py` (Phase 2 S3.3): the same tools, the same
 * transaction, the same fault points, so the same drivers run against both.
 *
 *     node examples/cooperating_notes_server.ts DB [--http PORT [--json]] [--no-keys]
 *
 * The notes live in the key store's own database (DB, table `notes`), next to `stateguard_keys`.
 *
 *   add_note(text)  mutating. Keyed, the note row and the key's record are written in one
 *                   `BEGIN IMMEDIATE` transaction on DB: a crash before the commit leaves neither
 *                   (a resend with the key executes, once); after it both exist (a resend
 *                   replays). A late or concurrent duplicate waits on the write lock, then finds
 *                   the record: it cannot commit a second note. This is the replay rule's first
 *                   case. Without a key it is an ordinary insert.
 *   list_notes()    read-only: the notes, one per line, in order.
 *
 * `--no-keys` serves the same tools with no keys: the non-cooperating baseline. Stdio by
 * default; `--http PORT` serves Streamable HTTP on 127.0.0.1 (`--json`: JSON answers instead of
 * event streams), one transport per session, as the SDK's example does.
 *
 * TEST-ONLY fault injection (labelled faults, never in production), the preload's variables:
 *   STATEGUARD_FAULT=before_begin|after_begin|after_effect|after_record|hang_after_record
 *   STATEGUARD_FAULT_FILE=<path>   fires once per file (whatever the process generation)
 *   STATEGUARD_FAULT_TOOLS=<list>  tools it applies to (default: add_note)
 * SIGKILL at the point. Keyed: before_begin before the transaction; after_begin inside it,
 * before the insert; after_effect inside it, after the insert, before the record and commit;
 * after_record committed, before the answer. Unkeyed: before_begin before the insert, every
 * after_* point after it.
 */

import { randomUUID } from "node:crypto";
import { existsSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { DatabaseSync } from "node:sqlite";
import { parseArgs } from "node:util";

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

import { IdempotencyStore, KeyedTools } from "../src/index.ts";
import type { KeyedPoint } from "../src/index.ts";

const TOOL = "add_note";
const { values: args, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    http: { type: "string" },
    json: { type: "boolean", default: false },
    "no-keys": { type: "boolean", default: false },
  },
});
const dbPath = positionals[0];
if (dbPath === undefined) throw new Error("usage: cooperating_notes_server.ts DB [--http PORT [--json]] [--no-keys]");

const fault = process.env["STATEGUARD_FAULT"];
const faultFile = process.env["STATEGUARD_FAULT_FILE"];
const faultTools = new Set((process.env["STATEGUARD_FAULT_TOOLS"] ?? TOOL).split(",").map((t) => t.trim()).filter(Boolean));

function fires(point: string, tool: string): boolean {
  if (fault !== point || faultFile === undefined || !faultTools.has(tool) || existsSync(faultFile)) return false;
  writeFileSync(faultFile, point);
  return true;
}

/** A crash point: SIGKILL, as a crash would (no cleanup, no commit, no answer). */
function crash(point: string): void {
  if (fires(point, TOOL)) process.kill(process.pid, "SIGKILL");
}

async function at(point: string): Promise<void> {
  if (!fires(point, TOOL)) return;
  if (point === "hang_after_record") await new Promise<never>(() => {});
  process.kill(process.pid, "SIGKILL");
}

const added = (text: string): CallToolResult => ({ content: [{ type: "text", text: `added ${text}` }] });

const store = new IdempotencyStore(dbPath, { scope: "notes" });
{
  const db = store.connect();
  db.exec("CREATE TABLE IF NOT EXISTS notes (seq INTEGER PRIMARY KEY, text TEXT NOT NULL)");
  db.close();
}

/** The keyed effect, inside the transaction that also records the key. */
function insertNote(db: DatabaseSync, input: Record<string, unknown>): CallToolResult {
  const text = input["text"];
  if (typeof text !== "string" || text === "") throw new Error("a note is non-empty text");
  crash("after_begin");
  db.prepare("INSERT INTO notes (text) VALUES (?)").run(text);
  crash("after_effect");
  return added(text);
}

const keyed = args["no-keys"]
  ? undefined
  : new KeyedTools(store, {
      onPoint: async (point: KeyedPoint, tool: string) => {
        if (tool !== TOOL) return;
        if (point === "before_begin" || point === "after_record") await at(point);
        if (point === "after_record") await at("hang_after_record");
      },
    }).transactional(TOOL, insertNote);

function build(): McpServer {
  const mcp = new McpServer({ name: "cooperating-notes-ts", version: "0.1.0" });
  keyed?.install(mcp.server);
  mcp.registerTool(
    TOOL,
    { description: "Add a note (keyed: in one transaction with its key's record).", inputSchema: { text: z.string() } },
    async ({ text }) => {
      // Reached without a key (or with --no-keys): an ordinary insert.
      if (!text) throw new Error("a note is non-empty text");
      await at("before_begin");
      const db = store.connect();
      try {
        db.prepare("INSERT INTO notes (text) VALUES (?)").run(text);
      } finally {
        db.close();
      }
      for (const point of ["after_begin", "after_effect", "after_record", "hang_after_record"]) await at(point);
      return added(text);
    },
  );
  mcp.registerTool(
    "list_notes",
    { description: "The notes, one per line, in order.", annotations: { readOnlyHint: true } },
    async () => {
      const db = store.connect();
      try {
        const rows = db.prepare("SELECT text FROM notes ORDER BY seq").all() as Array<{ text: string }>;
        return { content: [{ type: "text", text: rows.map((row) => row.text).join("\n") }] };
      } finally {
        db.close();
      }
    },
  );
  return mcp;
}

if (args.http === undefined) {
  await build().connect(new StdioServerTransport());
} else {
  const transports = new Map<string, StreamableHTTPServerTransport>();
  const readBody = async (req: IncomingMessage): Promise<unknown> => {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(chunk as Buffer);
    if (chunks.length === 0) return undefined;
    try {
      return JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown;
    } catch {
      return undefined;
    }
  };
  const refuse = (res: ServerResponse, status: number, message: string): void => {
    res
      .writeHead(status, { "content-type": "application/json" })
      .end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message }, id: null }));
  };
  createServer((req, res) => {
    void (async () => {
      if (req.url?.split("?")[0] !== "/mcp") {
        res.writeHead(404).end();
        return;
      }
      const header = req.headers["mcp-session-id"];
      const sessionId = typeof header === "string" ? header : undefined;
      const body = req.method === "POST" ? await readBody(req) : undefined;
      let transport = sessionId === undefined ? undefined : transports.get(sessionId);
      if (transport === undefined) {
        if (sessionId !== undefined) {
          refuse(res, 404, "Session not found");
          return;
        }
        if (req.method !== "POST" || !isInitializeRequest(body)) {
          refuse(res, 400, "Bad Request: No valid session ID provided");
          return;
        }
        const opened: StreamableHTTPServerTransport = new StreamableHTTPServerTransport({
          sessionIdGenerator: () => randomUUID(),
          enableJsonResponse: args.json,
          onsessioninitialized: (id) => {
            transports.set(id, opened);
          },
        });
        opened.onclose = () => {
          if (opened.sessionId !== undefined) transports.delete(opened.sessionId);
        };
        await build().connect(opened);
        transport = opened;
      }
      await transport.handleRequest(req, res, body);
    })().catch((error: unknown) => {
      process.stderr.write(`cooperating-notes-ts: ${String(error)}\n`);
      if (!res.headersSent) res.writeHead(500).end();
    });
  }).listen(Number(args.http), "127.0.0.1");
}
