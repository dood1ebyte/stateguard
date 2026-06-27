"""Tests for stateguard.core.models.contract."""

from __future__ import annotations

import copy
import hashlib

import pytest

from stateguard.core.models.contract import (
    MISSING,
    ContractSpec,
    FieldSpec,
    _MissingSentinel,
)
from stateguard.core.models.field_types import (
    FieldConstraint,
    FieldConstraintType,
    FieldType,
)


# ===========================================================================
# MISSING sentinel
# ===========================================================================


class TestMissingSentinel:

    # --- Identity and singleton -----------------------------------------------

    def test_missing_is_not_none(self) -> None:
        assert MISSING is not None

    def test_missing_is_singleton(self) -> None:
        a = _MissingSentinel()
        b = _MissingSentinel()
        assert a is b

    def test_new_instance_is_same_as_module_constant(self) -> None:
        assert _MissingSentinel() is MISSING

    def test_missing_is_same_object_every_reference(self) -> None:
        from stateguard.core.models.contract import MISSING as M2
        assert MISSING is M2

    # --- Truthiness -----------------------------------------------------------

    def test_bool_is_false(self) -> None:
        assert bool(MISSING) is False

    def test_is_falsy_in_if(self) -> None:
        triggered = False
        if MISSING:
            triggered = True
        assert triggered is False

    # --- Repr -----------------------------------------------------------------

    def test_repr_is_missing(self) -> None:
        assert repr(MISSING) == "MISSING"

    def test_str_is_missing(self) -> None:
        assert str(MISSING) == "MISSING"

    # --- Inequality with common falsy values ----------------------------------

    def test_not_equal_to_none(self) -> None:
        assert MISSING != None  # noqa: E711
        assert MISSING is not None

    def test_not_equal_to_zero(self) -> None:
        assert MISSING != 0

    def test_not_equal_to_false(self) -> None:
        assert MISSING is not False

    def test_not_equal_to_empty_string(self) -> None:
        assert MISSING != ""

    def test_not_equal_to_empty_list(self) -> None:
        assert MISSING != []

    # --- Copy survival --------------------------------------------------------

    def test_copy_returns_same_singleton(self) -> None:
        assert copy.copy(MISSING) is MISSING

    def test_deepcopy_returns_same_singleton(self) -> None:
        assert copy.deepcopy(MISSING) is MISSING

    def test_deepcopy_nested_in_dict_preserves_singleton(self) -> None:
        d = {"key": MISSING}
        d2 = copy.deepcopy(d)
        assert d2["key"] is MISSING

    # --- Identity check pattern (engine usage) --------------------------------

    def test_is_check_distinguishes_from_none(self) -> None:
        assert (MISSING is None) is False

    def test_is_check_detects_missing(self) -> None:
        value: object = MISSING
        assert (value is MISSING) is True

    def test_none_is_not_missing(self) -> None:
        value: object = None
        assert (value is MISSING) is False


# ===========================================================================
# FieldSpec
# ===========================================================================


