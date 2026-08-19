"""
TypeCoercionStrategy — repairs type and structure mismatches via safe casts.

Priority 30.  Only proposes casts where the source value unambiguously
represents a value of the target type:

* ``str`` → ``int``    — only if the string is a (possibly negative) digit
  sequence, e.g. ``"30"`` or ``"-5"``.
* ``str`` → ``float``  — only if ``float(value)`` succeeds.
* ``int`` → ``float``  — always safe.
* ``str`` → ``bool``   — only for the exact strings (case-insensitive)
  ``"true"``, ``"false"``, ``"1"``, ``"0"``.
* ``dict``/``list`` → ``str`` — JSON-serialise, for ``STRING`` and
  ``BYTES`` targets, only if ``json.dumps(value)`` succeeds (deterministic
  and round-trippable; repairs harness-side over-parsing of JSON text
  arguments).  ``BYTES`` targets also yield a ``str`` — the framework's
  native validation encodes it (e.g. Pydantic's lax str -> bytes).
* ``str`` → ``dict``/``list`` — JSON-*parse*, for ``OBJECT`` and ``ARRAY``
  targets, when the string parses to the declared shape.  This is the more
  common direction in practice: a model asked for an object returns it as a
  string, or a tool-calling harness forwards an argument unparsed.  Refused
  when the parse yields the wrong kind (``"123"`` is valid JSON but is not an
  object) or when array elements do not match the declared ``item_type``.
* value → ``list``     — wrap-in-list, only when the target is an ``ARRAY``
  with a declared ``item_type`` that the value already satisfies as a
  single element (lossless: ``"x"`` → ``["x"]``).  Bare/untyped arrays and
  values that are already lists are never wrapped.  **Parsing is attempted
  first**: a JSON array string satisfies ``item_type=string`` as a single
  element, so wrapping would turn ``'["a","b"]'`` into ``['["a","b"]']`` --
  which validates cleanly as a list of strings and is silently wrong.
* value → union        — for ``UNION`` targets, each member is tried with
  the rules above; the coercion is proposed only when exactly one member
  yields the highest-confidence candidate (ties are ambiguous and refused).

All other combinations are left unrepaired by this strategy (no operation
is proposed; ``confidence`` is never fabricated for unsafe casts).

This strategy determines *feasibility and confidence* only.  The actual
cast is performed by the engine when applying the ``COERCE`` operation,
using ``ContractSpec`` to look up the target ``FieldType`` (and, for
``ARRAY``/``UNION`` targets, the ``item_type`` / ``union_members``).
"""

from __future__ import annotations

import json
from typing import Any

from stateguard.core.errors.operations import (
    FIDELITY_EXACT,
    FIDELITY_NORMALISED,
    FIDELITY_WHITESPACE,
    FieldOperation,
    FieldOpType,
    RepairEvidence,
    RepairRisk,
)
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec, find_field_spec
from stateguard.core.models.field_types import FieldType, UnionMember, type_matches
from stateguard.core.paths import NOT_FOUND, get_nested_value

__all__ = ["TypeCoercionStrategy"]


# Violation kinds this strategy can act on.
#
# STRUCTURAL_MISMATCH is included because ``ContractValidator`` reports a
# non-dict in an OBJECT field that way rather than as a TYPE_MISMATCH -- so a
# JSON-encoded object arriving as a string was invisible to this strategy
# despite being exactly the case it now repairs. Root-level structural
# violations carry an empty field_path, which resolves to _NOT_FOUND and is
# skipped harmlessly.
_COERCIBLE_VIOLATIONS = frozenset({ViolationType.TYPE_MISMATCH, ViolationType.STRUCTURAL_MISMATCH})


# ---------------------------------------------------------------------------
# Round-trip fidelity
# ---------------------------------------------------------------------------
#
# These replace the four hand-picked confidence constants this module used to
# carry (0.95 numeric, 0.85 bool, 0.9 array-wrap, 0.85 JSON).  Those numbers
# described nothing measurable -- they were the author's feeling about each
# cast.  Fidelity is measured instead: convert the value, convert it back, and
# see how much of the original survived.

#: The three general rungs live on ``RepairEvidence``'s scale in
#: ``core.errors.operations`` so that this module's 0.85 and
#: ``EnumNormalizationStrategy``'s mean the same thing. Only the
#: coercion-specific rung is defined here.
_FIDELITY_EXACT = FIDELITY_EXACT
_FIDELITY_WHITESPACE = FIDELITY_WHITESPACE
_FIDELITY_NORMALISED = FIDELITY_NORMALISED

