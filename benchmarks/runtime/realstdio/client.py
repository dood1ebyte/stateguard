"""
The measured MCP client for the realstdio batches: the official Python SDK (mcp 2.x) over stdio.
One session is one server process: initialize, list tools, then one tools/call. COMMAND is a
JSON argv list (the relay, or the shim and server). With --naive, any failure is followed by a
NEW session (a new process) and the same call once more (a blind retry, no key). Prints one
JSON line describing what the client saw.
"""

import argparse
import json
import os
import time

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError


async def one_session(command, tool, arguments, wait, pause, go):
    params = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        with anyio.fail_after(20):
            await session.initialize()
            await session.list_tools()
        with anyio.fail_after(wait):
            return await session.call_tool(tool, arguments)


def attempt(command, tool, arguments, wait, pause=None, go=None):
    try:
        result = anyio.run(one_session, command, tool, arguments, wait, pause, go)
    except MCPError as exc:
        return {"status": "mcp_error", "code": exc.code, "message": exc.message[:300],
                "data": exc.data if isinstance(exc.data, dict) else None}
    except TimeoutError:
        return {"status": "timeout"}
    except BaseException as exc:  # the client's own crash (its transport raising) is the outcome
        if isinstance(exc, KeyboardInterrupt):
            raise
        inner = exc
        while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
            inner = inner.exceptions[0]
        if isinstance(inner, MCPError):
            return {"status": "mcp_error", "code": inner.code, "message": inner.message[:300],
                    "data": inner.data if isinstance(inner.data, dict) else None}
        if isinstance(inner, TimeoutError):
            return {"status": "timeout"}
        return {"status": "crash", "error": f"{type(inner).__name__}: {str(inner)[:200]}"}
    text = "".join(getattr(b, "text", "") for b in result.content)
    return {"status": "result", "is_error": bool(result.is_error), "text": text[:2000],
            "meta": result.meta or {}}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", help="JSON argv list")
    p.add_argument("tool")
    p.add_argument("arguments")
    p.add_argument("--wait", type=float, default=8.0)
    p.add_argument("--naive", action="store_true")
    p.add_argument("--pause-file")
    p.add_argument("--go-file")
    a = p.parse_args()
    arguments = json.loads(a.arguments)
    t0 = time.monotonic()
    first = attempt(json.loads(a.command), a.tool, arguments, a.wait, a.pause_file, a.go_file)
    out = {"first": first, "final": first, "retried": False}
    ok = first["status"] == "result" and not first["is_error"]
    if a.naive and not ok:
        out["retried"] = True
        out["final"] = attempt(json.loads(a.command), a.tool, arguments, a.wait)
    out["latency_s"] = round(time.monotonic() - t0, 3)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
