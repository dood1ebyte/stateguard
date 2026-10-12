"""
The relay records enough for the evidence summary to tell apart what Capture 1b
could not: a field StateGuard found invalid, a key an open schema allows, and a
call for a tool the listing does not name.

Capture 1b reported calls_with_schema_violations 19 for 15 server-rejected
calls (plus 2 StateGuard-stricter integral floats): the remaining 2 were S05/S06,
schema-valid calls whose only decision was an undeclared key; and calls_decided
counted U01, an unvalidated call, under listing "None".
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

OPEN = ServerTool(
    {
        "name": "git_log",
        "inputSchema": {
            "type": "object",
            "properties": {"repo_path": {"type": "string"}, "max_count": {"type": "integer"}},
            "required": ["repo_path"],
        },
    }
)
CLOSED = ServerTool(
    {
        "name": "git_show",
        "inputSchema": {
            "type": "object",
            "properties": {"repo_path": {"type": "string"}, "revision": {"type": "string"}},
            "required": ["repo_path", "revision"],
            "additionalProperties": False,
        },
    }
)
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("git_log", {"repo_path": "/r"}),  # valid
    ("git_log", {"repo_path": "/r", "verbose": True}),  # S05: undeclared, open schema
    ("git_log", {"repo_path": "/r", "max_count": True}),  # X01: violation
    ("git_show", {"repo_path": "/r", "revision": "HEAD", "verbose": True}),  # closed: violation
    ("git_stat", {"repo_path": "/r"}),  # U01: not listed
]


@pytest.fixture(scope="module")
def summary() -> dict[str, Any]:
    sink = io.StringIO()
    recorder = Recorder(sink, provenance=Provenance.REAL_CAPTURE, session="metrics")
    rig = Rig(ScriptedServer([OPEN, CLOSED]), RelayMode.SHADOW, recorder=recorder)
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        for tool, arguments in CALLS:
            assert rig.call(tool, arguments) is not None
    finally:
        rig.close()
        recorder.close()
    return summarize_evidence(parse_recording(sink.getvalue().splitlines()))


def test_an_unvalidated_call_is_counted_apart(summary: dict[str, Any]) -> None:
    observed = summary["stateguard_observed"]
    assert observed["calls_decided"] == 4 and observed["calls_unvalidated"] == 1
    assert "None" not in observed["decided_on_listing"]


def test_an_undeclared_key_is_a_violation_only_where_the_schema_is_closed(
    summary: dict[str, Any],
) -> None:
    observed = summary["stateguard_observed"]
    assert observed["calls_with_field_violations"] == 2  # max_count=true; closed-schema extra
    assert observed["calls_with_undeclared_fields"] == 1  # open-schema extra (S05 shape)
    assert observed["calls_with_schema_violations"] == 3  # legacy: any field decision
    # Shadow still forwarded every call as received.
    assert summary["wire"]["tool_calls_forwarded_unchanged"] == len(CALLS)
