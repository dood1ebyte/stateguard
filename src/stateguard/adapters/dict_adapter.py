"""
DictContractAdapter -- ``IContractAdapter`` for StateGuard's own simple,
internal JSON contract format.

This is the "plain dict adapter" anticipated by the original architecture
document's extensibility review: it uses the existing ``IContractAdapter``
extension point exactly as designed, contributing no new abstractions.

It exists to give the CLI's ``--schema`` flag and the benchmark harness's
``expected_schema`` field a concrete, framework-agnostic way to describe a
contract without requiring Pydantic (or any other framework) to be
installed.

This is NOT JSON Schema
-------------------------
Despite superficial similarity, this is **not** an implementation of the
real JSON Schema specification (no ``$ref``, no ``oneOf``/``anyOf``, no
``additionalProperties`` semantics beyond a single ``strict_mode`` flag,
etc.). It is StateGuard's own minimal format, chosen to map directly onto
``FieldSpec``/``ContractSpec`` with no impedance mismatch. A real
``JSONSchemaAdapter`` remains a credible future addition (see
``M9_AUDIT.md`` M10 recommendations) but is out of scope here.

Format
------
::

    {
      "strict_mode": false,
      "fields": [
        {
          "path": "temperature",
          "type": "float",
          "required": true
        },
        {
          "path": "humidity",
          "type": "integer",
          "required": false,
          "default": 60
        },
        {
          "path": "tags",
          "type": "array",
          "item_type": "string",
          "default": []
        },
        {
          "path": "address",
          "type": "object",
          "nested": {
            "fields": [
              {"path": "city", "type": "string"},
              {"path": "zip_code", "type": "string", "known_aliases": ["zip"]}
            ]
          }
        },
        {
          "path": "score",
          "type": "integer",
          "constraints": [
            {"type": "minimum", "value": 0},
            {"type": "maximum", "value": 100}
          ]
        }
      ]
    }

Top-level keys: ``"strict_mode"`` (bool, default ``false``) and
``"fields"`` (required, list of field dicts).

Per-field keys: ``"path"`` (required), ``"type"`` (required; one of the
``FieldType`` values: ``string``, ``integer``, ``float``, ``boolean``,
``object``, ``array``, ``any``, ``null``), ``"required"`` (bool, default
``true``), ``"default"`` (any JSON value; absent means no default),
``"known_aliases"`` (list of strings), ``"item_type"`` (a ``FieldType``
string, for ``"type": "array"`` fields), ``"nested"`` (a recursive schema
object with its own ``"fields"`` key, for ``"type": "object"`` fields --
per V1 scope, arrays of nested objects are not supported, matching the
same limitation documented for the Pydantic adapter), and
``"constraints"`` (list of ``{"type": ..., "value": ...}`` dicts, where
``"type"`` is one of the ``FieldConstraintType`` values).
"""

from __future__ import annotations

from typing import Any

from stateguard.core.errors.results import ValidationResult
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec
from stateguard.core.models.field_types import (
    FieldConstraint,
    FieldConstraintType,
    FieldType,
)
from stateguard.core.validator import ContractValidator

__all__ = ["DictContractAdapter"]


