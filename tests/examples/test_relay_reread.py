"""
Re-reading an unknown outcome -- only for a tool the operator declared READ_ONLY.

``decide_retry(reread_unknown=True)`` lets the relay ask once more when a
*read* went unanswered (deadline) or was lost with its server (transport).
Everything else keeps the old rule: an unknown outcome is never re-sent.

Ground truth is the scripted servers' execution ledgers and the client's
received messages, read directly. Every test checks that the client got
exactly one answer, and that nothing but the declared read ever reached a
server twice.
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

from stateguard.adapters.mcp import (  # noqa: E402
    Effect,
    EffectDeclaration,
    EffectPolicy,
    Observed,
    decide_retry,
)
from stateguard.core.trust import TrustDecision  # noqa: E402

pytestmark = pytest.mark.integration


def _tool(name: str) -> ServerTool:
    return ServerTool(
        {
            "name": name,
            "inputSchema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }
    )


TOOLS = [_tool("search"), _tool("put"), _tool("send"), _tool("other")]
EFFECTS = {"search": "read_only", "put": "idempotent", "send": "non_idempotent"}


class Plan:
    """The n-th tools/call across every server process gets ``actions[n]``."""

    def __init__(self, *actions: ServerAction) -> None:
        self.actions = list(actions)
        self._order = itertools.count()

    def __call__(self, call: ServerCall) -> ServerAction:
        index = next(self._order)
        return self.actions[index] if index < len(self.actions) else ServerAction.NORMAL


def _rig(
    plan: Plan,
    *,
    reread: bool = True,
    deadline: float | None = None,
    restarts: int = 0,
    policy: str = "idle-only",
    restart_fails: bool = False,
    mode: RelayMode = RelayMode.AUTO,
    server_cls: type[ScriptedServer] = ScriptedServer,
) -> Rig:
    def factory() -> ScriptedServer:
        if restart_fails:
            raise OSError("server cannot be started")
        return server_cls(list(TOOLS), faults=plan)

    rig = Rig(
        server_cls(list(TOOLS), faults=plan),
        mode,
        call_deadline=deadline,
        effects=EffectPolicy(EFFECTS),
        retry="auto",
        server_factory=factory,
        max_restarts=restarts,
        restart_policy=policy,
        reread_unknown=reread,
    )
    rig.request("server/discover")
    assert rig.wait_synced()
    return rig


def _send(rig: Rig, name: str, q: str = "x") -> int:
    rig._next_id += 1
    identifier = rig._next_id
    rig.sent_ids.append(identifier)
    rig.send(
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"q": q}, "_meta": {}},
        }
    )
    return identifier


def _answers(rig: Rig, identifier: int, *, settle: float = 0.6, timeout: float = 8.0) -> list[Any]:
    """Every answer the client got for *identifier*, after it has one and *settle* more."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = [m for m in rig.client_received if isinstance(m, dict) and m.get("id") == identifier]
        if got:
            time.sleep(settle)
            return [
                m for m in rig.client_received if isinstance(m, dict) and m.get("id") == identifier
            ]
        time.sleep(0.02)
    return []


def _ok(message: dict[str, Any]) -> bool:
    return "result" in message and message["result"].get("isError") is not True


def _unknown(message: dict[str, Any]) -> bool:
    data = message.get("error", {}).get("data", {})
    return data.get("io.stateguard/executionState") == "unknown"


def _received(rig: Rig, name: str) -> int:
    """tools/call messages for tool *name* that reached any server."""
    count = 0
    for line in rig.all_received:
        message = json.loads(line)
        if message.get("method") == "tools/call" and message["params"]["name"] == name:
            count += 1
    return count


def _events(rig: Rig, name: str) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("event") == name]


# ---------------------------------------------------------------------------
# The rule (library)
# ---------------------------------------------------------------------------

READ = EffectDeclaration(Effect.READ_ONLY, declared=True)


