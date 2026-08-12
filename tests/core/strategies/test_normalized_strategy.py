"""
NormalizedNameStrategy — renames that differ only in naming convention.

The point of separating this from fuzzy matching is that these are not
guesses. ``UserID`` and ``user_id`` are the same identifier written two ways;
recognising that is a decision about orthography, not meaning. Fuzzy scoring
put such a pair at roughly 0.86 -- indistinguishable in the trust model from a
genuine near-miss like ``user_email`` at 0.891, and priced identically.
"""

from __future__ import annotations

import pytest

from stateguard import ContractGuard
from stateguard.core.errors.operations import FieldOpType, RepairRisk
from stateguard.core.errors.results import RepairStatus
from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.core.strategies.fuzzy import jaro_winkler
from stateguard.core.strategies.normalized import (
    NormalizedNameStrategy,
    normalize_field_name,
)
from stateguard.core.trust import TrustDecision, TrustPolicy
from tests.conftest import make_violation


def _contract(*paths: str) -> ContractSpec:
    return ContractSpec(fields=[FieldSpec(p, FieldType.STRING) for p in paths])


def _missing(path: str) -> object:
    return make_violation(field_path=path, violation_type=ViolationType.MISSING_REQUIRED_FIELD)


def _unexpected(path: str) -> object:
    return make_violation(
        field_path=path,
        violation_type=ViolationType.UNEXPECTED_FIELD,
        severity=ViolationSeverity.WARNING,
    )


# ===========================================================================
# normalize_field_name
# ===========================================================================


class TestNormalizeFieldName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("user_id", "userid"),
            ("UserID", "userid"),
            ("USER-ID", "userid"),
            ("user id", "userid"),
            ("  user_id  ", "userid"),
            ("firstName", "firstname"),
            ("temperature", "temperature"),
            ("", ""),
        ],
    )
    def test_forms(self, name: str, expected: str) -> None:
        assert normalize_field_name(name) == expected

    def test_casefold_not_lower(self) -> None:
        """
        ``casefold`` is the Unicode-correct operation for caseless comparison;
        ``lower`` would leave these two disagreeing.
        """
        assert normalize_field_name("STRASSE") == normalize_field_name("straße")

    def test_only_the_final_segment_is_normalised(self) -> None:
        """
        The segments above the field locate it. Normalising them could let a
        rename jump between branches -- ``billing.zip_code`` must not be able
        to match ``shipping.zipCode``.
        """
        assert normalize_field_name("billing.zip_code") == "billing.zipcode"
        assert normalize_field_name("billing.zip_code") != normalize_field_name("shipping.zip_code")


# ===========================================================================
# Proposals
# ===========================================================================


class TestPropose:
    @pytest.mark.parametrize(
        ("declared", "received"),
        [
            ("user_id", "UserID"),
            ("first_name", "firstName"),
            ("zip_code", "ZIP-CODE"),
            ("user_id", "user id"),
            ("user_id", "USER_ID"),
        ],
    )
    def test_convention_variants_are_matched(self, declared: str, received: str) -> None:
        ops = NormalizedNameStrategy().propose(
            [_missing(declared), _unexpected(received)], _contract(declared), {received: "v"}
        )

        assert len(ops) == 1
        assert ops[0].op_type is FieldOpType.RENAME
        assert ops[0].source_path == received
        assert ops[0].target_path == declared

    def test_evidence_is_an_exact_match(self) -> None:
        ops = NormalizedNameStrategy().propose(
            [_missing("user_id"), _unexpected("UserID")], _contract("user_id"), {"UserID": "v"}
        )
        evidence = ops[0].evidence

        # Equal after normalisation -- exact, not approximate.
        assert evidence.name_match == 1.0
        assert evidence.margin == 1.0

    def test_risk_is_inferred_not_declared(self) -> None:
        """
        Nothing in the contract said these two names are the same field; the
        engine worked it out. DECLARED is reserved for what the schema states.
        """
        ops = NormalizedNameStrategy().propose(
            [_missing("user_id"), _unexpected("UserID")], _contract("user_id"), {"UserID": "v"}
        )
        assert ops[0].risk is RepairRisk.INFERRED

    def test_the_policy_applies_it(self) -> None:
        ops = NormalizedNameStrategy().propose(
            [_missing("user_id"), _unexpected("UserID")], _contract("user_id"), {"UserID": "v"}
        )
        scored, decision = TrustPolicy().evaluate(ops[0])

        assert decision is TrustDecision.APPLY
        assert scored.trust == pytest.approx(1.0)

    def test_unrelated_names_are_left_to_fuzzy(self) -> None:
        ops = NormalizedNameStrategy().propose(
            [_missing("temperature"), _unexpected("humidity")],
            _contract("temperature"),
            {"humidity": 1},
        )
        assert ops == []

    def test_no_missing_fields_proposes_nothing(self) -> None:
        ops = NormalizedNameStrategy().propose(
            [_unexpected("UserID")], _contract("user_id"), {"UserID": "v"}
        )
        assert ops == []

    def test_can_handle_mirrors_propose(self) -> None:
        strategy = NormalizedNameStrategy()
        matching = [_missing("user_id"), _unexpected("UserID")]
        unrelated = [_missing("temperature"), _unexpected("humidity")]

        assert strategy.can_handle(matching, _contract("user_id"), {}) is True
        assert strategy.can_handle(unrelated, _contract("temperature"), {}) is False


