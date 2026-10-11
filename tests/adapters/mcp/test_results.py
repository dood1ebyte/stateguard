"""Result integrity checks: observation only, value-free, never alter."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from stateguard.adapters.mcp import ResultIssueKind, check_tool_result

K = ResultIssueKind
TOOL: dict[str, Any] = {
    "name": "weather",
    "inputSchema": {"type": "object"},
    "outputSchema": {
        "type": "object",
        "properties": {"temp": {"type": "number"}, "unit": {"type": "string", "enum": ["C", "F"]}},
        "required": ["temp"],
    },
}


def _result(structured: Any = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": "x"}], **extra}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def test_conforming_structured_content_is_ok() -> None:
    check = check_tool_result(TOOL, _result({"temp": 21.5, "unit": "C"}))
    assert check.ok and check.structured_checked and check.issues == ()


def test_non_conforming_structured_content_is_reported_without_values() -> None:
    secret = "SECRET-123"
    check = check_tool_result(TOOL, _result({"temp": secret, "unit": "K"}))
    assert not check.ok
    (issue,) = check.issues
    assert issue.kind is K.STRUCTURED_CONTENT_INVALID
    assert set(issue.paths) == {"temp", "unit"}
    assert secret not in str(check.to_dict())


def test_absent_structured_content_is_advisory() -> None:
    check = check_tool_result(TOOL, _result())
    assert check.ok
    assert [i.kind for i in check.issues] == [K.STRUCTURED_CONTENT_ABSENT]


def test_error_results_are_not_held_to_the_output_schema() -> None:
    assert check_tool_result(TOOL, _result({"anything": 1}, isError=True)).issues == ()


@pytest.mark.parametrize("result", [None, "text", [], {"content": "x"}, {"isError": False}])
def test_malformed_results(result: Any) -> None:
    check = check_tool_result(TOOL, result)
    assert not check.ok and check.issues[0].kind is K.MALFORMED_RESULT


def test_no_output_schema_means_nothing_to_check() -> None:
    assert check_tool_result({"name": "t"}, _result({"x": 1})).issues == ()
    assert check_tool_result(None, _result({"x": 1})).issues == ()


@pytest.mark.parametrize(
    "schema",
    [{"type": "array"}, {"type": "object", "properties": {"a": {"not": {}}}}],
    ids=["non-object root", "unsupported keyword"],
)
def test_an_unreadable_output_schema_is_advisory(schema: dict[str, Any]) -> None:
    check = check_tool_result({**TOOL, "outputSchema": schema}, _result({"a": 1}))
    assert check.ok and check.issues[0].kind is K.OUTPUT_SCHEMA_UNREADABLE


def test_nothing_is_modified() -> None:
    result = _result({"temp": "warm"})
    tool = copy.deepcopy(TOOL)
    before_result, before_tool = copy.deepcopy(result), copy.deepcopy(tool)
    check_tool_result(tool, result)
    assert result == before_result and tool == before_tool
