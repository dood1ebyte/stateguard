"""
Tests for stateguard.core.strategies.enum_normalize.

Covers Phase 5: a payload value that names a declared enum member under a
different spelling is repaired to the member, priced under the trust model
rather than by a fresh constant.

The three declaration sources -- ``Literal``, ``enum.Enum``, and a JSON
Schema ``enum`` -- converge on one ``ENUM_VALUES`` constraint, so they share
one code path and are tested through it.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.errors.operations import FieldOpType, RepairRisk
from stateguard.core.errors.results import RepairStatus
from stateguard.core.errors.violations import ViolationType
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import (
    FieldConstraint,
    FieldConstraintType,
    FieldType,
)
from stateguard.core.strategies.enum_normalize import (
    EnumNormalizationStrategy,
    normalize_enum_value,
)
from stateguard.core.trust import TrustDecision, TrustPolicy
from stateguard.guard import ContractGuard
from tests.conftest import make_violation

STATUSES = ("open", "in_progress", "done")


def _enum_contract(*members: Any, path: str = "status") -> ContractSpec:
    return ContractSpec(
        fields=[
            FieldSpec(
                path,
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.ENUM_VALUES, tuple(members))],
            )
        ]
    )


def _violation(path: str = "status") -> Any:
    return make_violation(
        field_path=path,
        violation_type=ViolationType.VALUE_CONSTRAINT_VIOLATION,
    )


# ===========================================================================
# normalize_enum_value
# ===========================================================================


class TestNormalizeEnumValue:
    @pytest.mark.parametrize(
        "value",
        ["IN PROGRESS", "in-progress", "In Progress", "in_progress", "  in progress  "],
    )
    def test_all_realistic_spellings_converge(self, value: str) -> None:
        assert normalize_enum_value(value) == "in_progress"

    def test_casefold_is_unicode_aware(self) -> None:
        """casefold, not lower -- so the German sharp s agrees with 'ss'."""
        assert normalize_enum_value("STRASSE") == normalize_enum_value("straße")

    def test_separators_are_mapped_not_removed(self) -> None:
        """
        The deliberate difference from normalize_field_name: enum values are
        data, so 'onhold' must not be able to claim 'on_hold'.
        """
        assert normalize_enum_value("onhold") != normalize_enum_value("on_hold")

    def test_empty_string(self) -> None:
        assert normalize_enum_value("") == ""


# ===========================================================================
# Identity / applicability
# ===========================================================================


class TestIdentity:
    def test_name(self) -> None:
        assert EnumNormalizationStrategy().name == "EnumNormalizationStrategy"

    def test_priority_sits_between_coercion_and_default_fill(self) -> None:
        assert EnumNormalizationStrategy().priority == 35

    def test_registered_in_the_default_strategy_set(self) -> None:
        guard = ContractGuard.with_dict_schema()
        names = [s.name for s in guard._registry.strategies]
        assert names == [
            "ExactAliasStrategy",
            "NormalizedNameStrategy",
            "FuzzyFieldMatchStrategy",
            "TypeCoercionStrategy",
            "EnumNormalizationStrategy",
            "DefaultValueFillStrategy",
        ]


class TestCanHandle:
    def test_true_for_a_constraint_violation_on_an_enum_field(self) -> None:
        strategy = EnumNormalizationStrategy()
        assert (
            strategy.can_handle([_violation()], _enum_contract(*STATUSES), {"status": "DONE"})
            is True
        )

    def test_false_when_the_field_declares_no_enum(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("status", FieldType.STRING)])
        strategy = EnumNormalizationStrategy()
        assert strategy.can_handle([_violation()], contract, {"status": "DONE"}) is False

    def test_false_for_other_violation_types(self) -> None:
        other = make_violation(field_path="status", violation_type=ViolationType.TYPE_MISMATCH)
        strategy = EnumNormalizationStrategy()
        assert strategy.can_handle([other], _enum_contract(*STATUSES), {"status": 5}) is False

    def test_false_for_an_undeclared_path(self) -> None:
        strategy = EnumNormalizationStrategy()
        assert (
            strategy.can_handle([_violation("nope")], _enum_contract(*STATUSES), {"nope": "x"})
            is False
        )


# ===========================================================================
# propose
# ===========================================================================


class TestPropose:
    @pytest.mark.parametrize(
        ("received", "expected"),
        [
            ("DONE", "done"),
            ("Done", "done"),
            ("IN PROGRESS", "in_progress"),
            ("in-progress", "in_progress"),
            ("In-Progress", "in_progress"),
            ("  done  ", "done"),
            ("OPEN", "open"),
        ],
    )
    def test_recognised_spellings_propose_the_declared_member(
        self, received: str, expected: str
    ) -> None:
        ops = EnumNormalizationStrategy().propose(
            [_violation()], _enum_contract(*STATUSES), {"status": received}
        )
        assert len(ops) == 1
        assert ops[0].op_type is FieldOpType.SET_VALUE
        assert ops[0].target_path == "status"
        assert ops[0].value == expected
        assert ops[0].risk is RepairRisk.INFERRED
        # The strategy reports evidence; TrustPolicy assigns the score.
        assert ops[0].trust == 0.0

    @pytest.mark.parametrize("received", ["cancelled", "in progres", "", "unknown"])
    def test_a_value_that_is_not_a_member_proposes_nothing(self, received: str) -> None:
        """No Levenshtein, no prefix matching -- normalisation or nothing."""
        ops = EnumNormalizationStrategy().propose(
            [_violation()], _enum_contract(*STATUSES), {"status": received}
        )
        assert ops == []

    def test_a_value_already_valid_proposes_nothing(self) -> None:
        ops = EnumNormalizationStrategy().propose(
            [_violation()], _enum_contract(*STATUSES), {"status": "done"}
        )
        assert ops == []

    @pytest.mark.parametrize("received", [5, None, ["done"], {"a": 1}, True])
    def test_non_string_values_propose_nothing(self, received: Any) -> None:
        """Normalisation is a string operation; an int enum is not its problem."""
        ops = EnumNormalizationStrategy().propose(
            [_violation()], _enum_contract(*STATUSES), {"status": received}
        )
        assert ops == []

    def test_non_string_members_are_skipped(self) -> None:
        ops = EnumNormalizationStrategy().propose(
            [_violation()], _enum_contract(1, 2, 3), {"status": "ONE"}
        )
        assert ops == []

    def test_absent_path_proposes_nothing(self) -> None:
        ops = EnumNormalizationStrategy().propose([_violation()], _enum_contract(*STATUSES), {})
        assert ops == []

    def test_nested_enum_field(self) -> None:
        inner = _enum_contract(*STATUSES)
        contract = ContractSpec(fields=[FieldSpec("task", FieldType.OBJECT, nested_spec=inner)])
        ops = EnumNormalizationStrategy().propose(
            [_violation("task.status")], contract, {"task": {"status": "IN PROGRESS"}}
        )
        assert len(ops) == 1
        assert ops[0].target_path == "task.status"
        assert ops[0].value == "in_progress"


# ===========================================================================
# Evidence
# ===========================================================================


class TestFidelityIsMeasured:
    """
    ``value_preserved`` uses the same three rungs TypeCoercionStrategy grades
    its casts against, so the two strategies' scores stay comparable.
    """

    @pytest.mark.parametrize(
        ("received", "expected_fidelity"),
        [
            ("DONE", 1.0),  # case carries no information in a closed set
            ("Done", 1.0),
            ("  done  ", 0.95),  # surrounding whitespace
            ("IN PROGRESS", 0.85),  # separators rewritten
            ("in-progress", 0.85),
        ],
    )
    def test_fidelity_ladder(self, received: str, expected_fidelity: float) -> None:
        ops = EnumNormalizationStrategy().propose(
            [_violation()], _enum_contract(*STATUSES), {"status": received}
        )
        assert ops[0].evidence.value_preserved == pytest.approx(expected_fidelity)

    def test_an_uncontested_match_carries_full_margin(self) -> None:
        ops = EnumNormalizationStrategy().propose(
            [_violation()], _enum_contract(*STATUSES), {"status": "DONE"}
        )
        assert ops[0].evidence.margin == 1.0
        assert ops[0].evidence.alternatives_considered == 1

    def test_every_recognised_spelling_clears_the_inferred_bar(self) -> None:
        policy = TrustPolicy()
        for received in ("DONE", "  done  ", "IN PROGRESS", "in-progress"):
            ops = EnumNormalizationStrategy().propose(
                [_violation()], _enum_contract(*STATUSES), {"status": received}
            )
            assert policy.evaluate(ops[0])[1] is TrustDecision.APPLY, received

    def test_notes_and_rationale_never_quote_the_received_value(self) -> None:
        """
        The declared member is schema data and safe to log; the payload value
        is runtime data. RepairConfig.include_values_in_log defaults to False
        precisely so it does not appear.
        """
        secret = "TOP-SECRET-PAYLOAD"
        ops = EnumNormalizationStrategy().propose(
            [_violation()],
            _enum_contract("top_secret_payload"),
            {"status": secret},
        )
        assert len(ops) == 1
        assert secret not in ops[0].rationale
        assert all(secret not in note for note in ops[0].evidence.notes)


# ===========================================================================
# Collision guard
# ===========================================================================


class TestCollisionGuard:
    """
    Two declared members that normalise identically are a defect in the
    contract, not the payload, and no evidence about the value can resolve
    them. Every candidate is surfaced so the caller can choose.
    """

    COLLIDING = ("in_progress", "in progress")

    def test_all_colliding_members_are_proposed(self) -> None:
        ops = EnumNormalizationStrategy().propose(
            [_violation()], _enum_contract(*self.COLLIDING), {"status": "IN PROGRESS"}
        )
        assert {op.value for op in ops} == set(self.COLLIDING)
        assert all(op.evidence.margin == 0.0 for op in ops)
        assert all(op.evidence.alternatives_considered == 2 for op in ops)

    def test_tied_candidates_are_scored_identically(self) -> None:
        """
        The defect this guards: scoring each candidate on its own fidelity
        put 'in progress' (case-only, 1.0) and 'in_progress' (separator,
        0.85) 0.105 apart, straddling the abstain floor -- one was surfaced
        as ambiguous and the other quietly rejected, leaving a one-item list
        for a two-option decision.
        """
        ops = EnumNormalizationStrategy().propose(
            [_violation()], _enum_contract(*self.COLLIDING), {"status": "IN PROGRESS"}
        )
        fidelities = {op.evidence.value_preserved for op in ops}
        assert len(fidelities) == 1

        policy = TrustPolicy()
        decisions = {policy.evaluate(op)[1] for op in ops}
        assert decisions == {TrustDecision.AMBIGUOUS}

    def test_the_caller_sees_every_candidate(self) -> None:
        guard = ContractGuard.with_dict_schema()
        schema = {
            "fields": [
                {
                    "path": "status",
                    "type": "string",
                    "constraints": [{"type": "enum_values", "value": list(self.COLLIDING)}],
                }
            ]
        }
        result = guard.repair(schema, {"status": "IN PROGRESS"})

        assert result.status is RepairStatus.AMBIGUOUS
        assert result.repaired_output is None
        assert len(result.ambiguous) == 1
        assert {c.value for c in result.ambiguous[0].candidates} == set(self.COLLIDING)

    def test_a_collision_elsewhere_does_not_block_an_unrelated_field(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec(
                    "status",
                    FieldType.STRING,
                    constraints=[FieldConstraint(FieldConstraintType.ENUM_VALUES, self.COLLIDING)],
                ),
                FieldSpec(
                    "priority",
                    FieldType.STRING,
                    constraints=[FieldConstraint(FieldConstraintType.ENUM_VALUES, ("low", "high"))],
                ),
            ]
        )
        ops = EnumNormalizationStrategy().propose(
            [_violation("status"), _violation("priority")],
            contract,
            {"status": "IN PROGRESS", "priority": "HIGH"},
        )
        priority_ops = [op for op in ops if op.target_path == "priority"]
        assert len(priority_ops) == 1
        assert priority_ops[0].evidence.margin == 1.0


class TestMalformedEnumDeclaration:
    """
    A contract can declare ``enum_values`` as a bare string -- an easy
    authoring slip that ``DictContractAdapter`` accepts without complaint.
    ``tuple("open")`` splits that into ``('o', 'p', 'e', 'n')``, so a received
    ``"O"`` normalises cleanly onto ``'o'`` and the payload gets silently
    rewritten. Refusing is the only safe reading.
    """

    def test_a_string_enum_declaration_is_not_split_into_characters(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec(
                    "status",
                    FieldType.STRING,
                    constraints=[FieldConstraint(FieldConstraintType.ENUM_VALUES, "open")],
                )
            ]
        )
        strategy = EnumNormalizationStrategy()
        assert strategy.can_handle([_violation()], contract, {"status": "O"}) is False
        assert strategy.propose([_violation()], contract, {"status": "O"}) == []

    def test_end_to_end_a_malformed_schema_repairs_nothing(self) -> None:
        guard = ContractGuard.with_dict_schema()
        schema = {
            "fields": [
                {
                    "path": "status",
                    "type": "string",
                    "constraints": [{"type": "enum_values", "value": "open"}],
                }
            ]
        }
        result = guard.repair(schema, {"status": "O"})
        # Previously: SUCCESS with {'status': 'o'} -- data rewritten because
        # the schema was malformed.
        assert result.status is not RepairStatus.SUCCESS
        assert result.repaired_output is None

    @pytest.mark.parametrize("members", [["open", "done"], ("open", "done"), {"open", "done"}])
    def test_real_collections_still_work(self, members: Any) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec(
                    "status",
                    FieldType.STRING,
                    constraints=[FieldConstraint(FieldConstraintType.ENUM_VALUES, members)],
                )
            ]
        )
        ops = EnumNormalizationStrategy().propose([_violation()], contract, {"status": "DONE"})
        assert [op.value for op in ops] == ["done"]