#: ``"1"`` -> ``True`` is a reading of the string, not a re-encoding of it.
_FIDELITY_BOOL_NUMERIC = 0.80

# Strings accepted for str -> bool coercion (case-insensitive).
_BOOL_TRUE_WORDS = {"true"}
_BOOL_FALSE_WORDS = {"false"}
_BOOL_NUMERIC = {"1", "0"}
_BOOL_STRINGS = _BOOL_TRUE_WORDS | _BOOL_FALSE_WORDS | _BOOL_NUMERIC


def _roundtrip_fidelity(original: str, coerced: Any) -> float:
    """
    How much of *original* survives ``str(coerced)``.

    The point of measuring rather than assuming: ``"5"`` and ``"05"`` were
    previously both worth 0.95 simply because both are digit strings, even
    though only one of them round-trips.
    """
    return _compare_roundtrip(original, str(coerced))


def _json_roundtrip_fidelity(original: str, parsed: Any) -> float:
    """
    How much of *original* survives ``json.dumps(json.loads(original))``.

    The JSON analogue of ``_roundtrip_fidelity``: ``str()`` is the wrong
    renderer for a parsed container (``str({'a': 1})`` uses single quotes and
    can never match its own JSON source), so the comparison is made against a
    re-serialisation instead.

    What this actually distinguishes is formatting the *value* does not
    depend on.  ``'{"a": 1}'`` re-serialises byte-for-byte and scores 1.0;
    ``'{"a":1}'`` and ``'{"a": 1E2}'`` do not, and score
    ``_FIDELITY_NORMALISED`` for exactly the reason ``"05"`` -> ``5`` does.

    Losses that are *not* mere formatting -- a repeated object key, where one
    value is silently discarded -- never reach here: ``json_parsed`` refuses
    them outright.
    """
    try:
        back = json.dumps(parsed)
    except (TypeError, ValueError):  # pragma: no cover -- parsed came from JSON
        return _FIDELITY_NORMALISED
    return _compare_roundtrip(original, back)


def _compare_roundtrip(original: str, back: str) -> float:
    """Grade a re-rendered value against the text it was parsed from."""
    if back == original:
        return _FIDELITY_EXACT
    if back == original.strip():
        return _FIDELITY_WHITESPACE
    return _FIDELITY_NORMALISED


# ---------------------------------------------------------------------------
# Coercion feasibility
# ---------------------------------------------------------------------------


