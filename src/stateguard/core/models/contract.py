"""
Contract model: FieldSpec and ContractSpec.

These are the normalised, framework-agnostic representations of a schema
that the core engine reasons over.  Adapters produce a ``ContractSpec``
from their native schema format (Pydantic BaseModel, JSON Schema dict, etc.);
the engine never touches the original schema objects.

This module has zero external dependencies and is part of Layer 1
(depends on ``stateguard.core.models.field_types``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Final

from stateguard.core.models.field_types import FieldConstraint, FieldType

__all__ = [
    "MISSING",
    "ContractSpec",
    "FieldSpec",
]


# ---------------------------------------------------------------------------
# MISSING sentinel
# ---------------------------------------------------------------------------


class _MissingSentinel:
    """
    Singleton sentinel representing the absence of a declared default value.

    ``MISSING`` is the default for ``FieldSpec.default``.  It allows
    ``DefaultValueFillStrategy`` and the engine to distinguish between
    "this field has a default of ``None``" (``default=None``) and
    "this field has no default at all" (``default=MISSING``).

    Usage::

        if field_spec.default is MISSING:
            # no default — cannot use DefaultValueFillStrategy
            ...

    Characteristics
    ---------------
    * Singleton — ``_MissingSentinel() is MISSING`` is always ``True``.
    * Falsy — ``bool(MISSING)`` is ``False``.
    * Survives ``copy.copy`` and ``copy.deepcopy`` as the same singleton,
      so deep-copied ``FieldSpec`` objects retain identity equality on
      their ``default`` field.
    """

    _instance: _MissingSentinel | None = None

    def __new__(cls) -> _MissingSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> _MissingSentinel:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _MissingSentinel:
        return self


#: Singleton sentinel. Use ``is`` comparison: ``field_spec.default is MISSING``.
MISSING: Final[_MissingSentinel] = _MissingSentinel()


# ---------------------------------------------------------------------------
# FieldSpec
# ---------------------------------------------------------------------------


@dataclass
class FieldSpec:
    """
    Describes a single expected field within a contract.

    Attributes
    ----------
    path:
        Dot-notation path to this field relative to the contract root
        (e.g. ``"temperature"``, ``"address.city"``).
        Must be a non-empty string.
    field_type:
        The abstract type of this field's value.  Adapters map their native
        type systems to ``FieldType``.
    required:
        Whether this field must be present in the data.  Default ``True``.
        Optional fields (``required=False``) do not produce
        ``MISSING_REQUIRED_FIELD`` violations when absent.
    default:
        The value to write when this field is absent and
        ``DefaultValueFillStrategy`` is applicable.
        ``MISSING`` (the sentinel) means no default is declared.
        ``None`` is a valid explicit default distinct from ``MISSING``.
    constraints:
        Zero or more ``FieldConstraint`` objects applied to the field value
        beyond type-checking (e.g. minimum, max_length, pattern).
    known_aliases:
        Alternative names for this field in the input data, populated by
        the adapter from schema metadata (e.g. Pydantic ``Field(alias=...)``).
        Used exclusively by ``ExactAliasStrategy``.
    item_type:
        For ``FieldType.ARRAY`` fields, the ``FieldType`` of each element.
        ``None`` for non-array fields.  V1 does not support arrays of nested
        models; ``nested_spec`` is unused when ``item_type`` is set.
    nested_spec:
        For ``FieldType.OBJECT`` fields, the sub-``ContractSpec`` describing
        the nested structure.  ``None`` for non-object fields.
    """

    # Required
    path: str
    field_type: FieldType

    # Optional with defaults
    required: bool = True
    default: Any = field(default_factory=lambda: MISSING)
    constraints: list[FieldConstraint] = field(default_factory=list)
    known_aliases: list[str] = field(default_factory=list)
    item_type: FieldType | None = None
    nested_spec: ContractSpec | None = None

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError(
                "FieldSpec.path must be a non-empty string. "
                "Use dot-notation for nested fields (e.g. 'address.city')."
            )


# ---------------------------------------------------------------------------
# ContractSpec
# ---------------------------------------------------------------------------


@dataclass
class ContractSpec:
    """
    The normalised, framework-agnostic representation of a complete schema.

    Attributes
    ----------
    fields:
        All fields the contract declares.  Order does not affect behaviour;
        the validator and engine access fields by ``path``.
    source_ref:
        Opaque back-reference to the original framework schema — e.g. the
        Pydantic ``type[BaseModel]`` class.  The engine **never reads this**.
        Adapters receive it back in ``IContractAdapter.wrap()`` to rehydrate
        the final repaired object into the framework-native type.
    strict_mode:
        When ``True``, keys in the data that have no corresponding
        ``FieldSpec`` produce ``ViolationSeverity.ERROR`` violations.
        When ``False`` (default), they produce ``ViolationSeverity.WARNING``.
        Overrides ``GuardConfig.strict_mode`` at the per-contract level.
    contract_id:
        Stable 16-hex-character identifier derived from the field
        definitions.  Auto-generated if not provided.  Two ``ContractSpec``
        objects with identical fields (same paths, types, required flags, and
        strict_mode) produce the same ``contract_id`` regardless of the order
        in which fields were added.

        Pass an explicit value to pin a specific ID (useful in tests and
        for schemas that change rarely and need a stable log reference).
    """

    fields: list[FieldSpec]
    source_ref: Any = None
    strict_mode: bool = False
    contract_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.contract_id:
            self.contract_id = self._generate_contract_id()

    def _generate_contract_id(self) -> str:
        """
        Produce a deterministic 16-character hex ID from the field definitions.

        Algorithm
        ---------
        1. Sort fields by ``path`` (removes insertion-order dependence).
        2. For each field emit ``"path:type:required"`` as a single token.
        3. Prefix with the ``strict_mode`` flag.
        4. SHA-256 the UTF-8 encoded canonical string.
        5. Return the first 16 hex digits.
        """
        sorted_fields = sorted(self.fields, key=lambda f: f.path)
        parts: list[str] = [f"strict={int(self.strict_mode)}"]
        for f in sorted_fields:
            parts.append(f"{f.path}:{f.field_type.value}:{int(f.required)}")
        canonical = ";".join(parts)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
