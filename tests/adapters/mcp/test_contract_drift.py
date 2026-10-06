"""
Realistic tool-contract drift, end to end -- the safety property in one place.

Every case below is a call an agent could plausibly send to a tool from the
real-schema corpus (``tests/adapters/jsonschema/corpus``) after the tool or
the agent drifted: a parameter renamed or re-cased, a number sent as a
string, a required parameter the agent does not know about yet, a parameter
the server no longer takes, a schema that grew a construct StateGuard does
not read.

The property under test is not "repair as much as possible". It is:

    SAFE         -> repaired, and the forwarded arguments are exactly right
    AMBIGUOUS    -> not forwarded; the candidates are surfaced
    INVALID      -> not forwarded
    UNSUPPORTED  -> no outcome at all; a named error the caller must handle

and, across all of them, that no call is ever forwarded carrying a value
in a field the model did not put it in. ``test_false_positives.py`` covers
the near-miss *names* in depth; this file covers the drift *scenarios*, and
goes through ``repair_tool_call`` -- the entry point a proxy uses -- so the
None-arguments rule and the outcome mapping are exercised with them.
"""

from __future__ import annotations

import copy
import json
import warnings
from typing import Any

import pytest

from stateguard import ContractGuard, GuardConfig
from stateguard.adapters.jsonschema.errors import (
    JSONSchemaError,
    SchemaFeatureWarning,
    SchemaReferenceError,
    UnsupportedSchemaError,
)
from stateguard.adapters.mcp import MCPAction, ToolCallOutcome, repair_tool_call
from stateguard.core.explain import FieldDecisionKind
from stateguard.core.models.config import RepairMode

from tests.adapters.jsonschema.corpus import load_corpus

TOOLS: dict[str, dict[str, Any]] = {tool.name: tool.definition for tool in load_corpus()}


def _call(tool: str | dict[str, Any], arguments: Any, mode: RepairMode = RepairMode.AUTO) -> Any:
    definition = TOOLS[tool] if isinstance(tool, str) else tool
    guard = ContractGuard.with_mcp(config=GuardConfig(mode=mode))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SchemaFeatureWarning)
        return repair_tool_call(guard, definition, arguments)


def _evolved(tool: str, change: Any) -> dict[str, Any]:
    """The corpus tool with its ``inputSchema`` changed by *change*."""
    definition = copy.deepcopy(TOOLS[tool])
    change(definition["inputSchema"])
    return definition


# ===========================================================================
# SAFE: repaired, and exactly right
# ===========================================================================

SAFE: list[tuple[str, str, Any, dict[str, Any]]] = [
    (
        "camelCase parameters against a snake_case server",
        "git_checkout",
        {"repoPath": "/r", "branchName": "feat"},
        {"repo_path": "/r", "branch_name": "feat"},
    ),
    (
        "a parameter renamed to a longer name",
        "describe_table",
        {"table": "users"},
        {"table_name": "users"},
    ),
    (
        "an abbreviated parameter",
        "git_show",
        {"repo_path": "/r", "rev": "HEAD"},
        {"repo_path": "/r", "revision": "HEAD"},
    ),
    (
        "a renamed parameter, with an optional default filled",
        "git_diff",
        {"repo": "/r", "target": "main"},
        {"repo_path": "/r", "target": "main", "context_lines": 3},
    ),
    (
        "a number sent as a string",
        "git_log",
        {"repo_path": "/r", "max_count": "5"},
        {"repo_path": "/r", "max_count": 5},
    ),
    (
        "an integer sent as 5.0",
        "git_log",
        {"repo_path": "/r", "max_count": 5.0},
        {"repo_path": "/r", "max_count": 5},
    ),
    (
        "one file where a list of files is declared",
        "git_add",
        {"repo_path": "/r", "files": "a.txt"},
        {"repo_path": "/r", "files": ["a.txt"]},
    ),
    (
        "the whole argument object sent as a JSON string",
        "read_query",
        '{"query": "select 1"}',
        {"query": "select 1"},
    ),
    (
        "no arguments to a tool that takes none (MCP's arguments=None)",
        "list_tables",
        None,
        {},
    ),
]


