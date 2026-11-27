# Onboarding: StateGuard in front of an MCP server (Phase 2 S5.2)

**Install.** The package is not yet published (A5). Use the release-candidate wheel:

```
pip install dist/rc/sguard-0.2.0rc1-py3-none-any.whl      # later: pip install sguard
pip install 'dist/rc/sguard-0.2.0rc1-py3-none-any.whl[mcp]'   # also the HTTP relay
stateguard doctor -- node /path/to/server/dist/index.js /path/to/data   # check the setup
```

**How it works.** StateGuard runs as the server's command: the client starts `stateguard relay
stdio ... -- <the server's own command>`, and the relay starts the server.
- **`--effects FILE`** declares which tools are reads and which are writes:

  ```json
  {"tools": [{"name": "read_text_file", "effect": "read_only"},
             {"name": "write_file", "effect": "non_idempotent"}]}
  ```

  Only declared reads, and calls with honoured keys, are ever resent.
- **`--keys auto`** starts a Python or Node server with idempotency keys. It needs a plain
  `python ...` or `node <entry.js>` command: `npx` and `uvx` run unkeyed, and the relay says
  why. Running `stateguard doctor -- <command>` tells you which applies.

## Claude Desktop

In `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`; Windows:
`%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "notes-filesystem": {
      "command": "stateguard",
      "args": ["relay", "stdio", "--retry", "auto",
               "--effects", "/Users/me/.stateguard/filesystem-effects.json",
               "--keys", "auto",
               "--", "node", "/Users/me/mcp/server-filesystem/dist/index.js", "/Users/me/notes"]
    }
  }
}
```

## Cursor

In `~/.cursor/mcp.json`, or a project's `.cursor/mcp.json`. It uses the same `mcpServers` shape:

```json
{
  "mcpServers": {
    "notes-filesystem": {
      "command": "stateguard",
      "args": ["relay", "stdio", "--retry", "auto",
               "--effects", "/home/me/.stateguard/filesystem-effects.json",
               "--", "npx", "-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"]
    }
  }
}
```

(With `npx` the server runs unkeyed; use `node <entry.js>` for `--keys auto`.)

## Claude Code

```
claude mcp add notes-filesystem -- stateguard relay stdio --retry auto \
  --effects ~/.stateguard/filesystem-effects.json -- node ~/mcp/server-filesystem/dist/index.js ~/notes
```

or, in a project's `.mcp.json`, the same `mcpServers` entry as for Claude Desktop.

## A Streamable HTTP server

Run the relay in front of it, and point the client at the relay:

```
stateguard relay http --retry auto --effects effects.json --listen 127.0.0.1:8931 \
  --upstream http://127.0.0.1:3000/mcp
claude mcp add --transport http notes http://127.0.0.1:8931/mcp
```

## Before relying on it

- **Try it first:** `stateguard trial --dir DIR --calls calls.jsonl --sandbox DATA -- <server>`
  runs your calls in shadow, then auto, on copies of your data, and writes a before/after
  report.
- **The configuration S6 validates** is in `RECOMMENDED_CONFIG.md`.
- **Servers known to behave differently** are listed in `COMPATIBILITY_MATRIX.md`. For example,
  server-memory needs `--serialize-tools`.
- **Time to set up:** a scripted run from a clean environment reached its first protected call
  in about 10 s (`benchmarks/evidence/onboarding/O1/`).
