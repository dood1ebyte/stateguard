"""
PydanticContractExtractor — converts ``type[BaseModel]`` into ``ContractSpec``.

This is the inward-translation half of the Pydantic adapter: it walks
``model_class.model_fields`` and produces a normalised ``ContractSpec`` that
the core engine can reason about, with zero further Pydantic dependencies
once extraction is complete.

Alias resolution (``path`` vs ``known_aliases``)
--------------------------------------------------
For a field with ``Field(alias="temp_c")`` (default ``populate_by_name=False``),
Pydantic's ``model_validate`` expects the **alias** (``"temp_c"``) as the
input key -- the Python attribute name (``"temperature"``) is *not* accepted
and would be treated as an extra field.

To keep ``ContractSpec.fields[i].path`` consistent with what
``model_validate`` actually expects (so that after a successful repair,
``IContractAdapter.wrap`` succeeds), this extractor sets:

* ``path`` = the field's ``validation_alias`` (or ``alias``, if
  ``validation_alias`` is unset) when it is a plain string different from
  the Python attribute name; otherwise the Python attribute name.
* ``known_aliases`` = ``[<python attribute name>]`` whenever ``path`` is an
  alias -- so ``ExactAliasStrategy`` can repair tool output that used the
  Python attribute name instead of the alias.

``AliasChoices`` / ``AliasPath`` (Pydantic's multi-alias constructs) are not
supported in V1; if ``validation_alias``/``alias`` is not a plain ``str``,
it is ignored and ``path`` falls back to the attribute name.

Constraints
-----------
Only ``Ge``/``Le`` (-> ``MINIMUM``/``MAXIMUM``), ``MinLen``/``MaxLen``
(-> ``MIN_LENGTH``/``MAX_LENGTH``), and pattern metadata
(-> ``PATTERN``) are extracted.  ``Gt``/``Lt`` (strict inequality) have no
corresponding ``FieldConstraintType`` in V1 and are not extracted -- this is
a documented V1 limitation.

``Literal[...]`` annotations produce an ``ENUM_VALUES`` constraint from the
literal's value tuple, in addition to the ``FieldType`` inferred by
``PydanticTypeMapper``.

Defaults
--------
``FieldInfo.default`` is used when not ``PydanticUndefined``.  If
``default`` is ``PydanticUndefined`` but ``default_factory`` is set, the
factory is called once at extraction time and its result becomes
``FieldSpec.default``.  Otherwise ``FieldSpec.default`` is ``MISSING``.

Nested models
--------------
A field whose (optional-unwrapped) annotation is a ``BaseModel`` subclass
gets ``field_type=OBJECT`` and a recursively extracted ``nested_spec``.

Per V1 scope, ``List[SomeModel]`` (arrays of nested models) does **not**
produce a ``nested_spec`` -- ``item_type`` is reported as ``OBJECT`` by
``PydanticTypeMapper.get_item_type``, but per-item fields are not validated.

Recursive schemas (a model that references itself, directly or via a
cycle) are not guarded against and will cause unbounded recursion. This is
an accepted V1 limitation per the finalized scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stateguard.adapters.pydantic.type_mapper import PydanticTypeMapper
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldConstraint, FieldConstraintType

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic.fields import FieldInfo

__all__ = ["PydanticContractExtractor"]


class PydanticContractExtractor:
    """Extracts a normalised ``ContractSpec`` from a Pydantic ``BaseModel``."""

    @classmethod
    def extract(cls, model_class: type[BaseModel]) -> ContractSpec:
        """
        Build a ``ContractSpec`` from *model_class*.

        Parameters
        ----------
        model_class:
            A ``type[BaseModel]`` subclass.

        Returns
        -------
        ContractSpec
            ``source_ref`` is set to *model_class* so that
            ``PydanticAdapter.wrap`` can rehydrate instances later.
        """
        fields: list[FieldSpec] = [
            cls._extract_field(field_name, field_info)
            for field_name, field_info in model_class.model_fields.items()
        ]
        return ContractSpec(fields=fields, source_ref=model_class)

    # ------------------------------------------------------------------
    # Per-field extraction
    # ------------------------------------------------------------------

    @classmethod
    def _extract_field(cls, field_name: str, field_info: FieldInfo) -> FieldSpec:
        annotation = field_info.annotation

        path, known_aliases = cls._resolve_path_and_aliases(field_name, field_info)

        field_type = PydanticTypeMapper.map_annotation(annotation)
        item_type = PydanticTypeMapper.get_item_type(annotation)
        nested_model = PydanticTypeMapper.get_nested_model(annotation)
        nested_spec = cls.extract(nested_model) if nested_model is not None else None

        return FieldSpec(
            path=path,
            field_type=field_type,
            required=field_info.is_required(),
            default=cls._resolve_default(field_info),
            constraints=cls._extract_constraints(field_info, annotation),
            known_aliases=known_aliases,
            item_type=item_type,
            nested_spec=nested_spec,
        )

    # ------------------------------------------------------------------
    # Alias resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_path_and_aliases(
        field_name: str,
        field_info: FieldInfo,
    ) -> tuple[str, list[str]]:
        """
        Determine ``(path, known_aliases)`` for a field.

        See the module docstring for the alias-resolution rationale.
        """
        candidate = field_info.validation_alias
        if candidate is None:
            candidate = field_info.alias

        if isinstance(candidate, str) and candidate != field_name:
            return candidate, [field_name]

        return field_name, []

    # ------------------------------------------------------------------
    # Default resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_default(field_info: FieldInfo) -> Any:
        """
        Determine ``FieldSpec.default`` for a field.

        Returns ``MISSING`` if neither ``default`` nor ``default_factory``
        is set.  If ``default_factory`` is set, it is invoked once here.
        """
        from pydantic_core import PydanticUndefined  # noqa: PLC0415

        if field_info.default is not PydanticUndefined:
            return field_info.default

        if field_info.default_factory is not None:
            # Pydantic v2's default_factory is a zero-argument callable for
            # the vast majority of cases (list, dict, lambda: ...). V1 does
            # not support the rarer "validated-data-aware" factory signature.
            return field_info.default_factory()  # type: ignore[call-arg]

        return MISSING

    # ------------------------------------------------------------------
    # Constraint extraction
    # ------------------------------------------------------------------

    @classmethod
    def _extract_constraints(
        cls,
        field_info: FieldInfo,
        annotation: Any,
    ) -> list[FieldConstraint]:
        constraints: list[FieldConstraint] = []

        for meta in field_info.metadata:
            if hasattr(meta, "ge"):
                constraints.append(
                    FieldConstraint(FieldConstraintType.MINIMUM, meta.ge)
                )
            elif hasattr(meta, "le"):
                constraints.append(
                    FieldConstraint(FieldConstraintType.MAXIMUM, meta.le)
                )
            elif hasattr(meta, "min_length"):
                constraints.append(
                    FieldConstraint(FieldConstraintType.MIN_LENGTH, meta.min_length)
                )
            elif hasattr(meta, "max_length"):
                constraints.append(
                    FieldConstraint(FieldConstraintType.MAX_LENGTH, meta.max_length)
                )
            elif hasattr(meta, "pattern") and meta.pattern is not None:
                constraints.append(
                    FieldConstraint(FieldConstraintType.PATTERN, meta.pattern)
                )
            # Gt / Lt (strict inequality) intentionally not extracted --
            # no corresponding FieldConstraintType in V1.

        literal_values = PydanticTypeMapper.get_literal_values(annotation)
        if literal_values is not None:
            constraints.append(
                FieldConstraint(FieldConstraintType.ENUM_VALUES, literal_values)
            )

        return constraints
