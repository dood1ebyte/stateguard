"""Tests for stateguard.core.strategies.coerce."""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.errors.operations import FieldOpType, RepairRisk
from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType, UnionMember
from stateguard.core.strategies.coerce import (
    TypeCoercionStrategy,
    _coercion_evidence,
    _get_nested_value,
    _is_float_string,
    _is_integer_string,
    _NOT_FOUND,
    json_serialized,
    resolve_union_member,
)
from tests.conftest import make_violation


def make_contract() -> ContractSpec:
    return ContractSpec(fields=[FieldSpec("placeholder", FieldType.STRING)])


def _type_mismatch(field_path: str, expected_type: FieldType, received_value: Any) -> Any:
    return make_violation(
        field_path=field_path,
        violation_type=ViolationType.TYPE_MISMATCH,
        severity=ViolationSeverity.ERROR,
        expected_type=expected_type,
        received_value=received_value,
    )


# ===========================================================================
# Identity
# ===========================================================================


class TestIdentity:
    def test_name(self) -> None:
        assert TypeCoercionStrategy().name == "TypeCoercionStrategy"

    def test_priority(self) -> None:
        assert TypeCoercionStrategy().priority == 30


# ===========================================================================
# can_handle
# ===========================================================================


class TestCanHandle:
    def test_true_with_type_mismatch(self) -> None:
        v = _type_mismatch("count", FieldType.INTEGER, "5")
        strategy = TypeCoercionStrategy()
        assert strategy.can_handle([v], make_contract(), {"count": "5"}) is True

    def test_false_without_type_mismatch(self) -> None:
        v = make_violation(
            field_path="count",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = TypeCoercionStrategy()
        assert strategy.can_handle([v], make_contract(), {}) is False

    def test_false_with_no_violations(self) -> None:
        strategy = TypeCoercionStrategy()
        assert strategy.can_handle([], make_contract(), {}) is False


# ===========================================================================
# propose — str -> int
# ===========================================================================


class TestStrToInt:
    def test_positive_digit_string_coerces(self) -> None:
        v = _type_mismatch("count", FieldType.INTEGER, "5")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"count": "5"})
        assert len(ops) == 1
        op = ops[0]
        assert op.op_type is FieldOpType.COERCE
        assert op.target_path == "count"
        # "5" -> 5 -> "5" reproduces the input exactly.
        assert op.evidence.value_preserved == pytest.approx(1.0)
        assert op.risk is RepairRisk.REVERSIBLE
        assert op.trust == 0.0  # TrustPolicy assigns the score, not the strategy

    def test_negative_digit_string_coerces(self) -> None:
        v = _type_mismatch("delta", FieldType.INTEGER, "-5")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"delta": "-5"})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.REVERSIBLE

    def test_non_digit_string_does_not_coerce(self) -> None:
        v = _type_mismatch("count", FieldType.INTEGER, "five")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"count": "five"})
        assert ops == []

    def test_float_string_does_not_coerce_to_int(self) -> None:
        v = _type_mismatch("count", FieldType.INTEGER, "5.0")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"count": "5.0"})
        assert ops == []

    def test_negative_sign_alone_does_not_coerce(self) -> None:
        v = _type_mismatch("count", FieldType.INTEGER, "-")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"count": "-"})
        assert ops == []

    def test_string_with_leading_whitespace_does_not_coerce(self) -> None:
        """Per spec: only str.isdigit() or negative-int form; no whitespace handling."""
        v = _type_mismatch("count", FieldType.INTEGER, "  5")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"count": "  5"})
        assert ops == []

    def test_double_negative_does_not_coerce(self) -> None:
        v = _type_mismatch("count", FieldType.INTEGER, "--5")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"count": "--5"})
        assert ops == []


# ===========================================================================
# propose — str -> float
# ===========================================================================


