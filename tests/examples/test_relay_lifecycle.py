"""
The relay's own requests respect the MCP session lifecycle.

Found by the first real capture (docs/runtime/CAPTURE_1_PLAN.md, capture 1a):
against ``mcp-server-git`` (MCP SDK 1.30.0, the initialize handshake) the
relay sent its own ``tools/list`` right after forwarding the client's
``server/discover`` *probe* -- before ``initialize`` -- and the server
rejected it with ``-32602``. The relay had taken the probe's protocol
``_meta`` as proof of a stateless session.

What must hold:

* no relay-originated request reaches a server before its session is ready:
  under the initialize protocol, until the client's
  ``notifications/initialized`` reached it; under the stateless protocol,
  until the server answered a versioned request;
* the client's own handshake (probe, fallback, initialize) is untouched and
  succeeds, and the relay lists at the first valid moment;
* a legacy session's relay listings carry no stateless-protocol ``_meta``;
* after a restart the replayed handshake comes first, and a restart in the
  middle of the client's handshake defers the listing until it completes.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import META, RelayMode, Rig, ScriptedServer, ServerTool  # noqa: E402

pytestmark = pytest.mark.integration

OWN = "stateguard-relay:"
SEARCH = ServerTool(
    {
        "name": "search",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    }
)


class SdkV1Server(ScriptedServer):
    """
    What MCP SDK 1.x did in capture 1a: an unknown method (``server/discover``)
    is ``-32602``, and so is any request but ``initialize`` / ``ping`` before
    ``notifications/initialized``. ``violations`` lists the methods that were
    sent too early -- by anyone.
    """

    def __init__(self, tools: list[ServerTool]) -> None:
        super().__init__(tools)
        self.violations: list[str] = []

    def _handle(self, line: bytes, writer: IO[bytes]) -> bool:
        message = json.loads(line)
        method, identifier = message.get("method"), message.get("id")
        if method is not None and identifier is not None:
            if method == "server/discover":
                return self._invalid(writer, identifier, "Invalid request parameters")
            if method not in ("initialize", "ping") and not self._initialized:
                self.violations.append(str(method))
                return self._invalid(
                    writer, identifier, "Received request before initialization was complete"
                )
        return super()._handle(line, writer)

    def _invalid(self, writer: IO[bytes], identifier: Any, text: str) -> bool:
        error = {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": text}}
        self._write(writer, json.dumps(error).encode())
        return True


def _received(server: ScriptedServer) -> list[dict[str, Any]]:
    return [json.loads(line) for line in server.received]


def _methods(server: ScriptedServer) -> list[str]:
    return [m.get("method", "<response>") for m in _received(server)]


def _own_listings(server: ScriptedServer) -> list[dict[str, Any]]:
    return [m for m in _received(server) if str(m.get("id", "")).startswith(OWN)]


def _events(rig: Rig, name: str) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("event") == name]


def _initialize(rig: Rig, *, notify: bool = True) -> None:
    """The legacy handshake, as an SDK client sends it (no per-request _meta)."""
    rig.send(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t"},
            },
        }
    )
    answer = rig.wait_for("init", timeout=3.0)
    assert answer is not None and "result" in answer
    if notify:
        rig.send({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _probe_then_fallback(rig: Rig) -> None:
    """SDK 2.x against an SDK 1.x server: the stateless probe fails, then initialize."""
    answer = rig.request("server/discover", timeout=3.0)  # carries the protocol _meta
    assert answer is not None and answer["error"]["code"] == -32602
    _initialize(rig)


def _wait(condition: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# ---------------------------------------------------------------------------
# The capture-1a sequence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [RelayMode.SHADOW, RelayMode.AUTO])
def test_a_failed_probe_never_makes_the_relay_list_before_initialize(mode: RelayMode) -> None:
    server = SdkV1Server([SEARCH])
    rig = Rig(server, mode)
    try:
        _probe_then_fallback(rig)
        assert rig.wait_synced()
        answer = rig.call("search", {"query": "x"})
    finally:
        rig.close()
    assert server.violations == []  # the bug: ["tools/list"]
    methods = _methods(server)
    first_own = next(
        i for i, m in enumerate(_received(server)) if str(m.get("id", "")).startswith(OWN)
    )
    assert methods[:3] == ["server/discover", "initialize", "notifications/initialized"]
    assert first_own > methods.index("notifications/initialized")
    assert answer is not None and "result" in answer
    assert not _events(rig, "tools_list_failed")


def test_a_legacy_sessions_relay_listings_carry_no_probe_meta() -> None:
    server = SdkV1Server([SEARCH])
    rig = Rig(server, RelayMode.AUTO)
    try:
        _probe_then_fallback(rig)
        assert rig.wait_synced()
        rig.call("search", {"query": "x", "limit": "5"})  # repairable: the relay re-lists first
    finally:
        rig.close()
    listings = _own_listings(server)
    assert len(listings) >= 2
    assert all("_meta" not in (m.get("params") or {}) for m in listings)
    assert server.violations == []


def test_a_handshake_only_client_is_listed_after_initialized() -> None:
    server = SdkV1Server([SEARCH])
    rig = Rig(server, RelayMode.AUTO)
    try:
        _initialize(rig, notify=False)
        time.sleep(0.2)  # time enough for an early listing to arrive, if one were sent
        assert _own_listings(server) == []
        rig.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert rig.wait_synced()
    finally:
        rig.close()
    assert server.violations == []
    assert _methods(server)[:3] == ["initialize", "notifications/initialized", "tools/list"]


# ---------------------------------------------------------------------------
# The stateless protocol: listing follows the server's first answer
# ---------------------------------------------------------------------------


class SlowDiscover(ScriptedServer):
    """Answers server/discover only when released; keeps reading meanwhile."""

    def __init__(self, tools: list[ServerTool]) -> None:
        super().__init__(tools)
        self.release = threading.Event()

    def _handle(self, line: bytes, writer: IO[bytes]) -> bool:
        message = json.loads(line)
        if message.get("method") == "server/discover":

            def answer() -> None:
                self.release.wait(5)
                super(SlowDiscover, self)._handle(line, writer)

            threading.Thread(target=answer, daemon=True).start()
            return True
        return super()._handle(line, writer)


def test_a_stateless_session_is_listed_once_the_server_has_answered() -> None:
    server = SlowDiscover([SEARCH])
    rig = Rig(server, RelayMode.AUTO)
    try:
        rig.send(
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": META}}
        )
        time.sleep(0.2)
        assert _own_listings(server) == []  # the probe is not an answer
        server.release.set()
        assert rig.wait_for(1, timeout=3.0) is not None
        assert rig.wait_synced()
        answer = rig.call("search", {"query": "x"})
    finally:
        rig.close()
    (listing, *_) = _own_listings(server)
    assert listing["params"]["_meta"] == META  # the stateless session's version
    assert answer is not None and "result" in answer


def test_a_relay_listing_before_the_session_is_ready_is_deferred_not_sent() -> None:
    server = SdkV1Server([SEARCH])
    rig = Rig(server, RelayMode.AUTO)
    try:
        assert rig.relay._refresh_listing() == "failed"  # the T3 path, before any handshake
        time.sleep(0.1)
        assert _own_listings(server) == []
        (deferred,) = _events(rig, "tools_list_deferred")
        assert deferred["reason"] == "session not initialized"
        _probe_then_fallback(rig)  # the session still comes up, and is listed
        assert rig.wait_synced()
    finally:
        rig.close()
    assert server.violations == []


# ---------------------------------------------------------------------------
# Restarts
# ---------------------------------------------------------------------------


def _restart_rig(servers: list[SdkV1Server]) -> Rig:
    def factory() -> SdkV1Server:
        server = SdkV1Server([SEARCH])
        servers.append(server)
        return server

    first = SdkV1Server([SEARCH])
    servers.append(first)
    return Rig(first, RelayMode.AUTO, server_factory=factory, max_restarts=1)


def test_a_restart_replays_the_handshake_before_any_relay_request() -> None:
    servers: list[SdkV1Server] = []
    rig = _restart_rig(servers)
    try:
        _probe_then_fallback(rig)
        assert rig.wait_synced()
        rig.kill_server()
        assert _wait(lambda: _events(rig, "reinitialized"))
        assert _wait(lambda: rig.relay._synced_generation == 1)
        answer = rig.call("search", {"query": "after"})
    finally:
        rig.close()
    new = servers[1]
    assert new.violations == []
    assert _methods(new)[:3] == ["initialize", "notifications/initialized", "tools/list"]
    assert all("_meta" not in (m.get("params") or {}) for m in _own_listings(new))
    assert answer is not None and "result" in answer


def test_a_restart_mid_handshake_lists_only_after_the_client_finishes_it() -> None:
    servers: list[SdkV1Server] = []
    rig = _restart_rig(servers)
    try:
        _initialize(rig, notify=False)  # answered, but the client has not sent initialized
        rig.kill_server()
        assert _wait(lambda: _events(rig, "reinitialized"))
        time.sleep(0.2)
        new = servers[1]
        assert _methods(new) == ["initialize"]  # the replay only: nothing of the relay's own
        rig.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert _wait(lambda: rig.relay._synced_generation == 1)
        answer = rig.call("search", {"query": "x"})
    finally:
        rig.close()
    assert new.violations == []
    assert _methods(new)[:3] == ["initialize", "notifications/initialized", "tools/list"]
    assert answer is not None and "result" in answer