def _coercion_evidence(
    value: Any,
    target_type: FieldType,
    item_type: FieldType | None = None,
    union_members: tuple[UnionMember, ...] | None = None,
) -> tuple[RepairEvidence, RepairRisk] | None:
    """
    Measure the evidence for coercing *value* to *target_type*.

    Returns ``(evidence, risk)``, or ``None`` if no safe coercion is defined
    for this (value, target) pair.  ``TrustPolicy`` turns the evidence into a
    score; this function never invents one.

    *item_type* is consulted only for ``ARRAY`` targets and *union_members*
    only for ``UNION`` targets; both come from the field's ``FieldSpec``.
    """
    if target_type in (FieldType.STRING, FieldType.BYTES):
        serialised = json_serialized(value)
        if serialised is None:
            return None
        # json.loads(json.dumps(x)) == x for the containers we accept, so the
        # value survives intact -- but a container rendered as text is no
        # longer a container, which is why the risk is LOSSY rather than
        # REVERSIBLE.
        return (
            RepairEvidence(
                value_preserved=_FIDELITY_EXACT,
                # Rendering a container as text discards its structure, even
                # though every byte of the value survives. Recorded so that a
                # union offering both this and a wrap-in-list prefers the wrap.
                signals=(("structure_preserved", 0.0),),
                notes=(f"{type(value).__name__} serialises to JSON and parses back equal",),
            ),
            RepairRisk.LOSSY,
        )

    if target_type is FieldType.INTEGER:
        if isinstance(value, str) and not isinstance(value, bool) and _is_integer_string(value):
            fidelity = _roundtrip_fidelity(value, int(value))
            return (
                RepairEvidence(
                    value_preserved=fidelity,
                    notes=(f"str -> int round-trips at {fidelity:.2f}",),
                ),
                RepairRisk.REVERSIBLE,
            )
        return None

    if target_type is FieldType.FLOAT:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            # int -> float never loses the value.
            return (
                RepairEvidence(
                    value_preserved=_FIDELITY_EXACT,
                    notes=("int is exactly representable as float",),
                ),
                RepairRisk.REVERSIBLE,
            )
        if isinstance(value, str) and _is_float_string(value):
            fidelity = _roundtrip_fidelity(value, float(value))
            return (
                RepairEvidence(
                    value_preserved=fidelity,
                    notes=(f"str -> float round-trips at {fidelity:.2f}",),
                ),
                RepairRisk.REVERSIBLE,
            )
        return None

    if target_type is FieldType.BOOLEAN:
        if not isinstance(value, str):
            return None
        lowered = value.strip().lower()
        if lowered in _BOOL_TRUE_WORDS or lowered in _BOOL_FALSE_WORDS:
            fidelity = _FIDELITY_EXACT if value == lowered else _FIDELITY_WHITESPACE
            note = "string spells the boolean out"
        elif lowered in _BOOL_NUMERIC:
            fidelity = _FIDELITY_BOOL_NUMERIC
            note = "numeric string read as a boolean"
        else:
            return None
        # Reading a string as a boolean is an interpretation, not a
        # re-encoding -- "1" could legitimately have meant the integer 1.
        return RepairEvidence(value_preserved=fidelity, notes=(note,)), RepairRisk.INFERRED

    if target_type is FieldType.OBJECT:
        parsed_object = json_parsed(value, dict)
        if parsed_object is not None:
            fidelity = _json_roundtrip_fidelity(value, parsed_object)
            return (
                RepairEvidence(
                    value_preserved=fidelity,
                    signals=(("structure_preserved", 1.0),),
                    notes=(f"string parses as a JSON object, round-trips at {fidelity:.2f}",),
                ),
                RepairRisk.REVERSIBLE,
            )
        return None

    if target_type is FieldType.ARRAY:
        # Parsing is tried before wrapping, and the order is load-bearing.
        # A JSON array *string* satisfies `item_type=string` as a single
        # element, so wrapping would turn '["a","b"]' into ['["a","b"]'] --
        # which validates cleanly as a list of strings and is silently wrong.
        parsed_array = json_parsed(value, list)
        if parsed_array is not None and _parsed_array_matches(parsed_array, item_type):
            fidelity = _json_roundtrip_fidelity(value, parsed_array)
            return (
                RepairEvidence(
                    value_preserved=fidelity,
                    signals=(("structure_preserved", 1.0),),
                    notes=(
                        f"string parses as a JSON array of {len(parsed_array)} item(s), "
                        f"round-trips at {fidelity:.2f}",
                    ),
                ),
                RepairRisk.REVERSIBLE,
            )

        if _array_wrap_is_safe(value, item_type):
            # The element survives untouched, but cardinality is invented:
            # nothing in the payload said this was a one-element list.
            return (
                RepairEvidence(
                    value_preserved=_FIDELITY_EXACT,
                    # The value itself is untouched inside the list -- a dict
                    # stays a dict. Only cardinality is invented.
                    signals=(("structure_preserved", 1.0),),
                    notes=("value matches the declared item_type; wrapped as a single element",),
                ),
                RepairRisk.LOSSY,
            )
        return None

    if target_type is FieldType.UNION:
        resolved = resolve_union_member(value, union_members)
        if resolved is None:
            return None
        _member, evidence, risk = resolved
        return evidence, risk

    return None


def json_serialized(value: Any) -> str | None:
    """
    Return ``json.dumps(value)`` if *value* is a dict or list that
    serialises cleanly, else ``None``.

    Only containers qualify — scalars where a string is expected are a
    semantic mismatch, not an over-parsed JSON argument.  Containers
    holding non-JSON values (arbitrary objects, NaN under strict dumps,
    circular references) are refused rather than approximated.

    Shared with the engine's ``_coerce_value`` so that feasibility and
    application always produce the same serialisation.
    """
    if not isinstance(value, (dict, list)):
        return None
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return None


