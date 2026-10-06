"""
The MCP outcome truth table: every scenario, in both modes.

One row per (scenario, mode). Each row pins the whole decision -- not just
``action`` -- because the failure modes worth guarding against are
mismatches *between* fields: a shadow outcome that blocks, a HOLD that
forwards the preview, a FORWARD whose payload is not the committed one.

What every row asserts
----------------------
* ``action`` and ``auto_action``;
* whether the call is forwarded, and with exactly which payload;
* ``preview`` and ``proposed_arguments``;
* ``original_arguments`` is what was received;
* the decision is deterministic -- the same call twice yields the same
  ``to_dict()``.

The table itself is the documentation: ``docs/mcp-adapter.md`` reproduces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from stateguard import ContractGuard, GuardConfig
from stateguard.adapters.jsonschema.errors import JSONSchemaError
from stateguard.adapters.mcp import MCPAction, repair_tool_call
from stateguard.core.models.config import RepairMode

FORECAST: dict[str, Any] = {
    "name": "get_forecast",
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 14},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"], "default": "celsius"},
            "options": {
                "type": "object",
                "properties": {"hourly": {"type": "boolean"}},
                "required": ["hourly"],
            },
        },
        "required": ["location", "days"],
    },
}

ACCOUNT: dict[str, Any] = {
    "name": "lookup_account",
    "inputSchema": {
        "type": "object",
        "properties": {"account_id": {"type": "string"}, "account_name": {"type": "string"}},
        "required": ["account_id", "account_name"],
    },
}

NO_PARAMS: dict[str, Any] = {"name": "list_tables", "inputSchema": {"type": "object"}}

A = MCPAction


@dataclass(frozen=True)
class Row:
    scenario: str
    tool: dict[str, Any]
    arguments: Any
    auto_action: MCPAction
    #: What auto mode forwards / shadow previews, when auto_action is FORWARD.
    proposed: dict[str, Any] | None


ROWS = [
    Row(
        "valid, nothing to fill",
        FORECAST,
        {"location": "Mumbai", "days": 5, "unit": "celsius"},
        A.FORWARD,
        {"location": "Mumbai", "days": 5, "unit": "celsius"},
    ),
    Row(
        "valid, declared default filled",
        FORECAST,
        {"location": "Mumbai", "days": 5},
        A.FORWARD,
        {"location": "Mumbai", "days": 5, "unit": "celsius"},
    ),
    Row(
        "repairable: rename + coercion",
        FORECAST,
        {"loc": "Mumbai", "days": "5"},
        A.FORWARD,
        {"location": "Mumbai", "days": 5, "unit": "celsius"},
    ),
    Row(
        "repairable: nested coercion",
        FORECAST,
        {"location": "Mumbai", "days": 5, "options": {"hourly": "true"}},
        A.FORWARD,
        {"location": "Mumbai", "days": 5, "unit": "celsius", "options": {"hourly": True}},
    ),
    Row(
        "repairable: JSON-encoded argument object",
        FORECAST,
        '{"location": "Mumbai", "days": 5}',
        A.FORWARD,
        {"location": "Mumbai", "days": 5, "unit": "celsius"},
    ),
    Row("ambiguous rename", ACCOUNT, {"account": "acme"}, A.ESCALATE, None),
    Row("invalid: out of range", FORECAST, {"location": "Mumbai", "days": 99}, A.REFUSE, None),
    Row("invalid: missing required", FORECAST, {"days": 3}, A.REFUSE, None),
    Row("invalid: root is not an object", FORECAST, [1, 2, 3], A.REFUSE, None),
    Row("no-argument tool, arguments=None", NO_PARAMS, None, A.FORWARD, {}),
    Row("no-argument tool, arguments={}", NO_PARAMS, {}, A.FORWARD, {}),
]


def _guard(mode: RepairMode) -> ContractGuard:
    return ContractGuard.with_mcp(config=GuardConfig(mode=mode))


def _received(row: Row) -> Any:
    return {} if row.arguments is None else row.arguments


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.scenario)
class TestAutoMode:
    def test_the_action_is_the_auto_action(self, row: Row) -> None:
        outcome = repair_tool_call(_guard(RepairMode.AUTO), row.tool, row.arguments)
        assert outcome.action is row.auto_action
        assert outcome.auto_action is row.auto_action

    def test_what_is_forwarded(self, row: Row) -> None:
        outcome = repair_tool_call(_guard(RepairMode.AUTO), row.tool, row.arguments)
        if row.auto_action is A.FORWARD:
            assert outcome.should_forward
            assert outcome.arguments == row.proposed
        else:
            assert not outcome.should_forward
            assert outcome.arguments is None

    def test_payload_fields(self, row: Row) -> None:
        outcome = repair_tool_call(_guard(RepairMode.AUTO), row.tool, row.arguments)
        assert outcome.preview is None
        assert outcome.proposed_arguments == row.proposed
        assert outcome.original_arguments == _received(row)


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.scenario)
class TestShadowMode:
    def test_the_call_is_always_forwarded_as_received(self, row: Row) -> None:
        """Shadow never blocks and never alters: the server sees what the model sent."""
        outcome = repair_tool_call(_guard(RepairMode.SHADOW), row.tool, row.arguments)
        assert outcome.should_forward
        assert outcome.action in (A.FORWARD, A.HOLD)
        assert outcome.arguments == _received(row)

    def test_the_auto_decision_is_still_recorded(self, row: Row) -> None:
        outcome = repair_tool_call(_guard(RepairMode.SHADOW), row.tool, row.arguments)
        assert outcome.auto_action is row.auto_action
        assert outcome.proposed_arguments == row.proposed

    def test_hold_exactly_when_auto_would_do_something_else(self, row: Row) -> None:
        outcome = repair_tool_call(_guard(RepairMode.SHADOW), row.tool, row.arguments)
        unchanged = row.auto_action is A.FORWARD and row.proposed == _received(row)
        assert outcome.action is (A.FORWARD if unchanged else A.HOLD)

    def test_the_preview_is_auto_s_payload_or_nothing(self, row: Row) -> None:
        outcome = repair_tool_call(_guard(RepairMode.SHADOW), row.tool, row.arguments)
        if outcome.action is A.HOLD and row.auto_action is A.FORWARD:
            assert outcome.preview == row.proposed
        else:
            assert outcome.preview is None

    def test_the_reason_names_what_auto_would_have_done(self, row: Row) -> None:
        outcome = repair_tool_call(_guard(RepairMode.SHADOW), row.tool, row.arguments)
        if row.auto_action is A.REFUSE:
            assert "auto mode would refuse" in outcome.reason
        if row.auto_action is A.ESCALATE:
            assert "auto mode would escalate" in outcome.reason


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.scenario)
@pytest.mark.parametrize("mode", [RepairMode.AUTO, RepairMode.SHADOW])
class TestBothModes:
    def test_the_decision_is_deterministic(self, row: Row, mode: RepairMode) -> None:
        first = repair_tool_call(_guard(mode), row.tool, row.arguments).to_dict()
        second = repair_tool_call(_guard(mode), row.tool, row.arguments).to_dict()
        assert first == second

    def test_auto_and_shadow_agree_on_the_decision(self, row: Row, mode: RepairMode) -> None:
        """The same call gets the same auto_action and proposal in either mode."""
        outcome = repair_tool_call(_guard(mode), row.tool, row.arguments)
        other = repair_tool_call(
            _guard(RepairMode.SHADOW if mode is RepairMode.AUTO else RepairMode.AUTO),
            row.tool,
            row.arguments,
        )
        assert outcome.auto_action is other.auto_action
        assert outcome.proposed_arguments == other.proposed_arguments

    def test_the_record_carries_mode_and_both_actions(self, row: Row, mode: RepairMode) -> None:
        record = repair_tool_call(_guard(mode), row.tool, row.arguments).to_dict()
        assert record["mode"] == mode.value
        assert record["auto_action"] == row.auto_action.value
        assert record["proposed_arguments"] == row.proposed


class TestUnreadableSchemas:
    """No outcome is invented for a schema StateGuard cannot read -- in either mode."""

    @pytest.mark.parametrize("mode", [RepairMode.AUTO, RepairMode.SHADOW])
    @pytest.mark.parametrize(
        "tool",
        [
            {"name": "t", "inputSchema": {"type": "object", "allOf": [{}]}},
            {
                "name": "t",
                "inputSchema": {"type": "object", "properties": {"a": {"type": "strng"}}},
            },
            {"name": "t", "inputSchema": {"type": "string"}},
            {"name": "t", "description": "no schema"},
            {"name": "t", "inputSchema": ["not", "an", "object"]},
        ],
        ids=["unsupported", "malformed type", "non-object root", "no schema", "schema not object"],
    )
    def test_it_raises(self, tool: dict[str, Any], mode: RepairMode) -> None:
        with pytest.raises(JSONSchemaError):
            repair_tool_call(_guard(mode), tool, {})


class TestTheForwardingGateFires:
    """
    The invariants hold by construction, so the gate never fires on real
    input. These feed it outcomes a future bug could produce, to prove it is
    a real gate and not a comment.
    """

    @staticmethod
    def _outcome(mode: RepairMode, arguments: Any) -> Any:
        return repair_tool_call(_guard(mode), FORECAST, arguments)

    def test_shadow_forwarding_anything_but_the_original_is_refused(self) -> None:
        import dataclasses

        from stateguard.adapters.mcp.errors import ForwardingInvariantError
        from stateguard.adapters.mcp.outcomes import _check_forwarding_invariants

        sent = {"loc": "Mumbai", "days": "5"}
        held = self._outcome(RepairMode.SHADOW, sent)
        assert held.preview is not None
        leaked = dataclasses.replace(held, arguments=held.preview)
        with pytest.raises(ForwardingInvariantError, match="other than the one received"):
            _check_forwarding_invariants(leaked, sent)

    def test_shadow_blocking_is_refused(self) -> None:
        import dataclasses

        from stateguard.adapters.mcp.errors import ForwardingInvariantError
        from stateguard.adapters.mcp.outcomes import _check_forwarding_invariants

        sent = {"location": "x", "days": 99}
        held = self._outcome(RepairMode.SHADOW, sent)
        blocked = dataclasses.replace(held, action=A.REFUSE, arguments=None)
        with pytest.raises(ForwardingInvariantError, match="never blocks"):
            _check_forwarding_invariants(blocked, sent)

    def test_auto_forwarding_an_uncommitted_payload_is_refused(self) -> None:
        import dataclasses

        from stateguard.adapters.mcp.errors import ForwardingInvariantError
        from stateguard.adapters.mcp.outcomes import _check_forwarding_invariants

        sent = {"loc": "Mumbai", "days": "5"}
        forwarded = self._outcome(RepairMode.AUTO, sent)
        swapped = dataclasses.replace(forwarded, arguments=dict(sent))
        with pytest.raises(ForwardingInvariantError, match="committed"):
            _check_forwarding_invariants(swapped, sent)

    def test_an_auto_payload_that_does_not_validate_is_not_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The final validation boundary in repair_tool_call."""
        from stateguard.adapters.mcp.errors import ForwardingInvariantError
        from stateguard.core.errors.results import ValidationResult
        from tests.conftest import make_violation

        guard = _guard(RepairMode.AUTO)
        monkeypatch.setattr(
            guard,
            "validate",
            lambda schema, data: ValidationResult(
                is_valid=False, violations=[make_violation("days")], raw_input=data, contract_id=""
            ),
        )
        with pytest.raises(ForwardingInvariantError, match="does not validate"):
            repair_tool_call(guard, FORECAST, {"loc": "Mumbai", "days": "5"})


def test_the_documented_table_has_every_row() -> None:
    """``docs/mcp-adapter.md`` reproduces this table; a row added here must appear there."""
    doc = (Path(__file__).parents[3] / "docs" / "mcp-adapter.md").read_text()
    table = doc.split("### The outcome truth table", 1)[1].split("\n### ", 1)[0]
    documented = {
        line.split("|")[1].strip().replace("`", "")
        for line in table.splitlines()
        if line.startswith("| ") and not line.startswith("| Scenario")
    }
    assert {row.scenario for row in ROWS} <= documented