# ===========================================================================
# Collisions
# ===========================================================================


class TestCollisions:
    """
    Normalisation is many-to-one, so it can collide. A collision means the
    names genuinely cannot say which goes where, so nothing is proposed and
    fuzzy matching -- which has the margin machinery for exactly this -- gets
    its turn instead.
    """

    def test_two_contract_fields_normalising_alike_are_refused(self) -> None:
        ops = NormalizedNameStrategy().propose(
            [_missing("user_id"), _missing("userId"), _unexpected("USER_ID")],
            _contract("user_id", "userId"),
            {"USER_ID": "v"},
        )
        assert ops == []

    def test_two_payload_keys_normalising_alike_are_refused(self) -> None:
        ops = NormalizedNameStrategy().propose(
            [_missing("user_id"), _unexpected("UserID"), _unexpected("USER-ID")],
            _contract("user_id"),
            {"UserID": "a", "USER-ID": "b"},
        )
        assert ops == []

    def test_a_collision_does_not_block_an_unrelated_pair(self) -> None:
        ops = NormalizedNameStrategy().propose(
            [
                _missing("user_id"),
                _missing("userId"),
                _unexpected("USER_ID"),
                _missing("zip_code"),
                _unexpected("zipCode"),
            ],
            _contract("user_id", "userId", "zip_code"),
            {},
        )
        assert [(o.source_path, o.target_path) for o in ops] == [("zipCode", "zip_code")]


# ===========================================================================
# End to end
# ===========================================================================


class TestEndToEnd:
    @pytest.fixture
    def guard(self) -> ContractGuard:
        return ContractGuard.with_dict_schema()

    def test_repairs_through_the_guard(self, guard: ContractGuard) -> None:
        schema = {"fields": [{"path": "user_id", "type": "string"}]}
        result = guard.repair(schema, {"UserID": "u-1"})

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {"user_id": "u-1"}

    def test_runs_before_fuzzy(self, guard: ContractGuard) -> None:
        schema = {"fields": [{"path": "user_id", "type": "string"}]}
        result = guard.repair(schema, {"UserID": "u-1"})

        assert result.attempts[0].strategy_name == "NormalizedNameStrategy"

    def test_scores_higher_than_fuzzy_would_have(self, guard: ContractGuard) -> None:
        """
        The reason this strategy exists: fuzzy priced an orthographic variant
        as a guess, at a score close to genuine near-misses.
        """
        schema = {"fields": [{"path": "user_id", "type": "string"}]}
        applied = guard.repair(schema, {"UserID": "u-1"}).attempts[0].applied_operations[0]

        assert applied.trust == pytest.approx(1.0)
        assert applied.trust > jaro_winkler("user_id", "UserID")

    def test_a_declared_alias_still_wins(self, guard: ContractGuard) -> None:
        """ExactAliasStrategy is priority 10 and stays ahead."""
        schema = {
            "fields": [
                {"path": "user_id", "type": "string", "known_aliases": ["UserID"]},
            ]
        }
        result = guard.repair(schema, {"UserID": "u-1"})

        assert result.attempts[0].strategy_name == "ExactAliasStrategy"
        assert result.status is RepairStatus.SUCCESS

    def test_combines_with_coercion_across_attempts(self, guard: ContractGuard) -> None:
        schema = {"fields": [{"path": "user_count", "type": "integer"}]}
        result = guard.repair(schema, {"userCount": "42"})

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {"user_count": 42}

    def test_the_contested_case_is_still_withheld(self, guard: ContractGuard) -> None:
        """
        Adding a strategy must not open a new route to the repair the trust
        model exists to refuse. 'user_email' normalises to 'useremail', which
        matches neither field, so this strategy declines and fuzzy still
        abstains.
        """
        schema = {
            "fields": [
                {"path": "user_id", "type": "string"},
                {"path": "user_name", "type": "string"},
            ]
        }
        result = guard.repair(schema, {"user_email": "a@b.com"})

        assert result.status is RepairStatus.AMBIGUOUS
        assert result.repaired_output is None


class TestIdenticalNamesAreNotRenamed:
    def test_a_name_that_matches_itself_proposes_nothing(self) -> None:
        """
        A path reported as both missing and unexpected normalises to itself.
        Renaming it to itself is a no-op the engine would have to discard, so
        the pair is dropped before it becomes a proposal.
        """
        missing = make_violation(
            field_path="user_id",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        unexpected = make_violation(
            field_path="user_id",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = NormalizedNameStrategy()
        assert strategy._pairs([missing, unexpected]) == {}
        assert strategy.propose([missing, unexpected], _contract("user_id"), {"user_id": 1}) == []
