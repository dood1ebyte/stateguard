"""
Follow-ups to the trust model that the first Phase 3 pass got wrong.

Each class here pins a defect found reviewing that work:

* an abstaining strategy used to end the whole repair, so one uncertain
  rename suppressed certain repairs from lower-priority strategies;
* falling through to those strategies then re-opened the bug the abstention
  existed to prevent, because resolving the competing field made the refused
  rename look unopposed;
* abstained proposals were invisible in the per-attempt record, duplicated
  across attempts, and never grouped with their competitors;
* "did this operation change anything" was answered with ``==``, which
  cannot tell ``5`` from ``5.0``;
* ``RepairConfig.min_confidence_threshold`` had stopped being read at all.
"""

from __future__ import annotations

import pytest

from stateguard import ContractGuard
from stateguard.core.engine import _identical
from stateguard.core.errors.results import RepairStatus
from stateguard.core.models.config import GuardConfig, RepairConfig
from stateguard.core.trust import TrustPolicy


@pytest.fixture
def guard() -> ContractGuard:
    return ContractGuard.with_dict_schema()


# ===========================================================================
# Strategy fall-through
# ===========================================================================


class TestStrategyFallThrough:
    """
    A strategy that abstains on everything must not end the attempt.

    The engine used to run only the highest-priority applicable strategy. If
    all of its proposals were withheld, nothing changed, the loop called that
    no progress, and the run stopped -- so an uncertain rename could suppress
    a schema-declared default fill that was never in doubt.
    """

    CONTESTED_WITH_DEFAULT = {
        "fields": [
            {"path": "user_id", "type": "string"},
            {"path": "user_name", "type": "string", "default": "anon"},
        ]
    }

    def test_lower_priority_strategy_still_runs(self, guard: ContractGuard) -> None:
        result = guard.repair(self.CONTESTED_WITH_DEFAULT, {"user_email": "a@b.com"})

        assert "DefaultValueFillStrategy" in [a.strategy_name for a in result.attempts]
        assert result.repaired_output is not None
        assert result.repaired_output["user_name"] == "anon"

    def test_the_certain_repair_is_not_lost_to_an_unrelated_abstention(
        self, guard: ContractGuard
    ) -> None:
        """Before the fix this returned AMBIGUOUS with no output at all."""
        result = guard.repair(self.CONTESTED_WITH_DEFAULT, {"user_email": "a@b.com"})
        assert result.status is RepairStatus.PARTIAL

    def test_fall_through_is_logged(self, guard: ContractGuard) -> None:
        result = guard.repair(self.CONTESTED_WITH_DEFAULT, {"user_email": "a@b.com"})
        assert "strategy.passed_over" in {e.event for e in result.repair_log}

    def test_a_passed_over_strategys_abstention_survives_selection(
        self, guard: ContractGuard
    ) -> None:
        """
        The selected strategy must not erase what the skipped ones found.
        Overwriting rather than accumulating here is what silently disabled
        the taint guard below.
        """
        result = guard.repair(self.CONTESTED_WITH_DEFAULT, {"user_email": "a@b.com"})
        assert result.ambiguous, "fuzzy's abstention was dropped when default-fill was picked"


# ===========================================================================
# Withheld sources stay withheld
# ===========================================================================


