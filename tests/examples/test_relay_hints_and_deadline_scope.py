"""
Two opt-ins around re-reads: trusting the server's readOnlyHint, and a call
deadline scoped to the calls the relay can recover.

Both default off; with them off, nothing changes (covered by the existing
retry/re-read suites). Ground truth: the scripted servers' execution ledgers
and the client's received messages.
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
    DELAY_SECONDS,
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)

from stateguard.adapters.mcp import Effect, EffectPolicy  # noqa: E402

pytestmark = pytest.mark.integration


def _tool(name: str, **annotations: Any) -> ServerTool:
    definition: dict[str, Any] = {
        "name": name,
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
    if annotations:
        definition["annotations"] = annotations
    return ServerTool(definition)


TOOLS = [
    _tool("hinted_read", readOnlyHint=True),
    _tool("hinted_write", readOnlyHint=False, idempotentHint=True),
    _tool("plain"),
]


class Plan:
    def __init__(self, *actions: ServerAction) -> None:
        self.actions, self._order = list(actions), itertools.count()

    def __call__(self, call: ServerCall) -> ServerAction:
        index = next(self._order)
        return self.actions[index] if index < len(self.actions) else ServerAction.NORMAL


def _rig(plan: Plan, *, effects: dict[str, str] | None = None, **kwargs: Any) -> Rig:
    rig = Rig(
        ScriptedServer(list(TOOLS), faults=plan),
        RelayMode.AUTO,
        effects=EffectPolicy(effects or {}),
        retry="auto",
        reread_unknown=True,
        server_factory=lambda: ScriptedServer(list(TOOLS), faults=plan),
        **kwargs,
    )
    rig.request("server/discover")
    assert rig.wait_synced()
    return rig


def _call(rig: Rig, name: str, *, settle: float = 0.6, timeout: float = 6.0) -> list[Any]:
    rig._next_id += 1
    identifier = rig._next_id
    rig.send(
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"q": "x"}, "_meta": {}},
        }
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = [m for m in rig.client_received if isinstance(m, dict) and m.get("id") == identifier]
        if got:
            time.sleep(settle)
            break
        time.sleep(0.02)
    return [m for m in rig.client_received if isinstance(m, dict) and m.get("id") == identifier]


def _ok(m: dict[str, Any]) -> bool:
    return "result" in m and m["result"].get("isError") is not True


def _unknown(m: dict[str, Any]) -> bool:
    return m.get("error", {}).get("data", {}).get("io.stateguard/executionState") == "unknown"


def _received(rig: Rig, name: str) -> int:
    return sum(
        1
        for line in rig.all_received
        if (m := json.loads(line)).get("method") == "tools/call" and m["params"]["name"] == name
    )


# -- the library rule ---------------------------------------------------------


def test_only_read_only_hint_is_trusted_and_only_when_asked() -> None:
    policy = EffectPolicy({"declared_write": "non_idempotent"})
    hint = {"readOnlyHint": True, "idempotentHint": True}
    assert not policy.of("t", annotations=hint).declared  # off by default
    trusted = policy.of("t", annotations=hint, trust_read_only_hints=True)
    assert trusted.declared and trusted.effect is Effect.READ_ONLY
    assert trusted.to_dict()["source"] == "server_hint"
    # idempotentHint alone is never trusted
    assert not policy.of(
        "t", annotations={"idempotentHint": True}, trust_read_only_hints=True
    ).declared
    # an operator declaration always wins over a hint
    op = policy.of("declared_write", annotations=hint, trust_read_only_hints=True)
    assert op.effect is Effect.NON_IDEMPOTENT and op.source == "operator"
    assert "source" not in op.to_dict()  # an operator's record is unchanged


# -- trusted hints in the relay ----------------------------------------------


def test_a_trusted_hint_makes_a_hung_read_recoverable_without_an_effects_file() -> None:
    rig = _rig(
        Plan(ServerAction.HANG_BEFORE_EXECUTE), call_deadline=0.2, trust_read_only_hints=True
    )
    try:
        answers = _call(rig, "hinted_read")
        assert len(answers) == 1 and _ok(answers[0])
        decision = next(e for e in rig.audit if e.get("event") == "retry_decision" and e["acted"])
        assert decision["effect"]["source"] == "server_hint"
    finally:
        rig.close()


def test_without_trust_a_hint_changes_nothing() -> None:
    rig = _rig(Plan(ServerAction.HANG_BEFORE_EXECUTE), call_deadline=0.2)
    try:
        answers = _call(rig, "hinted_read")
        assert len(answers) == 1 and _unknown(answers[0])
        assert _received(rig, "hinted_read") == 1
    finally:
        rig.close()


@pytest.mark.parametrize("tool", ["hinted_write", "plain"])
def test_trusting_hints_never_rereads_a_tool_not_hinted_read_only(tool: str) -> None:
    rig = _rig(
        Plan(ServerAction.HANG_BEFORE_EXECUTE), call_deadline=0.2, trust_read_only_hints=True
    )
    try:
        answers = _call(rig, tool)
        assert len(answers) == 1 and _unknown(answers[0])
        assert _received(rig, tool) == 1
    finally:
        rig.close()


def test_an_operator_declaration_overrides_a_read_only_hint() -> None:
    rig = _rig(
        Plan(ServerAction.HANG_BEFORE_EXECUTE),
        effects={"hinted_read": "non_idempotent"},
        call_deadline=0.2,
        trust_read_only_hints=True,
    )
    try:
        answers = _call(rig, "hinted_read")
        assert len(answers) == 1 and _unknown(answers[0])
        assert _received(rig, "hinted_read") == 1
    finally:
        rig.close()


def test_a_trusted_hint_lets_a_lost_read_restart_and_reread() -> None:
    rig = _rig(Plan(ServerAction.CLOSE_AFTER_EXECUTE), max_restarts=2, trust_read_only_hints=True)
    try:
        answers = _call(rig, "hinted_read")
        assert len(answers) == 1 and _ok(answers[0])
        assert len(rig.servers) == 2
    finally:
        rig.close()


# -- deadline scope -----------------------------------------------------------


def test_reads_scope_never_cuts_a_slow_mutation_short() -> None:
    rig = _rig(
        Plan(ServerAction.DELAYED_RESPONSE),
        effects={"plain": "non_idempotent"},
        call_deadline=0.15,
        deadline_scope="recoverable",
    )
    try:
        answers = _call(rig, "plain", timeout=DELAY_SECONDS + 2)
        assert len(answers) == 1 and _ok(answers[0])  # the late answer, delivered
        assert len(rig.all_executed) == 1
    finally:
        rig.close()


def test_all_scope_still_cuts_a_slow_mutation_as_before() -> None:
    rig = _rig(
        Plan(ServerAction.DELAYED_RESPONSE),
        effects={"plain": "non_idempotent"},
        call_deadline=0.15,
    )
    try:
        answers = _call(rig, "plain", settle=DELAY_SECONDS + 0.3)
        assert len(answers) == 1 and _unknown(answers[0])
    finally:
        rig.close()


def test_reads_scope_still_rereads_a_hung_read() -> None:
    rig = _rig(
        Plan(ServerAction.HANG_BEFORE_EXECUTE),
        effects={"plain": "read_only"},
        call_deadline=0.15,
        deadline_scope="recoverable",
    )
    try:
        answers = _call(rig, "plain")
        assert len(answers) == 1 and _ok(answers[0])
    finally:
        rig.close()


def test_reads_scope_leaves_a_hung_mutation_unanswered_by_the_relay() -> None:
    rig = _rig(
        Plan(ServerAction.HANG_BEFORE_EXECUTE),
        effects={"plain": "non_idempotent"},
        call_deadline=0.15,
        deadline_scope="recoverable",
    )
    try:
        answers = _call(rig, "plain", timeout=1.0)
        assert answers == []  # as without a deadline: the client's own timeout decides
        assert _received(rig, "plain") == 1
    finally:
        rig.close()