@pytest.mark.parametrize("observed", [Observed.UNKNOWN_DEADLINE, Observed.UNKNOWN_TRANSPORT])
def test_rule_rereads_only_a_declared_read(observed: Observed) -> None:
    assert decide_retry(observed, READ, attempts_made=1, reread_unknown=True).retry
    for effect in (
        EffectDeclaration(Effect.IDEMPOTENT, declared=True),
        EffectDeclaration(Effect.NON_IDEMPOTENT, declared=True),
        EffectDeclaration(Effect.UNKNOWN, declared=False),
        # a hint is never a declaration
        EffectDeclaration(Effect.READ_ONLY, declared=False, hints={"readOnlyHint": True}),
    ):
        verdict = decide_retry(observed, effect, attempts_made=1, reread_unknown=True)
        assert verdict.decision is TrustDecision.AMBIGUOUS, effect


@pytest.mark.parametrize("observed", [Observed.UNKNOWN_DEADLINE, Observed.UNKNOWN_TRANSPORT])
def test_rule_is_off_by_default_and_bounded(observed: Observed) -> None:
    assert decide_retry(observed, READ, attempts_made=1).decision is TrustDecision.AMBIGUOUS
    spent = decide_retry(observed, READ, attempts_made=2, reread_unknown=True)
    assert spent.decision is TrustDecision.AMBIGUOUS


# ---------------------------------------------------------------------------
# Deadline: the same server, first answer wins
# ---------------------------------------------------------------------------


def test_a_hung_read_is_reread_and_answered_once() -> None:
    rig = _rig(Plan(ServerAction.HANG_BEFORE_EXECUTE), deadline=0.15)
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier)
        assert len(answers) == 1 and _ok(answers[0])
        assert len(rig.all_executed) == 1  # the original never ran; the re-read did
        assert _received(rig, "search") == 2
        (decision,) = [d for d in _events(rig, "retry_decision") if d["acted"]]
        assert decision["observed"] == "unknown_deadline"
    finally:
        rig.close()


def test_a_slow_read_delivers_whichever_answer_comes_first_and_only_that() -> None:
    # Original answers late (after the deadline); the re-read never answers.
    rig = _rig(Plan(ServerAction.DELAYED_RESPONSE, ServerAction.HANG_BEFORE_EXECUTE), deadline=0.3)
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier, settle=0.8, timeout=4.0)
        assert len(answers) == 1 and _ok(answers[0])
        assert _events(rig, "late_answer_delivered")
        time.sleep(0.5)  # the re-read's own deadline passes: no second answer
        assert _events(rig, "unknown_superseded")
        assert (
            len(
                [
                    m
                    for m in rig.client_received
                    if isinstance(m, dict) and m.get("id") == identifier
                ]
            )
            == 1
        )
    finally:
        rig.close()


def test_a_reread_answer_after_the_late_original_is_dropped() -> None:
    # The original's late success arrives first; the re-read's answer comes later still.
    class Slower(Plan):
        def __call__(self, call: ServerCall) -> ServerAction:
            action = super().__call__(call)
            if call.index == 1 or action is ServerAction.DELAYED_RESPONSE:
                return ServerAction.DELAYED_RESPONSE
            return action

    rig = _rig(Slower(ServerAction.DELAYED_RESPONSE), deadline=0.3)
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier, settle=DELAY_SECONDS + 0.6)
        assert len(answers) == 1 and _ok(answers[0])
    finally:
        rig.close()


class LateErrorServer(ScriptedServer):
    """The first tools/call is answered with a tool error, late; the rest normally."""

    first_done: bool = False

    def _call(self, identifier: Any, params: Any, message: Any, writer: Any) -> bool:
        if not LateErrorServer.first_done:
            LateErrorServer.first_done = True
            time.sleep(DELAY_SECONDS)
            self._reply(
                writer,
                identifier,
                {"content": [{"type": "text", "text": "boom"}], "isError": True},
            )
            return True
        return super()._call(identifier, params, message, writer)


def test_a_late_error_does_not_preempt_the_reread() -> None:
    LateErrorServer.first_done = False
    rig = _rig(Plan(), deadline=0.3, server_cls=LateErrorServer)
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier, settle=DELAY_SECONDS + 0.4)
        assert len(answers) == 1 and _ok(answers[0])  # the re-read's success, not the error
        assert not _events(rig, "late_answer_delivered")
    finally:
        rig.close()


