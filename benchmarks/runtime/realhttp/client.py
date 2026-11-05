"""
The measured MCP client for the realhttp batches: the official Python SDK (mcp 2.x) over
Streamable HTTP. One session: initialize, list tools, (optionally wait for the driver to restart
the server: --pause-file / --go-file), then one tools/call. With --naive, any failure is followed
by a NEW session and the same call once more (a blind retry, no key). Prints one JSON line
describing what the client saw.
"""

import argparse
import json
import time
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


async def one_session(url, tool, arguments, wait, pause, go):
    async with streamable_http_client(url) as (read, write), ClientSession(read, write) as session:
        with anyio.fail_after(20):
            await session.initialize()
            await session.list_tools()
        if pause:
            Path(pause).write_text("ready")
            while not Path(go).exists():
                await anyio.sleep(0.05)
        with anyio.fail_after(wait):
            return await session.call_tool(tool, arguments)


def attempt(url, tool, arguments, wait, pause=None, go=None):
    try:
        result = anyio.run(one_session, url, tool, arguments, wait, pause, go)
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
    p.add_argument("url")
    p.add_argument("tool")
    p.add_argument("arguments")
    p.add_argument("--wait", type=float, default=8.0)
    p.add_argument("--naive", action="store_true")
    p.add_argument("--pause-file")
    p.add_argument("--go-file")
    a = p.parse_args()
    arguments = json.loads(a.arguments)
    t0 = time.monotonic()
    first = attempt(a.url, a.tool, arguments, a.wait, a.pause_file, a.go_file)
    out = {"first": first, "final": first, "retried": False}
    ok = first["status"] == "result" and not first["is_error"]
    if a.naive and not ok:
        out["retried"] = True
        out["final"] = attempt(a.url, a.tool, arguments, a.wait)
    out["latency_s"] = round(time.monotonic() - t0, 3)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