class TestWithheldSourcesStayWithheld:
    """
    Repairing the competing field by other means must not license the rename
    that was already refused.

    With `user_id` and `user_name` both missing and one `user_email` present,
    the rename is correctly withheld. Fill `user_name` from its declared
    default and `user_email` becomes the only candidate for the only
    remaining field -- scoring 0.891 unopposed. Nothing was learned about
    where `user_email` belongs, so it must stay withheld.
    """

    SCHEMA = {
        "fields": [
            {"path": "user_id", "type": "string"},
            {"path": "user_name", "type": "string", "default": "anon"},
        ]
    }

    def test_the_email_never_lands_in_user_id(self, guard: ContractGuard) -> None:
        result = guard.repair(self.SCHEMA, {"user_email": "a@b.com"})
        assert result.repaired_output is not None
        assert "user_id" not in result.repaired_output

    def test_the_source_key_is_left_untouched(self, guard: ContractGuard) -> None:
        result = guard.repair(self.SCHEMA, {"user_email": "a@b.com"})
        assert result.repaired_output is not None
        assert result.repaired_output["user_email"] == "a@b.com"

    def test_both_placements_are_offered_to_the_caller(self, guard: ContractGuard) -> None:
        result = guard.repair(self.SCHEMA, {"user_email": "a@b.com"})
        offered = {(a.target_path, a.candidates[0].source_path) for a in result.ambiguous}
        assert offered == {("user_name", "user_email"), ("user_id", "user_email")}

    def test_an_uncontested_rename_is_unaffected(self, guard: ContractGuard) -> None:
        """The guard must only bite keys that were actually withheld."""
        schema = {
            "fields": [
                {"path": "temperature", "type": "float"},
                {"path": "humidity", "type": "integer", "default": 60},
            ]
        }
        result = guard.repair(schema, {"tempXYZ": 1.0})

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {"temperature": 1.0, "humidity": 60}


# ===========================================================================
# Ambiguity bookkeeping
# ===========================================================================


class TestAmbiguityBookkeeping:
    MULTI = {
        "fields": [
            {"path": "temperature", "type": "float"},
            {"path": "user_id", "type": "string"},
            {"path": "user_name", "type": "string"},
        ]
    }

    def test_no_duplicate_entries_across_attempts(self, guard: ContractGuard) -> None:
        """
        The same withheld repair is re-proposed on each attempt its strategy
        is selected. Appending blindly made a caller prompt twice for one
        decision.
        """
        result = guard.repair(self.MULTI, {"temp_celsius": 31.5, "user_email": "a@b.com"})

        assert len(result.attempts) >= 2
        targets = [a.target_path for a in result.ambiguous]
        assert len(targets) == len(set(targets))

    def test_abstained_ops_appear_in_the_attempt_record(self, guard: ContractGuard) -> None:
        """
        Previously an abstained operation was in neither `applied` nor
        `rejected`, so a single attempt read as "the strategy did nothing".
        """
        schema = {
            "fields": [
                {"path": "user_id", "type": "string"},
                {"path": "user_name", "type": "string"},
            ]
        }
        attempt = guard.repair(schema, {"user_email": "a@b.com"}).attempts[0]

        assert len(attempt.abstained_operations) == 1
        assert attempt.abstained_operations[0].source_path == "user_email"

    def test_candidates_are_ranked_by_trust(self, guard: ContractGuard) -> None:
        result = guard.repair(self.MULTI, {"temp_celsius": 31.5, "user_email": "a@b.com"})
        for item in result.ambiguous:
            trusts = [c.trust for c in item.candidates]
            assert trusts == sorted(trusts, reverse=True)

    def test_competing_proposals_for_one_field_are_grouped(self) -> None:
        """
        `candidates` is a list so a caller can pick between rival repairs for
        the same field. Two proposals for one target must merge into a single
        ranked entry, not two entries asking about the same field twice.
        """
        from stateguard.core.engine import RepairEngine
        from stateguard.core.errors.operations import (
            FieldOperation,
            FieldOpType,
            RepairEvidence,
            RepairRisk,
        )
        from stateguard.core.errors.results import AmbiguousRepair
        from stateguard.core.models.config import RepairConfig
        from stateguard.core.strategies.registry import StrategyRegistry
        from stateguard.logging.logger import RepairLogger

        engine = RepairEngine(
            registry=StrategyRegistry([]), config=RepairConfig(), logger=RepairLogger()
        )
        collected: list[AmbiguousRepair] = []

        def _rename(source: str, trust: float) -> FieldOperation:
            return FieldOperation(
                op_type=FieldOpType.RENAME,
                target_path="user_id",
                rationale="r",
                source_path=source,
                trust=trust,
                risk=RepairRisk.INFERRED,
                evidence=RepairEvidence(name_match=trust),
            )

        engine._record_ambiguous(collected, _rename("user_email", 0.66))
        engine._record_ambiguous(collected, _rename("userid_str", 0.71))
        # Re-proposed on a later attempt -- must not create a third entry.
        engine._record_ambiguous(collected, _rename("user_email", 0.66))

        assert len(collected) == 1
        assert [c.source_path for c in collected[0].candidates] == ["userid_str", "user_email"]