class TestFieldSpec:

    # --- Minimal construction -------------------------------------------------

    def test_minimal_construction(self) -> None:
        f = FieldSpec(path="temperature", field_type=FieldType.FLOAT)
        assert f.path == "temperature"
        assert f.field_type is FieldType.FLOAT

    def test_default_required_is_true(self) -> None:
        f = FieldSpec("x", FieldType.STRING)
        assert f.required is True

    def test_default_default_is_missing(self) -> None:
        f = FieldSpec("x", FieldType.STRING)
        assert f.default is MISSING

    def test_default_constraints_is_empty_list(self) -> None:
        f = FieldSpec("x", FieldType.STRING)
        assert f.constraints == []

    def test_default_known_aliases_is_empty_list(self) -> None:
        f = FieldSpec("x", FieldType.STRING)
        assert f.known_aliases == []

    def test_default_item_type_is_none(self) -> None:
        f = FieldSpec("x", FieldType.ARRAY)
        assert f.item_type is None

    def test_default_nested_spec_is_none(self) -> None:
        f = FieldSpec("x", FieldType.OBJECT)
        assert f.nested_spec is None

    # --- Full construction ----------------------------------------------------

    def test_full_construction(self) -> None:
        c = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        f = FieldSpec(
            path="age",
            field_type=FieldType.INTEGER,
            required=True,
            default=0,
            constraints=[c],
            known_aliases=["user_age"],
            item_type=None,
            nested_spec=None,
        )
        assert f.path == "age"
        assert f.field_type is FieldType.INTEGER
        assert f.required is True
        assert f.default == 0
        assert f.constraints == [c]
        assert f.known_aliases == ["user_age"]

    def test_explicit_none_default_is_distinct_from_missing(self) -> None:
        f = FieldSpec("x", FieldType.STRING, default=None)
        assert f.default is None
        assert f.default is not MISSING

    def test_optional_field(self) -> None:
        f = FieldSpec("x", FieldType.STRING, required=False)
        assert f.required is False

    # --- Path validation ------------------------------------------------------

    def test_empty_path_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            FieldSpec(path="", field_type=FieldType.STRING)

    def test_nonempty_path_passes(self) -> None:
        f = FieldSpec(path="a", field_type=FieldType.STRING)
        assert f.path == "a"

    def test_dot_notation_path(self) -> None:
        f = FieldSpec(path="address.city", field_type=FieldType.STRING)
        assert f.path == "address.city"

    def test_deeply_nested_path(self) -> None:
        f = FieldSpec(path="a.b.c.d", field_type=FieldType.STRING)
        assert f.path == "a.b.c.d"

    # --- Mutable list fields not shared between instances ---------------------

    def test_constraints_not_shared(self) -> None:
        f1 = FieldSpec("x", FieldType.STRING)
        f2 = FieldSpec("y", FieldType.STRING)
        f1.constraints.append(FieldConstraint(FieldConstraintType.NOT_NULL, True))
        assert f2.constraints == []

    def test_known_aliases_not_shared(self) -> None:
        f1 = FieldSpec("x", FieldType.STRING)
        f2 = FieldSpec("y", FieldType.STRING)
        f1.known_aliases.append("alias_x")
        assert f2.known_aliases == []

    # --- Nested spec ----------------------------------------------------------

    def test_nested_spec_field(self) -> None:
        inner = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)])
        outer = FieldSpec("address", FieldType.OBJECT, nested_spec=inner)
        assert outer.nested_spec is inner
        assert outer.nested_spec.fields[0].path == "city"

    # --- All FieldTypes constructable -----------------------------------------

    @pytest.mark.parametrize("ft", list(FieldType))
    def test_every_field_type_is_accepted(self, ft: FieldType) -> None:
        f = FieldSpec(path="field", field_type=ft)
        assert f.field_type is ft

    # --- deepcopy preserves MISSING identity ----------------------------------

    def test_deepcopy_preserves_missing_sentinel(self) -> None:
        f = FieldSpec("x", FieldType.STRING)
        f_copy = copy.deepcopy(f)
        assert f_copy.default is MISSING


# ===========================================================================
# ContractSpec
# ===========================================================================


