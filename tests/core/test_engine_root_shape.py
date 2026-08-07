"""
Non-object payload roots.

A ``ContractSpec`` describes the fields of an object, so a payload whose root
is not a ``dict`` has nothing to look field paths up in. Previously that
condition was not checked anywhere: unguarded ``key in data`` / ``data[key]``
expressions raised ``TypeError`` or ``IndexError`` part-way through
validation for almost every non-dict type, and ``[]`` was quietly misread as
``{}``.

The contract now is:

* ``repair()`` and ``validate()`` **never raise** on an unexpected root type.
* A root is *recovered* when doing so re-encodes data that is already
  structured, never when it would infer intent:

  - **already an object, just not a ``dict``** --- a ``Mapping`` that is not
    a ``dict`` subclass, a dataclass instance, a namedtuple. The keys and
    values are already named; the conversion is the type's own canonical
    dict form;
  - **an object that arrived mis-wrapped** --- a JSON-encoded string or
    bytes, a single-element sequence wrapping the object, or a key/value
    pair list (``[["a", 1]]``) under a strict guard.

* Every other non-object root is a ``STRUCTURAL_MISMATCH`` at ``field_path``
  ``""`` --- reported, never guessed at. That includes shapes Python would
  happily convert: ``dict()`` accepts ``[("a", 1)]`` without validating it,
  which is why the pair-list path is gated on its own guard rather than
  delegated to ``dict()``.

Two cases are excluded on purpose rather than by omission, both documented in
``CORE_HARDENING_PLAN.md``: a Pydantic ``BaseModel`` instance (needs an
adapter hook, not core awareness of an adapter's types) and a bare scalar
against a single-field contract (an inference about intent, so it belongs in
the confidence model).
"""

from __future__ import annotations

from collections import UserDict, namedtuple
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import pytest

from stateguard import ContractGuard
from stateguard.core.errors.results import RepairStatus
from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.core.validator import ContractValidator, root_structural_violation

SCHEMA = {
    "fields": [
        {"path": "a", "type": "integer"},
        {"path": "b", "type": "string"},
    ]
}

VALID_PAYLOAD = {"a": 1, "b": "x"}


@dataclass
class PayloadDataclass:
    a: int
    b: str


PayloadNamedTuple = namedtuple("PayloadNamedTuple", ["a", "b"])  # noqa: PYI024


