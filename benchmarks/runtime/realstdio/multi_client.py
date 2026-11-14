"""
The measured MCP client for R2 (TEST-ONLY): the official Python SDK (mcp 2.x) over stdio, making
several calls on one session with up to --concurrency of them in flight at once. COMMAND is a
JSON argv list (the relay, or the shim and server). If the session breaks (its process exits),
every call in flight on it ends as the client saw it, and the calls not yet started go on a new
session (a new process), as an application would reconnect. No call is sent twice unless
--naive (one blind resend after any failure, no key). Prints one JSON line:
``{"calls": [{"first", "final", "retried"}, ...], "sessions": N}`` in the order of the calls.

usage: multi_client.py COMMAND_JSON TOOL CALLS_JSON [--wait S] [--concurrency C] [--naive]
  CALLS_JSON: a list of argument objects, one per call.
"""

import argparse
import json
import os
import time

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError

#: The SDK's error for a request in flight when the connection closed (mcp_types).
CONNECTION_CLOSED = -32000


class SessionLost(Exception):
    """The session's connection closed: the calls not yet started go on a new one."""


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
    def __init__(self, calls, naive):
        self.todo = list(range(len(calls)))
        self.arguments = calls
        self.naive = naive
        self.seen = {}
        self.inflight = set()

    def settle(self, index, outcome):
        self.inflight.discard(index)
        entry = self.seen.setdefault(index, {"first": outcome, "retried": False})
        entry["final"] = outcome
        if self.naive and not _ok(outcome) and not entry["retried"]:
            entry["retried"] = True
            self.todo.insert(0, index)


async def drain(command, tool, calls, wait, concurrency):
    params = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        with anyio.fail_after(20):
            await session.initialize()
            await session.list_tools()
        limiter = anyio.Semaphore(concurrency)

        async def one(index):
            try:
                with anyio.fail_after(wait):
                    result = await session.call_tool(tool, calls.arguments[index])
            except MCPError as exc:
                calls.settle(index, _error(exc))
                if exc.code == CONNECTION_CLOSED and exc.message == "Connection closed":
                    raise SessionLost from exc
                return
            except TimeoutError:
                calls.settle(index, {"status": "timeout"})
                return
            finally:
                limiter.release()
            body = "".join(getattr(b, "text", "") for b in result.content)
            calls.settle(index, {"status": "result", "is_error": bool(result.is_error),
                                 "text": body[:500], "meta": result.meta or {}})

        async with anyio.create_task_group() as tg:
            while calls.todo or calls.inflight:
                if not calls.todo:
                    await anyio.sleep(0.01)  # a naive resend may still be queued by a settle
                    continue
                await limiter.acquire()
                if not calls.todo:
                    limiter.release()
                    continue
                index = calls.todo.pop(0)
                calls.inflight.add(index)
                tg.start_soon(one, index)


def _lost(exc):
    """The session ended because its connection closed (a SessionLost inside the groups)."""
    if isinstance(exc, SessionLost):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_lost(e) for e in exc.exceptions)
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command")
    p.add_argument("tool")
    p.add_argument("calls")
    p.add_argument("--wait", type=float, default=8.0)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--naive", action="store_true")
    a = p.parse_args()
    calls = Calls(json.loads(a.calls), a.naive)
    command = json.loads(a.command)
    t0 = time.monotonic()
    sessions = 0
    while calls.todo and sessions < 50:
        sessions += 1
        try:
            anyio.run(drain, command, a.tool, calls, a.wait, a.concurrency)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # the session broke: every call in flight on it ends
            lost = _lost(exc)
            outcome = (
                {"status": "mcp_error", "code": CONNECTION_CLOSED, "message": "Connection closed",
                 "data": None}
                if lost else _crash(exc)
            )
            inflight = sorted(calls.inflight)
            for index in inflight:
                calls.settle(index, outcome)
            calls.inflight.clear()
            if not inflight and not lost and calls.todo:
                # It broke before a call went out (the handshake): the next call ends with it.
                calls.settle(calls.todo.pop(0), outcome)
    calls.naive = False
    for index in list(calls.todo):  # never sent: 50 sessions broke first
        calls.settle(index, {"status": "crash", "error": "no session could be opened"})
    out = {"calls": [calls.seen[i] for i in sorted(calls.seen)], "sessions": sessions,
           "latency_s": round(time.monotonic() - t0, 3)}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
