"""
Whether two tool-call payloads are the same JSON.

Every "did StateGuard change this call?" decision goes through ``same_json``:
whether a request goes out byte-for-byte or rewritten, whether shadow mode
holds a call, what the reason says, and what the relay logs as
``would_modify``. Python's ``==`` is the wrong question for all of them: it
says ``2 == 2.0`` and ``True == 1``, so a repair that coerces ``2.0`` to ``2``
compared "unchanged" and the original -- the value the server rejects -- was
forwarded instead.

JSON-semantic equality, by JSON type: object, array, string, integer, number
(a float), boolean, null. ``2`` and ``2.0`` differ (they are different JSON
on the wire); ``True`` and ``1`` differ. Object key order does not matter,
array order does.

Zero external dependencies.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["same_json"]


def _kind(value: Any) -> str:
    # bool before int: bool is an int subclass.
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def same_json(a: Any, b: Any) -> bool:
    """
    True when *a* and *b* are the same JSON value, type for type.

    Iterative, not recursive: payloads come from outside the process, and a
    hostile one can nest deeper than Python's recursion limit.
    """
    pending: list[tuple[Any, Any]] = [(a, b)]
    while pending:
        x, y = pending.pop()
        kind = _kind(x)
        if kind != _kind(y):
            return False
        if kind == "object":
            if x.keys() != y.keys():
                return False
            pending.extend((x[key], y[key]) for key in x)
        elif kind == "array":
            if len(x) != len(y):
                return False
            pending.extend(zip(x, y, strict=True))
        elif x != y:
            return False
    return True
