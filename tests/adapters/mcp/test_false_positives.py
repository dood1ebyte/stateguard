"""
False positives -- ``MCP_ADAPTER_PLAN.md`` §6 Phase 4.3, §9 criterion 5.

*"The false-positive corpus produces zero wrong repairs; near-misses refuse."*

Why this is the test that matters most
--------------------------------------
§8 names a confident wrong rename as "the failure that damages trust most",
and it is the only failure StateGuard can cause that the alternative cannot.
A missed repair leaves the caller exactly where they were: the server rejects
the call, which is what would have happened anyway. A *wrong* repair sends
the server a well-formed call meaning something the model did not ask for,
and nothing downstream can tell. One of those costs a round trip; the other
costs the reason anyone would run this in production.

So these tests are deliberately asymmetric. They do not assert that a repair
happens. They assert that when one happens it lands on the right field, and
that when the evidence is thin nothing happens at all.

Built from the real corpus
--------------------------
The near-misses are not invented: they are drawn from
``tests/adapters/jsonschema/corpus``, so the field names that have to be told
apart are names real servers really ship. ``mcp-server-git`` gives
``repo_path`` on every tool alongside ``branch_name`` / ``base_branch``;
``mcp-server-time`` gives ``source_timezone`` and ``target_timezone`` on one
call. Those pairs are far harder than anything a fixture author would think
to write, because a fixture author knows what they are testing.
"""

from __future__ import annotations

import warnings
import zlib
from typing import Any

import pytest

from stateguard import ContractGuard
from stateguard.adapters.jsonschema.errors import SchemaFeatureWarning
from stateguard.adapters.mcp import MCPAction, MCPToolAdapter, outcome_for
from stateguard.core.errors.results import RepairStatus
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldConstraintType, FieldType

from tests.adapters.jsonschema.corpus import CorpusTool, load_corpus

CORPUS = load_corpus()
BY_NAME = {tool.name: tool for tool in CORPUS}


# ===========================================================================
# Helpers
# ===========================================================================


def _extract(tool: CorpusTool) -> ContractSpec:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SchemaFeatureWarning)
        return MCPToolAdapter().extract_contract(tool.definition)


def _value_for(spec: FieldSpec, tag: str) -> Any:
    """
    A distinct, traceable value for *spec*.

    Distinct matters: if every string field got the same value, "the sentinel
    ended up on the right field" could not be told from "the sentinel ended up
    on three fields", and the wrong-repair assertions would pass vacuously.
    """
    for constraint in spec.constraints:
        if constraint.constraint_type is FieldConstraintType.ENUM_VALUES:
            return constraint.value[0]

    if spec.field_type in (FieldType.STRING, FieldType.ANY):
        return f"value-of-{tag}"
    if spec.field_type is FieldType.INTEGER:
        return _distinct_int(spec, tag)
    if spec.field_type is FieldType.FLOAT:
        return 1.5
    if spec.field_type is FieldType.BOOLEAN:
        return True
    if spec.field_type is FieldType.ARRAY:
        return []
    raise AssertionError(f"No sentinel for {spec.field_type} on {spec.path!r}")


def _distinct_int(spec: FieldSpec, tag: str) -> int:
    """
    An integer inside *spec*'s declared bounds, spread across the whole range.

    Two properties are needed and the obvious one-liner has neither. It has
    to stay inside ``minimum``/``maximum`` -- ``mcp-server-fetch``'s
    ``start_index`` declares ``minimum: 0`` -- and it has to be unlikely to
    repeat, because ``_landed_on`` finds a value rather than tracking a key.
    Two fields sharing a sentinel makes the abbreviation assertion fail *as
    if the adapter mis-repaired*, which is the worst way for a test helper to
    break. ``1 + (sum(ord) % 5)`` offered five values, three in practice.

    ``crc32`` rather than ``hash``: stable across interpreter runs, so a
    failure reproduces.
    """
    low = _constraint(spec, FieldConstraintType.MINIMUM)
    high = _constraint(spec, FieldConstraintType.MAXIMUM)
    floor = int(low) if low is not None else 1
    ceiling = int(high) if high is not None else floor + 99_999
    return floor + zlib.crc32(tag.encode("utf-8")) % max(ceiling - floor + 1, 1)


