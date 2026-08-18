"""
Phase 5 end to end: enum repair through ``ContractGuard.repair``.

Success criterion 6 of ``CORE_HARDENING_PLAN.md`` requires enum repair to
pass for ``Literal``, ``enum.Enum``, and JSON Schema ``enum``. All three
declare the same ``ENUM_VALUES`` constraint, so this is one code path -- but
the criterion is about the three *entry points*, so all three are driven.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Literal

import pytest

from stateguard.core.errors.results import RepairStatus
from stateguard.guard import ContractGuard

pytest.importorskip("pydantic")

from pydantic import BaseModel  # noqa: E402


# See tests/adapters/pydantic/test_enum_extraction.py on why UP042 is
# suppressed rather than switching to StrEnum.
class Status(str, Enum):  # noqa: UP042
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class Task(BaseModel):
    title: str
    status: Status


class LiteralTask(BaseModel):
    status: Literal["open", "in_progress", "done"]


DICT_SCHEMA = {
    "fields": [
        {
            "path": "status",
            "type": "string",
            "constraints": [{"type": "enum_values", "value": ["open", "in_progress", "done"]}],
        }
    ]
}

SPELLINGS = ["IN PROGRESS", "in-progress", "In Progress", "In_Progress"]


@pytest.fixture
def pydantic_guard() -> ContractGuard:
    return ContractGuard.with_pydantic()


@pytest.fixture
def dict_guard() -> ContractGuard:
    return ContractGuard.with_dict_schema()


class TestThreeDeclarationSources:
    # On SUCCESS the Pydantic adapter's ``wrap`` returns a validated model
    # instance rather than a dict -- see ContractGuard.repair's docstring --
    # so these read the attribute. The dict adapter returns a dict.

    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_enum_subclass(self, pydantic_guard: ContractGuard, spelling: str) -> None:
        result = pydantic_guard.repair(Task, {"title": "t", "status": spelling})
        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output is not None
        assert result.repaired_output.status is Status.IN_PROGRESS

    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_literal(self, pydantic_guard: ContractGuard, spelling: str) -> None:
        result = pydantic_guard.repair(LiteralTask, {"status": spelling})
        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output is not None
        assert result.repaired_output.status == "in_progress"

    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_json_schema_enum(self, dict_guard: ContractGuard, spelling: str) -> None:
        result = dict_guard.repair(DICT_SCHEMA, {"status": spelling})
        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output is not None
        assert result.repaired_output["status"] == "in_progress"


class TestRepairedOutputRevalidates:
    def test_the_repaired_payload_constructs_the_model(self, pydantic_guard: ContractGuard) -> None:
        """A repair that does not survive the framework's own validator is
        not a repair."""
        result = pydantic_guard.repair(Task, {"title": "t", "status": "IN PROGRESS"})
        assert result.repaired_output is not None
        # wrap() already ran model_validate; round-tripping it proves the
        # repaired payload is acceptable to the framework, not just to us.
        task = Task.model_validate(result.repaired_output.model_dump())
        assert task.status is Status.IN_PROGRESS


class TestRefusals:
    def test_a_value_that_is_not_a_member_is_not_invented(
        self, pydantic_guard: ContractGuard
    ) -> None:
        result = pydantic_guard.repair(Task, {"title": "t", "status": "cancelled"})
        assert result.status is RepairStatus.FAILED
        assert result.repaired_output is None

    def test_a_near_miss_is_not_repaired(self, pydantic_guard: ContractGuard) -> None:
        """
        'in progres' is one character from a member. Levenshtein over enum
        values is a different and riskier feature than normalising spelling,
        and this strategy deliberately does not do it.
        """
        result = pydantic_guard.repair(Task, {"title": "t", "status": "in progres"})
        assert result.status is RepairStatus.FAILED

    def test_an_int_enum_out_of_range_is_not_repaired(self, pydantic_guard: ContractGuard) -> None:
        class Level(IntEnum):
            LOW = 1
            HIGH = 2

        class Ticket(BaseModel):
            level: Level

        result = pydantic_guard.repair(Ticket, {"level": 5})
        assert result.status is RepairStatus.FAILED


class TestTrustAndAudit:
    def test_case_only_scores_higher_than_a_separator_rewrite(
        self, dict_guard: ContractGuard
    ) -> None:
        exact = dict_guard.repair(DICT_SCHEMA, {"status": "DONE"})
        rewritten = dict_guard.repair(DICT_SCHEMA, {"status": "IN PROGRESS"})

        exact_trust = exact.attempts[0].applied_operations[0].trust
        rewritten_trust = rewritten.attempts[0].applied_operations[0].trust
        assert exact_trust == pytest.approx(1.0)
        assert rewritten_trust == pytest.approx(0.85)
        assert exact_trust > rewritten_trust

    def test_the_applied_operation_is_attributed_to_this_strategy(
        self, dict_guard: ContractGuard
    ) -> None:
        result = dict_guard.repair(DICT_SCHEMA, {"status": "DONE"})
        attempt = result.attempts[0]
        assert attempt.strategy_name == "EnumNormalizationStrategy"
        assert len(attempt.applied_operations) == 1

    def test_no_payload_value_reaches_the_repair_log(self, dict_guard: ContractGuard) -> None:
        result = dict_guard.repair(DICT_SCHEMA, {"status": "DONE"})
        rendered = " ".join(entry.message for entry in result.repair_log)
        assert "DONE" not in rendered


class TestMultiIssuePayload:
    """
    Success criterion 3: a 4-issue payload -- rename, coerce, enum, default --
    fully repairs in one ``repair()`` call. Enum repair was the missing link;
    until Phase 5 there was no strategy targeting VALUE_CONSTRAINT_VIOLATION.
    """

    SCHEMA = {
        "fields": [
            {"path": "temperature", "type": "float"},
            {
                "path": "status",
                "type": "string",
                "constraints": [{"type": "enum_values", "value": ["open", "in_progress"]}],
            },
            {"path": "humidity", "type": "integer", "default": 50},
        ]
    }

    def test_four_issues_repair_in_one_call(self, dict_guard: ContractGuard) -> None:
        result = dict_guard.repair(self.SCHEMA, {"temp_celsius": "31.5", "status": "IN PROGRESS"})

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {
            "temperature": 31.5,
            "status": "in_progress",
            "humidity": 50,
        }

    def test_every_applied_operation_changed_the_data(self, dict_guard: ContractGuard) -> None:
        """Success criterion 9, re-checked with the new strategy in the loop."""
        result = dict_guard.repair(self.SCHEMA, {"temp_celsius": "31.5", "status": "IN PROGRESS"})
        for attempt in result.attempts:
            if attempt.applied_operations:
                assert attempt.data_before != attempt.data_after
