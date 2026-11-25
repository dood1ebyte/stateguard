"""
The compatibility matrix's MCP client (TEST-ONLY): the official Python SDK (mcp 2.x), over stdio
(TARGET is a JSON argv list) or Streamable HTTP (TARGET is a URL). One session: initialize, list
tools, then at most one tools/call. Prints one JSON line: the server's name, the listed tools'
names (sorted), and what the call returned (the realstdio client's ``final`` shape).

    client.py TARGET [TOOL ARGUMENTS_JSON] [--wait S]
"""

import argparse
import json
import os
import time

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


async def session_run(target, tool, arguments, wait, out):
    if target.startswith("http"):
        transport = streamable_http_client(target)
    else:
        command = json.loads(target)
        params = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
        transport = stdio_client(params)
    async with transport as streams, ClientSession(streams[0], streams[1]) as session:
        with anyio.fail_after(30):
            init = await session.initialize()
            out["server"] = getattr(init.server_info, "name", None)
            listed = await session.list_tools()
            out["tools"] = sorted(t.name for t in listed.tools)
        if tool is None:
            return None
        with anyio.fail_after(wait):
            return await session.call_tool(tool, arguments)


def _error(exc):
    inner = exc
    while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
        inner = inner.exceptions[0]
    if isinstance(inner, MCPError):
        return {
            "status": "mcp_error",
            "code": inner.code,
            "message": inner.message[:300],
            "data": inner.data if isinstance(inner.data, dict) else None,
        }
    if isinstance(inner, TimeoutError):
        return {"status": "timeout"}
    return {"status": "crash", "error": f"{type(inner).__name__}: {str(inner)[:200]}"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("target")
    p.add_argument("tool", nargs="?")
    p.add_argument("arguments", nargs="?", default="{}")
    p.add_argument("--wait", type=float, default=8.0)
    a = p.parse_args()
    out = {"server": None, "tools": None}
    t0 = time.monotonic()
    try:
        result = anyio.run(session_run, a.target, a.tool, json.loads(a.arguments), a.wait, out)
    except BaseException as exc:  # the client's own crash (its transport raising) is the outcome
        if isinstance(exc, KeyboardInterrupt):
            raise
        out["final"] = _error(exc)
    else:
        if result is None:
            out["final"] = {"status": "listed"}
        else:
            text = "".join(getattr(b, "text", "") for b in result.content)
            out["final"] = {
                "status": "result",
                "is_error": bool(result.is_error),
                "text": text[:2000],
                "meta": result.meta or {},
            }
    out["latency_s"] = round(time.monotonic() - t0, 3)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