def _constraint(spec: FieldSpec, kind: FieldConstraintType) -> Any:
    for constraint in spec.constraints:
        if constraint.constraint_type is kind:
            return constraint.value
    return None


def _unique_sentinel(payload: dict[str, Any], field: str) -> Any:
    """
    *field*'s value, asserted unique within *payload*.

    ``_landed_on`` answers "where did this value end up", so a value carried
    by two fields cannot answer it. Checking here means a sentinel collision
    fails pointing at the helper instead of accusing the adapter of a wrong
    repair it did not make.
    """
    sentinel = payload[field]
    twins = sorted(key for key, value in payload.items() if value == sentinel)
    assert twins == [field], (
        f"Test-helper problem, not an adapter problem: the sentinel for "
        f"{field!r} is also the value of {[k for k in twins if k != field]!r}, "
        f"so _landed_on cannot tell where a repair went. Widen _value_for's "
        f"sentinel space for {payload[field]!r}."
    )
    return sentinel


def _required(tool: CorpusTool) -> list[FieldSpec]:
    return [spec for spec in _extract(tool).fields if spec.required]


def _valid_payload(tool: CorpusTool) -> dict[str, Any]:
    return {spec.path: _value_for(spec, spec.path) for spec in _required(tool)}


def _call(tool: CorpusTool, arguments: dict[str, Any]) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SchemaFeatureWarning)
        result = ContractGuard.with_mcp().repair(tool.definition, arguments)
    return outcome_for(result, arguments)


def _landed_on(outcome: Any, sentinel: Any) -> list[str]:
    """Which keys of the forwarded payload carry *sentinel*."""
    return sorted(key for key, value in (outcome.arguments or {}).items() if value == sentinel)


#: Every ``(tool, required field)`` pair in the corpus, as parameters.
RENAME_TARGETS = [
    pytest.param(tool, spec.path, id=f"{tool.name}:{spec.path}")
    for tool in CORPUS
    for spec in _required(tool)
]

#: The same, narrowed to fields whose name has a plausible abbreviation --
#: ``repo_path`` -> ``repo``. Skipped where the abbreviation is itself a
#: declared parameter, which would make it a different test.
ABBREVIATIONS = [
    pytest.param(tool, spec.path, spec.path.split("_")[0], id=f"{tool.name}:{spec.path}")
    for tool in CORPUS
    for spec in _required(tool)
    if "_" in spec.path and spec.path.split("_")[0] not in {f.path for f in _extract(tool).fields}
]


# ===========================================================================
# 4.3 -- nothing is guessed from nothing
# ===========================================================================


@pytest.mark.parametrize(("tool", "field"), RENAME_TARGETS)
class TestAnUnrecognisableKeyIsNeverGuessedOnto:
    """
    A key bearing no resemblance to anything the tool declares must not be
    renamed onto a field just because a field happens to be missing.

    Swept across every required parameter of all 21 corpus tools rather than
    argued from one example: "the threshold is high enough" is a claim about
    a distribution, and one hand-picked case cannot make it.
    """

    def test_a_garbage_key_is_not_forwarded(self, tool: CorpusTool, field: str) -> None:
        payload = _valid_payload(tool)
        payload.pop(field)
        payload["qqqq_zzzz"] = "value-of-garbage"

        outcome = _call(tool, payload)
        assert outcome.action is not MCPAction.FORWARD

    def test_the_garbage_value_never_reaches_a_declared_field(
        self, tool: CorpusTool, field: str
    ) -> None:
        """
        Asserted against the **engine's own working copy**, not the forwarded
        payload.

        Checking ``outcome.arguments`` could not fail: it is ``None`` for
        every action but ``FORWARD``, which the test above already rules out,
        so the assertion was true by construction rather than by behaviour.
        ``attempt.data_after`` is the payload as each repair attempt left it,
        and it exists whatever the verdict -- so this catches a rename that
        *was* applied and then failed for some unrelated reason, which is
        exactly the case where a wrong repair would hide behind a refusal.
        """
        payload = _valid_payload(tool)
        payload.pop(field)
        payload["qqqq_zzzz"] = "value-of-garbage"

        outcome = _call(tool, payload)
        declared = {spec.path for spec in _extract(tool).fields}

        for attempt in outcome.result.attempts:
            for key, value in (attempt.data_after or {}).items():
                assert not (value == "value-of-garbage" and key in declared), (
                    f"An unrecognisable key was renamed onto declared field "
                    f"{key!r} during {attempt.strategy_name}."
                )