class TestStrToFloat:
    def test_decimal_string_coerces(self) -> None:
        v = _type_mismatch("temperature", FieldType.FLOAT, "31.5")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"temperature": "31.5"})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.REVERSIBLE
        assert ops[0].target_path == "temperature"

    def test_integer_looking_string_coerces_to_float(self) -> None:
        """
        ``"30"`` -> ``30.0`` -> ``"30.0"`` does not reproduce the input, so
        this scores below an exact round trip even though the numeric value
        is untouched. The old model gave it the same 0.95 as ``"5"`` -> ``5``.
        """
        v = _type_mismatch("temperature", FieldType.FLOAT, "30")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"temperature": "30"})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved == pytest.approx(0.85)
        assert ops[0].risk is RepairRisk.REVERSIBLE

    def test_negative_decimal_string_coerces(self) -> None:
        v = _type_mismatch("delta", FieldType.FLOAT, "-3.5")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"delta": "-3.5"})
        assert len(ops) == 1

    def test_non_numeric_string_does_not_coerce(self) -> None:
        v = _type_mismatch("temperature", FieldType.FLOAT, "hot")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"temperature": "hot"})
        assert ops == []

    def test_scientific_notation_string_coerces(self) -> None:
        """float() accepts scientific notation; documents this behaviour."""
        v = _type_mismatch("value", FieldType.FLOAT, "1e10")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"value": "1e10"})
        assert len(ops) == 1

    def test_whitespace_padded_float_string_coerces(self) -> None:
        """float() strips surrounding whitespace; documents this behaviour."""
        v = _type_mismatch("value", FieldType.FLOAT, "  3.14  ")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"value": "  3.14  "})
        assert len(ops) == 1


# ===========================================================================
# propose — int -> float
# ===========================================================================


class TestIntToFloat:
    def test_int_always_coerces_to_float(self) -> None:
        v = _type_mismatch("temperature", FieldType.FLOAT, 30)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"temperature": 30})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.REVERSIBLE

    def test_negative_int_coerces_to_float(self) -> None:
        v = _type_mismatch("delta", FieldType.FLOAT, -10)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"delta": -10})
        assert len(ops) == 1

    def test_zero_int_coerces_to_float(self) -> None:
        v = _type_mismatch("delta", FieldType.FLOAT, 0)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"delta": 0})
        assert len(ops) == 1

    def test_bool_does_not_coerce_to_float(self) -> None:
        """bool is a subclass of int but must be excluded from int->float."""
        v = _type_mismatch("flag", FieldType.FLOAT, True)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"flag": True})
        assert ops == []


# ===========================================================================
# propose — str -> bool
# ===========================================================================


class TestStrToBool:
    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "false", "FALSE", "1", "0"])
    def test_recognized_strings_coerce(self, value: str) -> None:  # noqa: D102
        v = _type_mismatch("active", FieldType.BOOLEAN, value)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"active": value})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved is not None
        assert ops[0].risk is RepairRisk.INFERRED

    def test_whitespace_padded_recognized_string_coerces(self) -> None:
        v = _type_mismatch("active", FieldType.BOOLEAN, "  true  ")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"active": "  true  "})
        assert len(ops) == 1

    @pytest.mark.parametrize("value", ["yes", "no", "on", "off", "2", "truee"])
    def test_unrecognized_strings_do_not_coerce(self, value: str) -> None:
        v = _type_mismatch("active", FieldType.BOOLEAN, value)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"active": value})
        assert ops == []

    def test_int_does_not_coerce_to_bool(self) -> None:
        v = _type_mismatch("active", FieldType.BOOLEAN, 1)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"active": 1})
        assert ops == []


# ===========================================================================
# propose — dict/list -> str (JSON-serialise, STRING and BYTES targets)
# ===========================================================================


