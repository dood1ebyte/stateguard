"""
JSON Schema 2020-12 conformance for the keywords added in T5.

* Annotation keywords (``$anchor``, ``contentEncoding``, ``contentMediaType``,
  ``contentSchema``, OpenAPI ``example``) are read and change nothing an
  instance may be (2020-12 core section 4.3.1: unknown keywords SHOULD be
  treated as annotations; these are the ones that provably cannot assert).
* The boolean schema ``true`` accepts anything.
* A single-branch ``allOf`` beside annotation-only siblings is its branch.
* ``multipleOf`` and ``uniqueItems`` are enforced, and agree with the
  ``jsonschema`` reference validator (Draft 2020-12) on generated instances,
  in both directions.

Still refused, on purpose: two-branch ``allOf``, ``allOf`` beside an
asserting sibling, ``false``, ``not``, ``if``/``then``/``else``, and
unknown keywords that may loosen validation (OpenAPI ``nullable``: ignoring
it would make StateGuard refuse ``null`` the server accepts).
"""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from stateguard import ContractGuard
from stateguard.adapters.jsonschema import (
    MalformedSchemaError,
    UnsupportedSchemaError,
    fingerprint_schema,
)
from stateguard.adapters.mcp import MCPAction, repair_tool_call


def _tool(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": "t",
        "inputSchema": {"type": "object", "properties": properties, "required": required or []},
    }


def _valid(properties: dict[str, Any], arguments: dict[str, Any]) -> bool:
    return ContractGuard.with_mcp().validate(_tool(properties), arguments).is_valid


S = {"type": "string"}


class TestAnnotations:
    @pytest.mark.parametrize(
        "extra",
        [
            {"$anchor": "a1"},
            {"contentEncoding": "base64"},
            {"contentMediaType": "image/png"},
            {"contentSchema": {"type": "object"}},
            {"example": "hello"},
        ],
    )
    def test_an_annotation_is_read_and_changes_nothing(self, extra: dict[str, Any]) -> None:
        props = {"a": {**S, **extra}}
        assert _valid(props, {"a": "not base64 !!"})
        assert not _valid(props, {"a": 1})
        assert (
            fingerprint_schema(_tool(props)["inputSchema"]).contract
            == fingerprint_schema(_tool({"a": S})["inputSchema"]).contract
        )

    def test_nullable_is_still_refused_because_it_would_loosen(self) -> None:
        with pytest.raises(UnsupportedSchemaError, match="nullable"):
            _valid({"a": {**S, "nullable": True}}, {"a": None})


class TestBooleanTrue:
    def test_true_accepts_anything(self) -> None:
        for value in ("x", 1, None, [1, {}], {"k": "v"}):
            assert _valid({"a": True}, {"a": value})


class TestSingleBranchAllOf:
    DEFS = {"Repo": {"type": "object", "properties": {"name": S}, "required": ["name"]}}

    def _schema(self, field: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "$defs": self.DEFS,
            "properties": {"repo": field},
            "required": ["repo"],
        }

    def test_ref_wrapped_for_a_description_is_its_branch(self) -> None:
        wrapped = self._schema({"allOf": [{"$ref": "#/$defs/Repo"}], "description": "the repo"})
        tool = {"name": "t", "inputSchema": wrapped}
        guard = ContractGuard.with_mcp()
        assert guard.validate(tool, {"repo": {"name": "x"}}).is_valid
        assert not guard.validate(tool, {"repo": {}}).is_valid
        plain = self._schema({"$ref": "#/$defs/Repo", "description": "the repo"})
        assert fingerprint_schema(wrapped).contract == fingerprint_schema(plain).contract

    def test_a_sibling_default_is_kept(self) -> None:
        # Kept through the allOf collapse: a *required* n, missing, is set
        # from it (an optional one is left to the server, as any default).
        tool = _tool({"n": {"allOf": [{"type": "integer"}], "default": 5}}, required=["n"])
        outcome = repair_tool_call(ContractGuard.with_mcp(), tool, {})
        assert outcome.arguments == {"n": 5}

    def test_drift_inside_is_repaired(self) -> None:
        tool = {
            "name": "t",
            "inputSchema": self._schema({"allOf": [{"$ref": "#/$defs/Repo"}], "title": "Repo"}),
        }
        outcome = repair_tool_call(ContractGuard.with_mcp(), tool, {"repo": {"nme": "x"}})
        assert outcome.action is MCPAction.FORWARD and outcome.arguments == {"repo": {"name": "x"}}

    @pytest.mark.parametrize(
        "field",
        [
            {"allOf": [{"type": "string"}, {"minLength": 2}]},
            {"allOf": [{"type": "string"}], "maxLength": 3},
            {"allOf": [{"type": "string"}], "type": "string"},
        ],
        ids=["two branches", "asserting sibling", "type sibling"],
    )
    def test_anything_else_is_still_refused(self, field: dict[str, Any]) -> None:
        with pytest.raises(UnsupportedSchemaError, match="allOf"):
            _valid({"a": field}, {"a": "x"})