class DictContractAdapter(IContractAdapter):
    """
    ``IContractAdapter`` implementation for StateGuard's own simple JSON
    contract format (see module docstring).

    Since there is no "native" validator for a plain dict-based schema,
    ``validate`` delegates entirely to the framework-agnostic
    ``ContractValidator`` -- making this adapter's behaviour fully
    predictable and exercised by the same validator already covered by
    the core test suite.

    ``wrap`` returns the data dict unchanged: there is no framework-native
    type to rehydrate into.
    """

    def __init__(self) -> None:
        self._validator = ContractValidator()

    # ------------------------------------------------------------------
    # IContractAdapter
    # ------------------------------------------------------------------

    def extract_contract(self, schema: Any) -> ContractSpec:
        """
        Parse *schema* (a ``dict`` in StateGuard's simple JSON contract
        format) into a ``ContractSpec``.

        Raises
        ------
        TypeError
            If *schema* is not a ``dict``.
        ValueError
            If *schema* is structurally invalid (missing ``"fields"``,
            a field missing ``"path"``/``"type"``, an unrecognized type
            or constraint-type string, etc.).
        """
        if not isinstance(schema, dict):
            raise TypeError(
                "DictContractAdapter.extract_contract expects a dict, "
                f"got {type(schema).__name__!r}."
            )

        if "fields" not in schema:
            raise ValueError(
                "DictContractAdapter schema must have a top-level "
                "'fields' key (a list of field definitions)."
            )

        fields = [self._parse_field(f) for f in schema["fields"]]
        strict_mode = bool(schema.get("strict_mode", False))
        return ContractSpec(fields=fields, strict_mode=strict_mode)

    def validate(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> ValidationResult:
        """Delegate entirely to ``ContractValidator`` -- there is no
        framework-native validator for a plain dict schema."""
        return self._validator.validate(contract, data)

    def wrap(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a copy of *data* unchanged -- no rehydration needed."""
        return dict(data)

    # ------------------------------------------------------------------
    # Internal: field/constraint parsing
    # ------------------------------------------------------------------

    @classmethod
    def _parse_field(cls, field_dict: dict[str, Any]) -> FieldSpec:
        if "path" not in field_dict:
            raise ValueError(f"Field definition missing required 'path': {field_dict!r}")
        if "type" not in field_dict:
            raise ValueError(
                f"Field definition for '{field_dict['path']}' missing required 'type'."
            )

        path = field_dict["path"]
        field_type = cls._parse_field_type(field_dict["type"], path)
        required = bool(field_dict.get("required", True))
        default = field_dict.get("default", MISSING)
        known_aliases = list(field_dict.get("known_aliases", []))
        constraints = [cls._parse_constraint(c, path) for c in field_dict.get("constraints", [])]

        item_type: FieldType | None = None
        if "item_type" in field_dict:
            item_type = cls._parse_field_type(field_dict["item_type"], path)

        nested_spec: ContractSpec | None = None
        if "nested" in field_dict:
            nested_schema = field_dict["nested"]
            if not isinstance(nested_schema, dict) or "fields" not in nested_schema:
                raise ValueError(
                    f"Field '{path}' has a 'nested' value that is not a "
                    f"valid schema dict with a 'fields' key."
                )
            nested_fields = [cls._parse_field(f) for f in nested_schema["fields"]]
            nested_spec = ContractSpec(
                fields=nested_fields,
                strict_mode=bool(nested_schema.get("strict_mode", False)),
            )

        return FieldSpec(
            path=path,
            field_type=field_type,
            required=required,
            default=default,
            constraints=constraints,
            known_aliases=known_aliases,
            item_type=item_type,
            nested_spec=nested_spec,
        )

    @staticmethod
    def _parse_field_type(type_str: Any, path: str) -> FieldType:
        if not isinstance(type_str, str):
            raise ValueError(f"Field '{path}': 'type' must be a string, got {type_str!r}.")
        try:
            return FieldType(type_str)
        except ValueError as exc:
            valid = ", ".join(f"'{t.value}'" for t in FieldType)
            raise ValueError(
                f"Field '{path}': unrecognized type {type_str!r}. Valid types are: {valid}."
            ) from exc

    @staticmethod
    def _parse_constraint(constraint_dict: dict[str, Any], path: str) -> FieldConstraint:
        if "type" not in constraint_dict or "value" not in constraint_dict:
            raise ValueError(
                f"Field '{path}': each constraint must have 'type' and "
                f"'value' keys, got {constraint_dict!r}."
            )
        type_str = constraint_dict["type"]
        try:
            constraint_type = FieldConstraintType(type_str)
        except ValueError as exc:
            valid = ", ".join(f"'{t.value}'" for t in FieldConstraintType)
            raise ValueError(
                f"Field '{path}': unrecognized constraint type {type_str!r}. "
                f"Valid constraint types are: {valid}."
            ) from exc

        value = constraint_dict["value"]
        # ENUM_VALUES must be a tuple to match FieldConstraint's expected
        # shape elsewhere in the codebase (e.g. Pydantic Literal extraction).
        if constraint_type is FieldConstraintType.ENUM_VALUES and isinstance(value, list):
            value = tuple(value)

        return FieldConstraint(constraint_type, value)
