/**
 * A CONTROLLED test server: the TypeScript MCP SDK (1.32.1) over Streamable
 * HTTP, written the way the SDK's own example (and Express-based servers such
 * as server-everything) serve it -- one `McpServer` and one transport per
 * session, a 404 for a session it does not know. It contains no StateGuard
 * code: keys come only from the preload (`src/preload.ts`), as for an
 * unmodified third-party server.
 *
 *     node test/fixtures/http_notes_server.ts NOTES_FILE PORT [--slow SECONDS]
 *
 * Tools:
 *   add_note(text)  appends `text` and a newline to NOTES_FILE, fsync'd (the ground truth);
 *                   with --slow, after waiting SECONDS (a slow but healthy tool)
 *   list_notes()    the file's contents (annotated readOnlyHint)
 */

import { randomUUID } from "node:crypto";
import { closeSync, existsSync, fsyncSync, openSync, readFileSync, writeSync } from "node:fs";
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

const args = process.argv.slice(2);
const [notes, port] = args;
const slowAt = args.indexOf("--slow");
const slowSeconds = slowAt >= 0 ? Number(args[slowAt + 1]) : 0;
if (notes === undefined || port === undefined) throw new Error("usage: http_notes_server.ts NOTES_FILE PORT");
const notesFile: string = notes;

function build(): McpServer {
  const server = new McpServer({ name: "ts-http-notes", version: "0" });
  server.registerTool(
    "add_note",
    { description: "Append a note.", inputSchema: { text: z.string() } },
    async ({ text }) => {
      if (slowSeconds > 0) await new Promise((resolve) => setTimeout(resolve, slowSeconds * 1000));
      const fd = openSync(notesFile, "a");
      try {
        writeSync(fd, `${text}\n`);
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      return { content: [{ type: "text", text: "added" }] };
    },
  );
  server.registerTool(
    "list_notes",
    { description: "Every note, one per line.", annotations: { readOnlyHint: true } },
    async () => ({
      content: [{ type: "text", text: existsSync(notesFile) ? readFileSync(notesFile, "utf8") : "" }],
    }),
  );
  return server;
}

const transports = new Map<string, StreamableHTTPServerTransport>();

async function readBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  if (chunks.length === 0) return undefined;
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown;
  } catch {
    return undefined;
  }
}

function refuse(res: ServerResponse, status: number, message: string): void {
  res
    .writeHead(status, { "content-type": "application/json" })
    .end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message }, id: null }));
}

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
    process.stderr.write(`ts-http-notes: ${String(error)}\n`);
    if (!res.headersSent) res.writeHead(500).end();
  });
}).listen(Number(port), "127.0.0.1");