class TestMalformed:
    @pytest.mark.parametrize("bad", [0, -1, "5", True, None])
    def test_multiple_of_must_be_positive(self, bad: Any) -> None:
        with pytest.raises(MalformedSchemaError, match="multipleOf"):
            _valid({"a": {"type": "number", "multipleOf": bad}}, {"a": 1})

    @pytest.mark.parametrize("bad", ["yes", 1, None])
    def test_unique_items_must_be_boolean(self, bad: Any) -> None:
        with pytest.raises(MalformedSchemaError, match="uniqueItems"):
            _valid({"a": {"type": "array", "uniqueItems": bad}}, {"a": []})


class TestRepairInteraction:
    def test_a_coerced_value_still_has_to_be_a_multiple(self) -> None:
        tool = _tool({"n": {"type": "integer", "multipleOf": 5}}, ["n"])
        guard = ContractGuard.with_mcp()
        assert repair_tool_call(guard, tool, {"n": "10"}).arguments == {"n": 10}
        assert repair_tool_call(guard, tool, {"n": "7"}).action is MCPAction.REFUSE


# ---------------------------------------------------------------------------
# Differential against the reference validator
# ---------------------------------------------------------------------------

JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-20, 20),
    st.sampled_from([0.0, 1.0, 2.5, -3.0, 0.1, 0.3, 1e300]),
    st.sampled_from(["a", "b", ""]),
)
JSON = st.recursive(
    JSON_SCALARS,
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.sampled_from("xy"), inner, max_size=2)
    ),
    max_leaves=6,
)


def _agree(field: dict[str, Any], value: Any) -> None:
    schema = {"type": "object", "properties": {"a": field}}
    reference = jsonschema.Draft202012Validator(schema).is_valid({"a": value})
    ours = ContractGuard.with_mcp().validate({"name": "t", "inputSchema": schema}, {"a": value})
    assert ours.is_valid == reference, (field, value, ours.violations)


def _json_equal(a: Any, b: Any) -> bool:
    """
    JSON equality as JSON Schema 2020-12 ``uniqueItems`` defines it: JSON booleans
    are **not** JSON numbers (``true`` != ``1``), ``1 == 1.0``, arrays compare
    element-wise, objects by key set and value, strings/null by value. Derived from
    the spec, independent of StateGuard's implementation (so it is a genuine oracle).
    """
    a_bool, b_bool = isinstance(a, bool), isinstance(b, bool)
    if a_bool or b_bool:
        return a_bool and b_bool and a == b
    a_num, b_num = isinstance(a, (int, float)), isinstance(b, (int, float))
    if a_num or b_num:
        return a_num and b_num and float(a) == float(b)
    if isinstance(a, list) or isinstance(b, list):
        return (
            isinstance(a, list)
            and isinstance(b, list)
            and len(a) == len(b)
            and all(_json_equal(x, y) for x, y in zip(a, b, strict=True))
        )
    if isinstance(a, dict) or isinstance(b, dict):
        return (
            isinstance(a, dict)
            and isinstance(b, dict)
            and a.keys() == b.keys()
            and all(_json_equal(a[k], b[k]) for k in a)
        )
    return type(a) is type(b) and a == b  # strings, null


