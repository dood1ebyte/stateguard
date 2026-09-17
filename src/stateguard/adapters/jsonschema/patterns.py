"""
Screening for schema-supplied regular expressions.

``pattern`` is the only keyword in the supported subset whose value is
*executed*. Every other keyword is compared against; this one is handed to
``re`` and run. That matters here in a way it does not for the Pydantic
adapter, because a JSON Schema arrives from an MCP server -- the regex is
written by a third party, not by the developer deploying StateGuard.

What this defends against
-------------------------
Python's ``re`` is a backtracking engine, so a pattern with a nested
unbounded quantifier can take exponential time on a short input. Measured on
this codebase before the screen existed::

    pattern "(a+)+$" against 40 'a's + "!"   -> did not finish in 15s

That is a denial of service reachable from a tool definition, and no amount
of bounding the *input* fixes it: 40 characters was already enough.

Honest about the limit
----------------------
Detecting catastrophic backtracking in general is undecidable, and this is a
**heuristic, not a proof**. It catches the nested-quantifier family --
``(X+)+``, ``(X*)*``, ``(X+){2,}`` and their nestings -- which is the shape
behind essentially every real-world ReDoS report. It does **not** catch
overlapping alternation such as ``(a|a)+``.

A deployment that accepts genuinely adversarial schemas should not rely on
this alone; it should run extraction where a hung thread is survivable. The
screen exists so the common case fails loudly and cheaply, not so the problem
can be considered closed.

Why refuse rather than warn
---------------------------
Refusing is visible and actionable -- the schema's author can anchor the
pattern or rewrite the group -- and it matches how the rest of this adapter
treats what it cannot handle safely (``MCP_ADAPTER_PLAN.md`` §5). A warning
would leave the hang in place. The screen is deliberately conservative and
will reject some patterns that would not actually blow up; that trade is the
right way round when the alternative is an unkillable match.

Zero external dependencies.
"""

from __future__ import annotations

import re
from typing import Any

from stateguard.adapters.jsonschema.errors import UnsupportedSchemaError

__all__ = ["screen_pattern"]


#: ``{2,}`` / ``{,}`` -- a brace quantifier with no upper bound. ``{2,5}`` is
#: bounded and therefore safe, so it deliberately does not match.
_OPEN_ENDED_BRACE = re.compile(r"\{\d*,\}")


def screen_pattern(pattern: Any, path: str) -> str:
    """
    Return *pattern* if it is safe to run, else raise.

    Raises ``UnsupportedSchemaError`` when the pattern is not a string, does
    not compile, or carries a nested unbounded quantifier.
    """
    if not isinstance(pattern, str):
        raise UnsupportedSchemaError(
            f"Field '{path}': 'pattern' must be a string, got {type(pattern).__name__}: {pattern!r}"
        )

    try:
        re.compile(pattern)
    except re.error as exc:
        raise UnsupportedSchemaError(
            f"Field '{path}': 'pattern' {pattern!r} is not a valid regular expression ({exc})."
        ) from exc

    if _has_nested_unbounded_quantifier(pattern):
        raise UnsupportedSchemaError(
            f"Field '{path}': 'pattern' {pattern!r} nests an unbounded quantifier "
            f"inside a quantified group (the '(X+)+' shape), which can make "
            f"matching take exponential time on a short input. It is refused "
            f"rather than run, because the schema supplying it is not trusted. "
            f"Rewrite the group with a bounded quantifier such as '{{1,64}}', or "
            f"anchor the pattern so backtracking cannot cascade."
        )

    return pattern


def _has_nested_unbounded_quantifier(pattern: str) -> bool:
    """
    Whether *pattern* quantifies a group that itself contains a quantifier.

    ``(a+)+`` is the canonical catastrophic shape: the outer quantifier can
    partition the input among the inner one in exponentially many ways, and a
    failing suffix forces the engine to try all of them.
    """
    groups, quantifiers = _scan(pattern)
    if not groups or not quantifiers:
        return False

    quantifier_positions = set(quantifiers)
    return any(
        # The group is itself unboundedly quantified ...
        (end + 1) in quantifier_positions
        # ... and its body contains an unbounded quantifier.
        and any(start < position < end for position in quantifier_positions)
        for start, end in groups
    )


def _scan(pattern: str) -> tuple[list[tuple[int, int]], list[int]]:
    """
    Locate group spans and unbounded quantifiers in *pattern*.

    Returns ``(group_spans, quantifier_start_positions)``.

    Escapes and character classes are tracked so that a literal ``\\(`` or a
    ``[(]`` is not mistaken for a group, and ``[*+]`` inside a class is not
    mistaken for a quantifier -- both are ordinary characters there, and
    treating them as syntax would reject harmless patterns.
    """
    groups: list[tuple[int, int]] = []
    open_groups: list[int] = []
    quantifiers: list[int] = []

    index = 0
    length = len(pattern)
    in_character_class = False

    while index < length:
        char = pattern[index]

        if char == "\\":
            index += 2  # Skip the escape and whatever it escapes.
            continue

        if in_character_class:
            if char == "]":
                in_character_class = False
            index += 1
            continue

        if char == "[":
            in_character_class = True
        elif char == "(":
            open_groups.append(index)
        elif char == ")":
            if open_groups:
                groups.append((open_groups.pop(), index))
        elif char in "*+":
            quantifiers.append(index)
        elif char == "{":
            match = _OPEN_ENDED_BRACE.match(pattern, index)
            if match:
                quantifiers.append(index)
                index = match.end()
                continue

        index += 1

    return groups, quantifiers
