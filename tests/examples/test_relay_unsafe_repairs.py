"""
The relay on the unsafe-repair boundary, with mcp-server-git's real listing.

Capture 1b recorded Z01/Z02 as ``proposal: repair`` at trust 1.0, and the
targeted capture Z-T showed the real server accepting the proposed JSON-text
timestamp and returning the wrong history. Now:

* shadow forwards the call exactly as received and logs the proposal as
  ``escalate`` (held), still ``would_modify``;
* auto sends nothing to the server and answers the client itself;
* a safe repair beside them is unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import RelayMode, Rig, ScriptedServer, ServerTool  # noqa: E402

pytestmark = pytest.mark.integration

LISTING = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "adapters"
        / "jsonschema"
        / "corpus"
        / "schemas"
        / "mcp-server-git.json"
    ).read_text()
)["tools"]
REPO = "/srv/fixture-repo"
HELD = [
    ("git_status", {"repo_path": [REPO]}),  # Z01
    ("git_add", {"repo_path": REPO, "files": "notes.txt, README.txt"}),  # Z02
    ("git_log", {"repo_path": REPO, "start_timestamp": {"days": 30}}),  # Z-T P1
]
SAFE = ("git_add", {"repo_path": REPO, "files": "notes.txt"})  # R03


def _run(mode: RelayMode) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
    server = ScriptedServer([ServerTool(t) for t in LISTING])
    rig = Rig(server, mode)
    answers = []
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        for tool, arguments in [*HELD, SAFE]:
            answers.append(rig.call(tool, arguments))
    finally:
        rig.close()
    calls = [e for e in rig.audit if e.get("event") == "call"]
    received = [json.loads(line) for line in server.received if b'"tools/call"' in line]
    return calls, received, answers


def test_shadow_forwards_every_call_as_received_and_proposes_escalation() -> None:
    calls, received, _ = _run(RelayMode.SHADOW)
    assert [c["params"]["arguments"] for c in received] == [a for _, a in [*HELD, SAFE]]
    held, safe = calls[:3], calls[3]
    for event in held:
        assert event["proposal"] == "escalate" and event["would_modify"] is True
        assert event["auto_action"] == "escalate"
        (decision,) = [d for d in event["decisions"] if d["decision"] == "held"]
        assert decision["op"] == "coerce" and decision["trust"] == 1.0
    assert safe["proposal"] == "repair" and safe["auto_action"] == "forward"


def test_auto_sends_nothing_for_a_held_call_and_still_repairs_the_safe_one() -> None:
    calls, received, answers = _run(RelayMode.AUTO)
    # Only the safe call reached the server, repaired.
    assert [c["params"]["arguments"] for c in received] == [
        {"repo_path": REPO, "files": ["notes.txt"]}
    ]
    assert [c["action"] for c in calls] == ["escalate", "escalate", "escalate", "forward"]
    for answer in answers[:3]:
        # The relay answered (the server never saw the call), as a tool error
        # the model can read and correct.
        assert answer is not None and answer["result"]["isError"] is True
        assert answer["result"]["content"][0]["text"].startswith("The arguments were not sent")