# ===========================================================================
# Evidence quality
# ===========================================================================


class TestEvidenceQuality:
    def test_alternatives_counts_both_sides_of_the_contest(self, guard: ContractGuard) -> None:
        """
        Counting only candidates made the most contested case in the corpus --
        two fields competing for one key -- report a single alternative.
        """
        schema = {
            "fields": [
                {"path": "user_id", "type": "string"},
                {"path": "user_name", "type": "string"},
            ]
        }
        result = guard.repair(schema, {"user_email": "a@b.com"})
        evidence = result.ambiguous[0].candidates[0].evidence

        assert evidence.alternatives_considered == 2

    def test_the_note_says_which_side_the_alternative_was_on(self, guard: ContractGuard) -> None:
        """
        An unqualified "closest alternative" reads identically whether the
        competitor was another field to write into or another key to read
        from.
        """
        schema = {
            "fields": [
                {"path": "user_id", "type": "string"},
                {"path": "user_name", "type": "string"},
            ]
        }
        result = guard.repair(schema, {"user_email": "a@b.com"})
        notes = " ".join(result.ambiguous[0].candidates[0].evidence.notes)

        assert "another field also wanted this key" in notes


# ===========================================================================
# Did the operation actually change anything?
# ===========================================================================


class TestIdenticalComparison:
    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            (5, 5, True),
            (5, 5.0, False),  # == says True; different types
            (1, True, False),  # == says True; different types
            (0, False, False),
            ("a", "a", True),
            ({"x": 5}, {"x": 5.0}, False),
            ({"x": 5}, {"x": 5}, True),
            ([1, 2], [1, 2], True),
            ([1], [1.0], False),
            ([1], [1, 2], False),
            ({"x": 1}, {"y": 1}, False),
        ],
    )
    def test_type_aware_equality(self, left: object, right: object, expected: bool) -> None:
        assert _identical(left, right) is expected

    def test_a_redundant_write_is_still_detected_as_a_no_op(self, guard: ContractGuard) -> None:
        """
        Fixing the int/float conflation must not lose the original purpose:
        an operation writing the value that is already there did nothing.
        """
        assert _identical({"x": "hello"}, {"x": "hello"}) is True


# ===========================================================================
# min_confidence_threshold is wired up again
# ===========================================================================


class TestMinimumTrustFloor:
    SCHEMA = {
        "fields": [
            {"path": "temperature", "type": "float"},
            {"path": "humidity", "type": "integer"},
        ]
    }
    PAYLOAD = {"temp_celsius": 31.5, "humidity": 80}

    @staticmethod
    def _guard(threshold: float) -> ContractGuard:
        return ContractGuard.with_dict_schema(
            config=GuardConfig(repair=RepairConfig(min_confidence_threshold=threshold))
        )

    def test_default_threshold_leaves_the_bands_alone(self, guard: ContractGuard) -> None:
        assert guard.repair(self.SCHEMA, self.PAYLOAD).status is RepairStatus.SUCCESS

    def test_raising_it_withholds_a_repair_that_would_otherwise_apply(self) -> None:
        """
        The flag was accepted and ignored: 0.1 and 0.99 produced byte-identical
        output.
        """
        result = self._guard(0.99).repair(self.SCHEMA, self.PAYLOAD)
        assert result.status is RepairStatus.AMBIGUOUS

    def test_it_only_raises_the_bar_never_lowers_it(self) -> None:
        """A floor below a tier's own threshold must not weaken that tier."""
        policy = TrustPolicy(minimum_trust=0.1)
        from stateguard.core.errors.operations import RepairRisk

        assert policy.band_for(RepairRisk.INFERRED).apply_at == pytest.approx(0.75)
        assert policy.band_for(RepairRisk.LOSSY).apply_at == pytest.approx(0.95)


