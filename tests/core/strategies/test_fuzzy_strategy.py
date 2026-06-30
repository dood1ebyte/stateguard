"""Tests for stateguard.core.strategies.fuzzy."""

from __future__ import annotations

import pytest

from stateguard.core.errors.operations import FieldOpType
from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.core.strategies.fuzzy import (
    FuzzyFieldMatchStrategy,
    _combined_score,
    _levenshtein_distance,
    _normalized_score,
    _token_prefix_boost,
)
from tests.conftest import make_violation


# ===========================================================================
# _levenshtein_distance
# ===========================================================================


class TestLevenshteinDistance:

    def test_identical_strings(self) -> None:
        assert _levenshtein_distance("hello", "hello") == 0

    def test_empty_vs_empty(self) -> None:
        assert _levenshtein_distance("", "") == 0

    def test_empty_vs_nonempty(self) -> None:
        assert _levenshtein_distance("", "abc") == 3

    def test_nonempty_vs_empty(self) -> None:
        assert _levenshtein_distance("abc", "") == 3

    def test_single_substitution(self) -> None:
        assert _levenshtein_distance("cat", "bat") == 1

    def test_single_insertion(self) -> None:
        assert _levenshtein_distance("cat", "cats") == 1

    def test_single_deletion(self) -> None:
        assert _levenshtein_distance("cats", "cat") == 1

    def test_completely_different_equal_length(self) -> None:
        assert _levenshtein_distance("abc", "xyz") == 3

    def test_is_symmetric(self) -> None:
        assert _levenshtein_distance("kitten", "sitting") == _levenshtein_distance(
            "sitting", "kitten"
        )

    def test_classic_kitten_sitting(self) -> None:
        assert _levenshtein_distance("kitten", "sitting") == 3

    def test_case_sensitive(self) -> None:
        """_levenshtein_distance itself is case-sensitive; normalization
        happens in _normalized_score."""
        assert _levenshtein_distance("ABC", "abc") == 3

    def test_single_character_strings(self) -> None:
        assert _levenshtein_distance("a", "b") == 1
        assert _levenshtein_distance("a", "a") == 0

    def test_unicode_strings(self) -> None:
        assert _levenshtein_distance("café", "cafe") == 1

    def test_longer_first_argument(self) -> None:
        assert _levenshtein_distance("temp_celsius", "temperature") == 7

    def test_shorter_first_argument(self) -> None:
        # Same pair, arguments swapped — must be symmetric.
        assert _levenshtein_distance("temperature", "temp_celsius") == 7


# ===========================================================================
# _normalized_score
# ===========================================================================


class TestNormalizedScore:

    def test_identical_strings_score_one(self) -> None:
        assert _normalized_score("hello", "hello") == 1.0

    def test_case_insensitive_identical_scores_one(self) -> None:
        assert _normalized_score("ABC", "abc") == 1.0

    def test_both_empty_scores_one(self) -> None:
        assert _normalized_score("", "") == 1.0

    def test_empty_vs_nonempty_scores_zero(self) -> None:
        assert _normalized_score("", "x") == 0.0

    def test_completely_different_equal_length_scores_zero(self) -> None:
        assert _normalized_score("a", "b") == 0.0

    def test_is_symmetric(self) -> None:
        assert _normalized_score("user_id", "userId") == _normalized_score(
            "userId", "user_id"
        )

    def test_score_is_in_unit_interval(self) -> None:
        for a, b in [
            ("temperature", "temp_celsius"),
            ("a", "b"),
            ("", ""),
            ("x", ""),
            ("hello", "hello"),
        ]:
            score = _normalized_score(a, b)
            assert 0.0 <= score <= 1.0

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ("city", "cty", 0.75),
            ("zip_code", "zipcode", 0.875),
            ("zip_code", "postal_code", 0.45454545454545454),
            ("user_id", "userId", 0.8571428571428572),
            ("user_id", "usr_id", 0.8571428571428572),
            ("temperature", "humidity", 0.18181818181818177),
            ("temperature", "temp_celsius", 0.41666666666666663),
            ("café", "cafe", 0.75),
        ],
    )
    def test_known_value_table(self, a: str, b: str, expected: float) -> None:
        assert _normalized_score(a, b) == pytest.approx(expected)


# ===========================================================================
# _token_prefix_boost
# ===========================================================================


