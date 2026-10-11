"""
A StateGuard relay for MCP over stdio -- no MCP SDK required.

Put it where an MCP client expects a server command::

    python examples/mcp/stdio_proxy.py [--shadow] [--audit FILE] -- python my_server.py

It launches the real server, and every message passes through it verbatim,
byte for byte, except three it acts on:

``tools/list`` responses (and their pages)
    Collected with ``ToolListAccumulator`` and synced into a
    ``ToolRegistry``, so the relay always holds the server's current
    definitions. The responses themselves still reach the client untouched.
``notifications/tools/list_changed``
    Passed on, and the relay re-lists the tools itself -- with request ids
    in its own namespace, whose responses it consumes -- so validation never
    continues against a listing the server has replaced. Calls that arrive
    while the refresh is in flight wait for it.
``tools/call`` requests
    Validated against the tool's *current* schema. ``FORWARD`` sends the
    original request, with only ``params.arguments`` replaced when it was
    repaired; ``HOLD`` (shadow) sends it unchanged; ``ESCALATE`` / ``REFUSE``
    are answered by the relay with a ``CallToolResult`` carrying
    ``isError: true`` and the reason, and never reach the server.

Results are never touched. A response from the server is written to the
client as the exact bytes the server produced.

Policy, stated rather than buried
---------------------------------
The relay sits in the call path, so it **fails open**: whenever StateGuard
cannot decide, the call goes to the server exactly as the client sent it --
the behaviour the client would have had with no relay at all.

* A call to a tool the relay has no definition for is forwarded unvalidated
  -- whether the tool exists is the server's question, and it will answer it.
* A call to a tool whose schema StateGuard cannot read is forwarded
  unvalidated by default (``--unreadable forward``). ``--unreadable refuse``
  blocks such calls instead; that is a deliberate fail-closed choice, and it
  blocks calls the server may well accept.
* An internal StateGuard error -- an exception, or the forwarding gate
  rejecting StateGuard's own payload -- forwards the original call and is
  logged as ``category: "Z"`` (StateGuard-induced). It never turns into an
  error the client sees, and never stops the relay.
* ``--passthrough`` turns StateGuard off entirely: a byte relay, the
  baseline every measurement is compared against.

Audit log
---------
One JSON object per line (``--audit FILE``; stderr by default): every
listing sync (added / removed / updated / breaking, description changes) and
every call decision (action, what auto mode would do, the reason, and the
field decisions -- paths, kinds and trust, never argument values).

Scope
-----
An example of where StateGuard sits on the wire, kept small enough to read:
no reconnection, no timeouts beyond the listing wait, one server per relay.
MCP's stdio transport is newline-delimited JSON-RPC, which is all this
parses.
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
import threading
from typing import IO, Any

from stateguard import ContractGuard
from stateguard.adapters.mcp import (
    ForwardingInvariantError,
    MalformedMessageError,
    ToolCallRequest,
    ToolListAccumulator,
    ToolNotFoundError,
    ToolRegistry,
    ToolSchemaUnusableError,
    forward_message,
    refusal_response,
)
from stateguard.adapters.mcp.jsonrpc import (
    JSONRPC_VERSION,
    PROTOCOL_VERSION_META_KEY,
    TOOLS_CALL,
    TOOLS_LIST,
    TOOLS_LIST_CHANGED,
    is_notification,
    is_request,
    is_response,
    request_id,
    tool_error_response,
)
from stateguard.core.models.config import GuardConfig, RepairMode

#: Prefix for the ids of the relay's own requests, so their responses can be
#: told apart from the client's. A client is free to choose any id; one that
#: chose this prefix would have its tools/list responses consumed here.
OWN_ID_PREFIX = "stateguard-relay:"

#: How long a tools/call waits for a listing in flight before it is
#: validated against what the relay has (and the audit log says so).
LISTING_WAIT_SECONDS = 10.0


class StdioRelay:
    def __init__(
        self,
        guard: ContractGuard,
        client_in: IO[bytes],
        client_out: IO[bytes],
        server_in: IO[bytes],
        server_out: IO[bytes],
        audit: IO[str],
        *,
        unreadable: str = "forward",
        passthrough: bool = False,
    ) -> None:
        self.registry = ToolRegistry(guard)
        self._client_in, self._client_out = client_in, client_out
        self._server_in, self._server_out = server_in, server_out
        self._audit = audit
        self._unreadable = unreadable
        # The kill switch: relay bytes, decide nothing, learn nothing.
        self._passthrough = passthrough
        self._client_lists = ToolListAccumulator()
        self._own_lists = ToolListAccumulator()
        self._own_ids = (f"{OWN_ID_PREFIX}{n}" for n in itertools.count(1))
        self._listing_ready = threading.Event()
        # Protocol revisions without an initialize handshake (2026-07-28 on)
        # put the version on every request, in params._meta. The relay's own
        # requests must say the same, or the server reads them as another
        # protocol version entirely.
        self._protocol_meta: dict[str, Any] = {}
        self._listed = False
        # True from tools/list_changed until a refresh succeeds: calls made
        # meanwhile are validated against the last listing, and say so.
        self._listing_stale = False
        # Shadow observes; it never blocks. That holds for the paths that are
        # not outcomes too -- an unreadable schema, an internal error.
        self._shadow = guard.config.mode is RepairMode.SHADOW
        self._to_client, self._to_server, self._to_audit = (threading.Lock() for _ in range(3))

    # -- plumbing ------------------------------------------------------------

    def _send(self, stream: IO[bytes], lock: threading.Lock, line: bytes) -> None:
        with lock:
            stream.write(line if line.endswith(b"\n") else line + b"\n")
            stream.flush()

    def _send_json(self, stream: IO[bytes], lock: threading.Lock, message: Any) -> None:
        self._send(stream, lock, json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def _log(self, event: str, **fields: Any) -> None:
        with self._to_audit:
            self._audit.write(json.dumps({"event": event, **fields}, default=str) + "\n")
            self._audit.flush()

    def _request_listing(self, cursor: str | None = None) -> None:
        self._listed = True
        params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
        if self._protocol_meta:
            params["_meta"] = dict(self._protocol_meta)
        message = {
            "jsonrpc": JSONRPC_VERSION,
            "id": next(self._own_ids),
            "method": TOOLS_LIST,
            "params": params,
        }
        self._own_lists.observe_request(message)
        self._send_json(self._server_in, self._to_server, message)

    def _sync(self, tools: list[Any], source: str) -> None:
        report = self.registry.sync(tools)
        self._listing_stale = False
        self._log(
            "tools_synced",
            source=source,
            summary=report.to_dict()["summary"],
            changes=[
                {
                    "tool": str(c.key),
                    "kind": c.kind.value,
                    "breaking": c.breaking,
                    "description_changed": c.description_changed,
                    "state": c.current.state.value if c.current else None,
                }
                for c in report.changes
                if c.kind.value != "unchanged" or c.changed_fields
            ],
        )
        self._listing_ready.set()

    # -- client -> server ----------------------------------------------------

    def pump_client(self) -> None:
        for line in self._client_in:
            if self._passthrough:
                self._send(self._server_in, self._to_server, line)
                continue
            message = _parse(line)
            if is_request(message, TOOLS_CALL):
                self._on_call(line, message)
                continue
            try:
                first_versioned = self._remember_protocol(message)
                if is_request(message, TOOLS_LIST):
                    self._client_lists.observe_request(message)
            except Exception as exc:  # noqa: BLE001 -- fail open; see the module docstring
                self._internal_error("client message", exc)
                first_versioned = False
            self._send(self._server_in, self._to_server, line)
            # Learn the tools before the first call: after the handshake under
            # the initialize-based protocol, at the first versioned request
            # under the stateless one.
            if not self._listed and (
                is_notification(message, "notifications/initialized") or first_versioned
            ):
                self._request_listing()
        with self._to_server:
            self._server_in.close()

    def _internal_error(self, where: str, exc: BaseException, **fields: Any) -> None:
        """Record a StateGuard failure (category Z). The caller fails open."""
        self._log(
            "internal_error",
            category="Z",
            where=where,
            error=f"{type(exc).__name__}: {exc}",
            **fields,
        )

    def _remember_protocol(self, message: Any) -> bool:
        """Record the request's protocol _meta; True the first time one is seen."""
        params = message.get("params") if isinstance(message, dict) else None
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(meta, dict) or PROTOCOL_VERSION_META_KEY not in meta:
            return False
        first = not self._protocol_meta
        self._protocol_meta = {
            key: value for key, value in meta.items() if key.startswith("io.modelcontextprotocol/")
        }
        return first

    def _on_call(self, line: bytes, message: Any) -> None:
        """
        Decide, then send exactly once.

        ``_decide`` only computes where the call goes and with which bytes;
        this method does the one write. So an exception during the decision
        fails open (the original line goes to the server) and an exception
        during the write cannot cause a second send -- a relay that retried
        a write it could not confirm would risk executing the call twice.
        """
        try:
            to_server, payload = self._decide(line, message)
        except Exception as exc:  # noqa: BLE001 -- fail open; see the module docstring
            self._internal_error("tools/call", exc, id=request_id(message), action="unvalidated")
            to_server, payload = True, line
        if to_server:
            self._send(self._server_in, self._to_server, payload)
        else:
            self._send(self._client_out, self._to_client, payload)

    @staticmethod
    def _encode(message: Any) -> bytes:
        return json.dumps(message, separators=(",", ":")).encode("utf-8")

    def _decide(self, line: bytes, message: Any) -> tuple[bool, bytes]:
        """``(to_server, bytes)`` for one ``tools/call``. Never writes to a stream."""
        try:
            request = ToolCallRequest.parse(message)
        except MalformedMessageError:
            return True, line  # the server's to reject

        stale = not self._listing_ready.wait(LISTING_WAIT_SECONDS) or self._listing_stale
        try:
            outcome = self.registry.process_call(request.name, request.arguments)
        except ToolNotFoundError:
            self._log(
                "call",
                id=request.id,
                tool=request.name,
                action="unvalidated",
                reason="no definition for this tool; forwarded for the server to answer",
            )
            return True, line
        except ToolSchemaUnusableError as exc:
            return self._on_unreadable(line, request, exc)
        except ForwardingInvariantError as exc:
            # The gate rejected StateGuard's *own* payload. The client's
            # arguments are not suspect -- forward those, as if no relay.
            self._internal_error("forwarding gate", exc, id=request.id, action="unvalidated")
            return True, line

        self._log(
            "call",
            id=request.id,
            tool=request.name,
            action=outcome.action.value,
            auto_action=(outcome.auto_action or outcome.action).value,
            reason=outcome.reason,
            listing="stale" if stale else "current",
            decisions=[
                {
                    "path": d.path,
                    "decision": d.kind.value,
                    "op": d.operation.op_type.value if d.operation else None,
                    "trust": round(d.operation.trust, 3) if d.operation else None,
                }
                for d in outcome.result.field_decisions()
            ],
        )
        forwarded = forward_message(request, outcome)
        if forwarded is None:
            return False, self._encode(refusal_response(request, outcome))
        if forwarded == request.message:
            return True, line  # byte-for-byte
        return True, self._encode(forwarded)

    def _on_unreadable(
        self, line: bytes, request: ToolCallRequest, exc: ToolSchemaUnusableError
    ) -> tuple[bool, bytes]:
        if self._unreadable == "forward" or self._shadow:
            self._log(
                "call",
                id=request.id,
                tool=request.name,
                action="unvalidated",
                reason=f"schema unreadable; forwarded ({'shadow' if self._shadow else 'policy'}): "
                f"{exc.cause}",
            )
            return True, line
        self._log(
            "call",
            id=request.id,
            tool=request.name,
            action="refuse",
            reason=f"schema unreadable: {exc.cause}",
        )
        text = (
            f"Not sent: StateGuard cannot read the input schema of {request.name!r}, "
            f"so it cannot check calls to it. {exc.cause}"
        )
        return False, self._encode(tool_error_response(request, text))

    # -- server -> client ----------------------------------------------------

    def pump_server(self) -> None:
        for line in self._server_out:
            if self._passthrough:
                self._send(self._client_out, self._to_client, line)
                continue
            message = _parse(line)
            identifier = request_id(message)
            if (
                is_response(message)
                and isinstance(identifier, str)
                and identifier.startswith(OWN_ID_PREFIX)
            ):
                try:
                    self._on_own_listing(message)
                except Exception as exc:  # noqa: BLE001 -- fail open
                    self._internal_error("relay tools/list", exc)
                    self._listing_ready.set()  # never hold calls on a listing that failed
                continue  # the client never asked for this one
            try:
                if is_response(message):
                    listing = self._client_lists.observe_response(message)
                    if listing is not None:
                        self._sync(listing, source="client tools/list")
                elif is_notification(message, TOOLS_LIST_CHANGED):
                    self._listing_stale = True
                    self._listing_ready.clear()
                    self._request_listing()
            except Exception as exc:  # noqa: BLE001 -- fail open
                self._internal_error("server message", exc)
                self._listing_ready.set()
            self._send(self._client_out, self._to_client, line)  # untouched

    def _on_own_listing(self, message: Any) -> None:
        listing = self._own_lists.observe_response(message)
        if listing is not None:
            self._sync(listing, source="relay tools/list")
            return
        result = message.get("result")
        cursor = result.get("nextCursor") if isinstance(result, dict) else None
        if isinstance(cursor, str) and cursor:
            self._request_listing(cursor)
        elif "error" in message:
            self._log("tools_list_failed", error=message["error"])
            self._listing_ready.set()  # do not hold calls forever


