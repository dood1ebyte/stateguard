"""
The MCP drift corpus -- a durable, labelled regression asset.

``corpus/drift_corpus.json`` holds realistic stale and drifted calls against
tool definitions modelled on real MCP servers, and realistic definition
changes. Every case is labelled with what is *correct*, decided before
StateGuard was run against it:

====================  =====================================================
``repairable``        A safe correction exists; auto forwards exactly
                      ``forward``.
``unchanged``         Valid as sent; forwarded (declared defaults filled).
``ambiguous``         A correction exists but is not safe to pick; escalated.
``invalid``           No safe correction; refused.
====================  =====================================================

A case StateGuard currently gets wrong carries ``known_gap`` with the
reason. It must keep failing its label -- a gap that starts passing fails
the run as a stale marker, so the corpus never quietly understates what
StateGuard can do, and never lets a regression hide behind a marker.

Every case is also checked against the invariants that hold for *all*
calls: shadow forwards exactly what was received; a forwarded payload
re-validates and is a fixed point; every value that changed is explained by
a recorded repair or a declared default; decisions are deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stateguard import ContractGuard, GuardConfig
from stateguard.adapters.jsonschema.diff import diff_schemas
from stateguard.adapters.mcp import MCPAction, ToolCallOutcome, ToolRegistry
from stateguard.core.explain import FieldDecisionKind
from stateguard.core.models.config import RepairMode

CORPUS = json.loads((Path(__file__).parent / "corpus" / "drift_corpus.json").read_text())
CALLS: list[dict[str, Any]] = CORPUS["calls"]
CHANGES: list[dict[str, Any]] = CORPUS["schema_changes"]

ACTION = {
    "repairable": MCPAction.FORWARD,
    "unchanged": MCPAction.FORWARD,
    "ambiguous": MCPAction.ESCALATE,
    "invalid": MCPAction.REFUSE,
}


def _registry(mode: RepairMode = RepairMode.AUTO) -> ToolRegistry:
    registry = ToolRegistry(ContractGuard.with_mcp(config=GuardConfig(mode=mode)))
    report = registry.sync(CORPUS["tools"].values())
    assert not report.unusable, report.explain()
    return registry


AUTO = _registry()
SHADOW = _registry(RepairMode.SHADOW)


def _process(registry: ToolRegistry, case: dict[str, Any]) -> ToolCallOutcome:
    return registry.process_call(case["tool"], case["arguments"])


def _meets_label(case: dict[str, Any], outcome: ToolCallOutcome) -> bool:
    if outcome.action is not ACTION[case["class"]]:
        return False
    if "forward" in case and outcome.arguments != case["forward"]:
        return False
    received = {} if case["arguments"] is None else case["arguments"]
    return not (case["class"] == "repairable" and outcome.arguments == received)


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [case["name"] for case in cases]


# ===========================================================================
# Labels
# ===========================================================================


@pytest.mark.parametrize("case", CALLS, ids=_ids(CALLS))
def test_call_matches_its_label(case: dict[str, Any]) -> None:
    outcome = _process(AUTO, case)
    if case.get("known_gap"):
        assert not _meets_label(case, outcome), (
            f"known gap now passes -- remove its marker: {case['known_gap']}"
        )
        return
    assert _meets_label(case, outcome), (
        f"{case['class']} expected ({case['why']}); got {outcome.action.value} "
        f"{outcome.arguments!r}\n{outcome.explain()}"
    )


@pytest.mark.parametrize("change", CHANGES, ids=_ids(CHANGES))
def test_schema_change_matches_its_label(change: dict[str, Any]) -> None:
    diff = diff_schemas(change["before"], change["after"])
    assert diff.status.value == change["status"], f"{change['why']}\n{diff.explain()}"


def test_the_corpus_covers_every_class() -> None:
    assert {case["class"] for case in CALLS} == set(ACTION)
    statuses = {change["status"] for change in CHANGES}
    assert statuses == {"compatible", "incompatible", "equivalent", "invalid", "unsupported"}


# ===========================================================================
# Invariants, on every case
# ===========================================================================


@pytest.mark.parametrize("case", CALLS, ids=_ids(CALLS))
def test_shadow_forwards_exactly_what_was_received(case: dict[str, Any]) -> None:
    shadow = _process(SHADOW, case)
    auto = _process(AUTO, case)
    assert shadow.should_forward
    assert shadow.arguments == ({} if case["arguments"] is None else case["arguments"])
    assert shadow.auto_action is auto.action
    assert shadow.proposed_arguments == auto.proposed_arguments


@pytest.mark.parametrize("case", CALLS, ids=_ids(CALLS))
def test_a_forwarded_payload_is_valid_and_a_fixed_point(case: dict[str, Any]) -> None:
    outcome = _process(AUTO, case)
    if not outcome.should_forward:
        return
    tool = AUTO.get(case["tool"]).definition
    assert AUTO.guard.validate(tool, outcome.arguments).is_valid
    again = AUTO.process_call(case["tool"], outcome.arguments)
    assert again.action is MCPAction.FORWARD
    assert again.arguments == outcome.arguments
    assert not any(d.kind is FieldDecisionKind.REPAIRED for d in again.result.field_decisions())


@pytest.mark.parametrize("case", CALLS, ids=_ids(CALLS))
def test_every_changed_value_is_explained(case: dict[str, Any]) -> None:
    """Explanation consistency: nothing in the forwarded payload differs without a record."""
    outcome = _process(AUTO, case)
    if not outcome.should_forward:
        return
    original = outcome.original_arguments
    sent = json.loads(original) if isinstance(original, str) else original
    repaired = {
        d.path for d in outcome.result.field_decisions() if d.kind is FieldDecisionKind.REPAIRED
    }
    schema = AUTO.get(case["tool"]).input_schema or {}

    def walk(
        forwarded: dict[str, Any], received: dict[str, Any], props: dict[str, Any], prefix: str
    ) -> None:
        for key, value in forwarded.items():
            path = f"{prefix}{key}"
            if path in repaired:
                continue
            if key not in received:
                assert "default" in props.get(key, {}), f"{path} appeared with no record"
                assert value == props[key]["default"]
            elif isinstance(value, dict) and isinstance(received[key], dict):
                walk(value, received[key], props.get(key, {}).get("properties", {}), f"{path}.")
            else:
                assert value == received[key], f"{path} changed with no record"

    walk(outcome.arguments or {}, sent, schema.get("properties", {}), "")


@pytest.mark.parametrize("case", CALLS, ids=_ids(CALLS))
def test_decisions_are_deterministic(case: dict[str, Any]) -> None:
    first = _process(AUTO, case).to_dict()
    assert _process(_registry(), case).to_dict() == first
    assert _process(AUTO, case).explain() == _process(_registry(), case).explain()
