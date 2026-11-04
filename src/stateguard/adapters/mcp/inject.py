"""
Operator injection of idempotency keys into an **unmodified** Python MCP
server (``mcp`` 1.x or 2.x), at process start: the Python counterpart of the
TypeScript preload (``adapters/typescript/src/preload.ts``), configured by
the same environment variables:

    STATEGUARD_IDEMPOTENCY_DB=/var/lib/sg/keys.db \\
    STATEGUARD_IDEMPOTENCY_SCOPE=sqlite \\
    STATEGUARD_KEYED_TOOLS=write_query,create_table \\
      /srv/sqlite/venv/bin/python -m stateguard.adapters.mcp.inject \\
        mcp-server-sqlite --db-path /data/app.db

The command after the options is the server, run in this interpreter: a
script path, a console script on ``PATH`` (``mcp-server-sqlite``), or ``-m
module``. stateguard must be importable here: install it into the server's
environment, or put its ``src`` on ``PYTHONPATH`` (it has no dependencies of
its own and uses the server's ``mcp`` and ``anyio``).

Before the server starts, the server's own ``mcp`` is wrapped where a server
starts serving -- the low-level ``Server.run`` (stdio, and 1.x's Streamable
HTTP) and the Streamable HTTP session manager (2.x serves its sessions
without ``Server.run``) -- so that when the server starts serving:

- on ``mcp`` 1.x, its registered ``tools/list`` and ``tools/call`` handlers
  are wrapped (``idempotency_sdk1.KeyedTools.install``);
- on ``mcp`` 2.x, the keyed middleware is appended to its middleware
  (``idempotency_sdk.KeyedTools.middleware``).

``FastMCP`` and ``MCPServer`` run a low-level ``Server``, so they are covered
too. The server's files are not touched; this is an *adopting deployment*,
not an unmodified server.

Keyed tools are two-phase, with no recovery check: the tools' own effects (a
row written, a file changed) carry no key, so a crash between the effect and
its record leaves the key **in doubt**. It is reported as unknown and never
re-run. The store need not be exclusive (exclusivity only serves recovery),
but ``STATEGUARD_IDEMPOTENCY_EXCLUSIVE=1`` makes it so.

TEST-ONLY fault injection (labelled faults, never in production):

    STATEGUARD_FAULT=before_begin|after_begin|after_effect|after_record|hang_after_record
    STATEGUARD_FAULT_FILE=<path>   the fault fires once per file (whatever the process generation)
    STATEGUARD_FAULT_TOOLS=<list>  tools it applies to (default: the keyed tools)

The process kills itself (SIGKILL, as a crash would) at the point;
``hang_after_record`` never answers. Without ``STATEGUARD_IDEMPOTENCY_DB`` the
same points wrap the unmodified handler with no keys at all (``before_*`` =
before the handler, ``after_*`` = after it): the same faults, for arms
without the adapter.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import runpy
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio

from stateguard.adapters.mcp.idempotency import IdempotencyStore

FAULTS = ("before_begin", "after_begin", "after_effect", "after_record", "hang_after_record")

_ATTACHED = "_stateguard_injected"


def _list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


class Faults:
    """The TEST-ONLY crash points, from the environment."""

    def __init__(self, environ: dict[str, str], keyed: list[str]) -> None:
        self.fault = environ.get("STATEGUARD_FAULT")
        if self.fault is not None and self.fault not in FAULTS:
            raise SystemExit(f"stateguard inject: unknown fault {self.fault}")
        file = environ.get("STATEGUARD_FAULT_FILE")
        self.file = Path(file) if file else None
        self.tools = set(_list(environ.get("STATEGUARD_FAULT_TOOLS")) or keyed)

    def fires(self, point: str, tool: str) -> bool:
        if self.fault != point or self.file is None or tool not in self.tools:
            return False
        if self.file.exists():
            return False
        self.file.write_text(point)
        return True

    async def at(self, point: str, tool: str) -> None:
        if not self.fires(point, tool):
            return
        if point == "hang_after_record":
            await anyio.sleep_forever()  # never answer
        os.kill(os.getpid(), signal.SIGKILL)  # as a crash would: no cleanup, no answer

    async def on_point(self, point: str, tool: str) -> None:
        await self.at(point, tool)
        if point == "after_record":
            await self.at("hang_after_record", tool)


def _sdk_major() -> int:
    """The installed ``mcp``'s major version (1 or 2)."""
    try:
        return 1 if int(importlib.metadata.version("mcp").split(".")[0]) < 2 else 2
    except (importlib.metadata.PackageNotFoundError, ValueError):
        # No distribution metadata: 2.x ships its types as a top-level `mcp_types`.
        return 2 if importlib.util.find_spec("mcp_types") is not None else 1


