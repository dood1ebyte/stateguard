"""
The measured MCP client for R1 (TEST-ONLY): the official Python SDK (mcp 2.x) over Streamable
HTTP, making several calls of one tool, one after another, on one session. If the session breaks,
the call it broke on is recorded as the client saw it and the next call starts a new session, as
an application would reconnect; no call is sent twice unless --naive (one blind resend after any
failure, no key). Prints one JSON line: ``{"calls": [{"first", "final", "retried"}, ...]}`` in
the order of the texts given.

usage: multi_client.py URL TOOL TEXTS_JSON [--wait S] [--naive]
"""

import argparse
import json
import time

import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


def _error(exc):
    return {"status": "mcp_error", "code": exc.code, "message": exc.message[:300],
            "data": exc.data if isinstance(exc.data, dict) else None}


def _crash(exc):
    inner = exc
    while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
        inner = inner.exceptions[0]
    if isinstance(inner, MCPError):
        return _error(inner)
    if isinstance(inner, TimeoutError):
        return {"status": "timeout"}
    return {"status": "crash", "error": f"{type(inner).__name__}: {str(inner)[:200]}"}


def _ok(outcome):
    return outcome["status"] == "result" and not outcome["is_error"]


class Calls:
    def __init__(self, texts, naive):
        self.todo = list(enumerate(texts))
        self.naive = naive
        self.seen = {}

    def settle(self, outcome):
        """The call at the head of the queue ended with *outcome*."""
        index, text = self.todo.pop(0)
        entry = self.seen.setdefault(index, {"first": outcome, "retried": False})
        entry["final"] = outcome
        if self.naive and not _ok(outcome) and not entry["retried"]:
            entry["retried"] = True
            self.todo.insert(0, (index, text))


async def drain(url, tool, calls, wait):
    async with streamable_http_client(url) as (read, write), ClientSession(read, write) as session:
        with anyio.fail_after(20):
            await session.initialize()
            await session.list_tools()
        while calls.todo:
            _, text = calls.todo[0]
            try:
                with anyio.fail_after(wait):
                    result = await session.call_tool(tool, {"text": text})
            except MCPError as exc:
                calls.settle(_error(exc))
                continue
            except TimeoutError:
                calls.settle({"status": "timeout"})
                continue
            body = "".join(getattr(b, "text", "") for b in result.content)
            calls.settle({"status": "result", "is_error": bool(result.is_error),
                          "text": body[:500], "meta": result.meta or {}})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("tool")
    p.add_argument("texts")
    p.add_argument("--wait", type=float, default=8.0)
    p.add_argument("--naive", action="store_true")
    a = p.parse_args()
    calls = Calls(json.loads(a.texts), a.naive)
    t0 = time.monotonic()
    sessions = 0
    while calls.todo:
        sessions += 1
        try:
            anyio.run(drain, a.url, a.tool, calls, a.wait)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # the session broke: the call it broke on ends with it
            if calls.todo:
                calls.settle(_crash(exc))
    out = {"calls": [calls.seen[i] for i in sorted(calls.seen)], "sessions": sessions,
           "latency_s": round(time.monotonic() - t0, 3)}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
