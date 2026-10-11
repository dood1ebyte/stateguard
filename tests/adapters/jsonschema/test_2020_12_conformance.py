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
        tool = _tool({"n": {"allOf": [{"type": "integer"}], "default": 5}})
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


@settings(max_examples=500, deadline=None)
@given(value=st.lists(JSON, max_size=5))
def test_differential_unique_items(value: list[Any]) -> None:
    _agree({"type": "array", "uniqueItems": True}, value)


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