class TestJsonSerializeCoercion:
    def test_dict_to_string_coerces(self) -> None:
        """The openai-python #2702 shape: JSON object where str is expected."""
        payload = {"name": "hello_world", "dependencies": {"express": "^5.1.0"}}
        v = _type_mismatch("content", FieldType.STRING, payload)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"content": payload})
        assert len(ops) == 1
        assert ops[0].op_type is FieldOpType.COERCE
        assert ops[0].target_path == "content"
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.LOSSY

    def test_list_to_string_coerces(self) -> None:
        v = _type_mismatch("content", FieldType.STRING, [1, 2])
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"content": [1, 2]})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.LOSSY

    def test_dict_to_bytes_coerces(self) -> None:
        """BYTES targets get the same repair; the serialised str satisfies
        the framework's lax str -> bytes rule on revalidation."""
        payload = {"name": "hello_world"}
        v = _type_mismatch("content", FieldType.BYTES, payload)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"content": payload})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.LOSSY

    def test_non_serializable_dict_not_coerced(self) -> None:
        payload = {"x": object()}
        v = _type_mismatch("content", FieldType.STRING, payload)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"content": payload})
        assert ops == []

    def test_circular_dict_not_coerced(self) -> None:
        payload: dict[str, Any] = {}
        payload["self"] = payload
        v = _type_mismatch("content", FieldType.STRING, payload)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"content": payload})
        assert ops == []

    def test_scalar_to_string_not_coerced(self) -> None:
        """Only containers qualify: a scalar where a string is expected is a
        semantic mismatch, not an over-parsed JSON argument."""
        v = _type_mismatch("content", FieldType.STRING, 3.5)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"content": 3.5})
        assert ops == []

    def test_bytes_value_for_bytes_target_not_coerced(self) -> None:
        """A genuine bytes value already matches BYTES; nothing to propose."""
        v = _type_mismatch("content", FieldType.BYTES, b"\x89PNG")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"content": b"\x89PNG"})
        assert ops == []


# ===========================================================================
# propose — unsupported coercions
# ===========================================================================


class TestUnsupportedCoercions:
    def test_int_to_string_not_coerced(self) -> None:
        v = _type_mismatch("name", FieldType.STRING, 123)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"name": 123})
        assert ops == []

    def test_dict_to_int_not_coerced(self) -> None:
        v = _type_mismatch("count", FieldType.INTEGER, {"a": 1})
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"count": {"a": 1}})
        assert ops == []

    def test_object_target_type_not_coerced(self) -> None:
        v = _type_mismatch("address", FieldType.OBJECT, "not a dict")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"address": "not a dict"})
        assert ops == []

    def test_array_target_type_not_coerced(self) -> None:
        v = _type_mismatch("tags", FieldType.ARRAY, "not a list")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"tags": "not a list"})
        assert ops == []


# ===========================================================================
# propose — wrap-in-list (ARRAY targets)
# ===========================================================================


def _array_contract(item_type: FieldType | None) -> ContractSpec:
    return ContractSpec(fields=[FieldSpec("content", FieldType.ARRAY, item_type=item_type)])


def _union_contract(members: tuple[UnionMember, ...] | None) -> ContractSpec:
    return ContractSpec(fields=[FieldSpec("content", FieldType.UNION, union_members=members)])


class TestArrayWrap:
    def test_str_wraps_into_list_of_str(self) -> None:
        v = _type_mismatch("content", FieldType.ARRAY, "hello")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _array_contract(FieldType.STRING), {"content": "hello"})
        assert len(ops) == 1
        assert ops[0].op_type is FieldOpType.COERCE
        assert ops[0].target_path == "content"
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.LOSSY

    def test_item_type_mismatch_does_not_wrap(self) -> None:
        v = _type_mismatch("content", FieldType.ARRAY, "hello")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _array_contract(FieldType.INTEGER), {"content": "hello"})
        assert ops == []

    def test_bare_list_does_not_wrap(self) -> None:
        v = _type_mismatch("content", FieldType.ARRAY, "hello")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _array_contract(None), {"content": "hello"})
        assert ops == []

    def test_value_already_list_does_not_wrap(self) -> None:
        v = _type_mismatch("content", FieldType.ARRAY, ["x", 1])
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _array_contract(FieldType.STRING), {"content": ["x", 1]})
        assert ops == []

    def test_none_value_does_not_wrap(self) -> None:
        v = _type_mismatch("content", FieldType.ARRAY, None)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _array_contract(FieldType.STRING), {"content": None})
        assert ops == []

    def test_bool_does_not_wrap_into_list_of_int(self) -> None:
        """bool is a subclass of int but must not satisfy an INTEGER item type."""
        v = _type_mismatch("content", FieldType.ARRAY, True)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _array_contract(FieldType.INTEGER), {"content": True})
        assert ops == []

    def test_field_spec_not_found_does_not_wrap(self) -> None:
        v = _type_mismatch("content", FieldType.ARRAY, "hello")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"content": "hello"})
        assert ops == []


