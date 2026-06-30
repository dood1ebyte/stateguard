"""
PydanticTypeMapper — maps Python/typing annotations to ``FieldType``.

This is a pure mapping utility: no state, all static methods.  It is the
only place in the Pydantic adapter that reasons about ``typing`` module
internals (``get_origin``, ``get_args``, ``Annotated``, ``Union``, ``Literal``).

V1 scope
--------
Supported annotation shapes:

* Primitives: ``str``, ``int``, ``float``, ``bool``
* ``datetime.datetime`` / ``datetime.date`` -> ``FieldType.STRING``
* ``uuid.UUID`` -> ``FieldType.STRING``
* ``Optional[X]`` / ``Union[X, None]`` -> type of ``X``, with optionality
  reported separately via ``unwrap_optional``
* ``list`` / ``List[X]`` -> ``FieldType.ARRAY`` (item type via
  ``get_item_type``)
* ``dict`` / ``Dict[K, V]`` -> ``FieldType.OBJECT``
* Nested ``BaseModel`` subclasses -> ``FieldType.OBJECT``
  (nested contract extraction is the extractor's responsibility)
* ``Literal[...]`` -> ``FieldType`` inferred from the literal values'
  common type (``STRING`` if all values are ``str``, ``INTEGER`` if all
  ``int``, etc.); literal *values* are surfaced via ``get_literal_values``
  for the extractor to build an ``ENUM_VALUES`` constraint.
* ``Annotated[X, ...]`` -> unwrapped to ``X`` before any of the above.
* ``Any`` -> ``FieldType.ANY``

Explicitly NOT supported in V1 (per finalized scope):

* ``Union`` of multiple non-``None`` types (e.g. ``Union[int, str]``) ->
  falls back to ``FieldType.ANY``.
* ``List[BaseModel]`` (arrays of nested models) -> the array's
  ``item_type`` is reported as ``FieldType.OBJECT``, but no per-item
  ``nested_spec`` is produced; item-level field validation does not occur.
* Recursive schemas (a model referencing itself, directly or via a cycle)
  are not guarded against; extracting such a model will recurse
  indefinitely. Callers must not pass recursive schemas in V1.
"""

from __future__ import annotations

import datetime
import typing
import uuid
from typing import Annotated, Any, Union

from stateguard.core.models.field_types import FieldType

__all__ = ["PydanticTypeMapper"]


# ---------------------------------------------------------------------------
# Primitive type table
# ---------------------------------------------------------------------------

_PRIMITIVE_MAP: dict[Any, FieldType] = {
    str: FieldType.STRING,
    int: FieldType.INTEGER,
    float: FieldType.FLOAT,
    bool: FieldType.BOOLEAN,
    datetime.datetime: FieldType.STRING,
    datetime.date: FieldType.STRING,
    uuid.UUID: FieldType.STRING,
    type(None): FieldType.NULL,
}

# Maps the Python type of a Literal's values to a FieldType.
_LITERAL_VALUE_TYPE_MAP: dict[type, FieldType] = {
    str: FieldType.STRING,
    int: FieldType.INTEGER,
    float: FieldType.FLOAT,
    bool: FieldType.BOOLEAN,
}


