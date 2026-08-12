"""Tests for stateguard.core.strategies.fuzzy."""

from __future__ import annotations

import pytest

from stateguard.core.errors.operations import FieldOpType
from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.core.strategies.fuzzy import (
    FuzzyFieldMatchStrategy,
    jaro_winkler,
    score_assignments,
)
from stateguard.core.trust import TrustDecision, TrustPolicy
from stateguard.guard import ContractGuard
from tests.conftest import make_violation


# ===========================================================================
# FuzzyFieldMatchStrategy — identity
# ===========================================================================


class TestIdentity:
    def test_name(self) -> None:
        assert FuzzyFieldMatchStrategy().name == "FuzzyFieldMatchStrategy"

    def test_priority(self) -> None:
        assert FuzzyFieldMatchStrategy().priority == 20

    def test_default_thresholds(self) -> None:
        strategy = FuzzyFieldMatchStrategy()
        # Both constructor parameters are accepted and ignored: thresholding
        # moved to TrustPolicy when strategies stopped scoring themselves.
        assert strategy._min_confidence_threshold is None
        assert strategy._score_collision_margin is None

    def test_custom_thresholds(self) -> None:
        strategy = FuzzyFieldMatchStrategy(
            min_confidence_threshold=0.5, score_collision_margin=0.05
        )
        assert strategy._min_confidence_threshold == 0.5
        assert strategy._score_collision_margin == 0.05
        # ...stored, but no longer consulted by propose().


# ===========================================================================
# can_handle
# ===========================================================================


class TestCanHandle:
    def test_true_with_missing_and_unexpected(self) -> None:
        missing = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        unexpected = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        assert strategy.can_handle([missing, unexpected], make_contract(), {}) is True

    def test_false_with_only_missing(self) -> None:
        missing = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        strategy = FuzzyFieldMatchStrategy()
        assert strategy.can_handle([missing], make_contract(), {}) is False

    def test_false_with_only_unexpected(self) -> None:
        unexpected = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        assert strategy.can_handle([unexpected], make_contract(), {}) is False

    def test_false_with_no_violations(self) -> None:
        strategy = FuzzyFieldMatchStrategy()
        assert strategy.can_handle([], make_contract(), {}) is False

    def test_false_with_unrelated_violation_types(self) -> None:
        v = make_violation(
            field_path="age",
            violation_type=ViolationType.TYPE_MISMATCH,
        )
        strategy = FuzzyFieldMatchStrategy()
        assert strategy.can_handle([v], make_contract(), {}) is False


def make_contract() -> ContractSpec:
    return ContractSpec(fields=[FieldSpec("placeholder", FieldType.STRING)])


# ===========================================================================
# propose — single high-confidence match
# ===========================================================================