class TestTokenPrefixBoost:

    def test_temp_celsius_vs_temperature(self) -> None:
        """The motivating example: 'temp' is a token-prefix of 'temperature'."""
        score = _token_prefix_boost("temperature", "temp_celsius")
        assert score == pytest.approx(0.8)

    def test_temp_kelvin_vs_temperature(self) -> None:
        score = _token_prefix_boost("temperature", "temp_kelvin")
        assert score == pytest.approx(0.8090909090909091)

    def test_symmetric(self) -> None:
        assert _token_prefix_boost("temperature", "temp_celsius") == pytest.approx(
            _token_prefix_boost("temp_celsius", "temperature")
        )

    def test_no_relationship_returns_zero(self) -> None:
        assert _token_prefix_boost("temperature", "humidity") == 0.0

    def test_short_token_below_minimum_length_ignored(self) -> None:
        """'id' (2 chars) is below _MIN_PREFIX_TOKEN_LENGTH (3); no boost."""
        assert _token_prefix_boost("user_id", "usr_id") == 0.0

    def test_unrelated_strings_without_underscore_no_boost(self) -> None:
        """Two unrelated whole-string tokens (no underscore, no prefix
        relationship) receive no boost."""
        assert _token_prefix_boost("temperature", "humidity") == 0.0

    def test_case_insensitive(self) -> None:
        score = _token_prefix_boost("TEMPERATURE", "TEMP_CELSIUS")
        assert score == pytest.approx(0.8)

    def test_empty_strings_return_zero(self) -> None:
        assert _token_prefix_boost("", "") == 0.0

    def test_token_longer_than_other_string_no_match(self) -> None:
        assert _token_prefix_boost("ab_cdefgh", "ab") == 0.0

    def test_exact_match_via_whole_string_token(self) -> None:
        """A whole (no-underscore) name that is itself a prefix of the
        other name still qualifies as a token."""
        score = _token_prefix_boost("temp", "temperature")
        assert score == pytest.approx(0.8090909090909091)


# ===========================================================================
# _combined_score
# ===========================================================================


class TestCombinedScore:

    def test_temp_celsius_clears_default_threshold(self) -> None:
        """The canonical scenario: combined score clears the engine's
        default min_confidence_threshold (0.7)."""
        assert _combined_score("temperature", "temp_celsius") >= 0.7

    def test_combined_is_max_of_both_signals(self) -> None:
        a, b = "temperature", "temp_celsius"
        expected = max(_normalized_score(a, b), _token_prefix_boost(a, b))
        assert _combined_score(a, b) == expected

    def test_boost_never_lowers_a_strong_levenshtein_match(self) -> None:
        """zip_code/zipcode: Levenshtein (0.875) already exceeds the
        token-prefix boost (0.8125); combined must equal the higher value."""
        assert _combined_score("zip_code", "zipcode") == pytest.approx(0.875)

    def test_boost_raises_a_weak_levenshtein_match(self) -> None:
        assert _combined_score("temperature", "temp_celsius") == pytest.approx(0.8)

    def test_unrelated_strings_stay_low(self) -> None:
        assert _combined_score("xyz", "temperature") < 0.5

    def test_identical_strings_score_one(self) -> None:
        assert _combined_score("city", "city") == 1.0


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
        assert strategy._min_confidence_threshold == 0.7
        assert strategy._score_collision_margin == 0.15

    def test_custom_thresholds(self) -> None:
        strategy = FuzzyFieldMatchStrategy(
            min_confidence_threshold=0.5, score_collision_margin=0.05
        )
        assert strategy._min_confidence_threshold == 0.5
        assert strategy._score_collision_margin == 0.05


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
        assert op.confidence == pytest.approx(0.75)

    def test_rationale_contains_score_and_field_names(self) -> None:
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
        assert "0.75" in rationale

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
        ops = strategy.propose(
            [missing, unexpected], make_contract(), {"zipcode": "400001"}
        )
        assert len(ops) == 1
        assert ops[0].confidence == pytest.approx(0.875)
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
        ops = strategy.propose(
            [missing, unexpected], make_contract(), {"humidity": 80}
        )
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

    def test_custom_lower_threshold_allows_match(self) -> None:
        """With threshold 0.1, even a low-similarity pair is proposed."""
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
        ops = strategy.propose(
            [missing, unexpected], make_contract(), {"humidity": 80}
        )
        assert len(ops) == 1
        assert ops[0].confidence == pytest.approx(0.18181818181818177)


# ===========================================================================
# propose — collision detection
# ===========================================================================