# ===========================================================================
# propose — UNION targets
# ===========================================================================


class TestUnionCoercion:
    def test_dict_wraps_into_list_member(self) -> None:
        """The graph-rag-agent #49 shape: dict against str | list[str|dict].

        Also pins precedence: the dict is now coercible to the STRING
        member too (JSON-serialise, 0.85), but array wrap (0.9) wins
        uniquely, so the resolution stays deterministic."""
        members = (
            UnionMember(FieldType.STRING),
            UnionMember(FieldType.ARRAY, item_type=FieldType.ANY),
        )
        payload = {"low_level": [], "high_level": []}
        v = _type_mismatch("content", FieldType.UNION, payload)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _union_contract(members), {"content": payload})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.LOSSY

    def test_dict_serializes_into_string_member(self) -> None:
        """str | <object-like> union: only the STRING member is coercible."""
        members = (UnionMember(FieldType.STRING), UnionMember(FieldType.INTEGER))
        payload = {"name": "hello_world"}
        v = _type_mismatch("content", FieldType.UNION, payload)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _union_contract(members), {"content": payload})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.LOSSY

    def test_string_bytes_tie_refused(self) -> None:
        """str | bytes members both accept the JSON serialisation at equal
        confidence -> ambiguous tie, refused (documented limitation)."""
        members = (UnionMember(FieldType.STRING), UnionMember(FieldType.BYTES))
        payload = {"name": "hello_world"}
        v = _type_mismatch("content", FieldType.UNION, payload)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _union_contract(members), {"content": payload})
        assert ops == []

    def test_scalar_coercion_into_single_member(self) -> None:
        members = (UnionMember(FieldType.INTEGER), UnionMember(FieldType.ARRAY, FieldType.OBJECT))
        v = _type_mismatch("content", FieldType.UNION, "42")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _union_contract(members), {"content": "42"})
        assert len(ops) == 1
        assert ops[0].evidence.value_preserved == pytest.approx(1.0)
        assert ops[0].risk is RepairRisk.REVERSIBLE

    def test_int_member_wins_over_float_on_measured_fidelity(self) -> None:
        """
        '42' used to tie between int and float on identical constants, so the
        union refused. Round-trip fidelity separates them: str(int("42")) is
        "42" exactly, str(float("42")) is "42.0".
        """
        members = (UnionMember(FieldType.INTEGER), UnionMember(FieldType.FLOAT))
        v = _type_mismatch("content", FieldType.UNION, "42")
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _union_contract(members), {"content": "42"})
        assert len(ops) == 1
        assert ops[0].risk is RepairRisk.REVERSIBLE

    def test_no_coercible_member_refused(self) -> None:
        members = (UnionMember(FieldType.STRING), UnionMember(FieldType.OBJECT))
        v = _type_mismatch("content", FieldType.UNION, 3.5)
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _union_contract(members), {"content": 3.5})
        assert ops == []

    def test_union_without_members_refused(self) -> None:
        v = _type_mismatch("content", FieldType.UNION, {"a": 1})
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], _union_contract(None), {"content": {"a": 1}})
        assert ops == []


# ===========================================================================
# resolve_union_member
# ===========================================================================


