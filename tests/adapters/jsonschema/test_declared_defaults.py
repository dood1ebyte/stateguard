"""
Screening of declared ``default`` values.

A default is the one thing in a schema that StateGuard *writes* rather than
merely checks. ``JSONSchemaAdapter.wrap`` materialises it into the payload,
and it runs after the engine has finished -- so nothing downstream re-checks
what it wrote.

That made an unusable default worse than a dropped constraint. A schema
declaring ``{"type": "integer", "default": "abc"}`` had ``repair`` return
``ALREADY_VALID`` and ``validate`` then reject the very output it produced.
Not hypothetical drift either: a default left behind when a server's ``enum``
changed is exactly the failure this adapter exists for.

The engine already refuses to do this on the path it controls --
``DefaultValueFillStrategy`` fills, revalidates, and fails the repair when
the result does not hold up. These tests pin the same guarantee onto the path
that bypasses it.

Dropped with a warning rather than refused: an unusable default does not stop
the schema being *read*, validation is unaffected either way, and refusing
the whole document would fail hardest against precisely the drifted servers
this adapter is for. The field simply stops being auto-filled.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from stateguard import ContractGuard
from stateguard.adapters.jsonschema import JSONSchemaExtractor, SchemaFeatureWarning
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec


def _extract(schema: dict[str, Any]) -> ContractSpec:
    return JSONSchemaExtractor().extract(schema)


def _field(spec: ContractSpec, name: str) -> FieldSpec:
    for candidate in spec.fields:
        if candidate.path == name:
            return candidate
    raise AssertionError(f"no field {name!r} in {[f.path for f in spec.fields]}")


def _schema(field: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"a": {"type": "string"}, "n": field},
        "required": ["a"],
    }


class TestADefaultThatFailsItsOwnFieldIsDropped:
    @pytest.mark.parametrize(
        "field",
        [
            {"type": "integer", "default": "not-an-int"},
            {"type": "integer", "minimum": 10, "default": 0},
            {"type": "string", "minLength": 5, "default": "ab"},
            {"type": "string", "enum": ["celsius", "fahrenheit"], "default": "kelvin"},
        ],
        ids=["wrong-type", "below-minimum", "too-short", "outside-enum"],
    )
    def test_it_is_dropped(self, field: dict[str, Any]) -> None:
        with pytest.warns(SchemaFeatureWarning, match="does not satisfy"):
            spec = _extract(_schema(field))
        assert _field(spec, "n").default is MISSING

    def test_the_warning_carries_the_validators_own_reason(self) -> None:
        """
        So a schema author can see *which* constraint the default missed,
        rather than only that something was dropped.
        """
        with pytest.warns(SchemaFeatureWarning, match="not one of the allowed values"):
            _extract(
                _schema({"type": "string", "enum": ["celsius", "fahrenheit"], "default": "kelvin"})
            )

    def test_a_nested_fields_default_is_screened_too(self) -> None:
        with pytest.warns(SchemaFeatureWarning, match="does not satisfy"):
            spec = _extract(
                {
                    "type": "object",
                    "properties": {
                        "u": {
                            "type": "object",
                            "properties": {"n": {"type": "integer", "default": "bad"}},
                        }
                    },
                }
            )
        nested = _field(spec, "u").nested_spec
        assert nested is not None
        assert _field(nested, "n").default is MISSING


class TestUsableDefaultsAreUntouched:
    """The screen must not cost the feature it is protecting."""

    def test_a_valid_default_still_fills(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", SchemaFeatureWarning)
            spec = _extract(_schema({"type": "string", "enum": ["celsius"], "default": "celsius"}))
        assert _field(spec, "n").default == "celsius"

    def test_a_declared_null_default_survives_on_a_nullable_field(self) -> None:
        """
        ``default: null`` is a real declared default, distinct from having
        none at all -- ``DefaultValueFillStrategy`` reads exactly that
        difference. On a nullable field it is perfectly valid, and the screen
        must not mistake it for absence.
        """
        spec = _extract(
            {"type": "object", "properties": {"n": {"type": ["string", "null"], "default": None}}}
        )
        assert _field(spec, "n").default is None

    def test_no_declared_default_stays_missing(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", SchemaFeatureWarning)
            spec = _extract(_schema({"type": "integer"}))
        assert _field(spec, "n").default is MISSING

    def test_an_object_default_is_screened_against_its_nested_spec(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "u": {
                    "type": "object",
                    "properties": {"n": {"type": "string"}},
                    "required": ["n"],
                    "default": {"n": "bob"},
                }
            },
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error", SchemaFeatureWarning)
            spec = _extract(schema)
        assert _field(spec, "u").default == {"n": "bob"}


class TestTheEndToEndProperty:
    """
    The property all of this exists for: whatever StateGuard hands back must
    pass the contract StateGuard just certified.
    """

    def test_output_revalidates_against_the_schema_it_came_from(self) -> None:
        schema = _schema({"type": "integer", "minimum": 10, "default": 0})
        guard = ContractGuard.with_json_schema()

        with pytest.warns(SchemaFeatureWarning):
            result = guard.repair(schema, {"a": "x"})
        with pytest.warns(SchemaFeatureWarning):
            assert guard.validate(schema, result.repaired_output).is_valid

    def test_the_unusable_default_is_simply_absent_from_the_output(self) -> None:
        schema = _schema({"type": "integer", "minimum": 10, "default": 0})
        guard = ContractGuard.with_json_schema()
        with pytest.warns(SchemaFeatureWarning):
            result = guard.repair(schema, {"a": "x"})
        assert result.repaired_output == {"a": "x"}

    def test_a_usable_default_still_reaches_the_output(self) -> None:
        schema = _schema({"type": "string", "enum": ["celsius"], "default": "celsius"})
        guard = ContractGuard.with_json_schema()
        result = guard.repair(schema, {"a": "x"})
        assert result.repaired_output == {"a": "x", "n": "celsius"}