class TestSafeDriftIsRepairedExactly:
    @pytest.mark.parametrize(
        ("tool", "arguments", "expected"),
        [case[1:] for case in SAFE],
        ids=[case[0] for case in SAFE],
    )
    def test_auto_forwards_exactly_the_expected_arguments(
        self, tool: str, arguments: Any, expected: dict[str, Any]
    ) -> None:
        outcome = _call(tool, arguments)
        assert outcome.action is MCPAction.FORWARD
        assert outcome.arguments == expected

    @pytest.mark.parametrize(
        ("tool", "arguments", "expected"),
        [case[1:] for case in SAFE],
        ids=[case[0] for case in SAFE],
    )
    def test_shadow_forwards_the_original_and_previews_the_same_repair(
        self, tool: str, arguments: Any, expected: dict[str, Any]
    ) -> None:
        outcome = _call(tool, arguments, RepairMode.SHADOW)
        assert outcome.should_forward
        sent = {} if arguments is None else arguments
        if outcome.action is MCPAction.HOLD:
            assert outcome.arguments == sent
            assert outcome.preview == expected
        else:
            # Nothing to withhold: what auto would send is what was sent.
            assert outcome.arguments == expected == sent

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [case[1:3] for case in SAFE],
        ids=[case[0] for case in SAFE],
    )
    def test_every_change_is_accounted_for(self, tool: str, arguments: Any) -> None:
        """
        Each repaired field has a REPAIRED decision with a rationale and a
        trust score -- the answer to "why was this considered safe".
        """
        outcome = _call(tool, arguments)
        for decision in outcome.result.field_decisions():
            if decision.kind is FieldDecisionKind.REPAIRED:
                assert decision.operation is not None
                assert decision.reason
                assert 0.0 < decision.operation.trust <= 1.0


# ===========================================================================
# AMBIGUOUS: not forwarded, candidates surfaced
# ===========================================================================


class TestAmbiguousDriftIsNotForwarded:
    def test_a_bare_timezone_against_source_and_target_timezone(self) -> None:
        """
        The agent still calls ``convert_time`` the way it calls
        ``get_current_time``. ``timezone`` fits both parameters equally, so
        picking either would be a coin flip with the user's clock.
        """
        outcome = _call("convert_time", {"timezone": "UTC", "time": "10:00"})
        assert not outcome.should_forward
        assert outcome.arguments is None

    def test_a_shared_prefix_escalates_with_its_candidate(self) -> None:
        tool = {
            "name": "lookup_account",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "account_name": {"type": "string"},
                },
                "required": ["account_id", "account_name"],
            },
        }
        outcome = _call(tool, {"account": "acme"})
        assert outcome.action is MCPAction.ESCALATE
        held = [d for d in outcome.result.field_decisions() if d.kind is FieldDecisionKind.HELD]
        assert [c.source_path for d in held for c in d.candidates] == ["account"]
        assert "held" in outcome.explain()


# ===========================================================================
# INVALID: not forwarded
# ===========================================================================

INVALID: list[tuple[str, str, Any, str]] = [
    (
        "the server added a required parameter the agent does not send",
        "git_commit",
        {"repo_path": "/r"},
        "message",
    ),
    (
        "a word where a number is declared",
        "git_log",
        {"repo_path": "/r", "max_count": "ten"},
        "max_count",
    ),
    ("a number where a string is declared", "fetch", {"url": 123}, "url"),
    ("no arguments to a tool that needs some", "read_query", None, "query"),
]


class TestInvalidCallsAreRefused:
    @pytest.mark.parametrize(
        ("tool", "arguments", "field"),
        [case[1:] for case in INVALID],
        ids=[case[0] for case in INVALID],
    )
    def test_it_is_refused_naming_the_field(self, tool: str, arguments: Any, field: str) -> None:
        outcome = _call(tool, arguments)
        assert outcome.action is MCPAction.REFUSE
        assert outcome.arguments is None
        assert f"'{field}'" in outcome.reason

    def test_a_parameter_the_server_dropped_is_refused_when_the_schema_is_closed(self) -> None:
        closed = _evolved("git_status", lambda s: s.__setitem__("additionalProperties", False))
        outcome = _call(closed, {"repo_path": "/r", "verbose": True})
        assert outcome.action is MCPAction.REFUSE
        assert "'verbose'" in outcome.reason


# ===========================================================================
# Passed through: forwarded as received, and said so
# ===========================================================================


class TestUndeclaredKeysArePassedThroughVisibly:
    """
    An open schema allows keys it does not declare, so the call is valid and
    is forwarded with them untouched -- never renamed onto a declared field
    unless the evidence says so. What changed is that the record now says
    they were there.
    """

    def test_a_parameter_the_server_dropped_is_forwarded_untouched(self) -> None:
        outcome = _call("git_status", {"repo_path": "/r", "verbose": True})
        assert outcome.action is MCPAction.FORWARD
        assert outcome.arguments == {"repo_path": "/r", "verbose": True}
        assert ("verbose", FieldDecisionKind.UNRESOLVED) in [
            (d.path, d.kind) for d in outcome.result.field_decisions()
        ]

    def test_drift_on_an_optional_parameter_is_visible_in_the_explanation(self) -> None:
        """
        The documented limitation, made visible: ``maxLength`` is not renamed
        onto the optional ``max_length``, whose default is filled instead.
        The server would have done the same with no proxy -- but the
        explanation must not read as though nothing was off.
        """
        outcome = _call("fetch", {"url": "https://example.com", "maxLength": 2000})
        assert outcome.action is MCPAction.FORWARD
        assert outcome.arguments is not None
        assert outcome.arguments["max_length"] == 5000
        assert outcome.arguments["maxLength"] == 2000
        assert "unresolved  maxLength" in outcome.explain()

    def test_an_undeclared_key_alongside_a_repair_is_still_listed(self) -> None:
        outcome = _call("git_diff", {"repo": "/r", "target": "main", "verbose": True})
        assert outcome.action is MCPAction.FORWARD
        kinds = [(d.path, d.kind) for d in outcome.result.field_decisions()]
        assert ("repo_path", FieldDecisionKind.REPAIRED) in kinds
        assert ("verbose", FieldDecisionKind.UNRESOLVED) in kinds
        assert ("repo", FieldDecisionKind.UNRESOLVED) not in kinds  # consumed by the rename


