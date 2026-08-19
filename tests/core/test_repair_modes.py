"""
Phase 6: Shadow and Auto modes.

The claim Shadow Mode has to earn is that it is *the same engine* -- same
violations detected, same operations proposed, scored, applied to the working
copy and revalidated -- differing only in whether the outcome is handed back
as committed. Most of what follows checks that equivalence directly, because
a shadow run that behaved differently would tell a team nothing useful about
what auto would do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stateguard.core.errors.results import RepairStatus
from stateguard.core.models.config import GuardConfig, RepairConfig, RepairMode
from stateguard.guard import ContractGuard
from stateguard.logging.repair_history import RepairHistoryRecorder

SCHEMA: dict[str, Any] = {
    "fields": [
        {"path": "temperature", "type": "float"},
        {"path": "humidity", "type": "integer", "default": 50},
    ]
}
BROKEN: dict[str, Any] = {"temp_celsius": "31.5"}

PARTIAL_SCHEMA: dict[str, Any] = {
    "fields": [
        {"path": "temperature", "type": "float"},
        {"path": "station", "type": "string", "constraints": [{"type": "min_length", "value": 99}]},
    ]
}
PARTIAL_PAYLOAD: dict[str, Any] = {"temp_celsius": "31.5", "station": "KBOS"}

UNREPAIRABLE: dict[str, Any] = {"wholly_unrelated": 1}

# SCHEMA declares a default for `humidity`, so *any* payload gets at least
# that filled and lands on PARTIAL. A genuinely FAILED run needs a contract
# with nothing fillable.
NO_DEFAULT_SCHEMA: dict[str, Any] = {"fields": [{"path": "temperature", "type": "float"}]}


def guard(mode: RepairMode, **kwargs: Any) -> ContractGuard:
    return ContractGuard.with_dict_schema(config=GuardConfig(mode=mode, **kwargs))


@pytest.fixture
def auto() -> ContractGuard:
    return guard(RepairMode.AUTO)


@pytest.fixture
def shadow() -> ContractGuard:
    return guard(RepairMode.SHADOW)


# ===========================================================================
# Defaults
# ===========================================================================


class TestDefaults:
    def test_auto_is_the_default(self) -> None:
        """Introducing modes must not change any existing behaviour."""
        assert GuardConfig().mode is RepairMode.AUTO

    def test_a_default_guard_commits_its_repair(self) -> None:
        result = ContractGuard.with_dict_schema().repair(SCHEMA, BROKEN)
        assert result.mode is RepairMode.AUTO
        assert result.is_shadow is False
        assert result.repaired_output is not None
        assert result.proposed_output is None


# ===========================================================================
# The two fields
# ===========================================================================


class TestOutputPlacement:
    def test_shadow_withholds_the_payload_from_repaired_output(self, shadow: ContractGuard) -> None:
        """
        The safety property: code written against repaired_output gets None
        under shadow and fails visibly, rather than quietly receiving data
        nobody meant to commit.
        """
        result = shadow.repair(SCHEMA, BROKEN)
        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output is None
        assert result.proposed_output == {"temperature": 31.5, "humidity": 50}

    def test_the_two_fields_are_never_both_populated(
        self, auto: ContractGuard, shadow: ContractGuard
    ) -> None:
        for g in (auto, shadow):
            for payload in (BROKEN, UNREPAIRABLE, {"temperature": 1.0, "humidity": 2}):
                result = g.repair(SCHEMA, payload)
                populated = [
                    f for f in (result.repaired_output, result.proposed_output) if f is not None
                ]
                assert len(populated) <= 1, (g, payload)

    @pytest.mark.parametrize(
        ("payload", "expected_status"),
        [
            (BROKEN, RepairStatus.SUCCESS),
            ({"temperature": 1.0, "humidity": 2}, RepairStatus.ALREADY_VALID),
        ],
    )
    def test_shadow_holds_back_every_status_that_would_have_produced_output(
        self, shadow: ContractGuard, payload: dict[str, Any], expected_status: RepairStatus
    ) -> None:
        """
        Including ALREADY_VALID. "repaired_output is always None in SHADOW" is
        the invariant worth having; an exception for the case where nothing
        needed fixing is exactly the kind of carve-out that causes bugs.
        """
        result = shadow.repair(SCHEMA, payload)
        assert result.status is expected_status
        assert result.repaired_output is None
        assert result.proposed_output is not None

    def test_partial_is_held_back_too(self) -> None:
        result = guard(RepairMode.SHADOW).repair(PARTIAL_SCHEMA, PARTIAL_PAYLOAD)
        assert result.status is RepairStatus.PARTIAL
        assert result.repaired_output is None
        assert result.proposed_output is not None

    def test_a_failed_repair_proposes_nothing(self, shadow: ContractGuard) -> None:
        result = shadow.repair(NO_DEFAULT_SCHEMA, UNREPAIRABLE)
        assert result.status is RepairStatus.FAILED
        assert result.repaired_output is None
        assert result.proposed_output is None

    def test_an_unrepairable_root_carries_the_mode(self, shadow: ContractGuard) -> None:
        """The root-failure path returns early and must still report mode."""
        result = shadow.repair(SCHEMA, 5)
        assert result.status is RepairStatus.FAILED
        assert result.mode is RepairMode.SHADOW
        assert result.is_shadow is True


# ===========================================================================
# Equivalence
# ===========================================================================


class TestShadowIsTheSameEngine:
    """
    A shadow rollout is only informative if it does exactly what auto would.
    """

    @pytest.mark.parametrize(
        "payload",
        [BROKEN, UNREPAIRABLE, {"temperature": 1.0, "humidity": 2}, {"temperature": "x"}],
    )
    def test_status_matches_auto(
        self, auto: ContractGuard, shadow: ContractGuard, payload: dict[str, Any]
    ) -> None:
        assert shadow.repair(SCHEMA, payload).status == auto.repair(SCHEMA, payload).status

    def test_the_payload_matches_what_auto_committed(
        self, auto: ContractGuard, shadow: ContractGuard
    ) -> None:
        assert (
            shadow.repair(SCHEMA, BROKEN).proposed_output
            == auto.repair(SCHEMA, BROKEN).repaired_output
        )

    def test_the_same_operations_are_applied_to_the_working_copy(
        self, auto: ContractGuard, shadow: ContractGuard
    ) -> None:
        """
        Shadow really does apply -- to the engine's own copy. That is the only
        way to know the plan validates, and it is why the operation-level
        audit trail is identical in both modes.
        """

        def ops(result: Any) -> list[tuple[str, str]]:
            return [
                (op.op_type.value, op.target_path)
                for attempt in result.attempts
                for op in attempt.applied_operations
            ]

        assert ops(shadow.repair(SCHEMA, BROKEN)) == ops(auto.repair(SCHEMA, BROKEN))

    def test_trust_scores_are_identical(self, auto: ContractGuard, shadow: ContractGuard) -> None:
        """Mode is not a confidence setting -- TrustPolicy is unaffected."""

        def trusts(result: Any) -> list[float]:
            return [op.trust for attempt in result.attempts for op in attempt.applied_operations]

        assert trusts(shadow.repair(SCHEMA, BROKEN)) == trusts(auto.repair(SCHEMA, BROKEN))

    def test_ambiguous_candidates_are_still_surfaced(self) -> None:
        contested = {
            "fields": [
                {"path": "user_id", "type": "string"},
                {"path": "user_name", "type": "string"},
            ]
        }
        result = guard(RepairMode.SHADOW).repair(contested, {"user_email": "a@b.com"})
        assert result.status is RepairStatus.AMBIGUOUS
        # user_email scores highest against user_name (0.913), so that is the
        # pairing the assignment takes and then withholds on margin.
        assert [a.target_path for a in result.ambiguous] == ["user_name"]
        assert result.proposed_output is None

    def test_neither_mode_mutates_the_caller_payload(
        self, auto: ContractGuard, shadow: ContractGuard
    ) -> None:
        for g in (auto, shadow):
            payload = {"temp_celsius": "31.5"}
            g.repair(SCHEMA, payload)
            assert payload == {"temp_celsius": "31.5"}


# ===========================================================================
# Adapter rehydration
# ===========================================================================


class TestProposedOutputIsRehydrated:
    def test_a_shadow_preview_has_the_shape_auto_would_have_committed(self) -> None:
        """
        Flipping SHADOW -> AUTO must change which field carries the value and
        nothing about the value itself, or a week of shadow diffing proves
        nothing about what auto will do.
        """
        pytest.importorskip("pydantic")
        from pydantic import BaseModel

        class Weather(BaseModel):
            temperature: float

        auto_result = ContractGuard.with_pydantic().repair(Weather, {"temp_celsius": "31.5"})
        shadow_result = ContractGuard.with_pydantic(
            config=GuardConfig(mode=RepairMode.SHADOW)
        ).repair(Weather, {"temp_celsius": "31.5"})

        assert isinstance(auto_result.repaired_output, Weather)
        assert isinstance(shadow_result.proposed_output, Weather)
        assert shadow_result.proposed_output == auto_result.repaired_output

    def test_partial_is_not_wrapped_in_either_mode(self) -> None:
        """PARTIAL does not fully validate, so wrap is not attempted."""
        result = guard(RepairMode.SHADOW).repair(PARTIAL_SCHEMA, PARTIAL_PAYLOAD)
        assert isinstance(result.proposed_output, dict)


# ===========================================================================
# Interaction with other config
# ===========================================================================


class TestConfigInteractions:
    def test_mode_is_orthogonal_to_allow_partial_repair(self) -> None:
        result = guard(RepairMode.SHADOW, repair=RepairConfig(allow_partial_repair=False)).repair(
            PARTIAL_SCHEMA, PARTIAL_PAYLOAD
        )
        assert result.status is RepairStatus.FAILED
        assert result.proposed_output is None
        assert result.repaired_output is None

    def test_history_records_the_mode(self, tmp_path: Path) -> None:
        """
        Operation records are byte-identical in both modes, so without this a
        history file cannot distinguish a committed repair from a preview.
        """
        records: dict[str, list[dict[str, Any]]] = {}
        for mode in (RepairMode.AUTO, RepairMode.SHADOW):
            path = tmp_path / f"{mode.value}.jsonl"
            ContractGuard.with_dict_schema(
                config=GuardConfig(mode=mode),
                history=RepairHistoryRecorder(path=str(path)),
            ).repair(SCHEMA, BROKEN)
            records[mode.value] = [json.loads(line) for line in path.read_text().splitlines()]

        assert {r["mode"] for r in records["auto"]} == {"auto"}
        assert {r["mode"] for r in records["shadow"]} == {"shadow"}
        # Everything else about the records matches, which is the point.
        strip = lambda rs: [  # noqa: E731
            {k: v for k, v in r.items() if k not in ("mode", "timestamp")} for r in rs
        ]
        assert strip(records["auto"]) == strip(records["shadow"])

    def test_telemetry_carries_the_mode(self) -> None:
        from stateguard.telemetry.hooks import TelemetryEvent, TelemetryEventType

        seen: list[TelemetryEvent] = []

        class Hook:
            def emit(self, event: TelemetryEvent) -> None:
                seen.append(event)

        ContractGuard.with_dict_schema(
            config=GuardConfig(mode=RepairMode.SHADOW), telemetry=Hook()
        ).repair(SCHEMA, BROKEN)

        terminal = [
            e
            for e in seen
            if e.event_type
            in (TelemetryEventType.REPAIR_COMPLETED, TelemetryEventType.REPAIR_FAILED)
        ]
        assert terminal
        assert all(e.data["mode"] == "shadow" for e in terminal)


class TestModeCoercion:
    """
    ``RepairMode`` is a ``StrEnum``, so ``mode="shadow"`` is the natural thing
    to write. Uncoerced it defeated the mode silently: the engine tests
    ``is RepairMode.SHADOW``, which is False for a plain string, so it took
    the AUTO branch and committed a repair the caller asked it to withhold.
    """

    def test_a_string_mode_is_coerced_not_ignored(self) -> None:
        config = GuardConfig(mode="shadow")  # type: ignore[arg-type]
        assert config.mode is RepairMode.SHADOW

    def test_a_string_mode_actually_shadows(self) -> None:
        result = ContractGuard.with_dict_schema(
            config=GuardConfig(mode="shadow")  # type: ignore[arg-type]
        ).repair(SCHEMA, BROKEN)
        assert result.is_shadow is True
        assert result.repaired_output is None
        assert result.proposed_output is not None

    def test_a_string_auto_is_coerced_too(self) -> None:
        assert GuardConfig(mode="auto").mode is RepairMode.AUTO  # type: ignore[arg-type]

    def test_an_unknown_mode_is_rejected_at_construction(self) -> None:
        """Not at repair time, several frames from the mistake."""
        with pytest.raises(ValueError, match="bogus"):
            GuardConfig(mode="bogus")  # type: ignore[arg-type]

    def test_the_enum_still_round_trips(self) -> None:
        assert GuardConfig(mode=RepairMode.SHADOW).mode is RepairMode.SHADOW


class TestRootFailureTelemetry:
    def test_an_unrepairable_root_still_reports_its_mode(self) -> None:
        """
        The root-failure path returns before the loop, so it needs its own
        `mode` on the terminal event -- a dashboard filtering shadow traffic
        would otherwise drop exactly these.
        """
        from stateguard.telemetry.hooks import TelemetryEvent, TelemetryEventType

        seen: list[TelemetryEvent] = []

        class Hook:
            def emit(self, event: TelemetryEvent) -> None:
                seen.append(event)

        ContractGuard.with_dict_schema(
            config=GuardConfig(mode=RepairMode.SHADOW), telemetry=Hook()
        ).repair(SCHEMA, 5)

        failed = [e for e in seen if e.event_type is TelemetryEventType.REPAIR_FAILED]
        assert failed
        assert all(e.data["mode"] == "shadow" for e in failed)