class TestProposeCollision:

    def test_collision_proposes_nothing(self) -> None:
        """
        'userId' and 'usr_id' both score 0.8571 against 'user_id' —
        identical scores mean a collision (difference 0 < margin 0.15).
        No rename should be proposed.
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
        assert ops == []

    def test_collision_avoided_with_zero_margin(self) -> None:
        """With margin=0.0, equal scores are NOT a collision (0 < 0 is False),
        so the first-scored candidate (by registration order) is proposed."""
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
        strategy = FuzzyFieldMatchStrategy(score_collision_margin=0.0)
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

        # margin=0.25 > diff(0.2) -> collision -> no proposal
        strategy_collide = FuzzyFieldMatchStrategy(
            min_confidence_threshold=0.1, score_collision_margin=0.25
        )
        ops_collide = strategy_collide.propose(
            [missing, unexpected1, unexpected2], make_contract(), data
        )
        assert ops_collide == []

        # margin=0.1 < diff(0.2) -> no collision -> proposal made for best (abcdf, 0.8)
        strategy_clear = FuzzyFieldMatchStrategy(
            min_confidence_threshold=0.1, score_collision_margin=0.1
        )
        ops_clear = strategy_clear.propose(
            [missing, unexpected1, unexpected2], make_contract(), data
        )
        assert len(ops_clear) == 1
        assert ops_clear[0].source_path == "abcdf"
        assert ops_clear[0].confidence == pytest.approx(0.8)


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
        ops = strategy.propose(
            [missing, unexpected_self], make_contract(), {"duplicate_name": 1}
        )
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
        ops = strategy.propose(
            [missing, unexpected], make_contract(), {"cafe": "value"}
        )
        assert len(ops) == 1
        assert ops[0].confidence == pytest.approx(0.75)


# ===========================================================================
# Internal helper methods — direct tests
# ===========================================================================


class TestInternalHelpers:

    def test_find_missing_fields(self) -> None:
        v1 = make_violation(
            field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        v2 = make_violation(
            field_path="b",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        v3 = make_violation(
            field_path="c", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        result = FuzzyFieldMatchStrategy._find_missing_fields([v1, v2, v3])
        assert result == ["a", "c"]

    def test_find_unexpected_keys(self) -> None:
        v1 = make_violation(
            field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        v2 = make_violation(
            field_path="b",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        result = FuzzyFieldMatchStrategy._find_unexpected_keys([v1, v2])
        assert result == ["b"]

    def test_score_candidates_sorted_descending(self) -> None:
        result = FuzzyFieldMatchStrategy._score_candidates(
            "zip_code", ["postal_code", "zipcode"]
        )
        assert [c for c, _ in result] == ["zipcode", "postal_code"]
        assert result[0][1] > result[1][1]

    def test_score_candidates_empty(self) -> None:
        result = FuzzyFieldMatchStrategy._score_candidates("zip_code", [])
        assert result == []

    def test_check_collision_true_when_close(self) -> None:
        scores = [("a", 0.8), ("b", 0.75)]
        assert FuzzyFieldMatchStrategy._check_collision(scores, margin=0.1) is True

    def test_check_collision_false_when_far_apart(self) -> None:
        scores = [("a", 0.9), ("b", 0.5)]
        assert FuzzyFieldMatchStrategy._check_collision(scores, margin=0.15) is False

    def test_check_collision_false_with_single_candidate(self) -> None:
        scores = [("a", 0.9)]
        assert FuzzyFieldMatchStrategy._check_collision(scores, margin=0.15) is False

    def test_check_collision_false_with_no_candidates(self) -> None:
        assert FuzzyFieldMatchStrategy._check_collision([], margin=0.15) is False

    def test_check_collision_exact_boundary(self) -> None:
        """Difference exactly equal to margin is NOT a collision (< not <=)."""
        scores = [("a", 0.80), ("b", 0.65)]
        assert FuzzyFieldMatchStrategy._check_collision(scores, margin=0.15) is False


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
        assert ops[0].confidence >= 0.7

    def test_depth2_shared_prefix_inflates_similarity(self) -> None:
        """The shared 'address.' prefix raises the score well above what
        the bare field names ('city' vs 'cty', already 0.75) would give
        alone -- nesting context makes the match even more confident."""
        bare_score = FuzzyFieldMatchStrategy._score_candidates(
            "city", ["cty"]
        )[0][1]
        nested_score = FuzzyFieldMatchStrategy._score_candidates(
            "address.city", ["address.cty"]
        )[0][1]
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
        ops = strategy.propose(
            [missing, unexpected_wrong_branch], make_contract(), data
        )
        # "address.zip_code" vs "billing.zipcode" scores far below threshold
        # because their prefixes ("address." vs "billing.") differ entirely.
        assert ops == []


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
        assert ops[0].confidence == pytest.approx(0.985)

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
        scores_for_a = strategy._score_candidates(
            "branchA.code", ["branchA.cod", "branchB.cod"]
        )
        # The same-branch candidate scores higher, but not by a wide
        # margin -- "branchA" and "branchB" differ by only one character.
        assert scores_for_a[0][0] == "branchA.cod"
        margin = scores_for_a[0][1] - scores_for_a[1][1]
        assert margin < 0.15  # within the default collision margin

        ops = strategy.propose(
            [missing_a, missing_b, unexpected_a, unexpected_b],
            make_contract(),
            data,
        )
        # Collision detected for both missing fields -> no rename proposed
        # for either. Safe-by-default: no silently wrong cross-branch fix.
        assert ops == []


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
