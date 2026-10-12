"""
The relay says what kind of change auto mode would make -- and a declared
default written out for a valid call is not called a repair.

Found by the first real capture (capture 1a): 5 of 18 schema-valid calls to
``mcp-server-git`` were logged ``would_modify`` and summarised as repairs.
Each had only omitted an optional field with a declared ``default``, which
``JSONSchemaAdapter.wrap`` materialises and auto mode would have forwarded.
No field was invalid. The audit now carries ``proposal`` (``none`` /
``default_fill`` / ``repair`` / ``refuse`` / ``escalate``) and the evidence
summary counts default fills apart from repairs and schema violations.
"""

from __future__ import annotations

import io
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
    ({"query": "a", "page": 2}, "default_fill"),  # valid; omits defaulted optionals
    ({"query": "a", "page": 2, "limit": "5"}, "repair"),  # string for integer
    ({"query": "a", "limit": 5, "state": "open"}, "repair"),  # *required* field missing
    ({"page": 2}, "refuse"),  # required field missing, no default: nothing to repair
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


def test_a_default_fill_is_a_valid_call_with_no_field_decision(session: Any) -> None:
    calls, _, _ = session
    (fill,) = [e for e in calls if e["proposal"] == "default_fill"]
    assert fill["decisions"] == [] and fill["auto_action"] == "forward"
    assert all(e["decisions"] for e in calls if e["proposal"] in ("repair", "refuse"))


def test_shadow_still_forwards_every_call_as_received(session: Any) -> None:
    _, summary, _ = session
    wire = summary["wire"]
    assert wire["tool_calls_forwarded_unchanged"] == len(CASES)
    assert wire["tool_calls_forwarded_changed"] == 0


def test_the_evidence_counts_default_fills_apart(session: Any) -> None:
    _, summary, _ = session
    proposed = summary["proposed"]
    assert proposed["would_modify_by_kind"] == {"default_fill": 1, "refuse": 1, "repair": 2}
    assert proposed["would_repair_call"] == 2
    # Schema mismatches are the calls with an invalid field: not the default fill.
    assert summary["stateguard_observed"]["calls_with_schema_violations"] == 3