def _attacher(environ: dict[str, str]) -> Callable[[Any], None] | None:
    """What to do to each low-level ``Server`` when it starts, or None (nothing to inject)."""
    db = environ.get("STATEGUARD_IDEMPOTENCY_DB")
    keyed = _list(environ.get("STATEGUARD_KEYED_TOOLS"))
    faults = Faults(environ, keyed)
    if db is None and faults.fault is None:
        return None
    major = _sdk_major()
    if db is not None:
        if not keyed:
            raise SystemExit("stateguard inject: STATEGUARD_KEYED_TOOLS is empty")
        store = IdempotencyStore(
            db,
            scope=environ.get("STATEGUARD_IDEMPOTENCY_SCOPE", "default"),
            exclusive=environ.get("STATEGUARD_IDEMPOTENCY_EXCLUSIVE") == "1",
        )
        print(
            f"stateguard inject: keyed {','.join(keyed)} on mcp {major}.x "
            f"(store {db}, scope {store.scope})",
            file=sys.stderr,
            flush=True,
        )
        if major == 1:
            from stateguard.adapters.mcp import idempotency_sdk1

            tools1 = idempotency_sdk1.KeyedTools(store, on_point=faults.on_point)
            tools1.two_phase(*keyed)
            return tools1.install
        from stateguard.adapters.mcp import idempotency_sdk

        tools2 = idempotency_sdk.KeyedTools(store, on_point=faults.on_point)
        tools2.two_phase(*keyed)
        middleware = tools2.middleware()
        return lambda server: server.middleware.append(middleware)
    # Fault-only: the same crash points around the unmodified handler, no keys.
    return _fault_only_v1(faults) if major == 1 else _fault_only_v2(faults)


async def _around(faults: Faults, tool: str, run: Callable[[], Awaitable[Any]]) -> Any:
    await faults.at("before_begin", tool)
    await faults.at("after_begin", tool)
    result = await run()
    await faults.at("after_effect", tool)
    await faults.at("after_record", tool)
    await faults.at("hang_after_record", tool)
    return result


def _fault_only_v1(faults: Faults) -> Callable[[Any], None]:
    from mcp import types

    def attach(server: Any) -> None:
        handler = server.request_handlers.get(types.CallToolRequest)
        if handler is None:
            return

        async def on_call_tool(request: Any) -> Any:
            return await _around(faults, request.params.name, lambda: handler(request))

        server.request_handlers[types.CallToolRequest] = on_call_tool

    return attach


def _fault_only_v2(faults: Faults) -> Callable[[Any], None]:
    async def middleware(ctx: Any, call_next: Any) -> Any:
        if ctx.request_id is None or ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params if isinstance(ctx.params, dict) else {}
        tool = params.get("name")
        return await _around(faults, tool if isinstance(tool, str) else "", lambda: call_next(ctx))

    return lambda server: server.middleware.append(middleware)


def install(environ: dict[str, str] | None = None) -> bool:
    """
    Wrap the low-level ``Server.run`` of the installed ``mcp`` so each server
    gets the keys (or the test-only faults) the environment asks for when it
    starts. False if the environment asks for nothing.
    """
    attach = _attacher(dict(os.environ if environ is None else environ))
    if attach is None:
        return False
    from mcp.server.lowlevel.server import Server

    def attach_once(server: Any) -> None:
        if not getattr(server, _ATTACHED, False):
            attach(server)
            setattr(server, _ATTACHED, True)

    original_run = Server.run

    async def run(self: Any, *args: Any, **kwargs: Any) -> Any:
        attach_once(self)  # stdio (and 1.x's Streamable HTTP, which runs it per session)
        return await original_run(self, *args, **kwargs)

    Server.run = run  # type: ignore[method-assign]
    # 2.x serves Streamable HTTP sessions without Server.run (runner.serve_loop): the
    # session manager is handed the server when the server's own setup is complete.
    try:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    except ImportError:
        return True
    original_init = StreamableHTTPSessionManager.__init__

    def init(self: Any, app: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, app, *args, **kwargs)
        attach_once(app)

    StreamableHTTPSessionManager.__init__ = init  # type: ignore[method-assign]
    return True


def _run_target(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(
            "usage: python -m stateguard.adapters.mcp.inject (-m MODULE | SCRIPT) [ARGS...]"
        )
    if argv[0] == "-m":
        if len(argv) < 2:
            raise SystemExit("stateguard inject: -m needs a module")
        sys.argv = [argv[1], *argv[2:]]
        runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
        return
    script = argv[0]
    path: str | None = script
    if os.sep not in script and not Path(script).is_file():
        # A console script: this interpreter's own (an unactivated venv), else PATH.
        own = Path(sys.executable).parent / script
        path = str(own) if own.is_file() else shutil.which(script)
    if path is None:
        raise SystemExit(f"stateguard inject: no such script or command: {script}")
    sys.argv = [path, *argv[1:]]
    runpy.run_path(path, run_name="__main__")


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--"]:
        args = args[1:]
    install()
    _run_target(args)


if __name__ == "__main__":
    main()
