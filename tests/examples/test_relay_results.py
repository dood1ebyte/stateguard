"""The relay checks results against outputSchema, logs findings, and never alters them."""

from __future__ import annotations

import json
import sys
from pathlib import Path

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

WEATHER = ServerTool(
    {
        "name": "get_weather",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"temp_c": {"type": "number"}},
            "required": ["temp_c"],
        },
        "x-structured": {"temp_c": 21.5},
        "x-bad-structured": {"temp_c": "warm"},
    }
)


@pytest.mark.parametrize("mode", [RelayMode.AUTO, RelayMode.SHADOW])
@pytest.mark.parametrize("action", [ServerAction.NORMAL, ServerAction.BAD_STRUCTURED])
def test_results_are_checked_and_delivered_unchanged(mode: RelayMode, action: ServerAction) -> None:
    def plan(call: ServerCall) -> ServerAction:
        return action

    rig = Rig(ScriptedServer([WEATHER], faults=plan), mode)
    try:
        rig.request("server/discover")
        rig.wait_synced()
        response = rig.call("get_weather", {"city": "Pune"})
        assert response is not None
        expected = WEATHER.definition[
            "x-bad-structured" if action is ServerAction.BAD_STRUCTURED else "x-structured"
        ]
        assert response["result"]["structuredContent"] == expected  # untouched
        findings = [e for e in rig.audit if e.get("event") == "result_check"]
        if action is ServerAction.BAD_STRUCTURED:
            (finding,) = findings
            assert finding["category"] == "H" and finding["ok"] is False
            assert finding["issues"][0]["paths"] == ["temp_c"]
            assert "warm" not in json.dumps(finding)  # value-free
        else:
            assert findings == []
    finally:
        rig.close()