# ===========================================================================
# 4.3 -- when a repair does happen, it is the right one
# ===========================================================================


@pytest.mark.parametrize(("tool", "field", "abbreviation"), ABBREVIATIONS)
class TestAPlausibleAbbreviationLandsOnTheRightField:
    """
    The positive control, and the half that makes the negative one mean
    something: a suite where nothing ever repairs would pass every test above.

    These are the cases where a repair *should* happen -- ``repo`` for
    ``repo_path``, ``source`` for ``source_timezone`` -- and the tools are
    chosen by the corpus, not by hand, so ``convert_time`` lands here with
    both ``source_timezone`` and ``target_timezone`` in play at once.
    """

    def test_it_either_repairs_correctly_or_refuses(
        self, tool: CorpusTool, field: str, abbreviation: str
    ) -> None:
        payload = _valid_payload(tool)
        sentinel = _unique_sentinel(payload, field)
        del payload[field]
        payload[abbreviation] = sentinel

        outcome = _call(tool, payload)
        if outcome.action is not MCPAction.FORWARD:
            return  # Refusing is always an acceptable answer. Guessing is not.
        assert _landed_on(outcome, sentinel) == [field]

    def test_no_other_parameter_is_disturbed(
        self, tool: CorpusTool, field: str, abbreviation: str
    ) -> None:
        """
        A rename moves one key. Everything the model did get right has to
        arrive at the server unchanged -- a repair that quietly rewrote a
        neighbouring field would be invisible to the caller.
        """
        payload = _valid_payload(tool)
        sentinel = _unique_sentinel(payload, field)
        del payload[field]
        payload[abbreviation] = sentinel

        outcome = _call(tool, payload)
        if outcome.action is not MCPAction.FORWARD:
            return
        for key, value in payload.items():
            if key == abbreviation:
                continue
            assert outcome.arguments[key] == value


# ===========================================================================
# 4.3 -- genuine ambiguity
# ===========================================================================


class TestGenuineAmbiguityIsNotResolved:
    """
    ``mcp-server-time``'s ``convert_time`` is the corpus's gift here: it
    requires ``source_timezone`` *and* ``target_timezone``, so a model that
    sends a bare ``timezone`` has said something that genuinely could mean
    either. There is no right answer, and inventing one would send the server
    a conversion in the wrong direction -- a call that succeeds and returns
    the wrong time, which is the worst shape a failure can take.
    """

    @property
    def tool(self) -> CorpusTool:
        return BY_NAME["convert_time"]

    def test_a_bare_timezone_against_two_timezone_parameters_is_not_forwarded(self) -> None:
        arguments = {"time": "10:00", "timezone": "Europe/London", "zone": "Asia/Kolkata"}
        assert _call(self.tool, arguments).action is not MCPAction.FORWARD

    def test_neither_timezone_parameter_is_filled_in(self) -> None:
        arguments = {"time": "10:00", "timezone": "Europe/London", "zone": "Asia/Kolkata"}
        outcome = _call(self.tool, arguments)
        assert outcome.arguments is None

    def test_one_ambiguous_key_among_correct_ones_still_refuses(self) -> None:
        """
        Half-right is the dangerous shape: ``target_timezone`` is spelled
        correctly, so only ``source_timezone`` is missing and the pairing
        looks unambiguous -- but ``timezone`` is no more evidence for the
        source than for the target, and the tool is one where getting it
        backwards produces a plausible wrong answer rather than an error.
        """
        arguments = {
            "time": "10:00",
            "timezone": "Europe/London",
            "target_timezone": "Asia/Kolkata",
        }
        outcome = _call(self.tool, arguments)
        assert outcome.action is not MCPAction.FORWARD