@pytest.mark.parametrize("tool", ["put", "send", "other"])
def test_only_a_declared_read_is_reread_after_a_deadline(tool: str) -> None:
    rig = _rig(Plan(ServerAction.HANG_BEFORE_EXECUTE), deadline=0.15)
    try:
        identifier = _send(rig, tool)
        answers = _answers(rig, identifier)
        assert len(answers) == 1 and _unknown(answers[0])
        assert _received(rig, tool) == 1  # never re-sent
    finally:
        rig.close()


def test_without_reread_a_hung_read_is_unknown_as_before() -> None:
    rig = _rig(Plan(ServerAction.HANG_BEFORE_EXECUTE), deadline=0.15, reread=False)
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier)
        assert len(answers) == 1 and _unknown(answers[0])
        assert _received(rig, "search") == 1
    finally:
        rig.close()


def test_a_reread_that_hangs_too_is_unknown_once() -> None:
    rig = _rig(
        Plan(ServerAction.HANG_BEFORE_EXECUTE, ServerAction.HANG_BEFORE_EXECUTE), deadline=0.15
    )
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier, settle=0.6)
        assert len(answers) == 1 and _unknown(answers[0])
        assert _received(rig, "search") == 2  # one re-read, never more
    finally:
        rig.close()


def test_shadow_never_rereads() -> None:
    rig = _rig(Plan(ServerAction.HANG_BEFORE_EXECUTE), deadline=0.15, mode=RelayMode.SHADOW)
    try:
        identifier = _send(rig, "search")
        time.sleep(0.8)
        assert _received(rig, "search") == 1
        assert not any(
            isinstance(m, dict) and m.get("id") == identifier and _ok(m)
            for m in rig.client_received
        )
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# Transport: the restarted server, once its listing is synced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["idle-only", "after-unknown"])
@pytest.mark.parametrize(
    "action", [ServerAction.CLOSE_AFTER_EXECUTE, ServerAction.CLOSE_BEFORE_EXECUTE]
)
def test_a_read_lost_with_its_server_is_reread_on_the_restarted_one(
    policy: str, action: ServerAction
) -> None:
    rig = _rig(Plan(action), restarts=2, policy=policy)
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier)
        assert len(answers) == 1 and _ok(answers[0])
        assert len(rig.servers) == 2
        restarted = rig.servers[1]
        calls = [json.loads(line) for line in restarted.received]
        reread = [m for m in calls if m.get("method") == "tools/call"]
        assert len(reread) == 1 and str(reread[0]["id"]).startswith("stateguard-retry:")
        assert _events(rig, "reread_sent")[0]["generation"] == 1
    finally:
        rig.close()


@pytest.mark.parametrize("tool", ["put", "send", "other"])
def test_a_lost_non_read_is_never_resent_and_blocks_an_idle_only_restart(tool: str) -> None:
    rig = _rig(Plan(ServerAction.CLOSE_AFTER_EXECUTE), restarts=2, policy="idle-only")
    try:
        identifier = _send(rig, tool)
        answers = _answers(rig, identifier)
        assert len(answers) == 1 and _unknown(answers[0])
        assert _received(rig, tool) == 1
        assert len(rig.servers) == 1  # idle-only: a non-read in flight, no restart
        assert _events(rig, "restart_refused")
    finally:
        rig.close()


def test_a_read_and_a_mutation_lost_together_never_resend_the_mutation() -> None:
    # Server takes the read (never answers it), then dies on the mutation.
    rig = _rig(
        Plan(ServerAction.HANG_BEFORE_EXECUTE, ServerAction.CLOSE_AFTER_EXECUTE),
        restarts=2,
        policy="idle-only",
    )
    try:
        read_id = _send(rig, "search")
        send_id = _send(rig, "send")
        read_answers, send_answers = _answers(rig, read_id), _answers(rig, send_id)
        assert len(read_answers) == 1 and _unknown(read_answers[0])
        assert len(send_answers) == 1 and _unknown(send_answers[0])
        assert _received(rig, "send") == 1
        assert len(rig.servers) == 1  # idle-only refused: not every request was a read
    finally:
        rig.close()


