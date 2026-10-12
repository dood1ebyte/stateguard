"""
The unsafe-repair boundary at the MCP layer, on mcp-server-git's real schemas.

Every case here is a call real traffic produced (Capture 1b: Z01, Z02) or a
real server was shown to accept and misread (targeted capture Z-T,
``start_timestamp``). Auto mode escalates them instead of forwarding a
repaired payload; shadow forwards the call as received and reports the
escalation as its proposal. Safe deterministic repairs on the same tools are
unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stateguard import ContractGuard, GuardConfig
from stateguard.adapters.mcp import MCPAction, repair_tool_call
from stateguard.core.explain import FieldDecisionKind, field_decisions
from stateguard.core.models.config import RepairMode

TOOLS = {
    t["name"]: t
    for t in json.loads(
        (
            Path(__file__).parents[1] / "jsonschema" / "corpus" / "schemas" / "mcp-server-git.json"
        ).read_text()
    )["tools"]
}
REPO = "/srv/fixture-repo"
AUTO = ContractGuard.with_mcp()
SHADOW = ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW))

#: (case, tool, arguments, field) -- auto must escalate, never forward
HELD = [
    ("Z01 path sent as a list", "git_status", {"repo_path": [REPO]}, "repo_path"),
    (
        "Z02 comma-separated files",
        "git_add",
        {"repo_path": REPO, "files": "notes.txt, README.txt"},
        "files",
    ),
    (
        "files separated by a newline",
        "git_add",
        {"repo_path": REPO, "files": "a.txt\nb.txt"},
        "files",
    ),
    (
        "commit message sent as a list",
        "git_commit",
        {"repo_path": REPO, "message": ["fix bug"]},
        "message",
    ),
    (
        "timestamp sent as an object (Z-T P1)",
        "git_log",
        {"repo_path": REPO, "start_timestamp": {"days": 30}},
        "start_timestamp",
    ),
    (
        "two dates for one timestamp (Z-T P3)",
        "git_log",
        {"repo_path": REPO, "start_timestamp": ["2020-01-01", "2026-09-01"]},
        "start_timestamp",
    ),
    (
        "one date as a list (Z-T P2)",
        "git_log",
        {"repo_path": REPO, "start_timestamp": ["2026-09-01"]},
        "start_timestamp",
    ),
]

#: (case, tool, arguments, field, value auto forwards)
APPLIED = [
    ("R01 numeric string", "git_log", {"repo_path": REPO, "max_count": "3"}, "max_count", 3),
    (
        "R03 single file name",
        "git_add",
        {"repo_path": REPO, "files": "notes.txt"},
        "files",
        ["notes.txt"],
    ),
    (
        "R04 JSON array text",
        "git_add",
        {"repo_path": REPO, "files": '["notes.txt"]'},
        "files",
        ["notes.txt"],
    ),
    ("R08 integral float", "git_log", {"repo_path": REPO, "max_count": 3.0}, "max_count", 3),
    ("R06 camelCase name", "git_status", {"repoPath": REPO}, "repo_path", REPO),
]


@pytest.mark.parametrize("case,tool,arguments,field", HELD, ids=[c[0] for c in HELD])
def test_auto_escalates_instead_of_forwarding(
    case: str, tool: str, arguments: Any, field: str
) -> None:
    outcome = repair_tool_call(AUTO, TOOLS[tool], arguments)
    assert outcome.action is MCPAction.ESCALATE
    assert outcome.arguments is None  # nothing is forwarded in auto
    decision = next(d for d in field_decisions(outcome.result) if d.path == field)
    assert decision.kind is FieldDecisionKind.HELD
    assert decision.operation is not None and decision.operation.trust == 1.0
    assert decision.operation.evidence.contradiction is not None
    # The reason says why a trust-1.0 cast was held, not that trust fell short.
    assert "clears the LOSSY threshold" in decision.reason


@pytest.mark.parametrize("case,tool,arguments,field", HELD, ids=[c[0] for c in HELD])
def test_shadow_forwards_as_received_and_proposes_escalation(
    case: str, tool: str, arguments: Any, field: str
) -> None:
    outcome = repair_tool_call(SHADOW, TOOLS[tool], arguments)
    assert outcome.auto_action is MCPAction.ESCALATE
    assert outcome.action is not MCPAction.REFUSE  # shadow never blocks
    assert outcome.arguments == arguments


@pytest.mark.parametrize("case,tool,arguments,field,expected", APPLIED, ids=[c[0] for c in APPLIED])
def test_safe_repairs_on_the_same_tools_still_forward(
    case: str, tool: str, arguments: Any, field: str, expected: Any
) -> None:
    outcome = repair_tool_call(AUTO, TOOLS[tool], arguments)
    assert outcome.action is MCPAction.FORWARD
    assert outcome.arguments is not None
    assert outcome.arguments[field] == expected
    assert type(outcome.arguments[field]) is type(expected)
