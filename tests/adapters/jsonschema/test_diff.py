"""
Structural schema diff -- classification, change records, and soundness.

The claim under test: ``COMPATIBLE`` means every argument payload the old
schema accepted, the new one accepts too. The example tests pin each rule;
the property test checks the claim against StateGuard's own validator on
randomly generated schema pairs and payloads.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from stateguard import ContractGuard
from stateguard.adapters.jsonschema.diff import (
    ChangeKind,
    SchemaChangeStatus,
    diff_schemas,
)
from stateguard.adapters.jsonschema.identity import fingerprint_schema

S = SchemaChangeStatus
K = ChangeKind

BASE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        "sort": {"type": "string", "enum": ["relevance", "date"]},
        "filters": {
            "type": "object",
            "properties": {"lang": {"type": "string"}},
            "required": ["lang"],
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["query"],
}


def _with(change: Any, base: dict[str, Any] = BASE) -> dict[str, Any]:
    schema = copy.deepcopy(base)
    change(schema)
    return schema


def _closed(schema: dict[str, Any]) -> dict[str, Any]:
    return {**schema, "additionalProperties": False}


def _kinds(diff: Any) -> list[tuple[str, str, bool]]:
    return [(c.kind.value, c.path, c.breaking) for c in diff.changes]


# ===========================================================================
# Status
# ===========================================================================


class TestStatus:
    def test_unchanged(self) -> None:
        diff = diff_schemas(BASE, copy.deepcopy(BASE))
        assert diff.status is S.UNCHANGED
        assert diff.changes == ()
        assert diff.is_compatible

    def test_equivalent(self) -> None:
        after = _with(lambda s: s.__setitem__("description", "Search."))
        diff = diff_schemas(BASE, after)
        assert diff.status is S.EQUIVALENT
        assert diff.changes == ()

    def test_invalid(self) -> None:
        diff = diff_schemas(BASE, _with(lambda s: s.__setitem__("required", "query")))
        assert diff.status is S.INVALID
        assert diff.error and "required" in diff.error
        assert not diff.is_compatible

    def test_unsupported(self) -> None:
        diff = diff_schemas(
            BASE, _with(lambda s: s.__setitem__("allOf", [{"required": ["limit"]}]))
        )
        assert diff.status is S.UNSUPPORTED
        assert diff.error and "allOf" in diff.error

    def test_not_even_json_is_invalid(self) -> None:
        assert diff_schemas(BASE, {"type": "object", "default": float("nan")}).status is S.INVALID
        assert diff_schemas(BASE, ["no"]).status is S.INVALID

    def test_from_an_unreadable_schema(self) -> None:
        """Moving off an unsupported schema is still compared structurally."""
        before = _with(lambda s: s.__setitem__("allOf", [{"required": ["limit"]}]))
        diff = diff_schemas(before, BASE)
        assert diff.status is S.COMPATIBLE  # dropping an assertion only loosens
        assert _kinds(diff) == [("keyword_changed", "", False)]

    def test_dropping_pattern_properties_is_not_assumed_safe(self) -> None:
        before = {"type": "object", "patternProperties": {"^x": {}}, "additionalProperties": False}
        after = {"type": "object", "additionalProperties": False}
        assert diff_schemas(before, after).changes[0].breaking

    def test_from_a_schema_that_cannot_even_be_normalised(self) -> None:
        before = {"type": "object", "properties": {"a": {"$ref": "#/$defs/Nope"}}}
        diff = diff_schemas(before, BASE)
        assert diff.status is S.INCOMPATIBLE
        assert _kinds(diff) == [("schema_replaced", "", True)]

    def test_never_raises_for_content(self) -> None:
        for bad in (None, 3, "x", [], {"type": "object", "properties": {"a": {"$ref": 5}}}):
            diff_schemas(bad, bad)
            diff_schemas(BASE, bad)
            diff_schemas(bad, BASE)


# ===========================================================================
# Rules -- compatible
# ===========================================================================

COMPATIBLE: list[tuple[str, Any, list[tuple[str, str, bool]]]] = [
    (
        "requirement dropped",
        lambda s: s.__setitem__("required", []),
        [("required_removed", "query", False)],
    ),
    (
        "parameter removed, schema open",
        lambda s: s["properties"].pop("sort"),
        [("field_removed", "sort", False)],
    ),
    (
        "enum member added",
        lambda s: s["properties"]["sort"]["enum"].append("title"),
        [("enum_changed", "sort", False)],
    ),
    (
        "integer widened to number",
        lambda s: s["properties"]["limit"].__setitem__("type", "number"),
        [("type_changed", "limit", False)],
    ),
    (
        "type list widened",
        lambda s: s["properties"]["query"].__setitem__("type", ["string", "null"]),
        [("type_changed", "query", False)],
    ),
    (
        "maximum loosened",
        lambda s: s["properties"]["limit"].__setitem__("maximum", 100),
        [("constraint_changed", "limit", False)],
    ),
    (
        "minimum removed",
        lambda s: s["properties"]["limit"].pop("minimum"),
        [("constraint_changed", "limit", False)],
    ),
    (
        "default changed (behaviour, not acceptance)",
        lambda s: s["properties"]["limit"].__setitem__("default", 20),
        [("default_changed", "limit", False)],
    ),
    (
        "nested requirement dropped",
        lambda s: s["properties"]["filters"].__setitem__("required", []),
        [("required_removed", "filters.lang", False)],
    ),
    (
        "made nullable through anyOf",
        lambda s: s["properties"].__setitem__(
            "query", {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]}
        ),
        [("union_changed", "query", False)],
    ),
    (
        "array items unconstrained",
        lambda s: s["properties"]["tags"].pop("items"),
        [("items_changed", "tags[]", False)],
    ),
]

INCOMPATIBLE: list[tuple[str, Any, list[tuple[str, str, bool]]]] = [
    (
        "required parameter added",
        lambda s: (
            s["properties"].__setitem__("index", {"type": "string"})
            or s["required"].append("index")
        ),
        [("field_added", "index", True)],
    ),
    (
        "existing parameter made required",
        lambda s: s["required"].append("limit"),
        [("required_added", "limit", True)],
    ),
    (
        "enum member removed",
        lambda s: s["properties"]["sort"]["enum"].remove("date"),
        [("enum_changed", "sort", True)],
    ),
    (
        "number narrowed to integer",
        lambda s: s["properties"]["query"].__setitem__("type", "integer"),
        [("type_changed", "query", True)],
    ),
    (
        "maximum tightened",
        lambda s: s["properties"]["limit"].__setitem__("maximum", 10),
        [("constraint_changed", "limit", True)],
    ),
    (
        "pattern added",
        lambda s: s["properties"]["query"].__setitem__("pattern", "^[a-z]+$"),
        [("constraint_changed", "query", True)],
    ),
    (
        "nested type changed",
        lambda s: s["properties"]["filters"]["properties"]["lang"].__setitem__("type", "integer"),
        [("type_changed", "filters.lang", True)],
    ),
    (
        "array item type changed",
        lambda s: s["properties"]["tags"]["items"].__setitem__("type", "integer"),
        [("type_changed", "tags[]", True)],
    ),
    (
        "schema closed",
        lambda s: s.__setitem__("additionalProperties", False),
        [("additional_properties_changed", "*", True)],
    ),
    (
        "enum introduced",
        lambda s: s["properties"]["query"].__setitem__("enum", ["a", "b"]),
        [("enum_changed", "query", True)],
    ),
    (
        "renamed (removed + added required): no rename is inferred",
        lambda s: (
            s["properties"].__setitem__("q", s["properties"].pop("query"))
            or s.__setitem__("required", ["q"])
        ),
        [
            ("field_added", "q", True),
            ("field_removed", "query", False),
            ("required_removed", "query", False),
        ],
    ),
]


@pytest.mark.parametrize(
    ("label", "change", "expected"), COMPATIBLE, ids=[c[0] for c in COMPATIBLE]
)
def test_compatible_change(label: str, change: Any, expected: list[tuple[str, str, bool]]) -> None:
    diff = diff_schemas(BASE, _with(change))
    assert diff.status is S.COMPATIBLE, diff.explain()
    assert _kinds(diff) == expected


@pytest.mark.parametrize(
    ("label", "change", "expected"), INCOMPATIBLE, ids=[c[0] for c in INCOMPATIBLE]
)
def test_incompatible_change(
    label: str, change: Any, expected: list[tuple[str, str, bool]]
) -> None:
    diff = diff_schemas(BASE, _with(change))
    assert diff.status is S.INCOMPATIBLE, diff.explain()
    assert _kinds(diff) == expected


class TestAddingAnOptionalParameter:
    """
    Found by the soundness property: an open schema already accepted the
    key, with any value -- so declaring it can reject a call that sent one.
    """

    def test_to_a_closed_schema_is_compatible(self) -> None:
        before = _closed(BASE)
        after = _with(lambda s: s["properties"].__setitem__("page", {"type": "integer"}), before)
        diff = diff_schemas(before, after)
        assert diff.status is S.COMPATIBLE
        assert _kinds(diff) == [("field_added", "page", False)]

    def test_to_an_open_schema_can_reject_a_call_that_already_sent_it(self) -> None:
        after = _with(lambda s: s["properties"].__setitem__("page", {"type": "integer"}))
        diff = diff_schemas(BASE, after)
        assert diff.status is S.INCOMPATIBLE
        assert _kinds(diff) == [("field_added", "page", True)]
        assert "already sent" in diff.changes[0].detail

    def test_with_an_accept_anything_schema_is_compatible_even_when_open(self) -> None:
        after = _with(lambda s: s["properties"].__setitem__("page", {"description": "any"}))
        assert diff_schemas(BASE, after).status is S.COMPATIBLE


def test_removing_a_parameter_from_a_closed_schema_breaks() -> None:
    before = _closed(BASE)
    after = _with(lambda s: s["properties"].pop("sort"), before)
    diff = diff_schemas(before, after)
    assert diff.status is S.INCOMPATIBLE
    assert _kinds(diff) == [("field_removed", "sort", True)]


def test_oneof_changes_always_break() -> None:
    before = {
        "type": "object",
        "properties": {"v": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
    }
    after = copy.deepcopy(before)
    after["properties"]["v"]["oneOf"].append({"type": "boolean"})
    assert diff_schemas(before, after).status is S.INCOMPATIBLE


def test_narrowing_from_a_union_breaks() -> None:
    before = {
        "type": "object",
        "properties": {"v": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
    }
    after = {"type": "object", "properties": {"v": {"type": "string"}}}
    diff = diff_schemas(before, after)
    assert _kinds(diff) == [("union_changed", "v", True)]


class TestRecords:
    def test_records_serialise_and_never_include_absent(self) -> None:
        before = _closed(BASE)
        after = _with(lambda s: s["properties"].__setitem__("page", {"type": "integer"}), before)
        diff = diff_schemas(before, after)
        record = json.loads(json.dumps(diff.to_dict()))
        (change,) = record["changes"]
        assert change == {
            "kind": "field_added",
            "path": "page",
            "keyword": None,
            "breaking": False,
            "detail": "new optional parameter",
            "after": {"type": "integer"},
        }
        assert record["after"]["contract"] == diff.after.contract if diff.after else False

    def test_a_default_of_none_is_not_absent(self) -> None:
        after = _with(lambda s: s["properties"]["query"].__setitem__("default", None))
        (change,) = diff_schemas(BASE, after).changes
        assert change.kind is K.DEFAULT_CHANGED
        assert change.to_dict()["after"] is None

    def test_explain_is_deterministic_and_marks_breaking(self) -> None:
        after = _with(
            lambda s: s["required"].append("limit") or s["properties"]["sort"]["enum"].append("x")
        )
        text = diff_schemas(BASE, after).explain()
        assert text == diff_schemas(BASE, after).explain()
        assert text.splitlines()[0] == "INCOMPATIBLE"
        assert any(line.lstrip().startswith("! required_added") for line in text.splitlines())
        assert any(line.lstrip().startswith("~ enum_changed") for line in text.splitlines())


# ===========================================================================
# Properties
# ===========================================================================

_NAMES = ["a", "b", "c", "d"]
_prop = st.fixed_dictionaries(
    {"type": st.sampled_from(["string", "integer", "number", "boolean"])},
    optional={
        "minimum": st.integers(-3, 3),
        "maximum": st.integers(-3, 3),
        "enum": st.lists(st.integers(-3, 3), min_size=1, max_size=4, unique=True),
        "default": st.integers(-3, 3),
        "description": st.text(max_size=3),
    },
)
_schema = st.builds(
    lambda props, required, closed: {
        "type": "object",
        "properties": props,
        **({"required": sorted(set(required))} if required else {}),
        **({"additionalProperties": False} if closed else {}),
    },
    st.dictionaries(st.sampled_from(_NAMES), _prop, max_size=4),
    st.lists(st.sampled_from(_NAMES), max_size=3),
    st.booleans(),
)
_payload = st.dictionaries(
    st.sampled_from([*_NAMES, "extra"]),
    st.one_of(st.integers(-4, 4), st.booleans(), st.text("ab", max_size=2), st.floats(-4, 4)),
    max_size=5,
)


def _valid(schema: dict[str, Any], payload: dict[str, Any]) -> bool | None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ContractGuard.with_json_schema().validate(schema, payload).is_valid
        except ValueError:
            return None  # schema StateGuard refuses (e.g. an unusable default)


@settings(max_examples=300, deadline=None)
@given(before=_schema, after=_schema)
def test_property_no_changes_iff_same_contract(
    before: dict[str, Any], after: dict[str, Any]
) -> None:
    diff = diff_schemas(before, after)
    same = fingerprint_schema(before).contract == fingerprint_schema(after).contract
    assert (diff.changes == ()) == same, diff.explain()


# Independent pairs are rarely compatible, so this one is kept small; the
# realistic-edits property below is where the soundness claim is worked hard.
@settings(max_examples=100, deadline=None)
@given(before=_schema, after=_schema, payloads=st.lists(_payload, min_size=1, max_size=8))
def test_property_compatible_never_rejects_a_previously_valid_call(
    before: dict[str, Any], after: dict[str, Any], payloads: list[dict[str, Any]]
) -> None:
    """The soundness claim, checked against StateGuard's own validator."""
    diff = diff_schemas(before, after)
    assume(diff.is_compatible)
    for payload in payloads:
        old, new = _valid(before, payload), _valid(after, payload)
        if old is None or new is None:
            continue
        if old:
            assert new, (
                f"{payload!r} valid before, rejected after a {diff.status.value} change:\n{diff.explain()}"
            )