def test_reads_and_mutations_lost_together_after_unknown_reread_only_the_reads() -> None:
    rig = _rig(
        Plan(ServerAction.HANG_BEFORE_EXECUTE, ServerAction.CLOSE_AFTER_EXECUTE),
        restarts=2,
        policy="after-unknown",
    )
    try:
        read_id = _send(rig, "search")
        send_id = _send(rig, "send")
        read_answers, send_answers = _answers(rig, read_id), _answers(rig, send_id)
        assert len(read_answers) == 1 and _ok(read_answers[0])
        assert len(send_answers) == 1 and _unknown(send_answers[0])
        assert _received(rig, "send") == 1
        assert [c.name for c in rig.all_executed].count("send") == 1
    finally:
        rig.close()


def test_a_lost_read_whose_restart_fails_is_unknown_once() -> None:
    rig = _rig(
        Plan(ServerAction.CLOSE_BEFORE_EXECUTE),
        restarts=2,
        policy="after-unknown",
        restart_fails=True,
    )
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier, settle=0.6, timeout=12.0)
        assert len(answers) == 1 and _unknown(answers[0])
        assert _received(rig, "search") == 1
    finally:
        rig.close()


def test_a_reread_lost_with_the_restarted_server_too_is_unknown_once() -> None:
    rig = _rig(
        Plan(ServerAction.CLOSE_BEFORE_EXECUTE, ServerAction.CLOSE_BEFORE_EXECUTE),
        restarts=3,
        policy="after-unknown",
    )
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier, settle=0.6, timeout=12.0)
        assert len(answers) == 1 and _unknown(answers[0])
        assert _received(rig, "search") == 2  # the budget allows exactly one re-read
    finally:
        rig.close()


def test_without_reread_a_lost_read_is_unknown_and_idle_only_still_refuses() -> None:
    rig = _rig(Plan(ServerAction.CLOSE_AFTER_EXECUTE), restarts=2, policy="idle-only", reread=False)
    try:
        identifier = _send(rig, "search")
        answers = _answers(rig, identifier)
        assert len(answers) == 1 and _unknown(answers[0])
        assert len(rig.servers) == 1 and _received(rig, "search") == 1
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# The invocation record (stateguard.adapters.mcp.incidence) of a re-read
# ---------------------------------------------------------------------------


def _record(rig: Rig, identifier: int) -> dict[str, Any]:
    (record,) = [
        e for e in rig.audit if e.get("event") == "invocation" and e.get("id") == identifier
    ]
    return record


def test_a_hung_read_answered_by_its_reread_is_recorded_recovered() -> None:
    rig = _rig(Plan(ServerAction.HANG_BEFORE_EXECUTE), deadline=0.15)
    try:
        identifier = _send(rig, "search")
        _answers(rig, identifier)
        record = _record(rig, identifier)
        assert record["resolution"] == "recovered" and record["attempts"] == 2
        assert record["faults"]  # what made the re-read necessary
    finally:
        rig.close()


def test_a_read_lost_with_its_server_and_reread_is_recorded_recovered() -> None:
    rig = _rig(Plan(ServerAction.CLOSE_AFTER_EXECUTE), restarts=2, policy="after-unknown")
    try:
        identifier = _send(rig, "search")
        _answers(rig, identifier)
        record = _record(rig, identifier)
        assert record["resolution"] == "recovered" and record["attempts"] == 2
    finally:
        rig.close()


def test_without_reread_a_hung_read_is_recorded_unknown() -> None:
    rig = _rig(Plan(ServerAction.HANG_BEFORE_EXECUTE), deadline=0.15, reread=False)
    try:
        identifier = _send(rig, "search")
        _answers(rig, identifier)
        assert _record(rig, identifier)["resolution"] == "unknown"
    finally:
        rig.close()