class CustomMapping(Mapping):
    """A Mapping implementation that is not a dict subclass."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Any:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


# Roots that cannot be recovered into an object.
UNREPAIRABLE_ROOTS: list[tuple[str, Any]] = [
    ("none", None),
    ("int", 5),
    ("float", 3.14),
    ("bool", True),
    ("plain_str", "abc"),
    ("invalid_json_str", "{not json"),
    ("json_scalar_str", "42"),
    ("json_null_str", "null"),
    ("plain_bytes", b"bytes"),
    ("empty_list", []),
    ("list_of_ints", [1, 2]),
    ("list_of_two_dicts", [{"a": 1}, {"a": 2}]),
    ("nested_list", [[{"a": 1}]]),
    ("tuple", (1, 2)),
    ("set", {1, 2}),
    ("object", object()),
    # Pair-list shapes that fail _pairs_to_dict's guard.
    ("pairs_duplicate_key", [["a", 1], ["a", 2]]),
    ("pairs_non_str_key", [[1, "x"], [2, "y"]]),
    ("pairs_wrong_arity", [["a", 1, 2], ["b", "x", 3]]),
    ("pairs_mixed_shapes", [["a", 1], "bx"]),
]

# Roots that are recovered into an object.
REPAIRABLE_ROOTS: list[tuple[str, Any]] = [
    # Mis-wrapped objects.
    ("json_object_str", '{"a": 1, "b": "x"}'),
    ("json_object_bytes", b'{"a": 1, "b": "x"}'),
    ("single_element_list", [{"a": 1, "b": "x"}]),
    ("single_element_tuple", ({"a": 1, "b": "x"},)),
    ("json_single_element_array_str", '[{"a": 1, "b": "x"}]'),
    # Key/value pair lists (dict.items() / Object.entries() wire form).
    ("pair_list_of_lists", [["a", 1], ["b", "x"]]),
    ("pair_list_of_tuples", [("a", 1), ("b", "x")]),
    ("pair_list_json_str", '[["a", 1], ["b", "x"]]'),
    # Objects that already carry named fields, just not as a dict.
    ("mapping_proxy", MappingProxyType({"a": 1, "b": "x"})),
    ("user_dict", UserDict({"a": 1, "b": "x"})),
    ("custom_mapping", CustomMapping({"a": 1, "b": "x"})),
    ("dataclass_instance", PayloadDataclass(1, "x")),
    ("namedtuple_instance", PayloadNamedTuple(1, "x")),
]

ALL_ROOTS = UNREPAIRABLE_ROOTS + REPAIRABLE_ROOTS


@pytest.fixture
def guard() -> ContractGuard:
    return ContractGuard.with_dict_schema()


# ===========================================================================
# The headline guarantee: nothing raises
# ===========================================================================


class TestNeverRaises:
    @pytest.mark.parametrize(("label", "payload"), ALL_ROOTS, ids=[c[0] for c in ALL_ROOTS])
    def test_repair_returns_a_result(self, guard: ContractGuard, label: str, payload: Any) -> None:
        result = guard.repair(SCHEMA, payload)
        assert result.status in set(RepairStatus)

    @pytest.mark.parametrize(("label", "payload"), ALL_ROOTS, ids=[c[0] for c in ALL_ROOTS])
    def test_validate_returns_a_result(
        self, guard: ContractGuard, label: str, payload: Any
    ) -> None:
        assert guard.validate(SCHEMA, payload).is_valid is False

    @pytest.mark.parametrize(("label", "payload"), ALL_ROOTS, ids=[c[0] for c in ALL_ROOTS])
    def test_core_validator_returns_a_result(self, label: str, payload: Any) -> None:
        """ContractValidator is public API and is called directly by adapters."""
        contract = ContractSpec(fields=[FieldSpec("a", FieldType.INTEGER)])
        assert ContractValidator().validate(contract, payload).is_valid is False

    def test_pydantic_adapter_path_also_survives(self) -> None:
        pydantic = pytest.importorskip("pydantic")

        class Model(pydantic.BaseModel):
            a: int

        result = ContractGuard.with_pydantic().repair(Model, None)
        assert result.status is RepairStatus.FAILED

    def test_deeply_nested_json_string_does_not_blow_the_stack(self, guard: ContractGuard) -> None:
        """json.loads raises RecursionError, not JSONDecodeError, on deep input."""
        payload = "[" * 20_000 + "]" * 20_000
        assert guard.repair(SCHEMA, payload).status is RepairStatus.FAILED

    def test_uncopyable_root_does_not_raise(self, guard: ContractGuard) -> None:
        """
        ``deepcopy(MappingProxyType(...))`` raises
        ``TypeError: cannot pickle 'mappingproxy' object``. The engine used to
        take that copy *before* the root guard, so an ordinary read-only dict
        wrapper escaped the never-raises guarantee entirely.
        """
        assert guard.repair(SCHEMA, MappingProxyType({"a": 1, "b": "x"})).status is (
            RepairStatus.SUCCESS
        )

    def test_uncopyable_root_is_still_snapshot_in_original_input(
        self, guard: ContractGuard
    ) -> None:
        payload = MappingProxyType({"a": 1, "b": "x"})
        assert guard.repair(SCHEMA, payload).original_input == payload

    def test_conversion_that_itself_raises_is_a_failure_not_an_exception(
        self, guard: ContractGuard
    ) -> None:
        """
        ``dataclasses.asdict`` deep-copies non-dataclass field values, so a
        dataclass holding an uncopyable field raises during conversion. That
        must read as "not recoverable", never propagate.
        """

        @dataclass
        class Uncopyable:
            a: object

        result = guard.repair(SCHEMA, Uncopyable(MappingProxyType({"x": 1})))
        assert result.status is RepairStatus.FAILED


class TestCallerDataIsNeverMutated:
    """
    The recovery helpers return *shallow* copies, so without a deepcopy after
    normalisation the repair loop would write straight through into nested
    values still owned by the caller.
    """

    SCHEMA = {
        "fields": [
            {
                "path": "outer",
                "type": "object",
                "nested": {"fields": [{"path": "temperature", "type": "float"}]},
            }
        ]
    }

    def test_single_element_list_unwrap_does_not_mutate_nested_values(
        self, guard: ContractGuard
    ) -> None:
        inner = {"outer": {"temp_celsius": 31.5}}
        payload = [inner]

        result = guard.repair(self.SCHEMA, payload)

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {"outer": {"temperature": 31.5}}
        # The caller's object must be untouched.
        assert inner == {"outer": {"temp_celsius": 31.5}}

    def test_mapping_conversion_does_not_mutate_nested_values(self, guard: ContractGuard) -> None:
        inner = {"temp_celsius": 31.5}
        payload = MappingProxyType({"outer": inner})

        result = guard.repair(self.SCHEMA, payload)

        assert result.status is RepairStatus.SUCCESS
        assert inner == {"temp_celsius": 31.5}


# ===========================================================================
# Unrepairable roots
# ===========================================================================


class TestUnrepairableRoots:
    @pytest.mark.parametrize(
        ("label", "payload"), UNREPAIRABLE_ROOTS, ids=[c[0] for c in UNREPAIRABLE_ROOTS]
    )
    def test_status_is_failed(self, guard: ContractGuard, label: str, payload: Any) -> None:
        assert guard.repair(SCHEMA, payload).status is RepairStatus.FAILED

    @pytest.mark.parametrize(
        ("label", "payload"), UNREPAIRABLE_ROOTS, ids=[c[0] for c in UNREPAIRABLE_ROOTS]
    )
    def test_no_output_is_invented(self, guard: ContractGuard, label: str, payload: Any) -> None:
        assert guard.repair(SCHEMA, payload).repaired_output is None

    @pytest.mark.parametrize(
        ("label", "payload"), UNREPAIRABLE_ROOTS, ids=[c[0] for c in UNREPAIRABLE_ROOTS]
    )
    def test_reports_one_root_structural_violation(
        self, guard: ContractGuard, label: str, payload: Any
    ) -> None:
        result = guard.repair(SCHEMA, payload)
        assert len(result.initial_violations) == 1
        violation = result.initial_violations[0]
        assert violation.violation_type is ViolationType.STRUCTURAL_MISMATCH
        assert violation.severity is ViolationSeverity.ERROR
        assert violation.field_path == ""

    def test_empty_list_is_not_treated_as_an_empty_object(self, guard: ContractGuard) -> None:
        """``dict([])`` is ``{}``, which previously made [] look like a valid root."""
        result = guard.repair(SCHEMA, [])
        assert result.status is RepairStatus.FAILED
        assert result.initial_violations[0].violation_type is ViolationType.STRUCTURAL_MISMATCH

    def test_pair_list_with_duplicate_keys_is_refused(self, guard: ContractGuard) -> None:
        """A duplicate key makes the conversion lossy -- one value silently wins."""
        assert guard.repair(SCHEMA, [["a", 1], ["a", 2], ["b", "x"]]).status is RepairStatus.FAILED

    def test_pair_list_with_non_string_keys_is_refused(self, guard: ContractGuard) -> None:
        assert guard.repair(SCHEMA, [[1, "x"], [2, "y"]]).status is RepairStatus.FAILED

    def test_pair_list_with_wrong_arity_is_refused(self, guard: ContractGuard) -> None:
        """``dict()`` would reject these too -- the guard must not be looser."""
        assert guard.repair(SCHEMA, [["a", 1, 2]]).status is RepairStatus.FAILED

    def test_positional_value_list_is_not_mapped_onto_fields(self, guard: ContractGuard) -> None:
        """[1, "x"] has the right values in the right order -- still a guess."""
        assert guard.repair(SCHEMA, [1, "x"]).status is RepairStatus.FAILED

    def test_pydantic_model_instance_is_deliberately_not_converted(self) -> None:
        """
        A BaseModel instance has a canonical ``model_dump``, but recognising it
        in the core engine would make Layer 5 aware of an adapter's type
        system. It needs an IContractAdapter hook -- see
        CORE_HARDENING_PLAN.md 2b.1.
        """
        pydantic = pytest.importorskip("pydantic")

        class Model(pydantic.BaseModel):
            a: int
            b: str

        result = ContractGuard.with_dict_schema().repair(SCHEMA, Model(a=1, b="x"))
        assert result.status is RepairStatus.FAILED

    def test_bare_scalar_against_single_field_contract_is_not_guessed(
        self, guard: ContractGuard
    ) -> None:
        """
        Logged for the confidence model rather than done here: wrapping a
        scalar infers *intent*, unlike every recovery above, which only
        re-encodes data that is already structured. See
        CORE_HARDENING_PLAN.md 2b.4.
        """
        one_field = {"fields": [{"path": "city", "type": "string"}]}
        assert guard.repair(one_field, "Mumbai").status is RepairStatus.FAILED

    def test_multi_element_list_is_refused_rather_than_picked_from(
        self, guard: ContractGuard
    ) -> None:
        payload = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
        assert guard.repair(SCHEMA, payload).status is RepairStatus.FAILED

    def test_original_input_preserves_the_value_as_received(self, guard: ContractGuard) -> None:
        assert guard.repair(SCHEMA, [1, 2]).original_input == [1, 2]

    def test_failure_is_logged(self, guard: ContractGuard) -> None:
        result = guard.repair(SCHEMA, 5)
        assert "root.unsupported_type" in {e.event for e in result.repair_log}

    def test_no_repair_attempts_are_recorded(self, guard: ContractGuard) -> None:
        assert guard.repair(SCHEMA, 5).attempts == []


# ===========================================================================
# Recovered roots
# ===========================================================================


class TestRepairableRoots:
    @pytest.mark.parametrize(
        ("label", "payload"), REPAIRABLE_ROOTS, ids=[c[0] for c in REPAIRABLE_ROOTS]
    )
    def test_status_is_success(self, guard: ContractGuard, label: str, payload: Any) -> None:
        assert guard.repair(SCHEMA, payload).status is RepairStatus.SUCCESS

    @pytest.mark.parametrize(
        ("label", "payload"), REPAIRABLE_ROOTS, ids=[c[0] for c in REPAIRABLE_ROOTS]
    )
    def test_output_is_the_recovered_object(
        self, guard: ContractGuard, label: str, payload: Any
    ) -> None:
        assert guard.repair(SCHEMA, payload).repaired_output == VALID_PAYLOAD

    @pytest.mark.parametrize(
        ("label", "payload"), REPAIRABLE_ROOTS, ids=[c[0] for c in REPAIRABLE_ROOTS]
    )
    def test_normalisation_is_logged(self, guard: ContractGuard, label: str, payload: Any) -> None:
        result = guard.repair(SCHEMA, payload)
        assert "root.normalised" in {e.event for e in result.repair_log}

    def test_recovered_root_is_success_not_already_valid(self, guard: ContractGuard) -> None:
        """
        The object inside needed no field repair, but recovering the root *is*
        a repair -- so ALREADY_VALID ("no repair was needed") would be wrong.
        """
        result = guard.repair(SCHEMA, '{"a": 1, "b": "x"}')
        assert result.status is RepairStatus.SUCCESS
        assert result.status is not RepairStatus.ALREADY_VALID

    def test_a_real_dict_is_still_already_valid(self, guard: ContractGuard) -> None:
        assert guard.repair(SCHEMA, VALID_PAYLOAD).status is RepairStatus.ALREADY_VALID

    def test_original_input_keeps_the_unparsed_payload(self, guard: ContractGuard) -> None:
        raw = '{"a": 1, "b": "x"}'
        assert guard.repair(SCHEMA, raw).original_input == raw

    def test_recovered_root_then_field_repair(self, guard: ContractGuard) -> None:
        """
        Root normalisation feeds the normal repair loop: parse the string,
        then fuzzy-rename and coerce inside it. Exercises Phase 1 and Phase 2
        together.
        """
        schema = {
            "fields": [
                {"path": "temperature", "type": "float"},
                {"path": "humidity", "type": "integer"},
            ]
        }
        result = guard.repair(schema, '{"temp_celsius": "31.5", "humidity": 80}')

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {"temperature": 31.5, "humidity": 80}

    def test_recovered_root_that_cannot_be_fully_repaired_is_partial(
        self, guard: ContractGuard
    ) -> None:
        result = guard.repair(SCHEMA, '{"a": 1}')
        assert result.status is RepairStatus.FAILED
        assert result.repaired_output is None


# ===========================================================================
# Nested non-object values (pre-existing behaviour -- guard against regression)
# ===========================================================================


class TestNestedNonObjectStillReported:
    def test_nested_scalar_where_object_expected(self, guard: ContractGuard) -> None:
        schema = {
            "fields": [
                {
                    "path": "address",
                    "type": "object",
                    "nested": {"fields": [{"path": "city", "type": "string"}]},
                }
            ]
        }
        result = guard.repair(schema, {"address": "123 Main St"})

        assert result.status is RepairStatus.FAILED
        assert [v.violation_type for v in result.initial_violations] == [
            ViolationType.STRUCTURAL_MISMATCH
        ]
        assert result.initial_violations[0].field_path == "address"


# ===========================================================================
# root_structural_violation
# ===========================================================================


class TestRootStructuralViolation:
    def test_shape(self) -> None:
        violation = root_structural_violation([1, 2])
        assert violation.field_path == ""
        assert violation.violation_type is ViolationType.STRUCTURAL_MISMATCH
        assert violation.severity is ViolationSeverity.ERROR
        assert violation.expected_type is FieldType.OBJECT
        assert violation.received_value == [1, 2]

    @pytest.mark.parametrize(
        ("payload", "type_name"),
        [(None, "NoneType"), (5, "int"), ("abc", "str"), ([], "list"), ((), "tuple")],
    )
    def test_message_names_the_received_type(self, payload: Any, type_name: str) -> None:
        assert type_name in root_structural_violation(payload).message