class TestResolveUnionMember:
    def test_picks_unique_highest_confidence_member(self) -> None:
        members = (
            UnionMember(FieldType.BOOLEAN),  # "1" -> bool at 0.85
            UnionMember(FieldType.INTEGER),  # "1" -> int at 0.95
        )
        resolved = resolve_union_member("1", members)
        assert resolved is not None
        member, evidence, risk = resolved
        assert member.field_type is FieldType.INTEGER
        assert evidence.value_preserved == pytest.approx(1.0)
        assert risk is RepairRisk.REVERSIBLE

    def test_none_members_returns_none(self) -> None:
        assert resolve_union_member("1", None) is None
        assert resolve_union_member("1", ()) is None


# ===========================================================================
# propose — guard conditions
# ===========================================================================


class TestProposeGuards:
    def test_non_type_mismatch_violation_ignored(self) -> None:
        v = make_violation(
            field_path="count",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"count": "5"})
        assert ops == []

    def test_expected_type_none_ignored(self) -> None:
        v = make_violation(
            field_path="count",
            violation_type=ViolationType.TYPE_MISMATCH,
            severity=ViolationSeverity.ERROR,
            expected_type=None,
        )
        strategy = TypeCoercionStrategy()
        ops = strategy.propose([v], make_contract(), {"count": "5"})
        assert ops == []

    def test_value_not_in_data_ignored(self) -> None:
        v = _type_mismatch("count", FieldType.INTEGER, "5")
        strategy = TypeCoercionStrategy()
        # 'count' key absent from data entirely
        ops = strategy.propose([v], make_contract(), {})
        assert ops == []

    def test_empty_violations_returns_empty(self) -> None:
        strategy = TypeCoercionStrategy()
        assert strategy.propose([], make_contract(), {}) == []


# ===========================================================================
# propose — nested fields
# ===========================================================================


class TestProposeNested:
    def test_nested_field_coercion(self) -> None:
        v = _type_mismatch("address.zip_code", FieldType.INTEGER, "400001")
        strategy = TypeCoercionStrategy()
        data = {"address": {"zip_code": "400001"}}
        ops = strategy.propose([v], make_contract(), data)
        assert len(ops) == 1
        assert ops[0].target_path == "address.zip_code"

    def test_nested_field_missing_ignored(self) -> None:
        v = _type_mismatch("address.zip_code", FieldType.INTEGER, "400001")
        strategy = TypeCoercionStrategy()
        data = {"address": {}}
        ops = strategy.propose([v], make_contract(), data)
        assert ops == []

    def test_multiple_type_mismatches_each_handled(self) -> None:
        v1 = _type_mismatch("temperature", FieldType.FLOAT, "31.5")
        v2 = _type_mismatch("humidity", FieldType.INTEGER, "80")
        strategy = TypeCoercionStrategy()
        data = {"temperature": "31.5", "humidity": "80"}
        ops = strategy.propose([v1, v2], make_contract(), data)
        assert len(ops) == 2
        targets = {op.target_path for op in ops}
        assert targets == {"temperature", "humidity"}

    def test_depth3_nested_field_coercion(self) -> None:
        """Two levels of nested OBJECT: address.country.population.
        Confirms _get_nested_value's dotted-path walk has no depth limit."""
        v = _type_mismatch("address.country.population", FieldType.INTEGER, "1200000")
        strategy = TypeCoercionStrategy()
        data = {"address": {"country": {"population": "1200000"}}}
        ops = strategy.propose([v], make_contract(), data)
        assert len(ops) == 1
        op = ops[0]
        assert op.target_path == "address.country.population"
        assert op.evidence.value_preserved == pytest.approx(1.0)
        assert op.risk is RepairRisk.REVERSIBLE

    def test_depth3_nested_field_str_to_float(self) -> None:
        v = _type_mismatch("address.country.area_km2", FieldType.FLOAT, "3287263.5")
        strategy = TypeCoercionStrategy()
        data = {"address": {"country": {"area_km2": "3287263.5"}}}
        ops = strategy.propose([v], make_contract(), data)
        assert len(ops) == 1
        assert ops[0].target_path == "address.country.area_km2"

    def test_depth3_nested_field_unsupported_coercion_not_proposed(self) -> None:
        """int->str at depth 3 is correctly declined (not a supported cast)."""
        v = _type_mismatch("address.country.code", FieldType.STRING, 91)
        strategy = TypeCoercionStrategy()
        data = {"address": {"country": {"code": 91}}}
        ops = strategy.propose([v], make_contract(), data)
        assert ops == []

    def test_depth3_intermediate_branch_missing_ignored(self) -> None:
        """If 'country' itself is absent, the depth-3 path simply isn't
        found -- no crash, no spurious proposal."""
        v = _type_mismatch("address.country.population", FieldType.INTEGER, "1200000")
        strategy = TypeCoercionStrategy()
        data = {"address": {}}
        ops = strategy.propose([v], make_contract(), data)
        assert ops == []