def _unique_items_expected(items: list[Any]) -> bool:
    """
    Sound ground truth for ``uniqueItems``: all items distinct under JSON equality,
    by O(n^2) pairwise comparison. Used instead of the reference validator because
    ``jsonschema._utils.uniq`` sorts then compares only *adjacent* items, so it
    misses non-adjacent duplicates in arrays mixing JSON booleans and numbers
    (Python's ``<`` treats ``True`` and ``1`` as equal and interleaves them). See
    ``docs/runtime/WEEK4H_REGRESSION_AND_HARDENING_GATE.md``.
    """
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _json_equal(items[i], items[j]):
                return False
    return True


@settings(max_examples=500, deadline=None)
@given(value=st.lists(JSON, max_size=5))
# The exact Week 4G counterexample, pinned so the regression is covered forever:
# elements 0 and 2 are the identical JSON value, so the array is NOT unique.
@example(value=[[[True, True, True], None], [[True, True, 1], None], [[True, True, True], None]])
def test_differential_unique_items(value: list[Any]) -> None:
    # Compared against a sound oracle, NOT jsonschema's reference uniq() (unsound for
    # bool/number-mixed arrays; see _unique_items_expected and WEEK4H). StateGuard's
    # own uniqueItems check is spec-correct and must match the sound ground truth.
    schema = {"type": "object", "properties": {"a": {"type": "array", "uniqueItems": True}}}
    ours = ContractGuard.with_mcp().validate({"name": "t", "inputSchema": schema}, {"a": value})
    assert ours.is_valid == _unique_items_expected(value), (value, ours.violations)


def test_unique_items_bool_number_duplicate_where_reference_is_unsound() -> None:
    """
    Regression (Week 4H): an array whose JSON-identical elements are non-adjacent
    after sorting. StateGuard correctly rejects it; the reference ``jsonschema``
    validator wrongly accepts it (its sort+adjacent ``uniq`` misses the duplicate).
    Pins that StateGuard is sound here and documents the reference's limitation so
    the differential oracle choice above is never silently reverted.
    """
    value = [[[True, True, True], None], [[True, True, 1], None], [[True, True, True], None]]
    assert value[0] == value[2], "elements 0 and 2 are the same JSON value"
    assert _unique_items_expected(value) is False  # the sound ground truth: not unique
    schema = {"type": "object", "properties": {"a": {"type": "array", "uniqueItems": True}}}
    ours = ContractGuard.with_mcp().validate({"name": "t", "inputSchema": schema}, {"a": value})
    assert ours.is_valid is False, "StateGuard must flag the duplicate"
    # The reference is unsound for this input class; recorded, not relied upon.
    reference = jsonschema.Draft202012Validator(schema).is_valid({"a": value})
    assert reference is True, "demonstrates the reference validator's known unsoundness"
    # true != 1 (booleans are not numbers), but 1 == 1.0 (numbers compare equal):
    assert _json_equal(True, 1) is False
    assert _json_equal(1, 1.0) is True
    assert _json_equal(True, False) is False


@settings(max_examples=500, deadline=None)
@given(
    value=st.one_of(
        st.integers(-1000, 1000),
        st.floats(-1000, 1000, allow_nan=False, allow_infinity=False),
        st.sampled_from([0.3, 0.6, 1.5, 7.5, 1e300]),
    ),
    factor=st.sampled_from([1, 2, 5, 0.5, 0.1, 1.5, 3]),
)
@example(value=5e-324, factor=2)  # underflow: the quotient rounds to 0.0
def test_differential_multiple_of(value: float, factor: float) -> None:
    _agree({"type": "number", "multipleOf": factor}, value)
