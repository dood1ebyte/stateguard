"""
Tests for ``stateguard.core.explain`` -- per-field decisions, text, and dict.

What these protect
------------------
* Every decision is derived from what the engine recorded, so each kind is
  checked against a run that really produces it -- including ``DISCARDED``,
  which exists because an attempt the engine rolled back still lists its
  operations.
* ``explain()`` promises to be value-free and deterministic. Both are
  checked directly: no payload value appears in the text, and two runs over
  the same input explain identically.
* ``to_dict()`` promises JSON-ready data. It is checked by ``json.dumps``
  with no ``default=``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from stateguard import ContractGuard, GuardConfig
from stateguard.adapters.mcp import MCPAction, repair_tool_call
from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.explain import FieldDecisionKind
from stateguard.core.models.config import RepairConfig, RepairMode
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.core.strategies.registry import StrategyRegistry
from tests.conftest import MockContractAdapter
from tests.core.test_engine import make_engine
from tests.core.test_engine_convergence import _WhileMissing

FORECAST: dict[str, Any] = {
    "name": "get_forecast",
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string"},
            "days": {"type": "integer", "maximum": 14},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"], "default": "celsius"},
        },
        "required": ["location", "days"],
    },
}

ACCOUNT: dict[str, Any] = {
    "name": "lookup",
    "inputSchema": {
        "type": "object",
        "properties": {"account_id": {"type": "string"}, "account_name": {"type": "string"}},
        "required": ["account_id", "account_name"],
    },
}


@pytest.fixture
def auto() -> ContractGuard:
    return ContractGuard.with_mcp()


@pytest.fixture
def shadow() -> ContractGuard:
    return ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW))


def _kinds(result: Any) -> list[tuple[str, FieldDecisionKind]]:
    return [(d.path, d.kind) for d in result.field_decisions()]


# ===========================================================================
# Decisions
# ===========================================================================


class TestRepaired:
    def test_each_applied_operation_is_a_decision_in_application_order(
        self, auto: ContractGuard
    ) -> None:
        result = auto.repair(FORECAST, {"loc": "Mumbai", "days": "5"})
        assert _kinds(result) == [
            ("location", FieldDecisionKind.REPAIRED),
            ("days", FieldDecisionKind.REPAIRED),
        ]

    def test_a_decision_carries_the_operation_and_why_it_was_trusted(
        self, auto: ContractGuard
    ) -> None:
        result = auto.repair(FORECAST, {"loc": "Mumbai", "days": 5})
        (decision,) = result.field_decisions()
        assert decision.operation is not None
        assert decision.operation.op_type is FieldOpType.RENAME
        assert decision.operation.source_path == "loc"
        assert decision.operation.trust >= 0.75
        assert decision.reason == "Fuzzy name match: 'loc' -> 'location'."
        assert decision.strategy == "FuzzyFieldMatchStrategy"
        assert decision.attempt == 1

    def test_an_already_valid_payload_has_no_decisions(self, auto: ContractGuard) -> None:
        result = auto.repair(FORECAST, {"location": "Mumbai", "days": 5})
        assert result.field_decisions() == []
        assert "no field needed repair" in result.explain()


class TestHeld:
    def test_an_ambiguous_rename_is_held_with_its_candidates(self, auto: ContractGuard) -> None:
        result = auto.repair(ACCOUNT, {"account": "x"})
        held = [d for d in result.field_decisions() if d.kind is FieldDecisionKind.HELD]
        assert len(held) == 1
        assert held[0].path == "account_id"
        assert [c.source_path for c in held[0].candidates] == ["account"]
        assert "below the INFERRED threshold" in held[0].reason

    def test_a_held_field_is_not_also_listed_as_unresolved(self, auto: ContractGuard) -> None:
        result = auto.repair(ACCOUNT, {"account": "x"})
        paths = [d.path for d in result.field_decisions()]
        assert paths.count("account_id") == 1
        assert ("account_name", FieldDecisionKind.UNRESOLVED) in _kinds(result)


class TestUnresolved:
    def test_a_violation_nothing_repaired_is_unresolved(self, auto: ContractGuard) -> None:
        result = auto.repair(FORECAST, {"location": "x", "days": 99})
        (decision,) = result.field_decisions()
        assert decision.kind is FieldDecisionKind.UNRESOLVED
        assert decision.path == "days"
        assert decision.reason == "value_constraint_violation (error)"
        assert decision.violation is not None


class TestDiscarded:
    """The engine rolled an attempt back; its operations are in no payload."""

    @staticmethod
    def _result() -> Any:
        contract = ContractSpec(
            fields=[
                FieldSpec("a", FieldType.INTEGER, required=True),
                FieldSpec("b", FieldType.INTEGER, required=True),
                FieldSpec("c", FieldType.INTEGER, required=False),
            ]
        )
        fix_a = FieldOperation(FieldOpType.SET_DEFAULT, "a", "fills a", trust=1.0, value=1)
        break_c = FieldOperation(FieldOpType.SET_VALUE, "c", "breaks c", trust=1.0, value="x")
        registry = StrategyRegistry(
            [
                _WhileMissing("FixA", priority=10, trigger_path="a", operations=[fix_a]),
                _WhileMissing("BreakC", priority=20, trigger_path="b", operations=[break_c]),
            ]
        )
        engine = make_engine(registry=registry, config=RepairConfig(max_attempts=10))
        return engine.repair(contract, {}, MockContractAdapter())

    def test_the_rolled_back_operation_is_discarded_not_repaired(self) -> None:
        result = self._result()
        assert ("a", FieldDecisionKind.REPAIRED) in _kinds(result)
        assert ("c", FieldDecisionKind.DISCARDED) in _kinds(result)
        assert ("c", FieldDecisionKind.REPAIRED) not in _kinds(result)

    def test_the_reason_says_why_it_was_rolled_back(self) -> None:
        discarded = [
            d for d in self._result().field_decisions() if d.kind is FieldDecisionKind.DISCARDED
        ]
        assert "made the violation set worse" in discarded[0].reason


# ===========================================================================
# explain()
# ===========================================================================

SECRET = "SECRET-7f3a"


class TestExplainIsSafeToLog:
    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            (FORECAST, {"loc": SECRET, "days": "5"}),
            (FORECAST, {"location": SECRET, "days": 99}),
            (FORECAST, {"location": SECRET, "days": 5}),
            (ACCOUNT, {"account": SECRET}),
            (FORECAST, json.dumps({"location": SECRET, "days": 3})),
        ],
    )
    @pytest.mark.parametrize("mode", [RepairMode.AUTO, RepairMode.SHADOW])
    def test_no_payload_value_appears(self, tool: Any, arguments: Any, mode: RepairMode) -> None:
        guard = ContractGuard.with_mcp(config=GuardConfig(mode=mode))
        outcome = repair_tool_call(guard, tool, arguments)
        assert SECRET not in outcome.explain()

    def test_it_is_deterministic(self, auto: ContractGuard) -> None:
        arguments = {"loc": "Mumbai", "days": "5", "unit": "Celsius"}
        first = repair_tool_call(auto, FORECAST, arguments).explain()
        second = repair_tool_call(ContractGuard.with_mcp(), FORECAST, arguments).explain()
        assert first == second


class TestExplainText:
    def test_shadow_says_nothing_was_applied(self, shadow: ContractGuard) -> None:
        text = shadow.repair(FORECAST, {"loc": "Mumbai", "days": "5"}).explain()
        assert text.startswith("status: success (shadow) -- proposed, not applied")

    def test_shadow_does_not_claim_a_proposal_for_a_valid_call(self, shadow: ContractGuard) -> None:
        text = shadow.repair(FORECAST, {"location": "Mumbai", "days": 5}).explain()
        assert text.splitlines()[0] == "status: already_valid (shadow)"

    def test_a_run_with_no_payload_says_so(self, auto: ContractGuard) -> None:
        text = auto.repair(ACCOUNT, {"account": "x"}).explain()
        assert text.startswith("status: ambiguous (auto) -- no payload produced")

    def test_root_normalisation_is_reported(self, auto: ContractGuard) -> None:
        text = auto.repair(FORECAST, '{"location": "x", "days": 3}').explain()
        assert "root        parsed a JSON object" in text

    def test_the_outcome_leads_with_the_action(self, auto: ContractGuard) -> None:
        outcome = repair_tool_call(auto, ACCOUNT, {"account": "x"})
        first, rest = outcome.explain().split("\n", 1)
        assert first == f"ESCALATE: {outcome.reason}"
        assert rest == outcome.result.explain()


# ===========================================================================
# to_dict()
# ===========================================================================


class TestToDict:
    def test_it_serialises_without_help(self, auto: ContractGuard) -> None:
        for arguments in ({"loc": "Mumbai", "days": "5"}, {"account": "x"}):
            tool = ACCOUNT if "account" in arguments else FORECAST
            json.dumps(repair_tool_call(auto, tool, arguments).to_dict())

    def test_it_answers_what_happened_to_the_call(self, auto: ContractGuard) -> None:
        sent = {"loc": "Mumbai", "days": "5"}
        record = repair_tool_call(auto, FORECAST, sent).to_dict()
        assert record["action"] == "forward"
        assert record["arguments"] == {"location": "Mumbai", "days": 5}
        result = record["result"]
        assert result["original_input"] == sent
        assert result["valid_as_received"] is False
        assert result["repair_attempted"] is True
        assert [(d["path"], d["decision"]) for d in result["decisions"]] == [
            ("location", "repaired"),
            ("days", "repaired"),
        ]
        rename = result["decisions"][0]["operation"]
        assert rename["source_path"] == "loc"
        assert rename["evidence"]["name_match"] is not None
        assert result["remaining_violations"] == []
        assert all(a["kept"] for a in result["attempts"])

    def test_an_escalation_carries_its_candidates(self, auto: ContractGuard) -> None:
        record = repair_tool_call(auto, ACCOUNT, {"account": "x"}).to_dict()
        assert record["action"] == "escalate"
        assert record["arguments"] is None
        (held,) = [d for d in record["result"]["decisions"] if d["decision"] == "held"]
        assert [c["source_path"] for c in held["candidates"]] == ["account"]

    def test_a_shadow_record_keeps_the_preview_apart(self, shadow: ContractGuard) -> None:
        sent = {"loc": "Mumbai", "days": "5"}
        record = repair_tool_call(shadow, FORECAST, sent).to_dict()
        assert record["action"] == "hold"
        assert record["arguments"] == sent
        assert record["preview"] == {"location": "Mumbai", "days": 5}
        assert record["result"]["repaired_output"] is None

    def test_a_rehydrated_payload_is_dumped_to_plain_data(self) -> None:
        pydantic = pytest.importorskip("pydantic")

        class Weather(pydantic.BaseModel):
            temperature: float

        result = ContractGuard.with_pydantic().repair(Weather, {"temperature": "31.5"})
        record = result.to_dict()
        assert record["repaired_output"] == {"temperature": 31.5}
        json.dumps(record)

    def test_an_mcp_action_is_never_lost_in_serialisation(self, auto: ContractGuard) -> None:
        record = repair_tool_call(auto, FORECAST, {"location": "x", "days": 99}).to_dict()
        assert MCPAction(record["action"]) is MCPAction.REFUSE
        assert record["should_forward"] is False