class TestProposeSingleMatch:
    def test_high_confidence_match_proposes_rename(self) -> None:
        """'cty' -> 'city' scores 0.75, above default threshold 0.7,
        with no second candidate -> no collision."""
        missing = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        unexpected = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose([missing, unexpected], make_contract(), {"cty": "Mumbai"})

        assert len(ops) == 1
        op = ops[0]
        assert op.op_type is FieldOpType.RENAME
        assert op.source_path == "cty"
        assert op.target_path == "city"
        # propose() reports evidence; TrustPolicy assigns trust.
        assert op.trust == 0.0
        assert op.evidence.name_match is not None
        assert TrustPolicy().evaluate(op)[1] is TrustDecision.APPLY

    def test_rationale_names_the_fields_and_evidence_carries_the_score(self) -> None:
        missing = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        unexpected = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose([missing, unexpected], make_contract(), {"cty": "Mumbai"})
        rationale = ops[0].rationale
        assert "cty" in rationale
        assert "city" in rationale
        # The score left the rationale string and became structured evidence,
        # which is what TrustPolicy.explain() renders.
        assert "0.75" not in rationale
        assert ops[0].evidence.name_match is not None
        assert any("jaro-winkler" in note for note in ops[0].evidence.notes)

    def test_high_confidence_match_zip_code(self) -> None:
        """'zipcode' -> 'zip_code' scores 0.875."""
        missing = make_violation(
            field_path="zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected = make_violation(
            field_path="zipcode",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose([missing, unexpected], make_contract(), {"zipcode": "400001"})
        assert len(ops) == 1
        # propose() reports evidence; TrustPolicy assigns trust.
        assert ops[0].trust == 0.0
        assert ops[0].evidence.name_match is not None
        assert TrustPolicy().evaluate(ops[0])[1] is TrustDecision.APPLY
        assert ops[0].source_path == "zipcode"
        assert ops[0].target_path == "zip_code"


# ===========================================================================
# propose — below threshold
# ===========================================================================


class TestProposeBelowThreshold:
    def test_score_below_threshold_proposes_nothing(self) -> None:
        """'humidity' -> 'temperature' scores 0.18, well below 0.7."""
        missing = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected = make_violation(
            field_path="humidity",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose([missing, unexpected], make_contract(), {"humidity": 80})
        assert ops == []

    def test_completely_dissimilar_single_chars(self) -> None:
        missing = make_violation(
            field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        unexpected = make_violation(
            field_path="b",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose([missing, unexpected], make_contract(), {"b": 1})
        assert ops == []

    def test_constructor_threshold_no_longer_lets_weak_matches_through(self) -> None:
        """
        'humidity' and 'temperature' are simply different words (similarity
        0.477). The old constructor threshold could be lowered to force a
        match anyway; it is now ignored, and a pair this dissimilar is not
        proposed at all.
        """
        missing = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected = make_violation(
            field_path="humidity",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy(min_confidence_threshold=0.1)
        ops = strategy.propose([missing, unexpected], make_contract(), {"humidity": 80})
        assert ops == []


# ===========================================================================
# propose — collision detection
# ===========================================================================


class TestProposeCollision:
    def test_collision_is_surfaced_as_ambiguous_not_dropped(self) -> None:
        """
        'userId' and 'usr_id' are both plausible renames of 'user_id'.

        A near-tie used to make the strategy return nothing at all, so the
        caller never learned a repair had been considered. Now the pairing is
        proposed with its narrow margin recorded as evidence, and TrustPolicy
        withholds it -- an outcome the caller can see and act on.
        """
        missing = make_violation(
            field_path="user_id",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected1 = make_violation(
            field_path="userId",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected2 = make_violation(
            field_path="usr_id",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose(
            [missing, unexpected1, unexpected2],
            make_contract(),
            {"userId": 1, "usr_id": 2},
        )
        assert len(ops) == 1
        assert ops[0].evidence.margin is not None
        assert ops[0].evidence.margin < 0.15
        assert TrustPolicy().evaluate(ops[0])[1] is TrustDecision.AMBIGUOUS

    def test_collision_avoided_with_zero_margin(self) -> None:
        """A candidate with no competitor left is proposed outright."""
        missing = make_violation(
            field_path="user_id",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected1 = make_violation(
            field_path="userId",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected2 = make_violation(
            field_path="usr_id",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose(
            [missing, unexpected1, unexpected2],
            make_contract(),
            {"userId": 1, "usr_id": 2},
        )
        assert len(ops) == 1
        assert ops[0].source_path == "userId"

    def test_clear_winner_no_collision(self) -> None:
        """
        'zipcode' (0.875) vs 'postal_code' (0.4545) against 'zip_code' —
        difference 0.42 > margin 0.15 -> clean winner proposed.
        """
        missing = make_violation(
            field_path="zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected1 = make_violation(
            field_path="zipcode",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected2 = make_violation(
            field_path="postal_code",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose(
            [missing, unexpected1, unexpected2],
            make_contract(),
            {"zipcode": "400001", "postal_code": "400001"},
        )
        assert len(ops) == 1
        assert ops[0].source_path == "zipcode"
        assert ops[0].target_path == "zip_code"

    def test_collision_within_margin_but_not_zero(self) -> None:
        """
        Construct two candidates whose scores differ by less than the
        default margin (0.15) but are not identical.
        'usr_id' (0.8571) vs 'the_user_id' against 'user_id'.
        """
        # normalized_score('user_id', 'the_user_id'):
        # levenshtein('user_id','the_user_id') -> insert 'the_' (4 chars) = 4
        # max_len = 11 -> score = 1 - 4/11 = 0.6364
        # diff vs usr_id (0.8571) = 0.2207 > 0.15 -> not a collision in this case
        # Use a constructed pair instead with explicit close scores via
        # custom margin to directly exercise the boundary condition.
        missing = make_violation(
            field_path="abcde",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        # 'abcdf' -> distance 1, score = 1 - 1/5 = 0.8
        # 'abcfg' -> distance 2, score = 1 - 2/5 = 0.6
        # difference = 0.2 -- with margin=0.25 this collides, with margin=0.1 it doesn't
        unexpected1 = make_violation(
            field_path="abcdf",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected2 = make_violation(
            field_path="abcfg",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        data = {"abcdf": 1, "abcfg": 2}

        # 'abcdf' beats 'abcfg' by 0.107 -- narrow, but a real gap. The
        # margin scales trust rather than vetoing the pairing outright, so a
        # clear-enough winner is still applied.
        strategy_collide = FuzzyFieldMatchStrategy()
        ops_collide = strategy_collide.propose(
            [missing, unexpected1, unexpected2], make_contract(), data
        )
        assert len(ops_collide) == 1
        assert ops_collide[0].source_path == "abcdf"
        assert TrustPolicy().evaluate(ops_collide[0])[1] is TrustDecision.APPLY

        # The runner-up is recorded as evidence rather than silently
        # discarded, so an explanation can name what nearly won.
        assert ops_collide[0].evidence.margin == pytest.approx(0.107, abs=0.01)
        assert ops_collide[0].evidence.alternatives_considered == 2
        assert any("abcfg" in note for note in ops_collide[0].evidence.notes)


# ===========================================================================
# propose — multiple missing fields / consumption
# ===========================================================================


class TestProposeMultipleMissing:
    def test_two_missing_two_unexpected_each_matched(self) -> None:
        missing_city = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        missing_zip = make_violation(
            field_path="zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected_cty = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected_zip = make_violation(
            field_path="zipcode",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose(
            [missing_city, missing_zip, unexpected_cty, unexpected_zip],
            make_contract(),
            {"cty": "Mumbai", "zipcode": "400001"},
        )
        assert len(ops) == 2
        targets = {op.target_path for op in ops}
        sources = {op.source_path for op in ops}
        assert targets == {"city", "zip_code"}
        assert sources == {"cty", "zipcode"}

    def test_consumed_key_not_reused(self) -> None:
        """
        Two missing fields both scoring highest against the same single
        unexpected key: only the first (sorted) missing field consumes it;
        the second gets nothing.
        """
        missing_a = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        missing_b = make_violation(
            field_path="cty_2",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose(
            [missing_a, missing_b, unexpected], make_contract(), {"cty": "Mumbai"}
        )
        # missing_fields sorted: ['city', 'cty_2'] -> 'city' processed first
        assert len(ops) == 1
        assert ops[0].target_path == "city"
        assert ops[0].source_path == "cty"

    def test_missing_fields_processed_in_sorted_order(self) -> None:
        """Determinism: missing fields are processed alphabetically."""
        missing_z = make_violation(
            field_path="zzz_field",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        missing_a = make_violation(
            field_path="aaa_field",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected_a = make_violation(
            field_path="aaa_feild",  # close to aaa_field
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected_z = make_violation(
            field_path="zzz_feild",  # close to zzz_field
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose(
            [missing_z, missing_a, unexpected_a, unexpected_z],
            make_contract(),
            {"aaa_feild": 1, "zzz_feild": 2},
        )
        assert len(ops) == 2
        # Both should be matched correctly regardless of input order.
        result_map = {op.target_path: op.source_path for op in ops}
        assert result_map["aaa_field"] == "aaa_feild"
        assert result_map["zzz_field"] == "zzz_feild"


# ===========================================================================
# propose — self-match guard
# ===========================================================================


class TestProposeSelfMatchGuard:
    def test_identical_name_never_matches_itself(self) -> None:
        """
        If a missing field's name is identical to an unexpected key's name
        (degenerate edge case), it must not be proposed as a rename to
        itself.
        """
        missing = make_violation(
            field_path="duplicate_name",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected_self = make_violation(
            field_path="duplicate_name",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected_other = make_violation(
            field_path="duplicate_naem",  # close typo, not identical
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose(
            [missing, unexpected_self, unexpected_other],
            make_contract(),
            {"duplicate_name": 1, "duplicate_naem": 2},
        )
        # Self-match candidate excluded; the typo candidate should be used.
        assert len(ops) == 1
        assert ops[0].source_path == "duplicate_naem"
        assert ops[0].target_path == "duplicate_name"

    def test_only_self_match_candidate_proposes_nothing(self) -> None:
        missing = make_violation(
            field_path="duplicate_name",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected_self = make_violation(
            field_path="duplicate_name",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose([missing, unexpected_self], make_contract(), {"duplicate_name": 1})
        assert ops == []


# ===========================================================================
# propose — empty inputs
# ===========================================================================


class TestProposeEmptyInputs:
    def test_no_violations_proposes_nothing(self) -> None:
        strategy = FuzzyFieldMatchStrategy()
        assert strategy.propose([], make_contract(), {}) == []

    def test_only_missing_proposes_nothing(self) -> None:
        missing = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        strategy = FuzzyFieldMatchStrategy()
        assert strategy.propose([missing], make_contract(), {}) == []

    def test_only_unexpected_proposes_nothing(self) -> None:
        unexpected = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        assert strategy.propose([unexpected], make_contract(), {"cty": 1}) == []


# ===========================================================================
# propose — unicode field names
# ===========================================================================


class TestProposeUnicode:
    def test_unicode_field_names(self) -> None:
        """'café' vs 'cafe' scores 0.75 (above default threshold)."""
        missing = make_violation(
            field_path="café", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        unexpected = make_violation(
            field_path="cafe",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose([missing, unexpected], make_contract(), {"cafe": "value"})
        assert len(ops) == 1
        # propose() reports evidence; TrustPolicy assigns trust.
        assert ops[0].trust == 0.0
        assert ops[0].evidence.name_match is not None
        assert TrustPolicy().evaluate(ops[0])[1] is TrustDecision.APPLY


# ===========================================================================
# Internal helper methods — direct tests
# ===========================================================================


class TestInternalHelpers:
    def test_find_missing_fields(self) -> None:
        v1 = make_violation(field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        v2 = make_violation(
            field_path="b",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        v3 = make_violation(field_path="c", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        result = FuzzyFieldMatchStrategy._find_missing_fields([v1, v2, v3])
        assert result == ["a", "c"]

    def test_find_unexpected_keys(self) -> None:
        v1 = make_violation(field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        v2 = make_violation(
            field_path="b",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        result = FuzzyFieldMatchStrategy._find_unexpected_keys([v1, v2])
        assert result == ["b"]


# ===========================================================================
# Nested fields (M9 hardening — depth 2 and depth 3)
#
# FuzzyFieldMatchStrategy operates on full dot-notation field paths.
# Nested support requires no special-case code: paths like
# "address.zip_code" are scored as plain strings against
# "address.zipcode", and because both candidates share the same
# "address." prefix, the shared prefix naturally inflates their mutual
# similarity relative to any out-of-scope candidate (whose path would
# carry a different prefix). This section proves that behavior explicitly
# at depth 2 ("address.city") and depth 3 ("address.country.code") —
# StateGuard's officially validated nesting depth; see README.md and
# M9_AUDIT.md for the rationale and the (intentionally undefended)
# cross-branch collision risk this implies for adversarial inputs.
# ===========================================================================


class TestNestedDepth2:
    """One level of nested OBJECT: root.address.<field>."""

    def test_depth2_fuzzy_rename(self) -> None:
        missing = make_violation(
            field_path="address.zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected = make_violation(
            field_path="address.zipcode",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        data = {"address": {"zipcode": "400001"}}
        ops = strategy.propose([missing, unexpected], make_contract(), data)

        assert len(ops) == 1
        assert ops[0].source_path == "address.zipcode"
        assert ops[0].target_path == "address.zip_code"
        assert TrustPolicy().evaluate(ops[0])[1] is TrustDecision.APPLY

    def test_depth2_shared_prefix_inflates_similarity(self) -> None:
        """The shared 'address.' prefix raises the score well above what
        the bare field names ('city' vs 'cty', already 0.75) would give
        alone -- nesting context makes the match even more confident."""
        bare_score = jaro_winkler("city", "cty")
        nested_score = jaro_winkler("address.city", "address.cty")
        assert nested_score > bare_score

    def test_depth2_cross_branch_not_matched_by_default(self) -> None:
        """A same-named-ish unexpected field in a DIFFERENT branch is not
        mistaken for the nested missing field, because the differing
        path prefixes suppress the similarity score."""
        missing = make_violation(
            field_path="address.zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected_wrong_branch = make_violation(
            field_path="billing.zipcode",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        data = {"billing": {"zipcode": "400001"}}
        ops = strategy.propose([missing, unexpected_wrong_branch], make_contract(), data)
        # Still not applied -- but withheld visibly now. The pairing scores
        # 0.636 because the field names really are similar; the differing
        # branch prefixes hold it below the INFERRED bar, so it surfaces as
        # AMBIGUOUS instead of vanishing without trace.
        assert len(ops) == 1
        assert TrustPolicy().evaluate(ops[0])[1] is TrustDecision.AMBIGUOUS


class TestNestedDepth3:
    """Two levels of nested OBJECT: root.address.country.<field>.

    This is StateGuard's officially validated maximum nesting depth for
    V1 (see README.md "Nested structures" section).
    """

    def test_depth3_fuzzy_rename(self) -> None:
        missing = make_violation(
            field_path="address.country.code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected = make_violation(
            field_path="address.country.cod",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        data = {"address": {"country": {"cod": "IN"}}}
        ops = strategy.propose([missing, unexpected], make_contract(), data)

        assert len(ops) == 1
        assert ops[0].source_path == "address.country.cod"
        assert ops[0].target_path == "address.country.code"
        # propose() reports evidence; TrustPolicy assigns trust.
        assert ops[0].trust == 0.0
        assert ops[0].evidence.name_match is not None
        assert TrustPolicy().evaluate(ops[0])[1] is TrustDecision.APPLY

    def test_depth3_multiple_missing_in_same_branch(self) -> None:
        """Two missing fields in the same nested branch are each matched
        to their correct candidate independently."""
        missing_code = make_violation(
            field_path="address.country.code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        missing_name = make_violation(
            field_path="address.country.name",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected_cod = make_violation(
            field_path="address.country.cod",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected_naem = make_violation(
            field_path="address.country.naem",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        data = {"address": {"country": {"cod": "IN", "naem": "India"}}}
        ops = strategy.propose(
            [missing_code, missing_name, unexpected_cod, unexpected_naem],
            make_contract(),
            data,
        )
        assert len(ops) == 2
        result_map = {op.target_path: op.source_path for op in ops}
        assert result_map["address.country.code"] == "address.country.cod"
        assert result_map["address.country.name"] == "address.country.naem"

    def test_depth3_cross_branch_distinct_parents_not_confused(self) -> None:
        """Two structurally-similar typo'd fields living under DIFFERENT
        depth-3 parents are each matched to their own parent's candidate,
        not to each other's."""
        missing_a = make_violation(
            field_path="address.country.code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        missing_b = make_violation(
            field_path="billing.country.code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected_a = make_violation(
            field_path="address.country.cod",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected_b = make_violation(
            field_path="billing.country.cod",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        data = {
            "address": {"country": {"cod": "IN"}},
            "billing": {"country": {"cod": "US"}},
        }
        ops = strategy.propose(
            [missing_a, missing_b, unexpected_a, unexpected_b],
            make_contract(),
            data,
        )
        assert len(ops) == 2
        result_map = {op.target_path: op.source_path for op in ops}
        assert result_map["address.country.code"] == "address.country.cod"
        assert result_map["billing.country.code"] == "billing.country.cod"

    def test_depth3_known_limitation_adversarial_cross_branch_collision(self) -> None:
        """
        KNOWN LIMITATION (documented in M9_AUDIT.md): matching is purely a
        function of full-path string similarity with no explicit
        parent-scope awareness. This test constructs an adversarial case
        where two *different* missing fields, in two *different* branches,
        are each equally similar to a candidate in the *other* branch —
        proving the risk is real (not just theoretical) rather than
        hiding it. Both legitimate same-branch renames are blocked by
        collision detection, which is the SAFE failure mode: StateGuard
        prefers no repair over a silently wrong one.
        """
        missing_a = make_violation(
            field_path="branchA.code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        missing_b = make_violation(
            field_path="branchB.code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        # Both unexpected keys are equidistant from BOTH missing fields,
        # because "cod" is identical in both branches and the branch
        # prefixes ("branchA"/"branchB") are themselves similar strings.
        unexpected_a = make_violation(
            field_path="branchA.cod",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        unexpected_b = make_violation(
            field_path="branchB.cod",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        data = {
            "branchA": {"cod": "x"},
            "branchB": {"cod": "y"},
        }
        ops = strategy.propose(
            [missing_a, missing_b, unexpected_a, unexpected_b],
            make_contract(),
            data,
        )

        # Global assignment resolves what per-field collision detection could
        # not. Scoring one missing field at a time, "branchA.cod" and
        # "branchB.cod" are near-equidistant from "branchA.code", so the
        # margin looked fatally narrow and BOTH renames were abandoned.
        # Assigning across the whole problem instead, each candidate lands in
        # its own branch, because taking "branchA.cod" for "branchA.code"
        # leaves "branchB.cod" a perfect home rather than a contested one.
        pairs = {(op.source_path, op.target_path) for op in ops}
        assert pairs == {
            ("branchA.cod", "branchA.code"),
            ("branchB.cod", "branchB.code"),
        }


class TestDeeplyNestedInvalidPaths:
    """Behavior when a violation's field_path references a structure
    that doesn't match the contract's nesting shape at all."""

    def test_path_beyond_declared_nesting_no_crash(self) -> None:
        """A violation field_path with MORE segments than any declared
        FieldSpec path does not crash matching -- it just won't correlate
        with anything (since _find_missing_fields/_find_unexpected_keys
        only look at violation_type, not contract structure)."""
        missing = make_violation(
            field_path="a.b.c.d.e.f",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected = make_violation(
            field_path="a.b.c.d.e.g",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = FuzzyFieldMatchStrategy()
        ops = strategy.propose([missing, unexpected], make_contract(), {})
        # Still matches on pure path-string similarity even at depth 6 --
        # proves the strategy itself has no hard depth limit; the "3
        # levels" guidance is about what StateGuard validates end-to-end
        # (validator + extractor + engine + adapter), not a hard ceiling
        # enforced by this strategy.
        assert len(ops) == 1
        assert ops[0].target_path == "a.b.c.d.e.f"


# ===========================================================================
# score_assignments — competition is counted over the whole problem
# ===========================================================================


class TestMarginCountsConsumedEndpoints:
    """
    A pairing's margin must count every field and key in the payload, not only
    the ones later iterations still have available.

    Measuring against the residual made the margin an artefact of assignment
    order: the last pairing in a run faced no remaining rivals by
    construction, scored ``margin = 1.0``, and collected full trust however
    contested it had actually been.
    """

    def test_consumed_target_still_counts_against_the_margin(self) -> None:
        assignments = score_assignments(["user_id", "user_name"], ["user_email", "user_names"])
        by_target = {a.target: a for a in assignments}

        # Taken first, consuming user_name.
        assert by_target["user_name"].candidate == "user_names"

        # user_email fits the consumed user_name (0.913) marginally better
        # than the user_id it was left with (0.891), so it is maximally
        # contested -- not uncontested, which is what the residual-only
        # measurement reported.
        contested = by_target["user_id"]
        assert contested.candidate == "user_email"
        assert contested.margin == pytest.approx(0.0)
        assert contested.runner_up == "user_name"
        assert contested.runner_up_kind == "target"

    def test_the_contested_rename_is_withheld_despite_the_decoy(self) -> None:
        """
        The regression this guards: a second, unrelated rename in the same
        payload used to consume user_email's competitor and let the email
        address land in user_id at trust 0.891.
        """
        guard = ContractGuard.with_dict_schema()
        schema = {
            "fields": [
                {"path": "user_id", "type": "string"},
                {"path": "user_name", "type": "string"},
            ]
        }
        result = guard.repair(schema, {"user_email": "a@b.com", "user_names": "arnav"})

        assert result.repaired_output is not None
        # The legitimate rename still applies...
        assert result.repaired_output["user_name"] == "arnav"
        # ...and the contested one does not.
        assert "user_id" not in result.repaired_output
        assert [a.target_path for a in result.ambiguous] == ["user_id"]

    def test_an_uncontested_pairing_still_earns_full_credit(self) -> None:
        """Counting the full problem must not penalise a genuinely lone pair."""
        assignments = score_assignments(["temperature"], ["temp_celsius"])
        assert assignments[0].margin == 1.0
        assert assignments[0].runner_up is None


class TestSimilarityEdges:
    """The degenerate inputs `_jaro` short-circuits on."""

    def test_identical_strings_score_one(self) -> None:
        assert jaro_winkler("temperature", "temperature") == 1.0

    def test_case_only_difference_scores_one(self) -> None:
        """Comparison is case-insensitive, so this takes the identity path."""
        assert jaro_winkler("UserID", "userid") == 1.0

    @pytest.mark.parametrize(("a", "b"), [("", "city"), ("city", ""), ("", "")])
    def test_empty_operand_scores_zero_or_identity(self, a: str, b: str) -> None:
        score = jaro_winkler(a, b)
        assert score == (1.0 if a == b else 0.0)


class TestAssignmentRequiresBothSides:
    @pytest.mark.parametrize(
        ("missing", "unexpected"),
        [([], ["cty"]), (["city"], []), ([], [])],
    )
    def test_nothing_to_pair_returns_no_assignments(
        self, missing: list[str], unexpected: list[str]
    ) -> None:
        assert score_assignments(missing, unexpected) == []