def _parse(line: bytes) -> Any:
    try:
        return json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None  # not ours to understand: relayed as-is


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument(
        "--shadow", action="store_true", help="Observe and log; never change a call."
    )
    parser.add_argument("--audit", metavar="FILE", help="Audit log (JSON lines). Default: stderr.")
    parser.add_argument(
        "--unreadable",
        choices=["refuse", "forward"],
        default="forward",
        help="Calls to a tool whose schema StateGuard cannot read (default: forward, unvalidated).",
    )
    parser.add_argument(
        "--passthrough",
        action="store_true",
        help="Kill switch: relay bytes only, StateGuard off. The measurement baseline.",
    )
    parser.add_argument("server", nargs=argparse.REMAINDER, help="-- then the server command")
    args = parser.parse_args(argv)
    command = args.server[1:] if args.server[:1] == ["--"] else args.server
    if not command:
        parser.error("give the server command after --")

    mode = RepairMode.SHADOW if args.shadow else RepairMode.AUTO
    audit = open(args.audit, "a", encoding="utf-8") if args.audit else sys.stderr  # noqa: SIM115
    server = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert server.stdin is not None and server.stdout is not None
    relay = StdioRelay(
        ContractGuard.with_mcp(config=GuardConfig(mode=mode)),
        sys.stdin.buffer,
        sys.stdout.buffer,
        server.stdin,
        server.stdout,
        audit,
        unreadable=args.unreadable,
        passthrough=args.passthrough,
    )
    relay._log(
        "relay_started",
        mode="passthrough" if args.passthrough else mode.value,
        server=command,
    )
    to_server = threading.Thread(target=relay.pump_client, daemon=True)
    to_server.start()
    relay.pump_server()  # until the server closes its stdout
    return server.wait()


if __name__ == "__main__":
    sys.exit(main())