# ===========================================================================
# UNSUPPORTED: no outcome, a named error
# ===========================================================================

UNSUPPORTED: list[tuple[str, Any, type[JSONSchemaError]]] = [
    (
        "the schema grew an allOf",
        lambda s: s.__setitem__("allOf", [{"required": ["repo_path"]}]),
        UnsupportedSchemaError,
    ),
    (
        "a parameter became a remote $ref",
        lambda s: s["properties"].__setitem__("repo_path", {"$ref": "https://x.test/p.json"}),
        SchemaReferenceError,
    ),
    (
        "a parameter's type is not a JSON Schema type",
        lambda s: s["properties"].__setitem__("repo_path", {"type": "path"}),
        UnsupportedSchemaError,
    ),
]


class TestUnsupportedSchemasRaise:
    @pytest.mark.parametrize(
        ("change", "error"),
        [case[1:] for case in UNSUPPORTED],
        ids=[case[0] for case in UNSUPPORTED],
    )
    def test_no_outcome_is_invented(self, change: Any, error: type[JSONSchemaError]) -> None:
        with pytest.raises(error):
            _call(_evolved("git_status", change), {"repo_path": "/r"})

    def test_every_one_is_catchable_as_one_type(self) -> None:
        for _label, change, _error in UNSUPPORTED:
            with pytest.raises(JSONSchemaError):
                _call(_evolved("git_status", change), {"repo_path": "/r"})


# ===========================================================================
# Across everything: nothing is forwarded that the model did not send
# ===========================================================================


#: Every declared default across the corpus, by parameter name. The corpus
#: never declares two different defaults under one name, which the
#: assertion below relies on and therefore checks.
TOOLS_BY_ARGS_DEFAULTS: dict[str, Any] = {}
for _definition in TOOLS.values():
    for _name, _prop in _definition["inputSchema"].get("properties", {}).items():
        if "default" in _prop and _prop["default"] is not None:
            assert TOOLS_BY_ARGS_DEFAULTS.get(_name, _prop["default"]) == _prop["default"]
            TOOLS_BY_ARGS_DEFAULTS[_name] = _prop["default"]


def _all_outcomes() -> list[tuple[str, ToolCallOutcome]]:
    calls = [(c[0], c[1], c[2]) for c in SAFE] + [(c[0], c[1], c[2]) for c in INVALID]
    calls.append(("timezone", "convert_time", {"timezone": "UTC", "time": "10:00"}))
    calls.append(("dropped", "git_status", {"repo_path": "/r", "verbose": True}))
    return [(label, _call(tool, arguments)) for label, tool, arguments in calls]


@pytest.mark.parametrize(("label", "outcome"), _all_outcomes(), ids=lambda v: str(v)[:40])
def test_every_forwarded_value_came_from_the_model_or_the_schema(
    label: str, outcome: ToolCallOutcome
) -> None:
    """
    A forwarded field holds either a value the model sent (possibly
    re-typed or moved by a recorded operation) or a default the schema
    declared. Nothing else -- in particular, never a value from one of the
    model's keys moved onto a field no recorded operation names.
    """
    if not outcome.should_forward:
        assert outcome.arguments is None
        return
    assert outcome.arguments is not None
    original = outcome.result.original_input
    # A JSON-encoded root is recorded as received -- the string -- so the
    # values the model sent are the ones inside it.
    sent = json.loads(original) if isinstance(original, str) else original or {}
    moved = {
        d.path: d.operation
        for d in outcome.result.field_decisions()
        if d.kind is FieldDecisionKind.REPAIRED and d.operation is not None
    }
    for field, value in outcome.arguments.items():
        if field in moved:
            continue  # accounted for by a recorded, trusted operation
        if field in sent:
            assert value == sent[field], f"{label}: {field!r} changed with no operation"
        else:
            # Not sent and not repaired: it can only be a declared default.
            declared = TOOLS_BY_ARGS_DEFAULTS.get(field)
            assert declared is not None and value == declared, (
                f"{label}: {field!r} appeared with no operation and no declared default"
            )
