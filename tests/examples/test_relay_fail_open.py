"""
The relay fails open: a StateGuard failure never becomes a client failure.

The relay sits in the call path, so its fallback for anything it cannot
decide is the behaviour the client would have had without it -- the call
goes to the server exactly as sent. These tests inject failures into
StateGuard itself (not the server) and check four things each time:

* the call reaches the server **exactly once**, byte-identical;
* the client gets the server's own answer;
* the failure is logged as category Z (StateGuard-induced);
* the relay keeps working for the next call.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import RelayMode, Rig, ScriptedServer, ServerTool  # noqa: E402

from stateguard.adapters.mcp import ForwardingInvariantError  # noqa: E402

pytestmark = pytest.mark.integration

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


@pytest.fixture
def server() -> ScriptedServer:
    return ScriptedServer([SEARCH])


@pytest.fixture
def rig(server: ScriptedServer) -> Iterator[Rig]:
    r = Rig(server, RelayMode.AUTO)
    r.request("server/discover")
    yield r
    r.close()


def _echoed(response: dict[str, Any] | None) -> Any:
    assert response is not None, "no response"
    return json.loads(response["result"]["content"][0]["text"])["arguments"]


def _z(rig: Rig) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("category") == "Z"]


def _calls_received(server: ScriptedServer) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in server.received
        if json.loads(line).get("method") == "tools/call"
    ]


class _InjectedError(RuntimeError):
    pass


def test_baseline_a_drifted_call_is_repaired(rig: Rig, server: ScriptedServer) -> None:
    assert _echoed(rig.call("search", {"qury": "cats", "limit": "3"})) == {
        "query": "cats",
        "limit": 3,
    }
    assert _z(rig) == []


@pytest.mark.parametrize(
    "error",
    [_InjectedError("injected"), RecursionError("injected"), ForwardingInvariantError("injected")],
    ids=["exception", "recursion", "forwarding-gate"],
)
def test_a_decision_failure_forwards_the_original_call_once(
    rig: Rig, server: ScriptedServer, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(rig.relay.registry, "process_call", explode)
    sent = {"qury": "cats", "limit": "3"}
    response = rig.call("search", sent)

    assert response is not None and "result" in response
    calls = _calls_received(server)
    assert len(calls) == 1
    assert calls[0]["params"]["arguments"] == sent  # unrepaired: StateGuard could not decide
    (z,) = _z(rig)
    assert z["action"] == "unvalidated" and "injected" in z["error"]

    monkeypatch.undo()
    assert _echoed(rig.call("search", {"query": "dogs"})) == {"query": "dogs"}


def test_a_listing_failure_does_not_stall_or_block(
    server: ScriptedServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(server, RelayMode.AUTO, listing_wait=5.0)
    try:

        def explode(*args: Any, **kwargs: Any) -> Any:
            raise _InjectedError("sync failed")

        monkeypatch.setattr(rig.relay.registry, "sync", explode)
        assert rig.request("server/discover") is not None
        response = rig.call("search", {"query": "x"}, timeout=3.0)  # well under listing_wait
        assert _echoed(response) == {"query": "x"}
        assert any("sync failed" in z["error"] for z in _z(rig))
    finally:
        rig.close()


def test_a_client_message_failure_still_relays(
    rig: Rig, server: ScriptedServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise _InjectedError("observe failed")

    monkeypatch.setattr(rig.relay._client_lists, "observe_request", explode)
    response = rig.request("tools/list")
    assert response is not None and response["result"]["tools"]
    assert any("observe failed" in z["error"] for z in _z(rig))


def test_passthrough_arm_changes_nothing(server: ScriptedServer) -> None:
    rig = Rig(server, RelayMode.PASSTHROUGH)
    try:
        rig.request("server/discover")
        response = rig.call("search", {"qury": "cats"})
        assert response is not None and response["result"]["isError"] is True  # unrepaired
        assert _calls_received(server)[-1]["params"]["arguments"] == {"qury": "cats"}
        assert rig.audit == []
    finally:
        rig.close()
