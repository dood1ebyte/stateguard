"""
In-process checks of S4's code paths, beside their subprocess tests:
- the stdio relay's ``--serialize-tools`` lane, through the in-process ``Rig``;
- the HTTP relay's event-stream drain (``Relay._drain_later``), with fake streams;
- the injector's recovery-check loader and wiring (``STATEGUARD_RECOVERY_CHECK``).

The subprocess tests (``test_relay_serialize.py``, ``test_http_stream_drain.py``,
``test_inject_python.py``) show the behaviour end to end. Synthetic fixtures, not traffic.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import (  # noqa: E402
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)

pytestmark = pytest.mark.integration

WRITE = ServerTool({"name": "write", "inputSchema": {"type": "object"}})
READ = ServerTool({"name": "read", "inputSchema": {"type": "object"}})


class _First:
    """*action* for the first call of *tool*; NORMAL after."""

    def __init__(self, tool: str, action: ServerAction) -> None:
        self.tool, self.action, self.fired = tool, action, False

    def __call__(self, call: ServerCall) -> ServerAction:
        if call.name == self.tool and not self.fired:
            self.fired = True
            return self.action
        return ServerAction.NORMAL


def _call(ident: Any, tool: str, n: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
            "params": {"name": tool, "arguments": {"n": n}}}  # fmt: skip


def _rig(action: ServerAction, mode: RelayMode = RelayMode.AUTO, **kwargs: Any) -> Rig:
    server = ScriptedServer([WRITE, READ], faults=_First("write", action))
    rig = Rig(server, mode, serialize_tools=("write",), **kwargs)
    rig.request("server/discover")
    assert rig.wait_synced()
    return rig


def _events(rig: Rig, name: str) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("event") == name]


def test_serialised_calls_wait_for_the_previous_answer_in_order() -> None:
    rig = _rig(ServerAction.DELAYED_RESPONSE)  # the first answer comes 0.45 s late
    try:
        for n in range(3):
            rig.send(_call(n, "write", n))
        answers = [rig.wait_for(n, timeout=10) for n in range(3)]
        assert all(a is not None and a["result"]["isError"] is False for a in answers), answers
        assert [c.arguments["n"] for c in rig.server.executed] == [0, 1, 2]
        waited = _events(rig, "serialized")
        assert waited and waited[0]["id"] == 1 and waited[0]["waited_s"] >= 0.3
    finally:
        rig.close()


def test_a_tool_outside_the_lane_is_not_held() -> None:
    rig = _rig(ServerAction.DELAYED_RESPONSE)
    try:
        rig.send(_call("w", "write", 0))
        t0 = time.monotonic()
        rig.send(_call("r", "read", 1))
        assert rig.wait_for("r", timeout=10) is not None
        assert rig.wait_for("w", timeout=10) is not None
        assert not [e for e in _events(rig, "serialized") if e["id"] == "r"]
        assert time.monotonic() - t0 < 5
    finally:
        rig.close()


def test_a_call_the_lane_cannot_take_in_time_is_answered_not_sent() -> None:
    rig = _rig(ServerAction.DROP_RESPONSE, serialize_wait=0.3)  # the first is never answered
    try:
        rig.send(_call(0, "write", 0))
        rig.send(_call(1, "write", 1))
        answer = rig.wait_for(1, timeout=10)
        assert answer is not None and "error" in answer, answer
        assert answer["error"]["data"]["io.stateguard/executionState"] == "not_sent"
        assert [c.arguments["n"] for c in rig.server.executed] == [0]  # 1 never reached it
        (timeout,) = _events(rig, "serialize_timeout")
        assert timeout["id"] == 1
    finally:
        rig.close()


def test_shadow_does_not_serialise_and_says_it_would() -> None:
    rig = _rig(ServerAction.NORMAL, mode=RelayMode.SHADOW)
    try:
        rig.send(_call(0, "write", 0))
        assert rig.wait_for(0, timeout=10) is not None
        assert len(_events(rig, "serialize_would_apply")) == 1
        assert not _events(rig, "serialized")
    finally:
        rig.close()


# -- the HTTP relay's drain of an answered event stream ---------------------------------------------


class _Response:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class _Owner:
    """What ``_drain_later`` uses of the relay: a log and the set of running drains."""

    def __init__(self) -> None:
        self.logged: list[tuple[str, dict[str, Any]]] = []
        self._drains: set[asyncio.Task[None]] = set()

    def log(self, event: str, **fields: Any) -> None:
        self.logged.append((event, fields))


async def _lines(items: list[str], *, then_wait: bool = False) -> AsyncIterator[str]:
    for item in items:
        yield item
    if then_wait:
        await asyncio.sleep(60)


def _drain(
    lines: AsyncIterator[str], *, seconds: float | None = None, limit: int | None = None
) -> tuple[_Owner, _Response]:  # noqa: E501
    pytest.importorskip("httpx2")
    from stateguard.relay import http as relay_http

    owner, response = _Owner(), _Response()

    async def run() -> None:
        relay_http.Relay._drain_later(owner, response, lines, "u-1")  # type: ignore[arg-type]
        await asyncio.gather(*owner._drains)

    saved = relay_http.DRAIN_SECONDS, relay_http.DRAIN_BYTES
    relay_http.DRAIN_SECONDS = seconds if seconds is not None else saved[0]
    relay_http.DRAIN_BYTES = limit if limit is not None else saved[1]
    try:
        asyncio.run(run())
    finally:
        relay_http.DRAIN_SECONDS, relay_http.DRAIN_BYTES = saved
    return owner, response


def test_a_stream_that_ends_is_read_to_its_end_and_closed_quietly() -> None:
    owner, response = _drain(_lines(["", ": end"]))
    assert response.closed == 1 and owner.logged == [] and not owner._drains


def test_a_stream_held_open_is_closed_at_the_time_bound_and_logged() -> None:
    owner, response = _drain(_lines(["x"], then_wait=True), seconds=0.05)
    assert response.closed == 1
    assert owner.logged == [("stream_not_drained", {"id": "u-1", "bytes": 2})]


def test_a_stream_past_the_byte_bound_is_closed_and_logged() -> None:
    owner, response = _drain(_lines(["a" * 10] * 5), limit=25)
    assert response.closed == 1
    (event, fields) = owner.logged[0]
    assert event == "stream_not_drained" and fields["bytes"] > 25


# -- the injector's recovery check ----------------------------------------------------------------


def test_a_recovery_check_loads_by_module_or_file(tmp_path: Path) -> None:
    from stateguard.adapters.mcp.inject import load_recovery_check

    assert load_recovery_check("json:loads")("[1]") == [1]
    script = tmp_path / "checks.py"
    script.write_text("def check(tool, key, arguments):\n    return (tool, key, arguments)\n")
    assert load_recovery_check(f"{script}:check")("t", "k", {}) == ("t", "k", {})


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("json", "use module:function"),
        (":f", "use module:function"),
        ("json:no_such_name", "AttributeError"),
        ("json:__doc__", "is not callable"),
        ("/no/such/file.py:f", "FileNotFoundError"),
    ],
)
def test_a_recovery_check_that_cannot_load_stops_the_server(spec: str, message: str) -> None:
    from stateguard.adapters.mcp.inject import load_recovery_check

    with pytest.raises(SystemExit, match=message):
        load_recovery_check(spec)


class _Tools:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Any]] = []

    def two_phase(self, *names: str, recover: Any = None) -> None:
        self.calls.append((names, recover))


def test_the_check_covers_keyed_tools_not_declared_idempotent() -> None:
    from stateguard.adapters.mcp.idempotency import Recovery
    from stateguard.adapters.mcp.inject import _two_phase

    seen: list[tuple[str, str, Any]] = []

    def check(tool: str, key: str, arguments: Any) -> Recovery:
        seen.append((tool, key, arguments))
        return Recovery.found("done") if tool == "a" else Recovery.absent()

    tools = _Tools()
    _two_phase(tools, ["a", "b", "c"], ["c"], check, 2)
    names = [call[0] for call in tools.calls]
    assert names == [("a",), ("b",), ("c",)]
    found = tools.calls[0][1]("k1", {"x": 1})
    assert found.applied is True and found.result.content[0].text == "done"  # SDK result
    assert tools.calls[1][1]("k2", {}).applied is False
    assert tools.calls[2][1]("k3", {}).applied is False  # c: declared idempotent, rerun
    assert seen == [("a", "k1", {"x": 1}), ("b", "k2", {})]

    plain = _Tools()
    _two_phase(plain, ["a", "b"], [], None)
    assert plain.calls == [(("a", "b"), None)]  # no check: two-phase, no recovery


def test_a_found_result_becomes_the_sdks_result_or_stays_as_given() -> None:
    pytest.importorskip("mcp_types")
    from stateguard.adapters.mcp.inject import _as_tool_result

    text = _as_tool_result("added", 2)
    assert type(text).__name__ == "CallToolResult" and text.content[0].text == "added"
    as_json = _as_tool_result({"content": [{"type": "text", "text": "j"}]}, 2)
    assert as_json.content[0].text == "j"
    assert _as_tool_result({"content": "not a list"}, 2) == {"content": "not a list"}
    assert _as_tool_result(42, 2) == 42