class PydanticTypeMapper:
    """Stateless mapping utilities from typing annotations to ``FieldType``."""

    # ------------------------------------------------------------------
    # Annotated[] unwrapping
    # ------------------------------------------------------------------

    @staticmethod
    def strip_annotated(annotation: Any) -> Any:
        """
        Repeatedly unwrap ``Annotated[X, ...]`` to ``X``.

        Handles nested ``Annotated[Annotated[X, a], b]`` (which ``typing``
        normally flattens, but this is defensive). Non-``Annotated``
        annotations are returned unchanged.
        """
        while typing.get_origin(annotation) is Annotated:
            annotation = typing.get_args(annotation)[0]
        return annotation

    # ------------------------------------------------------------------
    # Optional[] / Union[X, None] unwrapping
    # ------------------------------------------------------------------

    @classmethod
    def unwrap_optional(cls, annotation: Any) -> tuple[Any, bool]:
        """
        Return ``(inner_type, is_optional)``.

        ``Optional[X]`` and ``Union[X, None]`` are both represented by
        ``typing`` as ``Union[X, NoneType]``.  If *annotation* is such a
        union with exactly one non-``None`` member, returns
        ``(X, True)``.  Otherwise returns ``(annotation, False)``
        (with ``Annotated`` stripped).

        A ``Union`` with more than one non-``None`` member (e.g.
        ``Union[int, str, None]``) is NOT unwrapped -- it is returned as-is
        with ``is_optional=False``, since V1 does not support general
        unions; ``map_annotation`` will report such annotations as
        ``FieldType.ANY``.
        """
        annotation = cls.strip_annotated(annotation)
        if typing.get_origin(annotation) is Union:
            args = typing.get_args(annotation)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1 and len(args) == 2:
                return cls.strip_annotated(non_none[0]), True
        return annotation, False

    # ------------------------------------------------------------------
    # Literal[] support
    # ------------------------------------------------------------------

    @classmethod
    def get_literal_values(cls, annotation: Any) -> tuple[Any, ...] | None:
        """
        Return the value tuple for ``Literal[...]`` annotations, or ``None``
        if *annotation* (after unwrapping ``Optional``/``Annotated``) is not
        a ``Literal``.
        """
        inner, _ = cls.unwrap_optional(annotation)
        if typing.get_origin(inner) is typing.Literal:
            return typing.get_args(inner)
        return None

    # ------------------------------------------------------------------
    # Primary mapping
    # ------------------------------------------------------------------

    @classmethod
    def map_annotation(cls, annotation: Any) -> FieldType:
        """
        Map *annotation* to its corresponding ``FieldType``.

        ``Optional``/``Annotated`` wrappers are stripped first.  See the
        module docstring for the full mapping table and V1 limitations.
        """
        inner, _ = cls.unwrap_optional(annotation)

        if inner is Any:
            return FieldType.ANY

        literal_values = cls.get_literal_values(annotation)
        if literal_values is not None:
            return cls._literal_field_type(literal_values)

        if inner in _PRIMITIVE_MAP:
            return _PRIMITIVE_MAP[inner]

        origin = typing.get_origin(inner)

        if origin is Union:
            # General (multi-type) union: not supported in V1.
            return FieldType.ANY

        if origin in (list, list):
            return FieldType.ARRAY

        if origin in (dict, dict):
            return FieldType.OBJECT

        if inner is list:
            return FieldType.ARRAY

        if inner is dict:
            return FieldType.OBJECT

        if cls._is_basemodel_subclass(inner):
            return FieldType.OBJECT

        return FieldType.ANY

    # ------------------------------------------------------------------
    # Array item type
    # ------------------------------------------------------------------

    @classmethod
    def get_item_type(cls, annotation: Any) -> FieldType | None:
        """
        Return the ``FieldType`` of array elements for ``list`` / ``List[X]``
        annotations, or ``None`` if *annotation* is not a list type or has
        no type argument (bare ``list``).

        For ``List[SomeBaseModel]`` returns ``FieldType.OBJECT`` (per V1
        scope: arrays of nested models are type-checked as objects but not
        recursively validated -- see module docstring).
        """
        inner, _ = cls.unwrap_optional(annotation)
        origin = typing.get_origin(inner)
        if origin not in (list, list):
            return None

        args = typing.get_args(inner)
        if not args:
            return None

        return cls.map_annotation(args[0])

    # ------------------------------------------------------------------
    # Nested model detection
    # ------------------------------------------------------------------

    @classmethod
    def get_nested_model(cls, annotation: Any) -> type[Any] | None:
        """
        Return the ``BaseModel`` subclass referenced by *annotation*, or
        ``None`` if *annotation* does not (directly) reference one.

        Only direct references are detected: ``SomeModel`` or
        ``Optional[SomeModel]``.  ``List[SomeModel]`` is intentionally
        excluded -- per V1 scope, arrays of nested models do not produce a
        ``nested_spec`` (see ``get_item_type``).
        """
        inner, _ = cls.unwrap_optional(annotation)
        if cls._is_basemodel_subclass(inner):
            return inner
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_basemodel_subclass(annotation: Any) -> bool:
        """``True`` if *annotation* is a class deriving from ``BaseModel``."""
        # Local import keeps pydantic out of any module that doesn't need it
        # at import time, and avoids a hard dependency at module load for
        # tooling that introspects this module without pydantic installed.
        from pydantic import BaseModel  # noqa: PLC0415

        return (
            isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
        )

    @staticmethod
    def _literal_field_type(values: tuple[Any, ...]) -> FieldType:
        """
        Infer a ``FieldType`` for a ``Literal[...]``'s value tuple.

        If all values share a common primitive type, that type is used.
        Mixed-type or empty literals fall back to ``FieldType.ANY``.

        Note: ``bool`` is checked before ``int`` since ``bool`` is a
        subclass of ``int`` in Python, and a ``Literal[True, False]``
        should map to ``BOOLEAN``, not ``INTEGER``.
        """
        if not values:
            return FieldType.ANY

        first = values[0]
        first_type: type | None = None
        for py_type in (bool, str, int, float):
            if isinstance(first, py_type):
                first_type = py_type
                break
        if first_type is None:
            return FieldType.ANY

        if not all(isinstance(v, first_type) for v in values):
            return FieldType.ANY

        return _LITERAL_VALUE_TYPE_MAP[first_type]
