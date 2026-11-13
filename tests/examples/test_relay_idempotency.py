"""
Idempotency keys: the one way a mutating call with an unknown outcome is resent.

Only to a server whose own listing says the tool honours keys
(``stateguard.adapters.mcp.idempotency``). The scripted server here keeps a
durable ``IdempotencyStore`` (an SQLite file shared by every process of it).
Ground truth is every server process's execution ledger and the client's
received messages; every test checks that nothing executed twice.
"""

from __future__ import annotations

import itertools
import json
import sys
import time
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

from stateguard.adapters.mcp import EffectPolicy  # noqa: E402
from stateguard.adapters.mcp.idempotency import (  # noqa: E402
    IDEMPOTENCY_KEY,
    BeginStatus,
    IdempotencyStore,
    call_key,
    fingerprint,
    tool_supports_keys,
    with_key,
)

pytestmark = pytest.mark.integration


def _tool(name: str) -> ServerTool:
    return ServerTool(
        {
            "name": name,
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    )


TOOLS = [_tool("pay"), _tool("search"), _tool("plain")]
EFFECTS = {"pay": "non_idempotent", "search": "read_only"}


class Plan:
    def __init__(self, *actions: ServerAction) -> None:
        self.actions, self._order = list(actions), itertools.count()

    def __call__(self, call: ServerCall) -> ServerAction:
        index = next(self._order)
        return self.actions[index] if index < len(self.actions) else ServerAction.NORMAL


# -- the store -----------------------------------------------------------------


def test_store_executes_once_replays_and_never_reruns_in_doubt(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "keys.db")
    fp = fingerprint("pay", {"q": "1"})
    assert store.begin("k1", fp).status is BeginStatus.EXECUTE
    # started, not done: still executing in this instance; in doubt for any other
    assert store.begin("k1", fp).status is BeginStatus.IN_PROGRESS
    other = IdempotencyStore(tmp_path / "keys.db")
    assert other.begin("k1", fp).status is BeginStatus.IN_DOUBT
    store.complete("k1", {"ok": True})
    replay = store.begin("k1", fp)
    assert replay.status is BeginStatus.REPLAY and replay.result == {"ok": True}
    assert store.begin("k1", fingerprint("pay", {"q": "2"})).status is BeginStatus.MISMATCH
    # durable: a new process opening the same file sees the same record
    again = IdempotencyStore(tmp_path / "keys.db")
    assert again.begin("k1", fp).status is BeginStatus.REPLAY


def test_contract_helpers() -> None:
    assert tool_supports_keys({"_meta": {"io.stateguard/idempotency": {"version": 1}}})
    assert not tool_supports_keys({"_meta": {"io.stateguard/idempotency": {"version": 2}}})
    assert not tool_supports_keys({"annotations": {"idempotentHint": True}})
    message = {"params": {"name": "pay", "_meta": {IDEMPOTENCY_KEY: "client-key"}}}
    assert call_key(with_key(message, "relay-key")) == "client-key"  # the client's key wins
    assert call_key(with_key({"params": {}}, "k")) == "k"


# -- the relay ------------------------------------------------------------------


def _rig(
    tmp_path: Path,
    plan: Plan,
    *,
    keys: bool = True,
    advertise_after_restart: bool = True,
    keyed_tools: frozenset[str] | None = None,
    mode: RelayMode = RelayMode.AUTO,
    **kwargs: Any,
) -> Rig:
    store = IdempotencyStore(tmp_path / "keys.db")
    servers = {"n": 0}

    def make() -> ScriptedServer:
        servers["n"] += 1
        advertise = servers["n"] == 1 or advertise_after_restart
        return ScriptedServer(
            list(TOOLS),
            faults=plan,
            idempotency=store if advertise else None,
            keyed_tools=keyed_tools,
        )

    rig = Rig(
        make(),
        mode,
        effects=EffectPolicy(EFFECTS),
        retry="auto",
        reread_unknown=True,
        idempotency_keys=keys,
        server_factory=make,
        **kwargs,
    )
    rig.request("server/discover")
    assert rig.wait_synced()
    return rig


def _send(rig: Rig, name: str, meta: dict[str, Any] | None = None) -> int:
    rig._next_id += 1
    identifier = rig._next_id
    rig.send(
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"q": str(identifier)}, "_meta": meta or {}},
        }
    )
    return identifier


