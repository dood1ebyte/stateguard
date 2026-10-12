"""
The relay says what kind of change auto mode would make, JSON-exactly.

Found by the first real capture (capture 1a): 5 of 18 schema-valid calls to
``mcp-server-git`` were logged ``would_modify`` and summarised as repairs.
Each had only omitted an optional field with a declared ``default``, which
auto mode used to write into the call. The audit carries ``proposal``
(``none`` / ``repair`` / ``refuse`` / ``escalate``; ``default_fill`` only in
recordings from before the MCP boundary stopped writing optional defaults),
and a valid call that omits a defaulted optional is now ``none``: nothing is
written, nothing is held, nothing is re-listed.

The F3 case: ``2.0`` repaired to ``2`` is a change, though Python says
``2 == 2.0`` -- logged ``would_modify``, and in auto sent as the integer.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import RelayMode, Rig, ScriptedServer, ServerTool  # noqa: E402

from stateguard.adapters.mcp import summarize_evidence  # noqa: E402
from stateguard.adapters.mcp.recording import Provenance, Recorder, parse_recording  # noqa: E402

pytestmark = pytest.mark.integration

TOOL = ServerTool(
    {
        "name": "list_items",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "page": {"type": "integer", "default": 1},
                "limit": {"type": "integer", "default": 10},
                "state": {"type": "string", "enum": ["open", "closed"], "default": "open"},
            },
            "required": ["query", "page"],
            "additionalProperties": False,
        },
    }
)

#: (arguments, the proposal auto mode's change would be)
CASES: list[tuple[dict[str, Any], str]] = [
    ({"query": "a", "page": 2, "limit": 5, "state": "open"}, "none"),  # complete and valid
    ({"query": "a", "page": 2}, "none"),  # valid; omits defaulted optionals: left to the server
    ({"query": "a", "page": 2, "limit": "5"}, "repair"),  # string for integer
    ({"query": "a", "limit": 5, "state": "open"}, "repair"),  # *required* field missing
    ({"page": 2}, "refuse"),  # required field missing, no default: nothing to repair
    ({"query": "a", "page": 2.0, "limit": 5, "state": "open"}, "repair"),  # 2.0 -> 2 (F3)
]


@pytest.fixture(scope="module")
def session() -> tuple[list[dict[str, Any]], dict[str, Any], Rig]:
    sink = io.StringIO()
    recorder = Recorder(sink, provenance=Provenance.REAL_CAPTURE, session="proposals")
    rig = Rig(ScriptedServer([TOOL]), RelayMode.SHADOW, recorder=recorder)
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        for arguments, _ in CASES:
            assert rig.call("list_items", arguments) is not None
    finally:
        rig.close()
        recorder.close()
    calls = [e for e in rig.audit if e.get("event") == "call"]
    summary = summarize_evidence(parse_recording(sink.getvalue().splitlines()))
    return calls, summary, rig


def test_each_call_is_classified_by_what_auto_mode_would_change(session: Any) -> None:
    calls, _, _ = session
    assert [e["proposal"] for e in calls] == [kind for _, kind in CASES]
    for event, (_, kind) in zip(calls, CASES, strict=True):
        assert event["would_modify"] is (kind != "none")


def test_a_valid_call_omitting_defaulted_optionals_is_not_a_change(session: Any) -> None:
    calls, _, _ = session
    omitted = calls[1]
    assert omitted["proposal"] == "none" and omitted["would_modify"] is False
    assert omitted["decisions"] == [] and omitted["action"] == "forward"  # not held
    assert omitted["listing"] == "current"  # and no re-listing before it
    assert all(e["decisions"] for e in calls if e["proposal"] in ("repair", "refuse"))
    assert not any(e["proposal"] == "default_fill" for e in calls)


def test_shadow_still_forwards_every_call_as_received(session: Any) -> None:
    _, summary, _ = session
    wire = summary["wire"]
    assert wire["tool_calls_forwarded_unchanged"] == len(CASES)
    assert wire["tool_calls_forwarded_changed"] == 0


def test_the_evidence_counts_only_real_changes(session: Any) -> None:
    _, summary, _ = session
    proposed = summary["proposed"]
    assert proposed["would_modify_by_kind"] == {"refuse": 1, "repair": 3}
    assert proposed["would_repair_call"] == 3
    # Schema mismatches are the calls with an invalid field.
    assert summary["stateguard_observed"]["calls_with_schema_violations"] == 4


def test_a_type_only_repair_reaches_the_server_as_the_repaired_type() -> None:
    """F3 end to end: auto sends 2 (once), shadow sends what it received, 2.0."""
    sent = {"query": "a", "page": 2.0, "limit": 5, "state": "open"}
    for mode, expected in ((RelayMode.AUTO, int), (RelayMode.SHADOW, float)):
        server = ScriptedServer([TOOL])
        rig = Rig(server, mode)
        try:
            rig.request("server/discover")
            assert rig.wait_synced()
            answer = rig.call("list_items", sent)
        finally:
            rig.close()
        assert answer is not None
        received = [json.loads(line) for line in server.received if b'"tools/call"' in line]
        assert len(received) == 1  # exactly one send
        assert type(received[0]["params"]["arguments"]["page"]) is expected, mode
        (event,) = [e for e in rig.audit if e.get("event") == "call"]
        assert event["would_modify"] is True and event["proposal"] == "repair"
