"""
T11 in the relay: an auth failure is recognised, logged as category K,
never retried, never altered -- and nothing else is mistaken for one.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import IO, Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime import auth  # noqa: E402
from runtime.harness import (  # noqa: E402
    DELAY_SECONDS,
    RelayMode,
    Rig,
    ScriptedServer,
    ServerTool,
)

from stateguard.adapters.mcp import EffectPolicy, ExecutionState  # noqa: E402

pytestmark = pytest.mark.integration

AUTH = ("authentication", "authorization")


@pytest.mark.parametrize("arm", auth.ARMS)
@pytest.mark.parametrize("case", auth.CASES, ids=[c.name for c in auth.CASES])
def test_every_case_is_classified_and_handled(case: auth.Case, arm: str) -> None:
    row = auth.run_case(case, arm)
    assert row["classified"] == row["expected"], row
    if row["expected"] in AUTH:
        assert row["auth_events"] == 1
        assert row["retries"] == 0 and row["executions"] == 0
        assert row["client_success"] is False
        assert row["delivered_as_sent"] is True  # never altered, never "repaired"
    else:
        assert row["auth_events"] == 0  # no false positive, UNKNOWN never reclassified
    if row["expected"] == "unknown":
        assert row["retries"] == 0  # UNKNOWN (local or upstream) is never re-sent


def test_the_measurement_is_clean() -> None:
    summary = auth.measure()
    assert summary["correctly_classified"] == summary["runs"]
    assert summary["auth_retried"] == 0 and summary["auth_turned_into_success"] == 0
    assert summary["auth_delivered_as_sent"] == summary["auth_runs"] > 0
    assert summary["false_positive_auth"] == 0
    assert summary["unknown_reclassified_as_auth"] == 0
    assert summary["unknown_retried"] == 0


def _rig(server: ScriptedServer, mode: RelayMode = RelayMode.AUTO, **kwargs: Any) -> Rig:
    rig = Rig(server, mode, **kwargs)
    rig.request("server/discover")
    rig.wait_synced()
    return rig


def _server(case: auth.Case) -> auth.CannedServer:
    return auth.CannedServer(
        [ServerTool(t) for t in auth.TOOLS],
        replies={json.dumps(case.arguments, sort_keys=True): case},
    )


def _case(name: str) -> auth.Case:
    return next(c for c in auth.CASES if c.name == name)


def test_shadow_logs_the_class_and_changes_nothing() -> None:
    case = _case("tool_result_401")
    rig = _rig(
        _server(case), RelayMode.SHADOW, retry="auto", effects=EffectPolicy({"search": "read_only"})
    )
    try:
        answer = rig.call(case.tool, case.arguments)
    finally:
        rig.close()
    assert case.first is not None
    assert answer == {"jsonrpc": "2.0", "id": rig.sent_ids[-1], **case.first}
    assert [e["failure"] for e in rig.audit if e.get("event") == "auth_failure"] == [
        "authentication"
    ]
    sent = [line for line in rig.all_received if b'"tools/call"' in line]
    assert len(sent) == 1


def test_the_retry_decision_is_logged_as_category_k() -> None:
    case = _case("rpc_structured_403")
    rig = _rig(_server(case), retry="auto", effects=EffectPolicy({"search": "read_only"}))
    try:
        rig.call(case.tool, case.arguments)
    finally:
        rig.close()
    (decision,) = [e for e in rig.audit if e.get("event") == "retry_decision"]
    assert decision["category"] == "K"
    assert decision["observed"] == "auth_error" and decision["acted"] is False
    (event,) = [e for e in rig.audit if e.get("event") == "auth_failure"]
    assert (event["category"], event["failure"], event["signal"]) == (
        "K",
        "authorization",
        "data:status",
    )
    assert event["tool"] == "search"


class _TransientThenUnauthorized(ScriptedServer):
    """First attempt: a transient tool error. The retry: 401 as a tool result."""

    attempts = 0

    def _call(
        self, identifier: Any, params: dict[str, Any], message: dict[str, Any], writer: IO[bytes]
    ) -> bool:
        self.attempts += 1
        text = "temporarily unavailable" if self.attempts == 1 else "401 Unauthorized"
        self._reply(
            writer,
            identifier,
            {
                "resultType": "complete",
                "content": [{"type": "text", "text": text}],
                "isError": True,
            },
        )
        return True


def test_an_auth_failure_on_the_retry_ends_it_there() -> None:
    server = _TransientThenUnauthorized([ServerTool(t) for t in auth.TOOLS])
    rig = _rig(server, retry="auto", effects=EffectPolicy({"search": "read_only"}))
    try:
        answer = rig.call("search", {"query": "x"})
        time.sleep(0.05)
    finally:
        rig.close()
    assert server.attempts == 2  # the original and one retry -- no third
    assert answer is not None and answer["id"] == rig.sent_ids[-1]
    assert answer["result"]["content"][0]["text"] == "401 Unauthorized"
    (event,) = [e for e in rig.audit if e.get("event") == "auth_failure"]
    assert event["id"] == rig.sent_ids[-1] and event["failure"] == "authentication"


class _UnauthorizedListing(ScriptedServer):
    def _handle(self, line: bytes, writer: IO[bytes]) -> bool:
        message = json.loads(line)
        if message.get("method") == "tools/list":
            self._write(
                writer,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32603,
                            "message": "Request failed",
                            "data": {"status": 401},
                        },
                    }
                ).encode(),
            )
            return True
        return super()._handle(line, writer)


def test_an_auth_failure_on_the_relays_own_listing_is_category_k() -> None:
    rig = Rig(_UnauthorizedListing([ServerTool(t) for t in auth.TOOLS]), RelayMode.AUTO)
    try:
        rig.request("server/discover")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(
            e.get("event") == "tools_list_failed" for e in rig.audit
        ):
            time.sleep(0.01)
    finally:
        rig.close()
    (failed,) = [e for e in rig.audit if e.get("event") == "tools_list_failed"]
    assert (failed["category"], failed["failure"]) == ("K", "authentication")


def test_a_late_auth_answer_never_reclassifies_an_unknown_outcome() -> None:
    case = _case("late_auth_after_deadline")
    rig = _rig(_server(case), call_deadline=0.2, retry="auto")
    try:
        answer = rig.call(case.tool, case.arguments, timeout=3.0)
        time.sleep(DELAY_SECONDS + 0.1)  # let the late 401 arrive
    finally:
        rig.close()
    identifier = rig.sent_ids[-1]
    assert answer is not None
    assert answer["error"]["data"]["io.stateguard/executionState"] == "unknown"
    assert rig.relay.ledger.get(identifier).state is ExecutionState.UNKNOWN
    assert not [e for e in rig.audit if e.get("event") == "auth_failure"]
    assert any(
        e.get("event") == "response_dropped" and e.get("disposition") == "late" for e in rig.audit
    )
    assert len([m for m in rig.client_received if m.get("id") == identifier]) == 1
