"""
The corpus tests -- ``MCP_ADAPTER_PLAN.md`` §6 Phase 4.2.

*"Assert every corpus schema extracts without error and round-trips."*

What these are evidence of, and what they are not
-------------------------------------------------
They are evidence that the supported subset (§5) is the *right* subset: 21
tool schemas from four widely-deployed public servers all extract, so the
subset was chosen from what tool definitions emit rather than from what was
convenient to implement.

They are not evidence that extraction is *correct* in detail -- that is what
``test_extractor.py``, ``test_type_mapper.py`` and ``test_under_validation.py``
are for, with schemas built to isolate one behaviour each. The corpus catches
the other failure: a shape nobody thought to write a fixture for.

Warnings are asserted, not silenced
-----------------------------------
A ``SchemaFeatureWarning`` means the adapter validated something more loosely
than the schema asked. The corpus is where that stops being hypothetical --
``mcp-server-fetch`` really does use ``exclusiveMinimum`` -- so the test pins
*which* warnings the corpus produces. A new one appearing is a real change in
what StateGuard enforces and should have to be acknowledged here.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from stateguard import ContractGuard
from stateguard.adapters.jsonschema.errors import SchemaFeatureWarning
from stateguard.adapters.mcp import MCPToolAdapter
from stateguard.core.errors.results import RepairStatus
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldConstraintType, FieldType

from .corpus import CorpusTool, load_corpus

CORPUS = load_corpus()

#: Every ``SchemaFeatureWarning`` the corpus is expected to produce, as
#: ``(tool, field, keyword)``. Both are ``mcp-server-fetch``'s ``max_length``,
#: which carries ``exclusiveMinimum``/``exclusiveMaximum`` -- the keywords §5
#: drops rather than rounding into inclusive bounds it would be wrong about.
EXPECTED_WARNINGS: set[tuple[str, str, str]] = {
    ("fetch", "max_length", "exclusiveMinimum"),
    ("fetch", "max_length", "exclusiveMaximum"),
}


# ===========================================================================
# Payload synthesis
# ===========================================================================


def _constraint(spec: FieldSpec, kind: FieldConstraintType) -> Any:
    for constraint in spec.constraints:
        if constraint.constraint_type is kind:
            return constraint.value
    return None


def _value_for(spec: FieldSpec) -> Any:
    """
    A value that satisfies *spec*, or raise.

    Raising on anything unhandled is deliberate. A synthesiser that returned
    ``None`` for a type it did not recognise would turn "the corpus grew a
    shape we cannot build a payload for" into a silently weaker test, which
    is the failure mode this whole phase exists to avoid.
    """
    enum = _constraint(spec, FieldConstraintType.ENUM_VALUES)
    if enum is not None:
        return enum[0]

    if spec.field_type is FieldType.STRING:
        minimum = _constraint(spec, FieldConstraintType.MIN_LENGTH) or 1
        return "x" * max(int(minimum), 1)
    if spec.field_type is FieldType.INTEGER:
        return int(_constraint(spec, FieldConstraintType.MINIMUM) or 1)
    if spec.field_type is FieldType.FLOAT:
        return float(_constraint(spec, FieldConstraintType.MINIMUM) or 1)
    if spec.field_type is FieldType.BOOLEAN:
        return True
    if spec.field_type is FieldType.ARRAY:
        return []
    if spec.field_type is FieldType.OBJECT:
        return _payload(spec.nested_spec, required_only=True) if spec.nested_spec else {}
    if spec.field_type is FieldType.ANY:
        return "x"

    raise AssertionError(
        f"The corpus grew a field type the payload synthesiser cannot build a "
        f"value for: {spec.path!r} is {spec.field_type}. Extend _value_for "
        f"rather than letting the round-trip test quietly stop covering it."
    )


def _payload(contract: ContractSpec, *, required_only: bool) -> dict[str, Any]:
    """A payload that satisfies *contract*."""
    data: dict[str, Any] = {}
    for spec in contract.fields:
        if required_only and not spec.required:
            continue
        if not spec.required and spec.default is not MISSING:
            # An optional field with a declared default is one ``wrap`` fills
            # anyway; letting the synthesiser guess a different value would
            # test the synthesiser, not the adapter.
            continue
        data[spec.path] = _value_for(spec)
    return data


def _extract(tool: CorpusTool) -> ContractSpec:
    """Extract, ignoring the loosening warnings pinned separately below."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SchemaFeatureWarning)
        return MCPToolAdapter().extract_contract(tool.definition)


# ===========================================================================
# The corpus itself
# ===========================================================================


class TestTheCorpusIsWorthTheName:
    """
    Guards on the corpus, not on the adapter.

    §9 criterion 2 asks for 15-20 schemas from *real public* servers. A test
    suite that kept passing while the corpus shrank to two fixtures would
    still be green and would no longer mean anything.
    """

    def test_it_holds_at_least_fifteen_tools(self) -> None:
        assert len(CORPUS) >= 15

    def test_they_come_from_several_independent_servers(self) -> None:
        assert len({tool.package for tool in CORPUS}) >= 3

    def test_every_fixture_records_where_it_came_from(self) -> None:
        """Provenance is what separates a corpus from invented fixtures."""
        for tool in CORPUS:
            assert tool.package
            assert tool.version and tool.version != "unknown"

    def test_every_entry_is_a_real_tool_definition(self) -> None:
        for tool in CORPUS:
            assert tool.definition["name"] == tool.name
            assert isinstance(tool.input_schema, dict)


