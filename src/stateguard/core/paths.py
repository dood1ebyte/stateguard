"""
Dot-notation payload navigation.

``FieldSpec.path`` addresses nested fields with dots (``"address.city"``), so
every strategy and the engine need the same answer to "what is at this path,
and is anything there at all".

The distinction that matters is between **absent** and **present but
``None``** — a field explicitly set to ``null`` is not a missing field, and a
repair that conflates the two either overwrites a deliberate ``null`` or
skips a field that needs filling.  ``NOT_FOUND`` is the sentinel that keeps
them apart; ``None`` cannot, because it is a legitimate value.

Shared rather than copied because the sentinel only works if there is exactly
one of it: two modules each defining their own ``_NotFound`` produce values
that are never ``is``-equal across a module boundary, so a helper returning
one module's sentinel silently reads as a real value to the other.

Zero dependencies — part of Layer 0.
"""

from __future__ import annotations

from typing import Any

__all__ = ["NOT_FOUND", "get_nested_value"]


class _NotFound:
    """Sentinel distinguishing 'path does not exist' from a value of ``None``."""

    _instance: _NotFound | None = None

    def __new__(cls) -> _NotFound:
        # A single instance, so ``is NOT_FOUND`` holds even if a caller
        # constructs one rather than importing the module-level sentinel.
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NOT_FOUND"

    def __bool__(self) -> bool:
        # Truthiness is the one test that must never be used on this object.
        # Falsy would alias it to ``0``, ``""``, ``[]`` and ``None`` -- the
        # exact conflation the sentinel exists to prevent -- and truthy would
        # make "nothing here" read as a value. Raising forces the ``is``
        # comparison that is always correct.
        raise TypeError(
            "NOT_FOUND has no truth value; compare with 'is NOT_FOUND' instead. "
            "Truthiness would conflate an absent path with a present 0, '', [] or None."
        )


NOT_FOUND = _NotFound()


def get_nested_value(data: Any, path: str) -> Any:
    """
    Return the value at dot-notation *path* within *data*, or ``NOT_FOUND``.

    ``NOT_FOUND`` is returned when any segment is absent or when an
    intermediate value is not a ``dict`` (a scalar cannot be descended into).
    A present value of ``None`` is returned as ``None``.
    """
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return NOT_FOUND
        current = current[part]
    return current