class _DuplicateJsonKeyError(ValueError):
    """
    Raised by ``_reject_duplicate_keys`` when a JSON object repeats a key.

    Subclasses ``ValueError`` so every existing ``json.loads`` call site
    already treats it as "not parseable" rather than letting it escape.
    """


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """
    ``object_pairs_hook`` that refuses an object with a repeated key.

    ``json.loads('{"a": 1, "a": 2}')`` silently returns ``{"a": 2}`` -- the
    first value is destroyed with no signal at all.  A repair that quietly
    discards data is the failure mode this whole layer exists to prevent, and
    refusing rather than resolving is already the rule at the root: see
    ``_pairs_to_dict`` in ``stateguard.core.engine``, which rejects a
    key/value pair list for exactly this reason.
    """
    seen: dict[str, Any] = {}
    for key, item in pairs:
        if key in seen:
            raise _DuplicateJsonKeyError(f"duplicate key {key!r} in JSON object")
        seen[key] = item
    return seen


def json_loads_strict(text: str | bytes) -> Any:
    """
    ``json.loads`` that refuses lossy input instead of silently resolving it.

    The only difference from ``json.loads`` is the duplicate-key guard (see
    ``_reject_duplicate_keys``), which applies at every depth including inside
    arrays.  Shared by ``json_parsed`` and by the engine's root-shape
    normalisation so a payload is judged the same way wherever it arrives.
    """
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