# ===========================================================================
# 4.3 -- values, not just names
# ===========================================================================


class TestAValueThatOnlyLooksCoercibleIsRefused:
    """
    The other half of a false positive: not renaming the wrong field, but
    coercing a value into a type it does not actually hold.
    """

    def test_a_word_is_not_coerced_into_an_integer(self) -> None:
        tool = BY_NAME["git_log"]
        outcome = _call(tool, {"repo_path": "/repo", "max_count": "many"})
        assert outcome.action is MCPAction.REFUSE
        assert outcome.result.status is RepairStatus.FAILED

    def test_a_numeric_string_is_coerced(self) -> None:
        """
        The control. ``"5"`` really is 5, and refusing it would make the
        adapter useless for the single most common drift shape there is.
        """
        tool = BY_NAME["git_log"]
        outcome = _call(tool, {"repo_path": "/repo", "max_count": "5"})
        assert outcome.action is MCPAction.FORWARD
        assert outcome.arguments["max_count"] == 5

    def test_a_value_outside_a_declared_bound_is_refused(self) -> None:
        """
        ``mcp-server-fetch``'s ``start_index`` declares ``minimum: 0``. A
        negative index is not a typo to fix; there is no honest repair, and
        inventing one would be StateGuard deciding what the caller meant.
        """
        tool = BY_NAME["fetch"]
        outcome = _call(tool, {"url": "https://example.com", "start_index": -5})
        assert outcome.action is not MCPAction.FORWARD


# ===========================================================================
# 4.3 -- the limitation the corpus exposed
# ===========================================================================


class TestDriftOnAnOptionalParameterIsNotRepaired:
    """
    Found by the corpus, and recorded rather than quietly fixed.

    ``FuzzyFieldMatchStrategy`` pairs an ``UNEXPECTED_FIELD`` with a
    ``MISSING_REQUIRED_FIELD``. An *optional* field is never reported missing
    -- that is what optional means -- so a misspelled optional parameter has
    no repair target, and even a one-character typo goes uncorrected. 12 of
    the corpus's 41 parameters (29%) are optional, so this is not a corner.

    Pinning it here does two things: it stops the behaviour changing by
    accident, and it makes the gap something a reader of the test suite finds
    rather than something a user discovers in production. Repairing onto
    optional fields is a change to the repair model -- it needs a view on
    whether an unexpected key is evidence that an optional field was meant --
    and belongs with the rest of Phase 5, not smuggled into a hardening pass.
    """

    def test_a_one_character_typo_on_an_optional_field_is_left_alone(self) -> None:
        tool = BY_NAME["git_branch"]
        arguments = {"repo_path": "/repo", "branch_type": "local", "contain": "abc"}
        outcome = _call(tool, arguments)

        assert outcome.result.status is RepairStatus.ALREADY_VALID
        assert "contains" not in (outcome.arguments or {})

    def test_the_declared_default_wins_over_what_the_model_meant(self) -> None:
        """
        The sharp edge, stated plainly. ``git_diff`` declares
        ``context_lines`` with a default of 3. A model writing ``context: 10``
        gets ``context_lines: 3`` filled in beside its unrecognised key, so
        the server honours 3 and the 10 is silently ignored -- no violation,
        no warning to the caller, no repair.
        """
        tool = BY_NAME["git_diff"]
        arguments = {"repo_path": "/repo", "target": "main", "context": 10}
        outcome = _call(tool, arguments)

        assert outcome.action is MCPAction.FORWARD
        assert outcome.arguments["context_lines"] == 3
        assert outcome.arguments["context"] == 10

    def test_it_is_at_least_not_a_wrong_repair(self) -> None:
        """
        The saving grace, and why this is a limitation rather than a defect:
        nothing is written into a declared parameter. The failure is a missed
        repair, which costs a round trip -- not a confident wrong one, which
        costs trust.
        """
        tool = BY_NAME["git_branch"]
        arguments = {"repo_path": "/repo", "branch_type": "local", "contain": "abc"}
        outcome = _call(tool, arguments)
        assert _landed_on(outcome, "abc") == ["contain"]
