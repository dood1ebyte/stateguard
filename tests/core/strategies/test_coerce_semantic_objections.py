"""
Deterministic is not the same as safe: the casts that carry a semantic objection.

Real traffic (Capture 1b and the targeted capture Z-T, ``docs/runtime/TASKMAP.md``
"Git/MCP domain closure") showed three casts that are exact as encodings and
wrong as meanings, each scored trust 1.0 and applied:

* a list sent for a path rendered as JSON text (Z01);
* a comma-separated list wrapped as one element (Z02);
* a JSON-text timestamp a real server *accepted*, returning the wrong history.

The two ``LOSSY`` casts now record a ``contradiction`` where no measurable
signal separates the right reading from the wrong one, and a string -> float
that changes the number is ``LOSSY`` rather than ``REVERSIBLE``. None of this
creates or alters a repair: it can only hold one for confirmation.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.errors.operations import RepairRisk
from stateguard.core.models.field_types import FieldType, UnionMember
from stateguard.core.strategies.coerce import _coercion_evidence
from stateguard.core.trust import TrustDecision, TrustPolicy

POLICY = TrustPolicy()


def _decide(
    value: Any, target: FieldType, **kw: Any
) -> tuple[RepairRisk, TrustDecision, str | None]:
    measured = _coercion_evidence(value, target, **kw)
    assert measured is not None, (value, target)
    evidence, risk = measured
    trust = POLICY.score(evidence)
    decision = POLICY.decide(trust, risk)
    if decision is TrustDecision.APPLY and evidence.contradiction is not None:
        decision = TrustDecision.AMBIGUOUS
    return risk, decision, evidence.contradiction


# ---------------------------------------------------------------------------
# Container -> JSON text: found, never applied unsupervised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        ["/repo"],  # Z01: a path
        ["fix bug"],  # a commit message
        ["line one", "line two"],
        {"days": 30},  # a timestamp a real server accepted and misread
        {"title": "t", "body": "b"},
        [],
        {},
    ],
)
@pytest.mark.parametrize("target", [FieldType.STRING, FieldType.BYTES])
def test_a_container_rendered_as_text_is_held(value: Any, target: FieldType) -> None:
    risk, decision, objection = _decide(value, target)
    assert risk is RepairRisk.LOSSY
    assert decision is TrustDecision.AMBIGUOUS
    assert objection is not None and "JSON text" in objection


def test_the_objection_is_carried_through_a_union_member() -> None:
    """``anyOf: [string, null]`` -- how optional strings (``start_timestamp``,
    ``contains``) are declared -- reaches the same cast through the union."""
    members = (UnionMember(FieldType.STRING), UnionMember(FieldType.NULL))
    risk, decision, objection = _decide(["2026-09-01"], FieldType.UNION, union_members=members)
    assert (risk, decision) == (RepairRisk.LOSSY, TrustDecision.AMBIGUOUS)
    assert objection is not None


def test_the_score_itself_is_unchanged() -> None:
    """The objection is a finding the score cannot express, not a lowered
    number: the cast is still exact, and is still reported as such."""
    evidence, _ = _coercion_evidence(["x"], FieldType.STRING)  # type: ignore[misc]
    assert POLICY.score(evidence) == 1.0


# ---------------------------------------------------------------------------
# Wrap-in-list: delimiters are an objection, a single token is not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["notes.txt, README.txt", "a;b", "a.txt\nb.txt", "a.txt\r\nb.txt", "one,"],
)
def test_a_delimited_string_is_not_wrapped_unsupervised(value: str) -> None:
    risk, decision, objection = _decide(value, FieldType.ARRAY, item_type=FieldType.STRING)
    assert (risk, decision) == (RepairRisk.LOSSY, TrustDecision.AMBIGUOUS)
    assert objection is not None and "delimiter" in objection


@pytest.mark.parametrize(
    "value,item_type",
    [
        ("notes.txt", FieldType.STRING),
        ("My Document.txt", FieldType.STRING),  # whitespace is not a delimiter here
        (5, FieldType.INTEGER),
        ({"path": "a"}, FieldType.OBJECT),
    ],
)
def test_a_single_value_is_still_wrapped(value: Any, item_type: FieldType) -> None:
    risk, decision, objection = _decide(value, FieldType.ARRAY, item_type=item_type)
    assert (risk, decision, objection) == (RepairRisk.LOSSY, TrustDecision.APPLY, None)


def test_space_separated_items_are_a_known_gap() -> None:
    """Pinned so a change is deliberate: whitespace is not treated as a
    delimiter, because file names with spaces are ordinary. ``"a.txt
    b.txt"`` is therefore still wrapped as one element."""
    _, decision, objection = _decide("a.txt b.txt", FieldType.ARRAY, item_type=FieldType.STRING)
    assert (decision, objection) == (TrustDecision.APPLY, None)


# ---------------------------------------------------------------------------
# str -> float: value-changing is LOSSY, formatting is REVERSIBLE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "12345678901234567891",
        "0.1000000000000000055511151231257827",
        "1e-400",
        # differs from its double only past the 28th significant digit
        "0.1000000000000000000000000000001",
    ],
)
def test_a_float_that_changes_the_number_is_lossy(text: str) -> None:
    risk, decision, _ = _decide(text, FieldType.FLOAT)
    assert risk is RepairRisk.LOSSY
    assert decision is TrustDecision.AMBIGUOUS


@pytest.mark.parametrize("text", ["2.5", "1.50", "1e3", "0.1", "-7", " 3.25 ", "1_000"])
def test_a_float_that_keeps_the_number_is_reversible(text: str) -> None:
    risk, decision, objection = _decide(text, FieldType.FLOAT)
    assert (risk, decision, objection) == (RepairRisk.REVERSIBLE, TrustDecision.APPLY, None)


# ---------------------------------------------------------------------------
# The safe deterministic casts are untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,target,kw",
    [
        ("3", FieldType.INTEGER, {}),
        ("00123", FieldType.INTEGER, {}),
        (3.0, FieldType.INTEGER, {}),
        ('["a.txt", "b.txt"]', FieldType.ARRAY, {"item_type": FieldType.STRING}),
        ('{"k": "v"}', FieldType.OBJECT, {}),
        ("true", FieldType.BOOLEAN, {}),
        ("1", FieldType.BOOLEAN, {}),
    ],
)
def test_safe_casts_still_apply_without_objection(
    value: Any, target: FieldType, kw: dict[str, Any]
) -> None:
    _, decision, objection = _decide(value, target, **kw)
    assert (decision, objection) == (TrustDecision.APPLY, None)
