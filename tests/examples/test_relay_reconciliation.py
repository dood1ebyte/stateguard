"""
Reconciliation in the relay: observe a declared predicate, or escalate -- never resent.

A stateful scripted server keeps a small "filesystem" (a dict shared by every
server process: the ground truth, read by the tests directly, never through
StateGuard). ``move`` is the non-idempotent tool under test; ``read`` is the
declared read-only probe. Each test checks the client's answer, the ledger's
full record, the audit evidence, and -- across every server process -- that the
original call reached a server exactly once and that no restarted server ever
received anything but probes.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import (  # noqa: E402
    DELAY_SECONDS,
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
    load_relay_module,
)

from stateguard.adapters.mcp import (  # noqa: E402
    EffectPolicy,
    ExecutionState,
    Postcondition,
    ReconcilePolicy,
    ResultValidity,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# A stateful server
# ---------------------------------------------------------------------------


def _tool(name: str, params: list[str], **annotations: Any) -> ServerTool:
    definition: dict[str, Any] = {
        "name": name,
        "inputSchema": {
            "type": "object",
            "properties": {p: {"type": "string"} for p in params},
            "required": params,
        },
    }
    if annotations:
        definition["annotations"] = annotations
    return ServerTool(definition)


TOOLS = [
    _tool("move", ["source", "destination"], destructiveHint=True),
    _tool("write", ["path", "content"]),
    _tool("read", ["path"], readOnlyHint=True),
    _tool("peek", ["path"], readOnlyHint=False),  # the server says: not read-only
]


@dataclass
class FsServer(ScriptedServer):
    fs: dict[str, str] = field(default_factory=dict)
    #: How this process answers ``read``: normal | rpc_error | malformed | hang | die | flip
    probe_mode: str = "normal"
    #: For "flip": another actor's change, made right after the first read is answered.
    flip: Callable[[], None] | None = None
    reads: int = 0

    def _call(
        self, identifier: Any, params: dict[str, Any], message: dict[str, Any], writer: IO[bytes]
    ) -> bool:
        name, arguments = params.get("name"), params.get("arguments") or {}
        with self._lock:
            call = ServerCall(self._calls, str(name), arguments, message)
            self._calls += 1
        if name in ("read", "peek"):
            return self._read(identifier, arguments, writer)
        action = self.faults(call)
        if action is ServerAction.CLOSE_BEFORE_EXECUTE:
            self.dead = True
            return False
        if action is ServerAction.HANG_BEFORE_EXECUTE:
            return True
        if action is ServerAction.DELAYED_RESPONSE:
            time.sleep(DELAY_SECONDS)
        result = self._execute(str(name), arguments)
        self.executed.append(call)  # the side effect happened
        if action is ServerAction.CLOSE_AFTER_EXECUTE:
            self.dead = True
            return False
        self._reply(writer, identifier, result)
        return True

    def _execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "move":
            src, dst = arguments["source"], arguments["destination"]
            if dst in self.fs:
                return _result("Destination already exists", error=True)
            if src not in self.fs:
                return _result("ENOENT: no such file", error=True)
            self.fs[dst] = self.fs.pop(src)
            return _result("moved")
        if name == "write":
            self.fs[arguments["path"]] = arguments["content"]
            return _result("written")
        return _result(f"unknown tool {name}", error=True)

    def _read(self, identifier: Any, arguments: dict[str, Any], writer: IO[bytes]) -> bool:
        self.reads += 1
        mode = self.probe_mode
        if mode == "hang":
            return True
        if mode == "die":
            self.dead = True
            return False
        if mode == "rpc_error":
            self._write(
                writer,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {"code": -32603, "message": "boom"},
                    }
                ).encode(),
            )
            return True
        if mode == "malformed":
            self._reply(writer, identifier, {"text": "no content list"})
            return True
        path = arguments.get("path")
        if path in self.fs:
            self._reply(writer, identifier, _result(self.fs[path]))
        else:
            self._reply(writer, identifier, _result("ENOENT: no such file", error=True))
        if mode == "flip" and self.reads == 1 and self.flip is not None:
            self.flip()
        return True


def _result(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


class First:
    """``action`` for the first call of ``tool`` across every process; NORMAL after."""

    def __init__(self, tool: str, action: ServerAction) -> None:
        self.tool, self.action, self.fired = tool, action, False

    def __call__(self, call: ServerCall) -> ServerAction:
        if call.name == self.tool and not self.fired:
            self.fired = True
            return self.action
        return ServerAction.NORMAL


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

READ = {"name": "read", "effect": "read_only"}
PEEK = {"name": "peek", "effect": "read_only"}


def _move_entry(probes: list[dict[str, Any]] | None = None, **block: Any) -> dict[str, Any]:
    return {
        "name": "move",
        "effect": "non_idempotent",
        "reconcile": {
            "timeout_seconds": block.pop("timeout_seconds", 2.0),
            "probes": probes
            or [
                {
                    "tool": "read",
                    "arguments": {"path": "$source"},
                    "expect": "absent",
                    "absent_marker": "ENOENT",
                },
                {
                    "tool": "read",
                    "arguments": {"path": "$destination"},
                    "expect": "present",
                    "absent_marker": "ENOENT",
                },
            ],
        },
    }


def _policies(*entries: dict[str, Any]) -> tuple[EffectPolicy, ReconcilePolicy]:
    entries = entries or (READ, PEEK, _move_entry())
    effects = EffectPolicy.from_entries(entries)
    return effects, ReconcilePolicy.from_entries(entries, effects)


@dataclass
class World:
    rig: Rig
    fs: dict[str, str]


def _world(
    *,
    action: ServerAction = ServerAction.CLOSE_AFTER_EXECUTE,
    actor: Callable[[dict[str, str]], None] | None = None,
    probe_mode: str = "normal",
    flip: Callable[[dict[str, str]], None] | None = None,
    entries: tuple[dict[str, Any], ...] = (),
    tools: list[ServerTool] | None = None,
    mode: RelayMode = RelayMode.AUTO,
    reconcile: bool = True,
    policy: str = "after-unknown",
    max_restarts: int = 2,
    call_deadline: float | None = None,
    attest_termination: bool = True,
    server_cls: type[FsServer] | None = None,
) -> World:
    fs = {"a.txt": "order 001"}
    faults = First("move", action)
    restarted = {"n": 0}

    def factory() -> ScriptedServer:
        restarted["n"] += 1
        if actor is not None and restarted["n"] == 1:
            actor(fs)  # another actor, after the original's server died, before any probe
        return (server_cls or FsServer)(
            tools or TOOLS,
            faults=faults,
            fs=fs,
            probe_mode=probe_mode if restarted["n"] == 1 else "normal",
            flip=(lambda: flip(fs)) if flip is not None else None,
        )

    effects, policy_obj = _policies(*entries)
    rig = Rig(
        (server_cls or FsServer)(tools or TOOLS, faults=faults, fs=fs),
        mode,
        server_factory=factory,
        max_restarts=max_restarts,
        restart_policy=policy,
        effects=effects,
        reconcile=policy_obj if reconcile else None,
        retry="off",
        call_deadline=call_deadline,
        # In-process: the restart joins the old server's serve thread -- its
        # whole executor -- and reports that as scope_ended evidence.
        attest_termination=attest_termination,
    )
    rig.request("server/discover")
    assert rig.wait_synced()
    return World(rig, fs)


def _move(world: World, timeout: float = 8.0) -> dict[str, Any] | None:
    return world.rig.call("move", {"source": "a.txt", "destination": "b.txt"}, timeout=timeout)


def _data(response: dict[str, Any] | None) -> dict[str, Any]:
    assert response is not None, "no answer"
    return dict(response.get("error", {}).get("data", {}))


def _events(rig: Rig, name: str) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("event") == name]


def _invocation(rig: Rig) -> Any:
    inv = rig.relay.ledger.get(rig.sent_ids[-1])
    assert inv is not None
    return inv


def _never_resent(world: World) -> None:
    """The original reached a server exactly once; later servers only saw probes."""
    rig = world.rig
    target = rig.sent_ids[-1]
    received = [json.loads(line) for line in rig.all_received]
    calls = [m for m in received if m.get("method") == "tools/call"]
    assert sum(1 for m in calls if m.get("id") == target) <= 1
    for server in rig.servers[1:]:
        for line in server.received:
            m = json.loads(line)
            if m.get("method") == "tools/call":
                assert m["params"]["name"] in ("read", "peek"), m
                assert str(m["id"]).startswith("stateguard-reconcile:")
    assert len([c for c in rig.all_executed if c.name == "move"]) <= 1
    assert not any(e.get("resent") for e in _events(rig, "reconcile_verdict"))


def _assert_unknown_record(inv: Any, postcondition: Postcondition) -> None:
    assert inv.state is ExecutionState.UNKNOWN
    assert ExecutionState.COMPLETED not in inv.history
    assert inv.postcondition is postcondition
    assert inv.attribution == "unknown"
    assert inv.result_validity is ResultValidity.NO_RESULT
    assert inv.reconciliation and inv.reconciliation[-1]["event"] == "verdict"
    assert inv.reconciliation[-1]["execution_state"] == "unknown"


# ---------------------------------------------------------------------------
# 1, 11: executed, response lost -> observed satisfied; execution and attribution unknown
# ---------------------------------------------------------------------------


def test_executed_and_lost_is_observed_satisfied_but_stays_unknown() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/executionState"] == "unknown"
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        assert data["io.stateguard/attribution"] == "unknown"
        assert world.fs == {"b.txt": "order 001"}  # ground truth: it ran
        inv = _invocation(world.rig)
        _assert_unknown_record(inv, Postcondition.OBSERVED_SATISFIED)
        assert [e["event"] for e in inv.reconciliation] == ["pending", "verdict"]
        probes = _events(world.rig, "reconcile_probe")
        assert [p["status"] for p in probes] == ["satisfied", "satisfied"]
        assert all(p["generation"] == 1 for p in probes)
        (verdict,) = _events(world.rig, "reconcile_verdict")
        assert verdict["postcondition"] == "observed_satisfied" and verdict["resent"] is False
        assert verdict["execution_state"] == "unknown" and verdict["attribution"] == "unknown"
        _never_resent(world)
    finally:
        world.rig.close()


def test_probe_evidence_holds_no_paths_or_contents() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE)
    try:
        _move(world)
        evidence = json.dumps(
            [e for e in world.rig.audit if str(e.get("event", "")).startswith("reconcile")]
            + _invocation(world.rig).reconciliation
        )
        assert "a.txt" not in evidence and "b.txt" not in evidence and "order 001" not in evidence
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# 2: never reached the server -> observed not satisfied, escalated (not "not executed")
# ---------------------------------------------------------------------------


def test_never_reached_is_observed_not_satisfied_and_escalated() -> None:
    world = _world(action=ServerAction.CLOSE_BEFORE_EXECUTE)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/executionState"] == "unknown"  # never "not_sent"
        assert data["io.stateguard/postcondition"] == "observed_not_satisfied"
        assert world.fs == {"a.txt": "order 001"} and not world.rig.all_executed
        _assert_unknown_record(_invocation(world.rig), Postcondition.OBSERVED_NOT_SATISFIED)
        _never_resent(world)
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# 3: executed, then another actor undid it -> observed not satisfied (looks like never ran)
# ---------------------------------------------------------------------------


def test_executed_then_undone_is_observed_not_satisfied_and_never_resent() -> None:
    def undo(fs: dict[str, str]) -> None:
        fs["a.txt"] = fs.pop("b.txt")

    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, actor=undo)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "observed_not_satisfied"
        assert len(world.rig.all_executed) == 1  # it did run
        _assert_unknown_record(_invocation(world.rig), Postcondition.OBSERVED_NOT_SATISFIED)
        _never_resent(world)
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# 4: another actor produced the state -> observed satisfied, attribution unknown
# ---------------------------------------------------------------------------


def test_another_actors_state_is_observed_satisfied_without_claiming_execution() -> None:
    def other(fs: dict[str, str]) -> None:
        fs["b.txt"] = fs.pop("a.txt")

    world = _world(action=ServerAction.CLOSE_BEFORE_EXECUTE, actor=other)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        assert data["io.stateguard/attribution"] == "unknown"
        assert data["io.stateguard/executionState"] == "unknown"
        assert not world.rig.all_executed  # ground truth: the original never ran
        _assert_unknown_record(_invocation(world.rig), Postcondition.OBSERVED_SATISFIED)
        _never_resent(world)
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# 5: stale or contradictory evidence -> undetermined / observed not satisfied
# ---------------------------------------------------------------------------


def test_server_lost_during_the_probe_is_undetermined() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, probe_mode="die")
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        _assert_unknown_record(_invocation(world.rig), Postcondition.UNDETERMINED)
        _never_resent(world)
    finally:
        world.rig.close()


def test_inconsistent_reads_during_one_probe_are_undetermined() -> None:
    probes = [
        {
            "tool": "read",
            "arguments": {"path": "$source"},
            "expect": "absent",
            "absent_marker": "ENOENT",
        },
        {
            "tool": "read",
            "arguments": {"path": "$source"},
            "expect": "absent",
            "absent_marker": "ENOENT",
        },
        {
            "tool": "read",
            "arguments": {"path": "$destination"},
            "expect": "present",
            "absent_marker": "ENOENT",
        },
    ]

    def reappear(fs: dict[str, str]) -> None:
        fs["a.txt"] = "order 001"  # changes between the two reads of the same path

    world = _world(
        action=ServerAction.CLOSE_AFTER_EXECUTE,
        probe_mode="flip",
        flip=reappear,
        entries=(READ, PEEK, _move_entry(probes)),
    )
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        reasons = data["io.stateguard/reconciliation"]["reasons"]
        assert any("inconsistent" in r for r in reasons)
        _never_resent(world)
    finally:
        world.rig.close()


def test_a_contradictory_state_is_observed_not_satisfied() -> None:
    def both(fs: dict[str, str]) -> None:
        fs["b.txt"] = "something else"  # source still there, destination taken

    world = _world(action=ServerAction.CLOSE_BEFORE_EXECUTE, actor=both)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "observed_not_satisfied"
        _never_resent(world)
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# 6: probe fails, times out, or answers malformed data -> undetermined
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "probe_mode, status",
    [
        ("rpc_error", "protocol_error"),
        ("malformed", "malformed"),
        ("hang", "unavailable"),
    ],
)
def test_a_failing_probe_is_undetermined(probe_mode: str, status: str) -> None:
    entries = (READ, PEEK, _move_entry(timeout_seconds=0.3))
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, probe_mode=probe_mode, entries=entries)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert {p["status"] for p in _events(world.rig, "reconcile_probe")} == {status}
        _assert_unknown_record(_invocation(world.rig), Postcondition.UNDETERMINED)
        _never_resent(world)
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# 7: the original executor may still run (deadline) -> no probe at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action", [ServerAction.HANG_BEFORE_EXECUTE, ServerAction.DELAYED_RESPONSE]
)
def test_a_live_executor_is_never_probed(action: ServerAction) -> None:
    world = _world(action=action, call_deadline=0.15)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert "may still be running" in data["io.stateguard/reconciliation"]["reasons"][0]
        if action is ServerAction.DELAYED_RESPONSE:
            time.sleep(DELAY_SECONDS + 0.3)  # it executes late, after the client was answered
            assert world.fs == {"b.txt": "order 001"}
        assert not _events(world.rig, "reconcile_probe")
        assert all(s.reads == 0 for s in world.rig.servers)  # type: ignore[attr-defined]
        assert len(world.rig.servers) == 1  # nothing restarted
        _assert_unknown_record(_invocation(world.rig), Postcondition.UNDETERMINED)
        _never_resent(world)
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# 8: missing, invalid or unusable probe configuration
# ---------------------------------------------------------------------------


def test_without_a_reconcile_policy_nothing_changes() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, reconcile=False)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/executionState"] == "unknown"
        assert "io.stateguard/postcondition" not in data
        assert all(s.reads == 0 for s in world.rig.servers)  # type: ignore[attr-defined]
        inv = _invocation(world.rig)
        assert inv.postcondition is Postcondition.NOT_CHECKED and not inv.reconciliation
        _never_resent(world)
    finally:
        world.rig.close()


def test_without_a_probe_channel_it_escalates_and_probes_nothing() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, policy="idle-only")
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert "no probe channel" in data["io.stateguard/reconciliation"]["reasons"][0]
        assert len(world.rig.servers) == 1
        _never_resent(world)
    finally:
        world.rig.close()


def test_a_probe_tool_the_server_does_not_list_is_unusable() -> None:
    tools = [t for t in TOOLS if t.name != "read"]
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, tools=tools)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert {p["status"] for p in _events(world.rig, "reconcile_probe")} == {"invalid"}
        assert all(s.reads == 0 for s in world.rig.servers)  # type: ignore[attr-defined]
        _never_resent(world)
    finally:
        world.rig.close()


def test_a_probe_tool_the_server_annotates_as_not_read_only_is_never_called() -> None:
    probes = [{"tool": "peek", "arguments": {"path": "$destination"}, "expect": "present"}]
    world = _world(
        action=ServerAction.CLOSE_AFTER_EXECUTE, entries=(READ, PEEK, _move_entry(probes))
    )
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert all(s.reads == 0 for s in world.rig.servers)  # type: ignore[attr-defined]
        _never_resent(world)
    finally:
        world.rig.close()


def test_main_refuses_unsafe_or_unusable_configuration(tmp_path: Path) -> None:
    relay = load_relay_module()
    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text(
        json.dumps({"tools": [{"name": "read", "effect": "idempotent"}, _move_entry()]})
    )
    usable = tmp_path / "usable.json"
    usable.write_text(json.dumps({"tools": [READ, _move_entry()]}))
    never_started = [sys.executable, "-c", "raise SystemExit('the server must not start')"]
    with pytest.raises(SystemExit) as exc:
        relay.main(
            [
                "--effects",
                str(unsafe),
                "--restart",
                "1",
                "--restart-policy",
                "after-unknown",
                "--",
                *never_started,
            ]
        )
    assert exc.value.code == 2
    # A valid reconcile block is refused too, in every mode: this entry point
    # cannot assert that every executor of a lost call has ended.
    for flags in (
        [],
        ["--restart", "1", "--restart-policy", "after-unknown"],
        ["--shadow"],
        ["--passthrough"],
    ):
        with pytest.raises(SystemExit) as exc:
            relay.main(["--effects", str(usable), *flags, "--", *never_started])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# 9: shadow never probes and forwards the original unchanged
# ---------------------------------------------------------------------------


def test_shadow_never_probes() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, mode=RelayMode.SHADOW)
    try:
        world.rig.call("move", {"source": "a.txt", "destination": "b.txt"}, timeout=1.0)
        assert world.rig.client_eof.wait(3.0) or True
        assert len(world.rig.servers) == 1  # shadow never restarts
        assert all(s.reads == 0 for s in world.rig.servers)  # type: ignore[attr-defined]
        assert not _events(world.rig, "reconcile_probe")
        assert _events(world.rig, "reconcile_skipped")[0]["reason"] == "shadow mode never probes"
        sent = [
            json.loads(x)
            for x in world.rig.servers[0].received
            if json.loads(x).get("method") == "tools/call"
        ]
        assert sent and sent[0]["params"]["arguments"] == {
            "source": "a.txt",
            "destination": "b.txt",
        }
        _never_resent(world)
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# 12: result validity is separate from the postcondition
# ---------------------------------------------------------------------------


def test_a_completed_call_has_result_validity_and_no_postcondition() -> None:
    world = _world(action=ServerAction.NORMAL)
    try:
        response = _move(world)
        assert response is not None and response["result"]["isError"] is False
        inv = _invocation(world.rig)
        assert inv.state is ExecutionState.COMPLETED
        assert inv.result_validity is ResultValidity.ENVELOPE_VALID
        assert inv.postcondition is Postcondition.NOT_CHECKED and inv.attribution is None
        assert not _events(world.rig, "reconcile_pending")
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# 13: "not sent" only when the ledger accepted it
# ---------------------------------------------------------------------------


def test_not_sent_is_never_claimed_for_a_request_the_ledger_holds_as_sent() -> None:
    world = _world(action=ServerAction.NORMAL)
    rig = world.rig
    try:
        rig.relay.ledger.received("x-1", "tools/call", "move")
        rig.relay.ledger.sent("x-1")
        rig.relay._answer_not_sent("x-1", "test")
        answer = rig.wait_for("x-1", timeout=2.0)
        assert _data(answer)["io.stateguard/executionState"] == "unknown"  # not "not_sent"
        assert rig.relay.ledger.get("x-1").state is ExecutionState.UNKNOWN
        (event,) = _events(rig, "not_sent_unconfirmed")
        assert event["ledger_state"] == "sent"
        # Already finished: the client has its answer; nothing more is sent.
        rig.relay._answer_not_sent("x-1", "again")
        assert rig.wait_for("x-1", timeout=0.3) is None
        # PENDING: accepted, and only then "not sent".
        rig.relay.ledger.received("x-2", "tools/call", "move")
        rig.relay._answer_not_sent("x-2", "test")
        assert _data(rig.wait_for("x-2", timeout=2.0))["io.stateguard/executionState"] == "not_sent"
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# 10: no scenario resends (and identical inputs give identical verdicts)
# ---------------------------------------------------------------------------


def test_identical_scenarios_give_identical_verdicts() -> None:
    verdicts = []
    for _ in range(3):
        world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE)
        try:
            data = _data(_move(world))
            verdicts.append(json.dumps(data["io.stateguard/reconciliation"], sort_keys=True))
            _never_resent(world)
        finally:
            world.rig.close()
    assert len(set(verdicts)) == 1


def test_a_restart_that_never_succeeds_escalates_without_probing() -> None:
    fs = {"a.txt": "order 001"}
    faults = First("move", ServerAction.CLOSE_AFTER_EXECUTE)

    def broken() -> ScriptedServer:
        raise OSError("cannot start the server")

    effects, policy = _policies()
    rig = Rig(
        FsServer(TOOLS, faults=faults, fs=fs),
        RelayMode.AUTO,
        server_factory=broken,
        max_restarts=2,
        restart_policy="after-unknown",
        effects=effects,
        reconcile=policy,
        retry="off",
    )
    world = World(rig, fs)
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert "no probe channel" in data["io.stateguard/reconciliation"]["reasons"][0]
        assert not _events(rig, "reconcile_probe")
        _assert_unknown_record(_invocation(rig), Postcondition.UNDETERMINED)
        _never_resent(world)
    finally:
        rig.close()