def _answers(rig: Rig, identifier: int, *, settle: float = 0.6, timeout: float = 8.0) -> list[Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(isinstance(m, dict) and m.get("id") == identifier for m in rig.client_received):
            time.sleep(settle)
            break
        time.sleep(0.02)
    return [m for m in rig.client_received if isinstance(m, dict) and m.get("id") == identifier]


def _ok(m: dict[str, Any]) -> bool:
    return "result" in m and m["result"].get("isError") is not True


def _unknown(m: dict[str, Any]) -> bool:
    return m.get("error", {}).get("data", {}).get("io.stateguard/executionState") == "unknown"


def _sent_calls(rig: Rig, name: str) -> list[dict[str, Any]]:
    return [
        m
        for line in rig.all_received
        if (m := json.loads(line)).get("method") == "tools/call" and m["params"]["name"] == name
    ]


def _executed(rig: Rig, name: str) -> int:
    return sum(1 for c in rig.all_executed if c.name == name)


def test_keys_go_only_to_advertised_non_read_tools(tmp_path: Path) -> None:
    rig = _rig(tmp_path, Plan(), keyed_tools=frozenset({"pay", "search"}))
    try:
        for name in ("pay", "search", "plain"):
            assert len(_answers(rig, _send(rig, name), settle=0.1)) == 1
        assert call_key(_sent_calls(rig, "pay")[0])  # advertised, mutating
        assert call_key(_sent_calls(rig, "search")[0]) is None  # a declared read needs none
        assert call_key(_sent_calls(rig, "plain")[0]) is None  # not advertised
    finally:
        rig.close()


@pytest.mark.parametrize("mode, keys", [(RelayMode.SHADOW, True), (RelayMode.AUTO, False)])
def test_no_keys_in_shadow_or_when_off(tmp_path: Path, mode: RelayMode, keys: bool) -> None:
    rig = _rig(tmp_path, Plan(), keys=keys, mode=mode)
    try:
        _answers(rig, _send(rig, "pay"), settle=0.1)
        assert call_key(_sent_calls(rig, "pay")[0]) is None
    finally:
        rig.close()


def test_a_lost_answer_is_replayed_from_the_record_after_a_deadline(tmp_path: Path) -> None:
    rig = _rig(tmp_path, Plan(ServerAction.DROP_RESPONSE), call_deadline=0.2)
    try:
        answers = _answers(rig, _send(rig, "pay"))
        assert len(answers) == 1 and _ok(answers[0])
        assert _executed(rig, "pay") == 1  # executed once; the resend was a replay
        sent = _sent_calls(rig, "pay")
        assert len(sent) == 2 and call_key(sent[0]) == call_key(sent[1])
    finally:
        rig.close()


def test_a_call_lost_with_its_server_is_replayed_by_the_restarted_one(tmp_path: Path) -> None:
    rig = _rig(tmp_path, Plan(ServerAction.CLOSE_AFTER_EXECUTE), max_restarts=2)
    try:
        answers = _answers(rig, _send(rig, "pay"))
        assert len(answers) == 1 and _ok(answers[0])
        assert _executed(rig, "pay") == 1 and len(rig.servers) == 2
    finally:
        rig.close()


@pytest.mark.parametrize(
    "action, extra",
    [
        (ServerAction.CLOSE_BEFORE_EXECUTE, {"max_restarts": 2}),
        (ServerAction.HANG_BEFORE_EXECUTE, {"call_deadline": 0.2}),
    ],
)
def test_a_key_in_doubt_is_unknown_and_never_executed_again(
    tmp_path: Path, action: ServerAction, extra: dict[str, Any]
) -> None:
    rig = _rig(tmp_path, Plan(action), **extra)
    try:
        answers = _answers(rig, _send(rig, "pay"))
        assert len(answers) == 1 and _unknown(answers[0])
        assert _executed(rig, "pay") == 0  # the resend was refused, not executed
        assert any(e.get("event") == "idempotency_in_doubt" for e in rig.audit)
    finally:
        rig.close()


def test_a_restarted_server_that_stops_advertising_gets_no_resend(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        Plan(ServerAction.CLOSE_AFTER_EXECUTE),
        max_restarts=2,
        restart_policy="after-unknown",
        advertise_after_restart=False,
    )
    try:
        answers = _answers(rig, _send(rig, "pay"))
        assert len(answers) == 1 and _unknown(answers[0])
        assert len(_sent_calls(rig, "pay")) == 1 and _executed(rig, "pay") == 1
    finally:
        rig.close()


def test_an_unadvertised_mutation_is_never_resent(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        Plan(ServerAction.CLOSE_AFTER_EXECUTE),
        max_restarts=2,
        restart_policy="after-unknown",
        keyed_tools=frozenset(),
    )
    try:
        answers = _answers(rig, _send(rig, "pay"))
        assert len(answers) == 1 and _unknown(answers[0])
        assert len(_sent_calls(rig, "pay")) == 1
    finally:
        rig.close()


def test_the_clients_own_key_is_kept_and_reused(tmp_path: Path) -> None:
    rig = _rig(tmp_path, Plan(ServerAction.DROP_RESPONSE), call_deadline=0.2)
    try:
        answers = _answers(rig, _send(rig, "pay", meta={IDEMPOTENCY_KEY: "mine"}))
        assert len(answers) == 1 and _ok(answers[0])
        assert {call_key(m) for m in _sent_calls(rig, "pay")} == {"mine"}
    finally:
        rig.close()


def test_two_keyed_mutations_in_flight_at_loss_each_execute_once(tmp_path: Path) -> None:
    # The first is executed and never answered; the server dies on the second.
    rig = _rig(
        tmp_path,
        Plan(ServerAction.DROP_RESPONSE, ServerAction.CLOSE_AFTER_EXECUTE),
        max_restarts=2,
    )
    try:
        first, second = _send(rig, "pay"), _send(rig, "pay")
        a, b = _answers(rig, first), _answers(rig, second)
        assert len(a) == 1 and _ok(a[0]) and len(b) == 1 and _ok(b[0])
        assert _executed(rig, "pay") == 2  # two calls, each executed exactly once
        keys = [call_key(m) for m in _sent_calls(rig, "pay")]
        assert len(keys) == 4 and len(set(keys)) == 2
    finally:
        rig.close()


class SlowFirstServer(ScriptedServer):
    """The first keyed call runs in the background and answers late (a slow tool)."""

    slow_done: bool = False

    def _call(self, identifier: Any, params: Any, message: Any, writer: Any) -> bool:
        import threading

        if SlowFirstServer.slow_done or params.get("name") != "pay":
            return super()._call(identifier, params, message, writer)
        SlowFirstServer.slow_done = True
        key = self._begin_key(identifier, "pay", params.get("arguments"), message, writer)

        def finish() -> None:
            time.sleep(0.5)
            self.executed.append(ServerCall(-1, "pay", params.get("arguments"), message))
            result = self._result_for(None, "pay", params.get("arguments"), ServerAction.NORMAL)
            if isinstance(key, str):
                self.idempotency.complete(key, result)
            self._reply(writer, identifier, result)

        threading.Thread(target=finish, daemon=True).start()
        return True


def test_a_slow_keyed_call_in_doubt_still_delivers_its_late_answer(tmp_path: Path) -> None:
    SlowFirstServer.slow_done = False
    store = IdempotencyStore(tmp_path / "keys.db")
    rig = Rig(
        SlowFirstServer(list(TOOLS), idempotency=store),
        RelayMode.AUTO,
        effects=EffectPolicy(EFFECTS),
        retry="auto",
        idempotency_keys=True,
        call_deadline=0.3,
        deadline_scope="recoverable",
    )
    rig.request("server/discover")
    assert rig.wait_synced()
    try:
        answers = _answers(rig, _send(rig, "pay"), settle=0.8)
        assert len(answers) == 1 and _ok(answers[0])  # the original's late success
        assert _executed(rig, "pay") == 1
        assert any(e.get("event") == "idempotency_in_doubt" for e in rig.audit)
    finally:
        rig.close()


def test_a_hung_keyed_call_still_running_is_unknown_after_the_answer_timeout(
    tmp_path: Path,
) -> None:
    """
    The original hangs; its resend at the deadline is answered "still running". The deadline is
    when to try again, so the call waits for an answer -- bounded by the answer timeout, after
    which it is unknown (never executed here, never resent again).
    """
    rig = _rig(
        tmp_path,
        Plan(ServerAction.HANG_BEFORE_EXECUTE),
        call_deadline=0.3,
        deadline_scope="recoverable",
        answer_timeout=0.6,
    )
    try:
        started = time.monotonic()
        identifier = _send(rig, "pay")
        answers = _answers(rig, identifier, settle=0.3)
        elapsed = time.monotonic() - started
        assert len(answers) == 1 and _unknown(answers[0])
        assert answers[0]["error"]["data"]["io.stateguard/reason"] == "answer_timeout"
        assert elapsed >= 0.85  # the deadline, then the answer timeout
        assert _executed(rig, "pay") == 0
    finally:
        rig.close()


@pytest.mark.parametrize(("scope", "answered"), [("recoverable", True), ("all", False)])
def test_a_slow_keyed_call_past_its_deadline(tmp_path: Path, scope: str, answered: bool) -> None:
    """
    recoverable (the default): the deadline is when to try again -- a keyed call slower than two
    deadlines gets its answer, executed once. all: the deadline is a hard bound -- unknown.
    """
    rig = _rig(
        tmp_path,
        Plan(ServerAction.DELAYED_RESPONSE),
        call_deadline=0.1,
        deadline_scope=scope,
    )
    try:
        answers = _answers(rig, _send(rig, "pay"))
        assert len(answers) == 1
        assert _ok(answers[0]) if answered else _unknown(answers[0]), answers
        assert _executed(rig, "pay") == 1
    finally:
        rig.close()


def test_a_slow_call_that_cannot_be_resent_is_never_cut_short(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        Plan(ServerAction.DELAYED_RESPONSE),
        keys=False,  # pay is non-idempotent and unkeyed: the relay can never resend it
        call_deadline=0.1,
        deadline_scope="recoverable",
    )
    try:
        answers = _answers(rig, _send(rig, "pay"))
        assert len(answers) == 1 and _ok(answers[0]), answers
        assert _executed(rig, "pay") == 1
    finally:
        rig.close()


def test_a_keyed_call_is_resent_once_after_a_tool_error(tmp_path: Path) -> None:
    # TRANSIENT_ERROR: the server answers isError without executing (nothing recorded).
    rig = _rig(tmp_path, Plan(ServerAction.TRANSIENT_ERROR))
    try:
        answers = _answers(rig, _send(rig, "pay"))
        assert len(answers) == 1 and _ok(answers[0])
        assert _executed(rig, "pay") == 1
        sent = _sent_calls(rig, "pay")
        assert len(sent) == 2 and call_key(sent[0]) == call_key(sent[1])
    finally:
        rig.close()


def test_an_unkeyed_mutation_is_not_resent_after_a_tool_error(tmp_path: Path) -> None:
    rig = _rig(tmp_path, Plan(ServerAction.TRANSIENT_ERROR), keys=False)
    try:
        answers = _answers(rig, _send(rig, "pay"))
        assert len(answers) == 1 and answers[0]["result"]["isError"] is True
        assert len(_sent_calls(rig, "pay")) == 1
    finally:
        rig.close()