# ===========================================================================
# Internal helpers — direct tests
# ===========================================================================


class TestGetNestedValue:
    def test_top_level_key(self) -> None:
        assert _get_nested_value({"a": 1}, "a") == 1

    def test_nested_key(self) -> None:
        assert _get_nested_value({"a": {"b": 2}}, "a.b") == 2

    def test_missing_top_level_key(self) -> None:
        assert _get_nested_value({"a": 1}, "b") is _NOT_FOUND

    def test_missing_nested_key(self) -> None:
        assert _get_nested_value({"a": {"b": 2}}, "a.c") is _NOT_FOUND

    def test_intermediate_not_a_dict(self) -> None:
        assert _get_nested_value({"a": "not a dict"}, "a.b") is _NOT_FOUND

    def test_value_none_is_distinct_from_not_found(self) -> None:
        result = _get_nested_value({"a": None}, "a")
        assert result is None
        assert result is not _NOT_FOUND


class TestIsIntegerString:
    @pytest.mark.parametrize("value", ["0", "5", "100", "-5", "-100"])
    def test_valid_integer_strings(self, value: str) -> None:
        assert _is_integer_string(value) is True

    @pytest.mark.parametrize("value", ["", "-", "--5", "5.0", "five", " 5", "5 ", "+5"])
    def test_invalid_integer_strings(self, value: str) -> None:
        assert _is_integer_string(value) is False


class TestIsFloatString:
    @pytest.mark.parametrize("value", ["0", "5", "3.14", "-3.14", "1e10", "1E-5", "  3.14  ", "+5"])
    def test_valid_float_strings(self, value: str) -> None:
        assert _is_float_string(value) is True

    @pytest.mark.parametrize("value", ["", "hot", "five", "3.14.15"])
    def test_invalid_float_strings(self, value: str) -> None:
        assert _is_float_string(value) is False


class TestJsonSerializedDirect:
    def test_dict_round_trips(self) -> None:
        assert json_serialized({"a": 1}) == '{"a": 1}'

    def test_list_round_trips(self) -> None:
        assert json_serialized([1, "x"]) == '[1, "x"]'

    @pytest.mark.parametrize("value", ["x", 1, 3.5, True, None, b"raw"])
    def test_scalars_refused(self, value: Any) -> None:
        assert json_serialized(value) is None

    def test_non_json_content_refused(self) -> None:
        assert json_serialized({"x": object()}) is None

    def test_circular_reference_refused(self) -> None:
        payload: dict[str, Any] = {}
        payload["self"] = payload
        assert json_serialized(payload) is None