# ===========================================================================
# 4.2 -- extraction
# ===========================================================================


@pytest.mark.parametrize("tool", CORPUS, ids=str)
class TestEveryCorpusSchemaExtracts:
    def test_it_extracts_without_refusal(self, tool: CorpusTool) -> None:
        """
        §9 criterion 2. A refusal here is not automatically a bug -- it may
        be a keyword §5 deliberately rejects -- but it is always a decision
        someone has to make deliberately, which is why it fails rather than
        skips.
        """
        assert isinstance(_extract(tool), ContractSpec)

    def test_every_declared_property_becomes_a_field(self, tool: CorpusTool) -> None:
        """
        The Phase 1b failure shape, checked structurally: a schema that
        extracts while silently dropping a property looks identical to one
        that extracted correctly, unless something counts.
        """
        declared = set(tool.input_schema.get("properties", {}))
        declared |= set(tool.input_schema.get("required", []))
        assert {spec.path for spec in _extract(tool).fields} == declared

    def test_required_survives_extraction(self, tool: CorpusTool) -> None:
        expected = set(tool.input_schema.get("required", []))
        assert {spec.path for spec in _extract(tool).fields if spec.required} == expected


# ===========================================================================
# 4.2 -- round-trip
# ===========================================================================


@pytest.mark.parametrize("tool", CORPUS, ids=str)
class TestEveryCorpusSchemaRoundTrips:
    """
    A payload built to satisfy the extracted contract must come back
    ``ALREADY_VALID`` -- extraction and validation have to agree about the
    same schema. They are different walks over the same document, and Phase
    1b's ``default``-that-fails-its-own-field bug was exactly a case where
    they did not.
    """

    @staticmethod
    def _round_trip(tool: CorpusTool, *, required_only: bool) -> Any:
        payload = _payload(_extract(tool), required_only=required_only)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SchemaFeatureWarning)
            return ContractGuard.with_mcp().repair(tool.definition, payload)

    def test_a_minimal_valid_payload_needs_no_repair(self, tool: CorpusTool) -> None:
        result = self._round_trip(tool, required_only=True)
        assert result.status is RepairStatus.ALREADY_VALID, result.remaining_violations

    def test_a_fully_populated_payload_needs_no_repair(self, tool: CorpusTool) -> None:
        result = self._round_trip(tool, required_only=False)
        assert result.status is RepairStatus.ALREADY_VALID, result.remaining_violations

    def test_the_output_validates_against_the_contract_it_came_from(self, tool: CorpusTool) -> None:
        """
        The guarantee Phase 1b's declared-default screen restored: whatever
        ``repair`` hands back must survive ``validate``. It did not, once --
        a schema with a default that failed its own field made ``repair``
        report ``ALREADY_VALID`` and ``validate`` reject that same payload.
        """
        result = self._round_trip(tool, required_only=True)
        assert result.repaired_output is not None

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SchemaFeatureWarning)
            validation = ContractGuard.with_mcp().validate(tool.definition, result.repaired_output)
        assert validation.is_valid, validation.violations


# ===========================================================================
# 4.2 -- what the corpus costs in enforcement
# ===========================================================================


def _parse_warning(message: str) -> tuple[str, str] | None:
    """Pull ``(field, keyword)`` out of a dropped-keyword warning."""
    if "was dropped" not in message:
        return None
    field = message.split("Field '", 1)[1].split("'", 1)[0]
    keyword = message.split("': '", 1)[1].split("'", 1)[0]
    return field, keyword


class TestLooseningIsAccountedFor:
    def test_the_corpus_produces_exactly_the_expected_warnings(self) -> None:
        """
        Every ``SchemaFeatureWarning`` is a field validated more loosely than
        its schema asked. Pinning the set means a new one has to be looked
        at rather than absorbed.
        """
        observed: set[tuple[str, str, str]] = set()
        for tool in CORPUS:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                MCPToolAdapter().extract_contract(tool.definition)
            for entry in caught:
                if not issubclass(entry.category, SchemaFeatureWarning):
                    continue
                parsed = _parse_warning(str(entry.message))
                if parsed is not None:
                    observed.add((tool.name, *parsed))

        assert observed == EXPECTED_WARNINGS

    def test_a_real_server_uses_the_keyword_we_drop(self) -> None:
        """
        Not a hypothetical. §5 chose to drop ``exclusiveMinimum`` rather than
        round it into ``MINIMUM``, and ``mcp-server-fetch`` is a deployed
        server that uses it -- so the decision is load-bearing, and the
        warning is the only thing telling a caller their bound is unenforced.
        """
        assert any(keyword.startswith("exclusive") for _, _, keyword in EXPECTED_WARNINGS)