def _mutate(schema: dict[str, Any], seed: int) -> dict[str, Any]:
    """A few realistic edits: what a server's next release actually does."""
    import random

    rng = random.Random(seed)
    out = copy.deepcopy(schema)
    props = out.setdefault("properties", {})
    for _ in range(rng.randint(1, 3)):
        op = rng.choice(
            [
                "add",
                "remove",
                "req+",
                "req-",
                "type",
                "enum+",
                "max",
                "min",
                "close",
                "open",
                "default",
            ]
        )
        names = sorted(props)
        if op == "add":
            props[rng.choice(_NAMES)] = {
                "type": rng.choice(["string", "integer", "number", "boolean"])
            }
        elif op == "remove" and names:
            props.pop(rng.choice(names))
        elif op == "req+":
            out["required"] = sorted(set(out.get("required", [])) | {rng.choice(_NAMES)})
        elif op == "req-" and out.get("required"):
            out["required"].pop()
            if not out["required"]:
                out.pop("required")
        elif op == "close":
            out["additionalProperties"] = False
        elif op == "open":
            out.pop("additionalProperties", None)
        elif names:
            prop = props[rng.choice(names)]
            if op == "type":
                prop["type"] = rng.choice(["string", "integer", "number", "boolean"])
            elif op == "enum+":
                prop["enum"] = sorted({*prop.get("enum", []), rng.randint(-3, 3)})
            elif op in ("max", "min"):
                prop["maximum" if op == "max" else "minimum"] = rng.randint(-3, 3)
            elif op == "default":
                prop["default"] = rng.randint(-3, 3)
    return out


@settings(max_examples=400, deadline=None)
@given(
    before=_schema, seed=st.integers(0, 2**32), payloads=st.lists(_payload, min_size=3, max_size=8)
)
def test_property_compatible_holds_under_realistic_edits(
    before: dict[str, Any], seed: int, payloads: list[dict[str, Any]]
) -> None:
    """
    Independent random pairs are rarely compatible; edits of one schema
    often are, which is where a wrong COMPATIBLE would hide. (A 6,000-example
    run of this found nothing; two earlier rules it did find are fixed.)
    """
    after = _mutate(before, seed)
    diff = diff_schemas(before, after)
    if not diff.is_compatible:
        return
    for payload in payloads:
        old, new = _valid(before, payload), _valid(after, payload)
        if old and new is False:
            raise AssertionError(f"{payload!r} rejected after:\n{diff.explain()}")
