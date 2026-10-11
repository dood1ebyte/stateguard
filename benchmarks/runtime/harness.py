"""
An in-process rig for driving the stdio relay against a scripted MCP server.

    client (this module)  ->  StdioRelay (examples/mcp/stdio_proxy.py)  ->  ScriptedServer
                          <-                                             <-

Everything runs in one process over OS pipes, on threads, so a test or a
measurement run can:

* inject faults into the *server* (``ScriptedServer(faults=...)``): an error
  result, a dropped response, a closed pipe, garbage, a duplicate response;
* inject faults into *StateGuard* (patch the relay's registry) and check that
  the relay fails open;
* count what the server actually **executed** -- the ground truth for
  duplicate-execution and phantom-success measurements, which a client-side
  view cannot see.

The server speaks the shapes of protocol 2026-07-28 (``server/discover``,
``tools/list``, ``tools/call``, results carrying ``resultType``) and the
``initialize`` handshake, which it simply acknowledges. It is a fixture, not
an MCP implementation.

Deterministic: no clocks in decisions, no randomness here. Fault schedules
are supplied by the caller (see ``faults.py`` for the seeded ones).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

ROOT = Path(__file__).resolve().parents[2]
RELAY_PATH = ROOT / "examples" / "mcp" / "stdio_proxy.py"
PROTOCOL_VERSION = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION}


def load_relay_module() -> Any:
    """Import ``examples/mcp/stdio_proxy.py`` (it is a script, not a package)."""
    name = "stateguard_stdio_proxy"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, RELAY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Server-side faults
# ---------------------------------------------------------------------------


class ServerAction(StrEnum):
    """What the scripted server does with one ``tools/call``."""

    NORMAL = "normal"  # execute, answer
    TOOL_ERROR = "tool_error"  # execute, answer isError: true
    REJECT_ARGUMENTS = "reject_arguments"  # do not execute, answer isError (validation)
    DROP_RESPONSE = "drop_response"  # execute, never answer (ambiguous completion)
    CLOSE_BEFORE_EXECUTE = "close_before_execute"  # close the pipe, nothing executed
    CLOSE_AFTER_EXECUTE = "close_after_execute"  # execute, then close the pipe
    GARBAGE = "garbage"  # execute, answer bytes that are not JSON
    DUPLICATE_RESPONSE = "duplicate_response"  # execute once, answer twice
    HANG_BEFORE_EXECUTE = "hang_before_execute"  # never execute, never answer
    TRANSIENT_ERROR = "transient_error"  # do not execute, answer isError (temporary)
    AUTH_EXPIRED = "auth_expired"  # do not execute, answer a JSON-RPC auth error
    DELAYED_RESPONSE = "delayed_response"  # execute, answer correctly after DELAY_SECONDS
    # execute, answer success with structuredContent breaking the tool's
    # outputSchema (the definition's "x-bad-structured")
    BAD_STRUCTURED = "bad_structured"


#: How late a DELAYED_RESPONSE answer is.
DELAY_SECONDS = 0.45


@dataclass
class ServerCall:
    """One ``tools/call`` as the server saw it."""

    index: int
    name: str
    arguments: Any
    message: dict[str, Any]


#: Decides the server's action for each call. Default: always NORMAL.
FaultPlan = Callable[[ServerCall], ServerAction]


@dataclass
class ServerTool:
    """A tool the scripted server exposes."""

    definition: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.definition["name"])

    def accepts(self, arguments: Any) -> bool:
        """
        The server's own argument check: required present, no undeclared key
        when ``additionalProperties`` is false, declared primitive types match.

        Deliberately simple and independent of StateGuard -- it plays the part
        of the real server's validator, so it must not share code with the
        thing being measured.
        """
        schema = self.definition.get("inputSchema") or {}
        if not isinstance(arguments, dict):
            return False
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in arguments:
                return False
        if schema.get("additionalProperties") is False and set(arguments) - set(properties):
            return False
        types = {
            "string": (str,),
            "integer": (int,),
            "number": (int, float),
            "boolean": (bool,),
            "array": (list,),
            "object": (dict,),
        }
        for name, value in arguments.items():
            sub = properties.get(name)
            if not isinstance(sub, dict):
                continue
            expected = sub.get("type")
            if isinstance(expected, str) and expected in types:
                ok = isinstance(value, types[expected])
                if expected in ("integer", "number") and isinstance(value, bool):
                    ok = False
                if not ok:
                    return False
            if "enum" in sub and value not in sub["enum"]:
                return False
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if "minimum" in sub and value < sub["minimum"]:
                    return False
                if "maximum" in sub and value > sub["maximum"]:
                    return False
        return True


@dataclass
class ScriptedServer:
    """
    A deterministic MCP server on a pair of byte streams. Run with ``serve()``
    on a thread; it returns when its input closes or a fault closes it.
    """

    tools: list[ServerTool]
    faults: FaultPlan = field(default=lambda call: ServerAction.NORMAL)
    #: Every call the server *executed*, in order -- the side-effect ledger.
    executed: list[ServerCall] = field(default_factory=list)
    #: Every line the server received, raw.
    received: list[bytes] = field(default_factory=list)
    _calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set_tools(self, tools: list[ServerTool]) -> None:
        """Swap the listing (schema drift mid-run). No notification is sent."""
        with self._lock:
            self.tools = list(tools)

    def serve(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        try:
            for line in reader:
                self.received.append(line)
                if not self._handle(line, writer):
                    break
        finally:
            with contextlib.suppress(OSError):
                writer.close()

    def _write(self, writer: IO[bytes], payload: bytes) -> None:
        writer.write(payload if payload.endswith(b"\n") else payload + b"\n")
        writer.flush()

    def _reply(self, writer: IO[bytes], identifier: Any, result: dict[str, Any]) -> None:
        self._write(
            writer,
            json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}).encode(),
        )

    def _handle(self, line: bytes, writer: IO[bytes]) -> bool:
        try:
            message = json.loads(line)
        except ValueError:
            return True
        if not isinstance(message, dict) or "method" not in message or "id" not in message:
            return True  # notifications, responses: nothing to answer
        method, identifier = message["method"], message["id"]
        params = message.get("params") or {}
        if method == "initialize":
            self._reply(
                writer,
                identifier,
                {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "scripted", "version": "1"},
                },
            )
            return True
        if method == "server/discover":
            self._reply(
                writer,
                identifier,
                {
                    "resultType": "complete",
                    "supportedVersions": [PROTOCOL_VERSION],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "scripted", "version": "1"},
                },
            )
            return True
        if method == "tools/list":
            with self._lock:
                listing = [tool.definition for tool in self.tools]
            self._reply(writer, identifier, {"resultType": "complete", "tools": listing})
            return True
        if method == "tools/call":
            return self._call(identifier, params, message, writer)
        self._write(
            writer,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
            ).encode(),
        )
        return True

    def _call(
        self, identifier: Any, params: dict[str, Any], message: dict[str, Any], writer: IO[bytes]
    ) -> bool:
        name, arguments = params.get("name"), params.get("arguments")
        with self._lock:
            call = ServerCall(self._calls, str(name), arguments, message)
            self._calls += 1
            tool = next((t for t in self.tools if t.name == name), None)
        action = self.faults(call)

        if action is ServerAction.CLOSE_BEFORE_EXECUTE:
            return False
        if action is ServerAction.HANG_BEFORE_EXECUTE:
            return True
        if action is ServerAction.TRANSIENT_ERROR:
            self._reply(writer, identifier, _error_result(f"{name}: temporarily unavailable"))
            return True
        if action is ServerAction.AUTH_EXPIRED:
            self._write(
                writer,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {"code": -32001, "message": "Unauthorized: token expired"},
                    }
                ).encode(),
            )
            return True
        if tool is None:
            self._reply(writer, identifier, _error_result(f"Unknown tool: {name}"))
            return True
        if action is ServerAction.REJECT_ARGUMENTS or not tool.accepts(arguments):
            self._reply(writer, identifier, _error_result(f"Invalid arguments for {name}"))
            return True

        self.executed.append(call)  # the side effect happens here
        if action is ServerAction.CLOSE_AFTER_EXECUTE:
            return False
        if action is ServerAction.DROP_RESPONSE:
            return True
        if action is ServerAction.GARBAGE:
            self._write(writer, b'{"jsonrpc": "2.0", "id": ' + json.dumps(identifier).encode())
            return True
        if action is ServerAction.TOOL_ERROR:
            self._reply(writer, identifier, _error_result(f"{name} failed while executing"))
            return True
        result: dict[str, Any] = {
            "resultType": "complete",
            "content": [
                {"type": "text", "text": json.dumps({"tool": name, "arguments": arguments})}
            ],
            "isError": False,
        }
        if "outputSchema" in tool.definition:
            key = "x-bad-structured" if action is ServerAction.BAD_STRUCTURED else "x-structured"
            result["structuredContent"] = tool.definition.get(key, {})
        if action is ServerAction.DELAYED_RESPONSE:
            import time

            time.sleep(DELAY_SECONDS)
        self._reply(writer, identifier, result)
        if action is ServerAction.DUPLICATE_RESPONSE:
            self._reply(writer, identifier, result)
        return True


def _error_result(text: str) -> dict[str, Any]:
    return {"resultType": "complete", "content": [{"type": "text", "text": text}], "isError": True}


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------


class RelayMode(StrEnum):
    """Which arm the relay runs as."""

    PASSTHROUGH = "passthrough"  # StateGuard off: the raw baseline
    SHADOW = "shadow"
    AUTO = "auto"


class Rig:
    """
    Client <-> StdioRelay <-> ScriptedServer, in-process, over OS pipes.

    ``request()`` sends one JSON-RPC message from the client side and waits
    for the response with its id, or returns ``None`` after ``timeout``.
    """

    def __init__(
        self,
        server: ScriptedServer,
        mode: RelayMode = RelayMode.AUTO,
        *,
        unreadable: str = "forward",
        listing_wait: float | None = None,
        refresh_before_intervening: bool = True,
        call_deadline: float | None = None,
        effects: Any = None,
        retry: str = "shadow",
    ) -> None:
        from stateguard import ContractGuard, GuardConfig
        from stateguard.core.models.config import RepairMode

        relay_module = load_relay_module()
        if listing_wait is not None:
            relay_module.LISTING_WAIT_SECONDS = listing_wait
        self.server = server
        self.audit: list[dict[str, Any]] = []
        self._responses: queue.Queue[dict[str, Any] | bytes] = queue.Queue()
        self._next_id = 0
        #: Every message the client received, in order (for duplicate detection).
        self.client_received: list[dict[str, Any] | bytes] = []
        #: Ids of the requests sent through ``request()``.
        self.sent_ids: list[Any] = []

        c2r_r, c2r_w = os.pipe()
        r2c_r, r2c_w = os.pipe()
        r2s_r, r2s_w = os.pipe()
        s2r_r, s2r_w = os.pipe()
        self._client_w = os.fdopen(c2r_w, "wb")
        self._client_r = os.fdopen(r2c_r, "rb")

        repair_mode = RepairMode.SHADOW if mode is RelayMode.SHADOW else RepairMode.AUTO
        self._relay_client_out = os.fdopen(r2c_w, "wb")
        self.relay = relay_module.StdioRelay(
            ContractGuard.with_mcp(config=GuardConfig(mode=repair_mode)),
            os.fdopen(c2r_r, "rb"),
            self._relay_client_out,
            os.fdopen(r2s_w, "wb"),
            os.fdopen(s2r_r, "rb"),
            _AuditSink(self.audit),
            unreadable=unreadable,
            passthrough=mode is RelayMode.PASSTHROUGH,
            refresh_before_intervening=refresh_before_intervening,
            call_deadline=call_deadline,
            effects=effects,
            retry=retry,
        )
        server_in, server_out = os.fdopen(r2s_r, "rb"), os.fdopen(s2r_w, "wb")
        self._threads = [
            threading.Thread(target=server.serve, args=(server_in, server_out), daemon=True),
            threading.Thread(target=self._pump_client_side, daemon=True),
            threading.Thread(target=self.relay.pump_client, daemon=True),
            threading.Thread(target=self._pump_server_side, daemon=True),
            threading.Thread(target=self.relay.run_deadlines, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _pump_server_side(self) -> None:
        # As the real relay's process does when its server exits: stop, which
        # closes the client's stdout. Without this the client side never sees EOF.
        try:
            self.relay.pump_server()
        finally:
            with contextlib.suppress(OSError):
                self._relay_client_out.close()

    def wait_synced(self, count: int = 1, *, timeout: float = 5.0) -> bool:
        """Wait until the relay has synced *count* listings (passthrough: no-op)."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sum(1 for e in self.audit if e.get("event") == "tools_synced") >= count:
                return True
            time.sleep(0.01)
        return False

    def _pump_client_side(self) -> None:
        for line in self._client_r:
            try:
                message: dict[str, Any] | bytes = json.loads(line)
            except ValueError:
                message = line  # bytes the client could not parse
            self.client_received.append(message)
            self._responses.put(message)

    def send(self, message: dict[str, Any]) -> None:
        self._client_w.write((json.dumps(message) + "\n").encode())
        self._client_w.flush()

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 5.0
    ) -> dict[str, Any] | None:
        self._next_id += 1
        identifier = self._next_id
        self.sent_ids.append(identifier)
        self.send(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": method,
                "params": {**(params or {}), "_meta": dict(META)},
            }
        )
        return self.wait_for(identifier, timeout=timeout)

    def wait_for(self, identifier: Any, *, timeout: float) -> dict[str, Any] | None:
        deadline_waits = max(1, int(timeout / 0.05))
        for _ in range(deadline_waits):
            try:
                message = self._responses.get(timeout=0.05)
            except queue.Empty:
                continue
            if isinstance(message, dict) and message.get("id") == identifier:
                return message
        return None

    def call(self, name: str, arguments: Any, *, timeout: float = 5.0) -> dict[str, Any] | None:
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    def close(self) -> None:
        """EOF from the client end; it propagates client -> relay -> server -> back."""
        with contextlib.suppress(OSError):
            self._client_w.close()
        for thread in self._threads:
            thread.join(timeout=2)
        self.leaked_threads = sum(1 for t in self._threads if t.is_alive())


class _AuditSink:
    """A text stream the relay can write JSON lines to, collected as dicts."""

    def __init__(self, into: list[dict[str, Any]]) -> None:
        self._into = into

    def write(self, text: str) -> int:
        for line in text.splitlines():
            if line.strip():
                self._into.append(json.loads(line))
        return len(text)

    def flush(self) -> None:
        pass