class TestCoercionEvidenceDirect:
    """
    ``_coercion_evidence`` replaces ``_coercion_confidence``.

    The old function returned one of four constants chosen by hand. This one
    measures round-trip fidelity and declares the consequence of being wrong,
    leaving the score to TrustPolicy.
    """

    @staticmethod
    def _fidelity(value: Any, target: FieldType, **kw: Any) -> float | None:
        measured = _coercion_evidence(value, target, **kw)
        return None if measured is None else measured[0].value_preserved

    @staticmethod
    def _risk(value: Any, target: FieldType, **kw: Any) -> RepairRisk | None:
        measured = _coercion_evidence(value, target, **kw)
        return None if measured is None else measured[1]

    # --- round-trip fidelity is measured, not assumed ---------------------

    @pytest.mark.parametrize(
        ("value", "target", "expected"),
        [
            ("5", FieldType.INTEGER, 1.0),  # str(int("5")) == "5"
            ("05", FieldType.INTEGER, 0.85),  # str(int("05")) == "5" != "05"
            ("-7", FieldType.INTEGER, 1.0),
            ("5.5", FieldType.FLOAT, 1.0),
            (5, FieldType.FLOAT, 1.0),  # int is exactly representable
        ],
    )
    def test_roundtrip_fidelity(self, value: Any, target: FieldType, expected: float) -> None:
        assert self._fidelity(value, target) == pytest.approx(expected)

    def test_whitespace_padded_integers_are_refused(self) -> None:
        """Pre-existing behaviour, unchanged: ``" 5 ".isdecimal()`` is False."""
        assert _coercion_evidence(" 5 ", FieldType.INTEGER) is None

    def test_exact_and_normalised_casts_are_no_longer_scored_alike(self) -> None:
        """
        The headline of the change: "5" and "05" both used to be worth 0.95
        simply because both are digit strings, even though only one of them
        survives a round trip.
        """
        assert self._fidelity("5", FieldType.INTEGER) > self._fidelity("05", FieldType.INTEGER)

    # --- refusals ---------------------------------------------------------

    @pytest.mark.parametrize(
        ("value", "target"),
        [
            ("five", FieldType.INTEGER),
            (True, FieldType.FLOAT),
            ("maybe", FieldType.BOOLEAN),
            ("x", FieldType.STRING),
            ("x", FieldType.BYTES),
            ("x", FieldType.OBJECT),
            ("x", FieldType.ARRAY),
            ("x", FieldType.ANY),
            ("x", FieldType.NULL),
        ],
    )
    def test_unsupported_pairs_refused(self, value: Any, target: FieldType) -> None:
        assert _coercion_evidence(value, target) is None

    @pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "infinity"])
    def test_non_finite_floats_refused(self, value: str) -> None:
        """
        ``float()`` accepts these, but NaN poisons every comparison it touches
        and neither NaN nor Infinity survives ``json.dumps`` as valid JSON.
        They are not lossless casts of a numeric string.
        """
        assert _coercion_evidence(value, FieldType.FLOAT) is None

    # --- booleans ---------------------------------------------------------

    def test_spelled_boolean_beats_numeric_boolean(self) -> None:
        """ "1" -> True reads the string; "true" -> True re-encodes it."""
        spelled = self._fidelity("true", FieldType.BOOLEAN)
        numeric = self._fidelity("1", FieldType.BOOLEAN)
        assert spelled is not None and numeric is not None
        assert spelled > numeric

    # --- risk tiers -------------------------------------------------------

    def test_numeric_casts_are_reversible(self) -> None:
        assert self._risk("5", FieldType.INTEGER) is RepairRisk.REVERSIBLE
        assert self._risk(5, FieldType.FLOAT) is RepairRisk.REVERSIBLE

    def test_reading_a_string_as_a_boolean_is_inferred(self) -> None:
        assert self._risk("true", FieldType.BOOLEAN) is RepairRisk.INFERRED

    def test_serialising_a_container_is_lossy(self) -> None:
        """Every byte survives, but a container rendered as text is not one."""
        assert self._fidelity({"a": 1}, FieldType.STRING) == 1.0
        assert self._risk({"a": 1}, FieldType.STRING) is RepairRisk.LOSSY
        assert self._risk({"a": 1}, FieldType.BYTES) is RepairRisk.LOSSY

    def test_bool_string_to_integer_none(self) -> None:
        """A 'bool string' that is not digit-form must not coerce to int."""
        assert _coercion_evidence("true", FieldType.INTEGER) is None
