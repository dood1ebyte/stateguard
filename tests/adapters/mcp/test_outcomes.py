"""
``RepairResult`` -> forward / hold / escalate / refuse.

The mapping exists so no call site has to re-derive it from five statuses,
two payload fields and an ambiguity list. These tests pin the two
distinctions that mapping is *for*:

* ``ESCALATE`` is not ``REFUSE`` -- an ambiguous result carries candidates
  and is actionable; a failure carries nothing.
* ``HOLD`` is not ``FORWARD`` -- shadow deliberately withholds the payload,
  and a proxy that forwarded the preview would be applying repairs the
  caller asked it not to apply.

They are driven through the real engine rather than hand-built
``RepairResult`` objects, so a change in what the engine produces shows up
here instead of being papered over by a fixture.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard import ContractGuard
from stateguard.adapters.mcp import MCPAction, outcome_for, repair_tool_call
from stateguard.core.errors.results import RepairStatus
from stateguard.core.models.config import GuardConfig, RepairConfig, RepairMode

TOOL: dict[str, Any] = {
    "name": "get_forecast",
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 14},
            "unit": {
                "type": "string",
                "enum": ["celsius", "fahrenheit"],
                "default": "celsius",
            },
        },
        "required": ["location", "days"],
    },
}


def _outcome(guard: ContractGuard, arguments: dict[str, Any]) -> Any:
    return outcome_for(guard.repair(TOOL, arguments), arguments)


@pytest.fixture
def auto() -> ContractGuard:
    return ContractGuard.with_mcp()


class TestForward:
    def test_a_repaired_call_forwards_the_repaired_arguments(self, auto: ContractGuard) -> None:
        outcome = _outcome(auto, {"loc": "Mumbai", "days": "5"})
        assert outcome.action is MCPAction.FORWARD
        # Exactly the repair's changes: no declared default rides along.
        assert outcome.arguments == {"location": "Mumbai", "days": 5}
        assert outcome.preview is None

    def test_an_already_valid_call_forwards(self, auto: ContractGuard) -> None:
        outcome = _outcome(auto, {"location": "Mumbai", "days": 5, "unit": "celsius"})
        assert outcome.action is MCPAction.FORWARD
        assert outcome.result.status is RepairStatus.ALREADY_VALID

    def test_an_omitted_optional_default_is_not_written(self, auto: ContractGuard) -> None:
        """
        ``unit`` declares a default and the model left it out. The call is
        valid; the server applies its own default. Writing ``unit: celsius``
        into the call would change the request and repair nothing.
        """
        sent = {"location": "Mumbai", "days": 5}
        outcome = _outcome(auto, sent)
        assert outcome.result.status is RepairStatus.ALREADY_VALID
        assert outcome.arguments == sent
        assert "unchanged" in outcome.reason

    def test_the_reason_says_unchanged_when_it_is(self, auto: ContractGuard) -> None:
        outcome = _outcome(auto, {"location": "Mumbai", "days": 5, "unit": "celsius"})
        assert "unchanged" in outcome.reason

    def test_should_forward_is_true(self, auto: ContractGuard) -> None:
        assert _outcome(auto, {"loc": "Mumbai", "days": "5"}).should_forward is True


class TestHold:
    """Shadow mode: observe, do not change what the server receives."""

    @pytest.fixture
    def shadow(self) -> ContractGuard:
        return ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW))

    def test_a_repairable_call_holds(self, shadow: ContractGuard) -> None:
        outcome = _outcome(shadow, {"loc": "Mumbai", "days": "5"})
        assert outcome.action is MCPAction.HOLD

    def test_the_repair_is_offered_as_a_preview_not_as_arguments(
        self, shadow: ContractGuard
    ) -> None:
        """
        The distinction the whole action exists for. A proxy that read
        ``arguments`` without checking ``action`` must not apply repairs the
        caller explicitly asked it to withhold: it gets the model's payload,
        unrepaired, and the repair only ever appears on ``preview``.
        """
        sent = {"loc": "Mumbai", "days": "5"}
        outcome = _outcome(shadow, sent)
        assert outcome.arguments == sent
        assert outcome.preview == {"location": "Mumbai", "days": 5}
        assert outcome.arguments != outcome.preview

    def test_the_call_still_proceeds(self, shadow: ContractGuard) -> None:
        """Shadow withholds the repair, not the call."""
        assert _outcome(shadow, {"loc": "Mumbai", "days": "5"}).should_forward is True

    def test_the_preview_matches_what_auto_would_have_sent(
        self, shadow: ContractGuard, auto: ContractGuard
    ) -> None:
        """
        A week of shadow diffing only tells you something if the preview is
        exactly what flipping to auto would produce.
        """
        arguments = {"loc": "Mumbai", "days": "5"}
        assert _outcome(shadow, arguments).preview == _outcome(auto, arguments).arguments

    def test_an_omitted_optional_default_is_nothing_to_hold(self, shadow: ContractGuard) -> None:
        """
        Auto mode sends a valid call that omitted a defaulted optional
        exactly as received, so shadow has no diff to show: ``FORWARD``, no
        preview. (It used to be ``HOLD``, previewing a default auto mode
        wrote into the call -- the injection capture 1a found on 5 of 18
        valid calls.)
        """
        arguments = {"location": "Mumbai", "days": 5}
        outcome = _outcome(shadow, arguments)
        assert outcome.result.status is RepairStatus.ALREADY_VALID
        assert outcome.action is MCPAction.FORWARD
        assert outcome.preview is None

    def test_holding_still_sends_the_model_s_arguments(self, shadow: ContractGuard) -> None:
        """
        ``HOLD`` means the proxy forwards what it received, and ``arguments``
        says so. It used to be ``None``, which made the natural
        ``if outcome.should_forward: send(outcome.arguments)`` send nothing
        -- breaking shadow's one promise, that the server sees exactly what
        the model wrote.
        """
        sent = {"location": "Mumbai", "days": 5}
        outcome = _outcome(shadow, sent)
        assert outcome.should_forward is True
        assert outcome.arguments == sent
        assert outcome.arguments is not sent  # a copy: mutating it cannot reach the caller's dict

    def test_an_already_valid_call_with_nothing_to_fill_forwards(
        self, shadow: ContractGuard
    ) -> None:
        """
        The other half: when auto would send exactly what the model sent,
        there is no diff, so there is nothing to hold for review.
        """
        arguments = {"location": "Mumbai", "days": 5, "unit": "celsius"}
        outcome = _outcome(shadow, arguments)
        assert outcome.action is MCPAction.FORWARD
        assert outcome.arguments == arguments
        assert outcome.preview is None

    def test_the_preview_matches_auto_on_an_already_valid_payload_too(
        self, shadow: ContractGuard, auto: ContractGuard
    ) -> None:
        """
        The same promise as ``test_the_preview_matches_what_auto_would_have
        _sent``, extended to the status that used to escape it: shadow's
        preview is what flipping to auto would put on the wire.
        """
        arguments = {"location": "Mumbai", "days": "5"}  # repairable: days is a string
        assert _outcome(shadow, arguments).preview == _outcome(auto, arguments).arguments

    def test_shadow_is_read_from_the_mode_not_from_the_payload_fields(
        self, shadow: ContractGuard, auto: ContractGuard
    ) -> None:
        """
        ``RepairResult.is_shadow`` reports the mode, which is the question
        being asked. Inferring it from ``proposed_output is not None`` is a
        different question that happens to share an answer today.
        """
        arguments = {"location": "Mumbai", "days": 5}
        assert _outcome(shadow, arguments).result.is_shadow is True
        assert _outcome(auto, arguments).result.is_shadow is False


class TestRefuse:
    def test_an_unrepairable_call_is_refused(self, auto: ContractGuard) -> None:
        outcome = _outcome(auto, {"zzzz": 1})
        assert outcome.action is MCPAction.REFUSE
        assert outcome.arguments is None
        assert outcome.should_forward is False

    def test_the_reason_names_the_offending_fields(self, auto: ContractGuard) -> None:
        outcome = _outcome(auto, {"zzzz": 1})
        assert "'location'" in outcome.reason
        assert "'days'" in outcome.reason

    def test_the_full_result_is_still_attached(self, auto: ContractGuard) -> None:
        """A refusal is not a dead end -- the audit trail comes with it."""
        outcome = _outcome(auto, {"zzzz": 1})
        assert outcome.result.remaining_violations


class TestEscalate:
    """
    ``AMBIGUOUS`` means a repair was found and not trusted. Folding it into
    ``REFUSE`` would discard the distinction the hardening phase created --
    and it is the actionable half, because the candidates come with it.
    """

    @pytest.fixture
    def strict(self) -> ContractGuard:
        # A trust floor high enough that an inferred rename cannot clear it,
        # which is what makes the engine abstain rather than apply.
        return ContractGuard.with_mcp(
            config=GuardConfig(repair=RepairConfig(min_confidence_threshold=0.99))
        )

    def test_an_abstained_repair_escalates_rather_than_refusing(
        self, strict: ContractGuard
    ) -> None:
        outcome = _outcome(strict, {"loc": "Mumbai", "days": 5})
        assert outcome.result.status is RepairStatus.AMBIGUOUS
        assert outcome.action is MCPAction.ESCALATE

    def test_it_does_not_forward(self, strict: ContractGuard) -> None:
        outcome = _outcome(strict, {"loc": "Mumbai", "days": 5})
        assert outcome.arguments is None
        assert outcome.should_forward is False

    def test_the_candidates_come_with_it(self, strict: ContractGuard) -> None:
        outcome = _outcome(strict, {"loc": "Mumbai", "days": 5})
        assert outcome.result.ambiguous
        assert any(a.candidates for a in outcome.result.ambiguous)

    def test_the_reason_names_the_ambiguous_field(self, strict: ContractGuard) -> None:
        outcome = _outcome(strict, {"loc": "Mumbai", "days": 5})
        assert "'location'" in outcome.reason


class TestReasonFormatting:
    """
    Reason lines go straight into proxy logs, so they have to stay readable
    when a tool has many fields rather than turning into a wall of names.
    """

    def test_many_fields_are_summarised(self) -> None:
        wide = {
            "name": "wide",
            "inputSchema": {
                "type": "object",
                "properties": {f"f{i}": {"type": "string"} for i in range(8)},
                "required": [f"f{i}" for i in range(8)],
            },
        }
        guard = ContractGuard.with_mcp()
        outcome = outcome_for(guard.repair(wide, {}), {})
        assert outcome.action is MCPAction.REFUSE
        assert "and 5 more" in outcome.reason

    def test_few_fields_are_named_in_full(self, auto: ContractGuard) -> None:
        outcome = _outcome(auto, {})
        assert "and" not in outcome.reason.split("violation(s) remain:")[1]


class TestTheReasonMatchesWhatWasActuallySent:
    """
    A reason line is the audit trail. If it says "unchanged" about a payload
    that was changed, the log is wrong in the one direction that matters:
    shadow mode's entire argument is that the diff it reports can be trusted.

    The reason used to be chosen by whether a **top-level** key had been
    added (when defaults were still written into calls, a nested one was
    missed); then by Python ``==``, which called a payload repaired from
    ``2.0`` to ``2`` "unchanged". Whether anything changed is now asked of
    ``same_json``, at any depth, type for type.
    """

    NESTED: dict[str, Any] = {
        "name": "configure",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {
                        "inner": {"type": "string", "default": "filled"},
                        "n": {"type": "integer"},
                    },
                }
            },
            "required": ["outer"],
        },
    }

    def test_auto_does_not_call_a_type_only_nested_change_unchanged(self) -> None:
        arguments = {"outer": {"n": 2.0}}
        outcome = outcome_for(ContractGuard.with_mcp().repair(self.NESTED, arguments), arguments)

        assert outcome.arguments == {"outer": {"n": 2}}
        assert type(outcome.arguments["outer"]["n"]) is int  # Python == would call this unchanged
        assert "unchanged" not in outcome.reason

    def test_a_nested_optional_default_is_not_written(self) -> None:
        arguments = {"outer": {}}
        outcome = outcome_for(ContractGuard.with_mcp().repair(self.NESTED, arguments), arguments)
        assert outcome.arguments == arguments
        assert "forwarded unchanged" in outcome.reason

    def test_shadow_holds_a_type_only_nested_change(self) -> None:
        guard = ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW))
        arguments = {"outer": {"n": 2.0}}
        outcome = outcome_for(guard.repair(self.NESTED, arguments), arguments)

        assert outcome.action is MCPAction.HOLD
        assert outcome.preview == {"outer": {"n": 2}}
        assert "none named" not in outcome.reason

    def test_unchanged_is_still_said_when_it_is_true(self) -> None:
        """The control: the honest case must not have been made unreachable."""
        arguments = {"outer": {"inner": "explicit"}}
        outcome = outcome_for(ContractGuard.with_mcp().repair(self.NESTED, arguments), arguments)

        assert outcome.arguments == arguments
        assert "forwarded unchanged" in outcome.reason


# ===========================================================================
# ``arguments`` is what to send, whenever anything is sent
# ===========================================================================

_SHAPES: list[Any] = [
    {"location": "Mumbai", "days": 5, "unit": "celsius"},  # valid as sent
    {"location": "Mumbai", "days": 5},  # valid, default to fill
    {"loc": "Mumbai", "days": "5"},  # repairable
    {"location": "Mumbai", "days": 99},  # out of range
    {"location": "Mumbai"},  # missing required, no default
    '{"location": "Mumbai", "days": 5}',  # JSON-encoded root
]


class TestArgumentsMatchShouldForward:
    """
    One rule a proxy can rely on without reading the action at all:
    ``arguments`` is non-``None`` exactly when ``should_forward`` is true,
    and under ``HOLD`` it is never the preview.
    """

    @pytest.mark.parametrize("mode", [RepairMode.AUTO, RepairMode.SHADOW])
    @pytest.mark.parametrize("arguments", _SHAPES)
    def test_the_rule_holds(self, mode: RepairMode, arguments: Any) -> None:
        guard = ContractGuard.with_mcp(config=GuardConfig(mode=mode))
        outcome = repair_tool_call(guard, TOOL, arguments)
        assert (outcome.arguments is not None) is outcome.should_forward
        if outcome.action is MCPAction.HOLD:
            assert outcome.arguments == arguments
            assert outcome.arguments != outcome.preview


# ===========================================================================
# repair_tool_call
# ===========================================================================

NO_PARAMS: dict[str, Any] = {
    "name": "list_allowed_directories",
    "inputSchema": {"type": "object", "properties": {}},
}


class TestRepairToolCall:
    def test_it_is_repair_then_outcome_for(self, auto: ContractGuard) -> None:
        arguments = {"loc": "Mumbai", "days": "5"}
        one_step = repair_tool_call(auto, TOOL, arguments)
        two_step = _outcome(auto, arguments)
        assert (one_step.action, one_step.arguments, one_step.reason) == (
            two_step.action,
            two_step.arguments,
            two_step.reason,
        )

    @pytest.mark.parametrize("mode", [RepairMode.AUTO, RepairMode.SHADOW])
    def test_absent_arguments_to_a_tool_with_no_parameters_forward(self, mode: RepairMode) -> None:
        """
        MCP makes ``arguments`` optional and the Python SDK sends ``None``
        for a no-argument call. Treated as a payload, ``None`` is a
        non-object root, and every such call was refused.
        """
        guard = ContractGuard.with_mcp(config=GuardConfig(mode=mode))
        outcome = repair_tool_call(guard, NO_PARAMS, None)
        assert outcome.action is MCPAction.FORWARD
        assert outcome.arguments == {}

    def test_absent_arguments_to_a_tool_that_needs_some_are_refused(
        self, auto: ContractGuard
    ) -> None:
        outcome = repair_tool_call(auto, TOOL, None)
        assert outcome.action is MCPAction.REFUSE
        assert "'location'" in outcome.reason

    def test_an_unsupported_schema_raises_rather_than_deciding(self, auto: ContractGuard) -> None:
        """
        No outcome is invented for a tool whose schema cannot be read:
        blocking it or passing it through unvalidated is the caller's policy.
        """
        from stateguard.adapters.jsonschema.errors import JSONSchemaError

        tool = {"name": "t", "inputSchema": {"type": "object", "allOf": [{}]}}
        with pytest.raises(JSONSchemaError, match="allOf"):
            repair_tool_call(auto, tool, {})


# ===========================================================================
# Reasons name what actually happened
# ===========================================================================


class TestRootNormalisationReason:
    """
    A JSON-encoded argument object is unwrapped before any field strategy
    runs, so it is not an operation. The reason used to read "Repaired 0
    field(s)" in auto, and in shadow claimed the arguments "matched the
    tool's schema" -- which they did not.
    """

    ENCODED = '{"location": "Mumbai", "days": 5, "unit": "celsius"}'

    def test_auto_says_the_root_was_normalised(self, auto: ContractGuard) -> None:
        outcome = repair_tool_call(auto, TOOL, self.ENCODED)
        assert outcome.action is MCPAction.FORWARD
        assert outcome.arguments == {"location": "Mumbai", "days": 5, "unit": "celsius"}
        assert outcome.reason.startswith("Normalised the payload root")
        assert "0 field" not in outcome.reason

    def test_auto_names_both_when_fields_were_repaired_too(self, auto: ContractGuard) -> None:
        outcome = repair_tool_call(auto, TOOL, '{"loc": "Mumbai", "days": 5}')
        assert outcome.reason.startswith("Normalised the payload root")
        assert "repaired 1 field(s)" in outcome.reason

    def test_shadow_does_not_claim_the_arguments_matched(self) -> None:
        shadow = ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW))
        outcome = repair_tool_call(shadow, TOOL, self.ENCODED)
        assert outcome.action is MCPAction.HOLD
        assert "matched" not in outcome.reason
        assert "payload-root normalisation" in outcome.reason
