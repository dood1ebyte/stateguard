"""
Arrays of objects: elements are validated against their item contract.

Before this, ``item_type=OBJECT`` was the whole story -- an element's
contents were never checked, and ``{"files": [{"path": 1}]}`` against items
requiring a string ``path`` was reported as matching the tool's schema.
Elements are now validated and violations reported at ``files[0].path``.
They are never *repaired*: no strategy addresses a path through a list, so
the call is refused rather than forwarded under a false claim.
"""

from __future__ import annotations

import json
import warnings
from typing import Any

import pytest

from stateguard import ContractGuard
from stateguard.adapters.jsonschema import (
    JSONSchemaAdapter,
    SchemaFeatureWarning,
    UnsupportedSchemaError,
)
from stateguard.adapters.mcp import MCPAction, repair_tool_call
from stateguard.core.errors.results import RepairStatus

S = {"type": "string"}

PUSH_FILES: dict[str, Any] = {
    "name": "push_files",
    "inputSchema": {
        "type": "object",
        "properties": {
            "owner": S,
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": S,
                        "content": S,
                        "mode": {"type": "string", "enum": ["100644", "100755"]},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        "required": ["owner", "files"],
    },
}


def _call(arguments: Any) -> Any:
    return repair_tool_call(ContractGuard.with_mcp(), PUSH_FILES, arguments)


class TestElementsAreValidated:
    def test_a_valid_array_forwards(self) -> None:
        outcome = _call(
            {"owner": "o", "files": [{"path": "a", "content": "x"}, {"path": "b", "content": "y"}]}
        )
        assert outcome.action is MCPAction.FORWARD

    @pytest.mark.parametrize(
        ("files", "path", "kind"),
        [
            ([{"path": 1, "content": "x"}], "files[0].path", "type_mismatch"),
            (
                [{"path": "a", "content": "x"}, {"content": "y"}],
                "files[1].path",
                "missing_required_field",
            ),
            (
                [{"path": "a", "content": "x", "mode": "777"}],
                "files[0].mode",
                "value_constraint_violation",
            ),
            ([{"path": None, "content": "x"}], "files[0].path", "null_not_allowed"),
        ],
    )
    def test_a_bad_element_is_refused_with_its_index(
        self, files: list[Any], path: str, kind: str
    ) -> None:
        outcome = _call({"owner": "o", "files": files})
        assert outcome.action is MCPAction.REFUSE
        remaining = {
            (v.field_path, v.violation_type.value) for v in outcome.result.remaining_violations
        }
        assert (path, kind) in remaining
        assert path in outcome.reason

    def test_nested_objects_inside_elements(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cell": {
                                "type": "object",
                                "properties": {"n": {"type": "integer"}},
                                "required": ["n"],
                            }
                        },
                    },
                }
            },
        }
        result = ContractGuard.with_json_schema().repair(
            schema, {"rows": [{"cell": {"n": 1}}, {"cell": {}}]}
        )
        assert result.status is RepairStatus.FAILED
        assert [v.field_path for v in result.remaining_violations] == ["rows[1].cell.n"]

    def test_a_closed_element_rejects_extra_keys(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"k": S},
                        "additionalProperties": False,
                    },
                }
            },
        }
        result = ContractGuard.with_json_schema().repair(schema, {"tags": [{"k": "a", "x": 1}]})
        assert result.status is RepairStatus.FAILED
        assert [v.field_path for v in result.remaining_violations] == ["tags[0].x"]

    def test_elements_with_nothing_declared_are_only_typed(self) -> None:
        schema = {
            "type": "object",
            "properties": {"xs": {"type": "array", "items": {"type": "object"}}},
        }
        assert JSONSchemaAdapter().extract_contract(schema).fields[0].item_spec is None
        assert ContractGuard.with_json_schema().validate(schema, {"xs": [{"anything": 1}]}).is_valid


class TestElementsAreNeverRepaired:
    def test_a_drifted_key_inside_an_element_is_refused_not_guessed(self) -> None:
        """A missed repair, never a wrong one -- and never an invented key."""
        sent = {"owner": "o", "files": [{"filePath": "a", "content": "x"}]}
        outcome = _call(sent)
        assert outcome.action is MCPAction.REFUSE
        for attempt in outcome.result.attempts:
            for op in attempt.applied_operations:
                assert "[" not in op.target_path

    def test_no_bracketed_key_is_ever_written(self) -> None:
        """
        _set_nested creates intermediate dicts; a strategy handed
        'files[0].path' would have produced a top-level key 'files[0]'.
        """
        sent = {"ownr": "o", "files": [{"filePath": "a", "content": "x"}]}
        result = ContractGuard.with_mcp().repair(PUSH_FILES, sent)
        payloads = [result.repaired_output, result.proposed_output] + [
            a.data_after for a in result.attempts
        ]
        for payload in payloads:
            assert "files[0]" not in json.dumps(payload or {}).replace('"files": [', "")

    def test_top_level_repair_still_happens_around_a_valid_array(self) -> None:
        outcome = _call({"ownr": "o", "files": [{"path": "a", "content": "x"}]})
        assert outcome.action is MCPAction.FORWARD
        assert outcome.arguments == {"owner": "o", "files": [{"path": "a", "content": "x"}]}


class TestTheMarkerIsUnambiguous:
    @pytest.mark.parametrize("name", ["a[0]", "x]", "[y"])
    def test_property_names_with_brackets_are_refused(self, name: str) -> None:
        with pytest.raises(UnsupportedSchemaError, match="array elements"):
            JSONSchemaAdapter().extract_contract({"type": "object", "properties": {name: S}})


class TestObjectUnionBranches:
    def test_the_remaining_gap_is_loud(self) -> None:
        """Object branches of a union are still typed, not validated -- and say so."""
        schema = {
            "type": "object",
            "properties": {
                "target": {
                    "anyOf": [
                        {"type": "object", "properties": {"email": S}, "required": ["email"]},
                        {"type": "object", "properties": {"phone": S}, "required": ["phone"]},
                    ]
                }
            },
        }
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            JSONSchemaAdapter().extract_contract(schema)
        messages = [str(w.message) for w in caught if issubclass(w.category, SchemaFeatureWarning)]
        assert any("does not validate" in m for m in messages)

    def test_and_the_registry_records_it_per_tool(self) -> None:
        from stateguard.adapters.mcp import ToolRegistry

        registry = ToolRegistry()
        registry.register(
            {
                "name": "notify",
                "inputSchema": {
                    "type": "object",
                    "properties": {"t": {"oneOf": [{"type": "object", "properties": {"a": S}}, S]}},
                },
            }
        )
        assert any("does not validate" in w for w in registry.get("notify").warnings)
