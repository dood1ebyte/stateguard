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
from stateguard.adapters.mcp import MCPAction, outcome_for
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
        assert outcome.arguments == {"location": "Mumbai", "days": 5, "unit": "celsius"}
        assert outcome.preview is None

    def test_an_already_valid_call_forwards(self, auto: ContractGuard) -> None:
        outcome = _outcome(auto, {"location": "Mumbai", "days": 5, "unit": "celsius"})
        assert outcome.action is MCPAction.FORWARD
        assert outcome.result.status is RepairStatus.ALREADY_VALID

    def test_the_reason_admits_when_defaults_changed_the_payload(self, auto: ContractGuard) -> None:
        """
        "Already valid" and "what I am sending differs from what you sent"
        are both true when a default gets filled. A log line claiming the
        arguments went out unchanged would be wrong.
        """
        outcome = _outcome(auto, {"location": "Mumbai", "days": 5})
        assert outcome.arguments is not None
        assert outcome.arguments["unit"] == "celsius"
        assert "'unit'" in outcome.reason
        assert "unchanged" not in outcome.reason

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
        ``arguments`` without checking ``action`` would apply repairs the
        caller explicitly asked it to withhold; ``None`` means it sends
        nothing instead.
        """
        outcome = _outcome(shadow, {"loc": "Mumbai", "days": "5"})
        assert outcome.arguments is None
        assert outcome.preview == {"location": "Mumbai", "days": 5, "unit": "celsius"}

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

    def test_an_already_valid_call_holds_when_defaults_would_be_filled(
        self, shadow: ContractGuard
    ) -> None:
        """
        Shadow also withholds the *default fill*, which is a change to the
        payload like any other -- and therefore a change shadow has to show.

        This used to report ``FORWARD`` with no preview and a reason reading
        "forwarded unchanged", because the mapping inferred shadow from
        ``proposed_output`` and never asked about ``ALREADY_VALID``. The
        payload sent was right; what was lost was the only signal a team
        running shadow has that auto would send something else.
        """
        arguments = {"location": "Mumbai", "days": 5}
        outcome = _outcome(shadow, arguments)
        assert outcome.result.status is RepairStatus.ALREADY_VALID
        assert outcome.action is MCPAction.HOLD
        assert outcome.preview == {"location": "Mumbai", "days": 5, "unit": "celsius"}
        assert "default" in outcome.reason

    def test_holding_still_sends_the_model_s_arguments(self, shadow: ContractGuard) -> None:
        """
        ``HOLD`` means the proxy forwards what it received. ``arguments`` is
        ``None`` so a caller cannot forward the preview by accident.
        """
        outcome = _outcome(shadow, {"location": "Mumbai", "days": 5})
        assert outcome.arguments is None
        assert outcome.should_forward is True

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
        arguments = {"location": "Mumbai", "days": 5}
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
    added, while ``JSONSchemaAdapter.wrap`` fills declared defaults at every
    depth. So a default on a *nested* property produced a payload that
    differed from the model's under a line reading "forwarded unchanged" --
    and the shadow wording rendered as "would also have filled none named",
    a sentence promising a list and then naming nothing.
    """

    NESTED: dict[str, Any] = {
        "name": "configure",
        "inputSchema": {
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {"inner": {"type": "string", "default": "filled"}},
                }
            },
            "required": ["outer"],
        },
    }

    def test_auto_does_not_call_a_changed_payload_unchanged(self) -> None:
        arguments = {"outer": {}}
        outcome = outcome_for(ContractGuard.with_mcp().repair(self.NESTED, arguments), arguments)

        assert outcome.arguments != arguments, "precondition: wrap filled a nested default"
        assert "unchanged" not in outcome.reason

    def test_auto_names_the_nested_path_it_filled(self) -> None:
        arguments = {"outer": {}}
        outcome = outcome_for(ContractGuard.with_mcp().repair(self.NESTED, arguments), arguments)
        assert "'outer.inner'" in outcome.reason

    def test_shadow_names_it_too_instead_of_naming_nothing(self) -> None:
        guard = ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW))
        arguments = {"outer": {}}
        outcome = outcome_for(guard.repair(self.NESTED, arguments), arguments)

        assert outcome.action is MCPAction.HOLD
        assert "'outer.inner'" in outcome.reason
        assert "none named" not in outcome.reason

    def test_unchanged_is_still_said_when_it_is_true(self) -> None:
        """The control: the honest case must not have been made unreachable."""
        arguments = {"outer": {"inner": "explicit"}}
        outcome = outcome_for(ContractGuard.with_mcp().repair(self.NESTED, arguments), arguments)

        assert outcome.arguments == arguments
        assert "forwarded unchanged" in outcome.reason