# ===========================================================================
# What the caller is told about an abstention
# ===========================================================================


class TestAmbiguityReasonIsTrue:
    """
    ``AmbiguousRepair.reason`` is the field a caller reads to decide what to
    do. It has to be a true statement about why the repair was withheld.

    There are exactly two ways to abstain: the trust score fell short, or it
    cleared the bar and the source key was already withheld earlier in the
    run. Reporting both as "below the threshold" produced text that
    contradicted its own numbers.
    """

    CONTESTED = {
        "fields": [
            {"path": "user_id", "type": "string"},
            {"path": "user_name", "type": "string", "default": "anon"},
        ]
    }

    def test_a_held_source_is_not_described_as_below_threshold(self) -> None:
        guard = ContractGuard.with_dict_schema()
        result = guard.repair(self.CONTESTED, {"user_email": "a@b.com"})

        held = next(a for a in result.ambiguous if a.target_path == "user_id")
        best = held.best
        assert best is not None

        # The rename to user_id scores 0.891, which clears INFERRED's 0.75
        # bar -- it was withheld because user_email had already been declined.
        assert best.trust > 0.75
        assert "below" not in held.reason
        assert "already withheld" in held.reason
        assert "user_email" in held.reason

    def test_a_genuinely_low_score_still_says_below(self) -> None:
        guard = ContractGuard.with_dict_schema()
        result = guard.repair(self.CONTESTED, {"user_email": "a@b.com"})

        low = next(a for a in result.ambiguous if a.target_path == "user_name")
        best = low.best
        assert best is not None
        assert best.trust < 0.75
        assert "is below the INFERRED threshold" in low.reason

    def test_the_reason_describes_the_top_ranked_candidate(self) -> None:
        """Merging rivals must not leave the reason describing a lower one."""
        guard = ContractGuard.with_dict_schema()
        result = guard.repair(self.CONTESTED, {"user_email": "a@b.com"})
        for entry in result.ambiguous:
            best = entry.best
            assert best is not None
            assert f"{best.trust:.2f}" in entry.reason


class TestAttemptAttribution:
    """
    An attempt may consult several strategies before one has something it can
    apply. ``proposed_operations`` therefore spans all of them, so the record
    has to say which ones were consulted or the operations cannot be
    attributed at all.
    """

    def test_considered_strategies_records_every_strategy_consulted(self) -> None:
        guard = ContractGuard.with_dict_schema()
        result = guard.repair(
            {
                "fields": [
                    {"path": "user_id", "type": "string"},
                    {"path": "user_name", "type": "string", "default": "anon"},
                ]
            },
            {"user_email": "a@b.com"},
        )

        first = result.attempts[0]
        # Fuzzy proposed a rename it abstained on, so the engine moved on to
        # the default fill -- whose name is the one on strategy_name.
        assert first.strategy_name == "DefaultValueFillStrategy"
        assert "FuzzyFieldMatchStrategy" in first.considered_strategies
        assert first.considered_strategies[-1] == first.strategy_name
        # The carried proposal is present, and now attributable.
        assert any(op.source_path == "user_email" for op in first.proposed_operations)

    def test_a_single_strategy_attempt_lists_only_itself(self) -> None:
        guard = ContractGuard.with_dict_schema()
        result = guard.repair(
            {"fields": [{"path": "temperature", "type": "float"}]},
            {"temperature": "31.5"},
        )
        assert result.attempts[0].considered_strategies == ["TypeCoercionStrategy"]