def json_parsed(value: Any, expected: type) -> Any | None:
    """
    Parse *value* as JSON and return the result if it is an *expected*, else
    ``None``.

    The inverse of ``json_serialized``, and the more common direction in
    practice: a model asked for an object returns the object *as a string*,
    or a tool-calling harness forwards an argument without parsing it.

    Three guards make this safe to attempt:

    * only strings are considered, and only when the contract declares a
      structured target — a scalar where an object is expected is a semantic
      mismatch, not an unparsed argument;
    * the parse must actually yield *expected*.  ``"123"`` is valid JSON but
      produces an ``int``, which is not an object, so it is refused rather
      than half-accepted;
    * the parse must not be lossy.  A repeated object key makes ``json.loads``
      discard a value without saying so, which is refused outright rather than
      priced down — there is no fidelity score that honestly describes
      "one of these two values is gone".

    Deeply nested input raises ``RecursionError`` rather than
    ``JSONDecodeError``; both are treated as "not parseable" rather than
    propagated, matching ``_normalise_root_payload``.

    Shared with the engine's ``_coerce_value`` so that feasibility and
    application always agree on what was parsed.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = json_loads_strict(value)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, expected) else None


def _parsed_array_matches(parsed: list[Any], item_type: FieldType | None) -> bool:
    """``True`` if every element of *parsed* satisfies *item_type*."""
    if item_type is None:
        return True
    return all(type_matches(item, item_type) for item in parsed)


def _array_wrap_is_safe(value: Any, item_type: FieldType | None) -> bool:
    """
    ``True`` if wrapping *value* as a single-element list is a safe repair
    for an ``ARRAY`` target.

    Refused when the value is already a list (that is an item-level
    problem, not a wrapping problem) and when *item_type* is unknown
    (wrapping into an untyped array would be a guess).

    Also refused when the value is a string that *parses* as a JSON array.
    Parsing is tried before wrapping precisely so that ``'["a","b"]'``
    becomes ``["a", "b"]`` rather than ``['["a","b"]']`` -- but that ordering
    only helps while the parse succeeds *and* the elements fit.  Given
    ``'[1, 2]'`` against ``item_type=string`` the parse yields the wrong
    element types, control fell through to here, and wrapping produced
    ``['[1, 2]']``: a list of one string, which validates cleanly and is
    exactly the silently-wrong result the ordering exists to prevent.

    A string that is a serialised array names its author's intent.  If its
    elements do not fit the contract that is an item-level mismatch no cast
    repairs, and re-reading the text as a single element is never what was
    meant.
    """
    if isinstance(value, list):
        return False
    if item_type is None:
        return False
    if json_parsed(value, list) is not None:
        return False
    return type_matches(value, item_type)


def resolve_union_member(
    value: Any,
    union_members: tuple[UnionMember, ...] | None,
) -> tuple[UnionMember, RepairEvidence, RepairRisk] | None:
    """
    Pick the union member *value* can be safely coerced to.

    Evaluates every member with the same rules as ``_coercion_evidence``
    (scalar casts; wrap-in-list for ``ARRAY`` members) and ranks them by:

    1. **fidelity** — how much of the value survives;
    2. **structure preservation** — whether the value keeps its own shape.
       A ``dict`` accepted by both a ``str`` member and a ``list`` member
       survives byte-for-byte either way, but ``[{...}]`` keeps it a mapping
       while ``'{"...": ...}'`` turns it into text.  Structured beats
       stringified;
    3. **risk** — the least consequential member wins a remaining tie.

    Returns ``None`` when no member is coercible, or when the top two rank
    identically on all three — a genuinely ambiguous union is refused rather
    than guessed at.

    Shared with the engine's ``_coerce_value`` so that feasibility and
    application always resolve to the same member.
    """
    if not union_members:
        return None

    candidates: list[tuple[tuple[float, float, int], UnionMember, RepairEvidence, RepairRisk]] = []
    for member in union_members:
        measured = _coercion_evidence(value, member.field_type, item_type=member.item_type)
        if measured is None:
            continue
        evidence, risk = measured
        fidelity = evidence.value_preserved if evidence.value_preserved is not None else 0.0
        structure = dict(evidence.signals).get("structure_preserved", 1.0)
        candidates.append(((fidelity, structure, -int(risk)), member, evidence, risk))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None

    _rank, member, evidence, risk = candidates[0]
    return member, evidence, risk


def _is_integer_string(value: str) -> bool:
    """
    ``True`` if *value* is a decimal integer string, optionally negative.

    Uses ``str.isdecimal`` rather than ``str.isdigit``: ``"²".isdigit()`` is
    ``True`` but ``int("²")`` raises, which previously produced a
    high-confidence coercion that silently did nothing when applied.
    """
    if value.isdecimal():
        return True
    return bool(value.startswith("-") and len(value) > 1 and value[1:].isdecimal())


def _is_float_string(value: str) -> bool:
    """
    ``True`` if *value* names a finite float.

    ``float()`` also accepts ``"nan"``, ``"inf"``, ``"infinity"`` and their
    signed forms.  Those are not lossless casts of a numeric string: NaN
    poisons every downstream comparison, and neither survives
    ``json.dumps`` as valid JSON.  They are refused here rather than given a
    high fidelity score.
    """
    try:
        parsed = float(value)
    except ValueError:
        return False
    return parsed == parsed and parsed not in (float("inf"), float("-inf"))


# ---------------------------------------------------------------------------
# TypeCoercionStrategy
# ---------------------------------------------------------------------------


class TypeCoercionStrategy(IRepairStrategy):
    """
    Proposes ``COERCE`` operations for TYPE_MISMATCH and STRUCTURAL_MISMATCH
    violations where a
    safe, lossless cast exists from the received value to the contract's
    declared type.
    """

    @property
    def name(self) -> str:
        return "TypeCoercionStrategy"

    @property
    def priority(self) -> int:
        return 30

    def can_handle(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> bool:
        return any(v.violation_type in _COERCIBLE_VIOLATIONS for v in violations)

    def propose(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[FieldOperation]:
        operations: list[FieldOperation] = []

        for violation in violations:
            if violation.violation_type not in _COERCIBLE_VIOLATIONS:
                continue
            if violation.expected_type is None:
                continue

            value = get_nested_value(data, violation.field_path)
            if value is NOT_FOUND:
                continue

            # Measure against the type the *contract* declares for this path,
            # not the violation's expected_type, because that is what the
            # engine's applier will cast to. The two are the same for every
            # violation except an array-item mismatch, where the validator
            # sets expected_type to the item type: coercing ["a", 1] to STRING
            # is feasible (json-serialise) but the applier would be casting to
            # ARRAY, so the proposal was scored 1.0 and could never be applied.
            # Reading one source for both keeps them from diverging at all.
            field_spec = find_field_spec(contract, violation.field_path)
            target_type: FieldType = violation.expected_type
            item_type: FieldType | None = None
            union_members: tuple[UnionMember, ...] | None = None
            if field_spec is not None:
                target_type = field_spec.field_type
                item_type = field_spec.item_type
                union_members = field_spec.union_members

            measured = _coercion_evidence(
                value,
                target_type,
                item_type=item_type,
                union_members=union_members,
            )
            if measured is None:
                continue
            evidence, risk = measured

            operations.append(
                FieldOperation(
                    op_type=FieldOpType.COERCE,
                    target_path=violation.field_path,
                    # No value interpolation: rationale strings end up in log
                    # entries, and RepairConfig.include_values_in_log defaults
                    # to False precisely so runtime values do not.
                    rationale=(
                        f"Coerce {type(value).__name__} at "
                        f"'{violation.field_path}' to {target_type.value}."
                    ),
                    risk=risk,
                    evidence=evidence,
                )
            )

        return operations
