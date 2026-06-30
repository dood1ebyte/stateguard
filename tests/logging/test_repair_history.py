"""Tests for stateguard.logging.repair_history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.results import RepairAttempt, RepairResult, RepairStatus
from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.logging.logger import RepairLogger
from stateguard.logging.repair_history import (
    DEFAULT_HISTORY_PATH,
    RepairHistoryRecorder,
    _get_nested_value,
    _NOT_FOUND,
    _violation_type_for_path,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def history_path(tmp_path: Path) -> Path:
    return tmp_path / "history" / "repairs.jsonl"


@pytest.fixture
def recorder(history_path: Path) -> RepairHistoryRecorder:
    return RepairHistoryRecorder(path=history_path)


def _make_violation(
    field_path: str,
    violation_type: ViolationType = ViolationType.MISSING_REQUIRED_FIELD,
) -> ContractViolation:
    return ContractViolation(
        field_path=field_path,
        violation_type=violation_type,
        severity=ViolationSeverity.ERROR,
        message="test violation",
    )


def _make_operation(
    op_type: FieldOpType = FieldOpType.RENAME,
    target_path: str = "temperature",
    source_path: str | None = "temp_celsius",
    confidence: float = 0.8,
) -> FieldOperation:
    kwargs: dict[str, Any] = {}
    if op_type is FieldOpType.RENAME:
        kwargs["source_path"] = source_path
    elif op_type in (FieldOpType.SET_DEFAULT, FieldOpType.SET_VALUE):
        kwargs["value"] = "filled"
    return FieldOperation(
        op_type=op_type,
        target_path=target_path,
        confidence=confidence,
        rationale="test rationale",
        **kwargs,
    )


def _make_attempt(
    strategy_name: str = "FuzzyFieldMatchStrategy",
    applied_operations: list[FieldOperation] | None = None,
    data_before: dict[str, Any] | None = None,
    data_after: dict[str, Any] | None = None,
    succeeded: bool = True,
    attempt_number: int = 1,
    violations_targeted: list[str] | None = None,
) -> RepairAttempt:
    return RepairAttempt(
        attempt_number=attempt_number,
        strategy_name=strategy_name,
        violations_targeted=violations_targeted or [],
        proposed_operations=applied_operations or [],
        applied_operations=applied_operations or [],
        rejected_operations=[],
        data_before=data_before or {},
        data_after=data_after or {},
        succeeded=succeeded,
    )


def _make_result(
    status: RepairStatus = RepairStatus.SUCCESS,
    attempts: list[RepairAttempt] | None = None,
    initial_violations: list[ContractViolation] | None = None,
    contract_id: str = "abc123",
) -> RepairResult:
    return RepairResult(
        status=status,
        original_input={},
        initial_violations=initial_violations or [],
        remaining_violations=[],
        attempts=attempts or [],
        repair_log=RepairLogger().entries,
        contract_id=contract_id,
        repaired_output={"temperature": 31.5},
    )


# ===========================================================================
# Construction / configuration
# ===========================================================================


class TestConstruction:

    def test_default_path(self) -> None:
        recorder = RepairHistoryRecorder()
        assert recorder.path == DEFAULT_HISTORY_PATH

    def test_default_path_under_home_dotstateguard(self) -> None:
        assert DEFAULT_HISTORY_PATH == Path.home() / ".stateguard" / "repairs.jsonl"

    def test_custom_path_as_path_object(self, history_path: Path) -> None:
        recorder = RepairHistoryRecorder(path=history_path)
        assert recorder.path == history_path

    def test_custom_path_as_string(self, history_path: Path) -> None:
        recorder = RepairHistoryRecorder(path=str(history_path))
        assert recorder.path == history_path

    def test_enabled_by_default(self) -> None:
        recorder = RepairHistoryRecorder()
        assert recorder.enabled is True

    def test_enabled_false(self, history_path: Path) -> None:
        recorder = RepairHistoryRecorder(path=history_path, enabled=False)
        assert recorder.enabled is False


# ===========================================================================
# record — disabled recorder
# ===========================================================================


class TestRecordDisabled:

    def test_disabled_returns_true_without_writing(self, history_path: Path) -> None:
        recorder = RepairHistoryRecorder(path=history_path, enabled=False)
        result = _make_result()
        assert recorder.record(result) is True
        assert not history_path.exists()


# ===========================================================================
# record — basic success path
# ===========================================================================


class TestRecordBasic:

    def test_record_creates_parent_directories(
        self, recorder: RepairHistoryRecorder, history_path: Path
    ) -> None:
        assert not history_path.parent.exists()
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])
        assert recorder.record(result) is True
        assert history_path.exists()

    def test_record_returns_true_on_success(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])
        assert recorder.record(result) is True

    def test_record_writes_one_line_per_applied_operation(
        self, recorder: RepairHistoryRecorder, history_path: Path
    ) -> None:
        op1 = _make_operation(target_path="temperature")
        op2 = _make_operation(target_path="humidity", source_path="hum")
        attempt = _make_attempt(applied_operations=[op1, op2])
        result = _make_result(attempts=[attempt])
        recorder.record(result)

        lines = history_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_record_appends_across_multiple_calls(
        self, recorder: RepairHistoryRecorder, history_path: Path
    ) -> None:
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])

        recorder.record(result)
        recorder.record(result)

        lines = history_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_each_line_is_valid_json(
        self, recorder: RepairHistoryRecorder, history_path: Path
    ) -> None:
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])
        recorder.record(result)

        for line in history_path.read_text().strip().split("\n"):
            json.loads(line)  # must not raise


# ===========================================================================
# record — field content
# ===========================================================================


class TestRecordFieldContent:

    def test_record_contains_required_fields(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])
        recorder.record(result)

        records = recorder.read_all()
        record = records[0]
        for key in (
            "timestamp", "contract_id", "status", "strategy",
            "violation_type", "field_path", "field_before", "field_after",
            "confidence", "success", "attempt_number", "op_type",
        ):
            assert key in record

    def test_contract_id_matches(self, recorder: RepairHistoryRecorder) -> None:
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt], contract_id="xyz999")
        recorder.record(result)
        assert recorder.read_all()[0]["contract_id"] == "xyz999"

    def test_status_matches(self, recorder: RepairHistoryRecorder) -> None:
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(status=RepairStatus.PARTIAL, attempts=[attempt])
        recorder.record(result)
        assert recorder.read_all()[0]["status"] == "partial"

    def test_strategy_matches(self, recorder: RepairHistoryRecorder) -> None:
        op = _make_operation()
        attempt = _make_attempt(strategy_name="ExactAliasStrategy", applied_operations=[op])
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        assert recorder.read_all()[0]["strategy"] == "ExactAliasStrategy"

    def test_confidence_matches(self, recorder: RepairHistoryRecorder) -> None:
        op = _make_operation(confidence=0.92)
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        assert recorder.read_all()[0]["confidence"] == pytest.approx(0.92)

    def test_op_type_matches(self, recorder: RepairHistoryRecorder) -> None:
        op = _make_operation(op_type=FieldOpType.RENAME)
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        assert recorder.read_all()[0]["op_type"] == "rename"

    def test_attempt_number_matches(self, recorder: RepairHistoryRecorder) -> None:
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op], attempt_number=3)
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        assert recorder.read_all()[0]["attempt_number"] == 3

    def test_success_matches_attempt_succeeded(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op], succeeded=False)
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        assert recorder.read_all()[0]["success"] is False

    def test_field_path_is_target_path(self, recorder: RepairHistoryRecorder) -> None:
        op = _make_operation(target_path="address.city")
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        assert recorder.read_all()[0]["field_path"] == "address.city"

    def test_field_before_read_from_source_path_for_rename(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        op = _make_operation(
            op_type=FieldOpType.RENAME, target_path="temperature", source_path="temp_celsius"
        )
        attempt = _make_attempt(
            applied_operations=[op],
            data_before={"temp_celsius": 31.5},
            data_after={"temperature": 31.5},
        )
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        record = recorder.read_all()[0]
        assert record["field_before"] == 31.5
        assert record["field_after"] == 31.5

    def test_field_before_read_from_target_path_for_coerce(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        op = _make_operation(
            op_type=FieldOpType.COERCE, target_path="count", source_path=None, confidence=0.95
        )
        attempt = _make_attempt(
            applied_operations=[op],
            data_before={"count": "5"},
            data_after={"count": 5},
        )
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        record = recorder.read_all()[0]
        assert record["field_before"] == "5"
        assert record["field_after"] == 5

    def test_field_before_none_when_not_found(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        op = _make_operation(
            op_type=FieldOpType.SET_DEFAULT, target_path="humidity", source_path=None
        )
        attempt = _make_attempt(
            applied_operations=[op],
            data_before={},
            data_after={"humidity": 60},
        )
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        record = recorder.read_all()[0]
        assert record["field_before"] is None
        assert record["field_after"] == 60

    def test_violation_type_resolved_from_target_path(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        op = _make_operation(
            op_type=FieldOpType.SET_DEFAULT, target_path="humidity", source_path=None
        )
        attempt = _make_attempt(applied_operations=[op])
        violation = _make_violation("humidity", ViolationType.MISSING_REQUIRED_FIELD)
        result = _make_result(attempts=[attempt], initial_violations=[violation])
        recorder.record(result)
        assert recorder.read_all()[0]["violation_type"] == "missing_required_field"

    def test_violation_type_resolved_from_source_path_fallback(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        """For a RENAME, the violation is typically on the source_path
        (UNEXPECTED_FIELD) rather than the target_path."""
        op = _make_operation(
            op_type=FieldOpType.RENAME, target_path="temperature", source_path="temp_celsius"
        )
        attempt = _make_attempt(applied_operations=[op])
        violation = _make_violation("temp_celsius", ViolationType.UNEXPECTED_FIELD)
        result = _make_result(attempts=[attempt], initial_violations=[violation])
        recorder.record(result)
        assert recorder.read_all()[0]["violation_type"] == "unexpected_field"

    def test_violation_type_none_when_unresolvable(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        op = _make_operation(target_path="unrelated_field", source_path="also_unrelated")
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt], initial_violations=[])
        recorder.record(result)
        assert recorder.read_all()[0]["violation_type"] is None

    def test_timestamp_is_iso_format(self, recorder: RepairHistoryRecorder) -> None:
        from datetime import datetime

        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])
        recorder.record(result)
        timestamp_str = recorder.read_all()[0]["timestamp"]
        # must not raise
        datetime.fromisoformat(timestamp_str)


# ===========================================================================
# record — no attempts (ALREADY_VALID / immediate FAILED)
# ===========================================================================


class TestRecordNoAttempts:

    def test_already_valid_writes_one_summary_record(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        result = _make_result(status=RepairStatus.ALREADY_VALID, attempts=[])
        recorder.record(result)
        records = recorder.read_all()
        assert len(records) == 1

    def test_already_valid_summary_fields_are_none(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        result = _make_result(status=RepairStatus.ALREADY_VALID, attempts=[])
        recorder.record(result)
        record = recorder.read_all()[0]
        assert record["strategy"] is None
        assert record["violation_type"] is None
        assert record["field_path"] is None
        assert record["field_before"] is None
        assert record["field_after"] is None
        assert record["confidence"] is None
        assert record["attempt_number"] is None
        assert record["op_type"] is None

    def test_already_valid_summary_success_is_true(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        result = _make_result(status=RepairStatus.ALREADY_VALID, attempts=[])
        recorder.record(result)
        assert recorder.read_all()[0]["success"] is True

    def test_failed_with_no_attempts_summary_success_is_false(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        result = _make_result(status=RepairStatus.FAILED, attempts=[])
        recorder.record(result)
        assert recorder.read_all()[0]["success"] is False

    def test_failed_with_no_attempts_status_recorded(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        result = _make_result(status=RepairStatus.FAILED, attempts=[])
        recorder.record(result)
        assert recorder.read_all()[0]["status"] == "failed"


# ===========================================================================
# record — attempts present but no operations applied
# ===========================================================================


class TestRecordAttemptsWithNoAppliedOps:

    def test_attempt_with_zero_applied_operations_writes_nothing(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        """An attempt where every proposed op was rejected (confidence too
        low) or none were proposed contributes zero operation-level
        records -- this is a known, accepted gap: such attempts are
        invisible in the history file. See M9_AUDIT.md."""
        attempt = _make_attempt(applied_operations=[])
        result = _make_result(status=RepairStatus.FAILED, attempts=[attempt])
        recorder.record(result)
        assert recorder.read_all() == []


# ===========================================================================
# record — multiple attempts
# ===========================================================================


class TestRecordMultipleAttempts:

    def test_two_attempts_two_operations_total(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        op1 = _make_operation(target_path="address.zip_code", source_path="address.zipcode")
        op2 = _make_operation(
            op_type=FieldOpType.COERCE, target_path="address.country.population",
            source_path=None, confidence=0.95,
        )
        attempt1 = _make_attempt(strategy_name="FuzzyFieldMatchStrategy", applied_operations=[op1], attempt_number=1)
        attempt2 = _make_attempt(strategy_name="TypeCoercionStrategy", applied_operations=[op2], attempt_number=2)
        result = _make_result(attempts=[attempt1, attempt2])
        recorder.record(result)

        records = recorder.read_all()
        assert len(records) == 2
        assert records[0]["strategy"] == "FuzzyFieldMatchStrategy"
        assert records[1]["strategy"] == "TypeCoercionStrategy"


# ===========================================================================
# record — failure resilience
# ===========================================================================


class TestRecordFailureResilience:

    def test_unwritable_parent_path_returns_false(self, tmp_path: Path) -> None:
        """A path component that is a FILE (not a directory) cannot have
        children created under it -- record() must return False, not raise."""
        blocking_file = tmp_path / "blocking_file"
        blocking_file.write_text("not a directory")
        bad_path = blocking_file / "repairs.jsonl"

        recorder = RepairHistoryRecorder(path=bad_path)
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])

        assert recorder.record(result) is False
        assert not bad_path.exists()

    def test_non_serializable_value_falls_back_to_str(
        self, recorder: RepairHistoryRecorder
    ) -> None:
        """A field value that isn't natively JSON-serializable (e.g. a
        custom object) is stringified via default=str rather than
        crashing the whole record() call."""

        class Weird:
            def __str__(self) -> str:
                return "weird-repr"

        op = _make_operation(target_path="x", source_path=None, op_type=FieldOpType.SET_VALUE)
        attempt = _make_attempt(
            applied_operations=[op],
            data_before={},
            data_after={"x": Weird()},
        )
        result = _make_result(attempts=[attempt])

        assert recorder.record(result) is True
        record = recorder.read_all()[0]
        assert record["field_after"] == "weird-repr"

    def test_record_never_raises_on_arbitrary_internal_error(
        self, recorder: RepairHistoryRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(recorder, "_build_lines", boom)
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])

        assert recorder.record(result) is False


# ===========================================================================
# read_all
# ===========================================================================


class TestReadAll:

    def test_read_all_nonexistent_file_returns_empty(
        self, history_path: Path
    ) -> None:
        recorder = RepairHistoryRecorder(path=history_path)
        assert recorder.read_all() == []

    def test_read_all_skips_malformed_lines(
        self, recorder: RepairHistoryRecorder, history_path: Path
    ) -> None:
        op = _make_operation()
        attempt = _make_attempt(applied_operations=[op])
        result = _make_result(attempts=[attempt])
        recorder.record(result)

        with open(history_path, "a") as f:
            f.write("not valid json\n")
            f.write("\n")  # blank line should also be skipped

        records = recorder.read_all()
        assert len(records) == 1

    def test_read_all_returns_empty_on_unreadable_path(
        self, tmp_path: Path
    ) -> None:
        directory_as_file = tmp_path / "a_directory"
        directory_as_file.mkdir()
        recorder = RepairHistoryRecorder(path=directory_as_file)
        # Path.exists() is True (it's a directory) but open() for reading
        # a directory raises -- read_all must swallow this gracefully.
        assert recorder.read_all() == []


# ===========================================================================
# Internal helpers — direct tests
# ===========================================================================


class TestGetNestedValueHelper:

    def test_top_level(self) -> None:
        assert _get_nested_value({"a": 1}, "a") == 1

    def test_nested(self) -> None:
        assert _get_nested_value({"a": {"b": 2}}, "a.b") == 2

    def test_missing_returns_not_found(self) -> None:
        assert _get_nested_value({"a": 1}, "b") is _NOT_FOUND

    def test_empty_path_returns_not_found(self) -> None:
        assert _get_nested_value({"a": 1}, "") is _NOT_FOUND

    def test_intermediate_not_dict_returns_not_found(self) -> None:
        assert _get_nested_value({"a": 1}, "a.b") is _NOT_FOUND

    def test_value_none_is_distinct_from_not_found(self) -> None:
        result = _get_nested_value({"a": None}, "a")
        assert result is None
        assert result is not _NOT_FOUND


class TestViolationTypeForPathHelper:

    def test_match_found(self) -> None:
        v = _make_violation("temperature", ViolationType.TYPE_MISMATCH)
        result = _violation_type_for_path([v], "temperature")
        assert result == "type_mismatch"

    def test_no_match_returns_none(self) -> None:
        v = _make_violation("temperature", ViolationType.TYPE_MISMATCH)
        result = _violation_type_for_path([v], "humidity")
        assert result is None

    def test_empty_violations_returns_none(self) -> None:
        assert _violation_type_for_path([], "temperature") is None

    def test_first_match_wins(self) -> None:
        v1 = _make_violation("x", ViolationType.MISSING_REQUIRED_FIELD)
        v2 = _make_violation("x", ViolationType.TYPE_MISMATCH)
        result = _violation_type_for_path([v1, v2], "x")
        assert result == "missing_required_field"