class TestContractSpec:

    # --- Basic construction ---------------------------------------------------

    def test_minimal_construction(self) -> None:
        c = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        assert len(c.fields) == 1
        assert c.fields[0].path == "x"

    def test_default_source_ref_is_none(self) -> None:
        c = ContractSpec(fields=[])
        assert c.source_ref is None

    def test_default_strict_mode_is_false(self) -> None:
        c = ContractSpec(fields=[])
        assert c.strict_mode is False

    def test_source_ref_stores_arbitrary_object(self) -> None:
        sentinel = object()
        c = ContractSpec(fields=[], source_ref=sentinel)
        assert c.source_ref is sentinel

    def test_source_ref_stores_string(self) -> None:
        c = ContractSpec(fields=[], source_ref="MyModel")
        assert c.source_ref == "MyModel"

    def test_source_ref_stores_class(self) -> None:
        class FakeModel:
            pass
        c = ContractSpec(fields=[], source_ref=FakeModel)
        assert c.source_ref is FakeModel

    def test_strict_mode_true(self) -> None:
        c = ContractSpec(fields=[], strict_mode=True)
        assert c.strict_mode is True

    # --- contract_id auto-generation ------------------------------------------

    def test_contract_id_is_auto_generated(self) -> None:
        c = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        assert c.contract_id != ""
        assert isinstance(c.contract_id, str)

    def test_contract_id_is_16_hex_chars(self) -> None:
        c = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        assert len(c.contract_id) == 16
        assert all(ch in "0123456789abcdef" for ch in c.contract_id)

    def test_explicit_contract_id_is_preserved(self) -> None:
        c = ContractSpec(fields=[], contract_id="my-custom-id-001")
        assert c.contract_id == "my-custom-id-001"

    def test_explicit_contract_id_prevents_auto_generation(self) -> None:
        fixed = "abcdef1234567890"
        c = ContractSpec(fields=[FieldSpec("temp", FieldType.FLOAT)], contract_id=fixed)
        assert c.contract_id == fixed

    # --- contract_id determinism ----------------------------------------------

    def test_contract_id_is_deterministic_same_fields(self) -> None:
        fields = [FieldSpec("temperature", FieldType.FLOAT)]
        c1 = ContractSpec(fields=fields)
        c2 = ContractSpec(fields=fields)
        assert c1.contract_id == c2.contract_id

    def test_contract_id_is_deterministic_reconstructed_fields(self) -> None:
        c1 = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
            FieldSpec("humidity", FieldType.INTEGER),
        ])
        c2 = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
            FieldSpec("humidity", FieldType.INTEGER),
        ])
        assert c1.contract_id == c2.contract_id

    def test_contract_id_is_insertion_order_independent(self) -> None:
        """
        Fields in different order must yield the same contract_id because
        _generate_contract_id sorts by path before hashing.
        """
        f_temp = FieldSpec("temperature", FieldType.FLOAT)
        f_hum = FieldSpec("humidity", FieldType.INTEGER)
        c1 = ContractSpec(fields=[f_temp, f_hum])
        c2 = ContractSpec(fields=[f_hum, f_temp])
        assert c1.contract_id == c2.contract_id

    def test_contract_id_differs_on_different_paths(self) -> None:
        c1 = ContractSpec(fields=[FieldSpec("a", FieldType.STRING)])
        c2 = ContractSpec(fields=[FieldSpec("b", FieldType.STRING)])
        assert c1.contract_id != c2.contract_id

    def test_contract_id_differs_on_different_types(self) -> None:
        c1 = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        c2 = ContractSpec(fields=[FieldSpec("x", FieldType.INTEGER)])
        assert c1.contract_id != c2.contract_id

    def test_contract_id_differs_on_required_vs_optional(self) -> None:
        c1 = ContractSpec(fields=[FieldSpec("x", FieldType.STRING, required=True)])
        c2 = ContractSpec(fields=[FieldSpec("x", FieldType.STRING, required=False)])
        assert c1.contract_id != c2.contract_id

    def test_contract_id_differs_on_strict_mode(self) -> None:
        fields = [FieldSpec("x", FieldType.STRING)]
        c1 = ContractSpec(fields=fields, strict_mode=False)
        c2 = ContractSpec(fields=fields, strict_mode=True)
        assert c1.contract_id != c2.contract_id

    def test_contract_id_for_empty_schema_is_stable(self) -> None:
        c1 = ContractSpec(fields=[])
        c2 = ContractSpec(fields=[])
        assert c1.contract_id == c2.contract_id

    # --- contract_id hash verification ----------------------------------------

    def test_contract_id_matches_expected_sha256(self) -> None:
        """Verify the exact hashing algorithm so it can't silently change."""
        c = ContractSpec(fields=[FieldSpec("temperature", FieldType.FLOAT)])
        canonical = "strict=0;temperature:float:1"
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        assert c.contract_id == expected

    def test_contract_id_with_multiple_fields_matches_sorted_hash(self) -> None:
        c = ContractSpec(fields=[
            FieldSpec("z_field", FieldType.BOOLEAN),
            FieldSpec("a_field", FieldType.INTEGER),
        ])
        # Fields sorted by path: a_field, z_field
        canonical = "strict=0;a_field:integer:1;z_field:boolean:1"
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        assert c.contract_id == expected

    # --- Multi-field contracts ------------------------------------------------

    def test_multiple_fields_stored(self) -> None:
        c = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
            FieldSpec("humidity", FieldType.INTEGER),
            FieldSpec("description", FieldType.STRING, required=False),
        ])
        assert len(c.fields) == 3

    # --- Nested contracts -----------------------------------------------------

    def test_nested_contract_via_field_spec(self) -> None:
        inner = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)])
        outer_field = FieldSpec("address", FieldType.OBJECT, nested_spec=inner)
        outer = ContractSpec(fields=[outer_field])
        assert outer.fields[0].nested_spec is inner
        assert outer.fields[0].nested_spec.fields[0].path == "city"

    def test_deeply_nested_contracts(self) -> None:
        level3 = ContractSpec(fields=[FieldSpec("code", FieldType.STRING)])
        level2_field = FieldSpec("postal", FieldType.OBJECT, nested_spec=level3)
        level2 = ContractSpec(fields=[level2_field])
        level1_field = FieldSpec("address", FieldType.OBJECT, nested_spec=level2)
        level1 = ContractSpec(fields=[level1_field])
        assert (
            level1.fields[0]
            .nested_spec.fields[0]  # type: ignore[union-attr]
            .nested_spec.fields[0]  # type: ignore[union-attr]
            .path == "code"
        )
