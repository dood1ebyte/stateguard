"""Tests for stateguard.core.errors.results (and supporting logger/telemetry)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import pytest

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.results import (
    RepairAttempt,
    RepairResult,
    RepairStatus,
    ValidationResult,
)
from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.logging.logger import LogLevel, RepairLogEntry, RepairLogger
from stateguard.telemetry.hooks import (
    ITelemetryHook,
    TelemetryEvent,
    TelemetryEventType,
)
from stateguard.telemetry.noop import NoopTelemetry


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _violation(
    path: str = "temperature",
    vtype: ViolationType = ViolationType.MISSING_REQUIRED_FIELD,
) -> ContractViolation:
    return ContractViolation(
        field_path=path,
        violation_type=vtype,
        severity=ViolationSeverity.ERROR,
        message="test violation",
    )


def _rename_op(src: str = "temp_celsius", tgt: str = "temperature") -> FieldOperation:
    return FieldOperation(
        op_type=FieldOpType.RENAME,
        target_path=tgt,
        confidence=0.85,
        rationale="Fuzzy match",
        source_path=src,
    )


def _attempt(
    number: int = 1,
    strategy: str = "FuzzyFieldMatchStrategy",
    succeeded: bool = True,
) -> RepairAttempt:
    op = _rename_op()
    return RepairAttempt(
        attempt_number=number,
        strategy_name=strategy,
        violations_targeted=["v-001"],
        proposed_operations=[op],
        applied_operations=[op],
        rejected_operations=[],
        data_before={"temp_celsius": 31.5},
        data_after={"temperature": 31.5},
        succeeded=succeeded,
    )


def _log_entry(
    level: LogLevel = LogLevel.INFO,
    event: str = "repair.complete",
) -> RepairLogEntry:
    return RepairLogEntry(
        timestamp=datetime.now(tz=timezone.utc),
        level=level,
        event=event,
        message="Test log entry",
        data={"field": "temperature"},
    )


def _result(
    status: RepairStatus = RepairStatus.SUCCESS,
    repaired_output: dict[str, object] | None = None,
) -> RepairResult:
    return RepairResult(
        status=status,
        original_input={"temp_celsius": 31.5},
        initial_violations=[_violation()],
        remaining_violations=[],
        attempts=[_attempt()],
        repair_log=[_log_entry()],
        contract_id="abc123",
        repaired_output=repaired_output,
    )


# ===========================================================================
# RepairStatus
# ===========================================================================


class TestRepairStatus:

    def test_all_expected_values_present(self) -> None:
        expected = {"success", "partial", "failed", "already_valid"}
        assert {rs.value for rs in RepairStatus} == expected

    def test_member_count(self) -> None:
        assert len(RepairStatus) == 4

    def test_string_equality_success(self) -> None:
        assert RepairStatus.SUCCESS == "success"

    def test_string_equality_partial(self) -> None:
        assert RepairStatus.PARTIAL == "partial"

    def test_string_equality_failed(self) -> None:
        assert RepairStatus.FAILED == "failed"

    def test_string_equality_already_valid(self) -> None:
        assert RepairStatus.ALREADY_VALID == "already_valid"

    @pytest.mark.parametrize("member", list(RepairStatus))
    def test_every_member_round_trips_via_value(self, member: RepairStatus) -> None:
        assert RepairStatus(member.value) is member

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            RepairStatus("unknown")


# ===========================================================================
# ValidationResult
# ===========================================================================


class TestValidationResult:

    def test_valid_construction(self) -> None:
        vr = ValidationResult(
            is_valid=True,
            violations=[],
            raw_input={"temperature": 30.0},
            contract_id="test-cid",
        )
        assert vr.is_valid is True
        assert vr.violations == []
        assert vr.raw_input == {"temperature": 30.0}
        assert vr.contract_id == "test-cid"

    def test_invalid_with_violations(self) -> None:
        v = _violation()
        vr = ValidationResult(
            is_valid=False,
            violations=[v],
            raw_input={"temp_celsius": 31.5},
            contract_id="cid",
        )
        assert vr.is_valid is False
        assert len(vr.violations) == 1
        assert vr.violations[0] is v

    def test_validated_at_auto_generated(self) -> None:
        vr = ValidationResult(is_valid=True, violations=[], raw_input={}, contract_id="c")
        assert isinstance(vr.validated_at, datetime)
        assert vr.validated_at.tzinfo is not None

    def test_validated_at_explicit(self) -> None:
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        vr = ValidationResult(
            is_valid=True, violations=[], raw_input={}, contract_id="c",
            validated_at=ts,
        )
        assert vr.validated_at == ts

    def test_raw_input_stores_empty_dict(self) -> None:
        vr = ValidationResult(is_valid=True, violations=[], raw_input={}, contract_id="c")
        assert vr.raw_input == {}

    def test_raw_input_stores_nested_dict(self) -> None:
        data = {"address": {"city": "Mumbai"}}
        vr = ValidationResult(is_valid=True, violations=[], raw_input=data, contract_id="c")
        assert vr.raw_input["address"]["city"] == "Mumbai"

    def test_violations_not_shared_between_instances(self) -> None:
        vr1 = ValidationResult(is_valid=True, violations=[], raw_input={}, contract_id="c")
        vr2 = ValidationResult(is_valid=True, violations=[], raw_input={}, contract_id="c")
        vr1.violations.append(_violation())
        assert vr2.violations == []

    def test_equality(self) -> None:
        vr1 = ValidationResult(is_valid=True, violations=[], raw_input={}, contract_id="c")
        vr2 = ValidationResult(
            is_valid=True, violations=[], raw_input={}, contract_id="c",
            validated_at=vr1.validated_at,
        )
        assert vr1 == vr2


# ===========================================================================
# RepairAttempt
# ===========================================================================


class TestRepairAttempt:

    def test_basic_construction(self) -> None:
        a = _attempt()
        assert a.attempt_number == 1
        assert a.strategy_name == "FuzzyFieldMatchStrategy"
        assert a.succeeded is True

    def test_attempt_id_auto_generated(self) -> None:
        a = _attempt()
        assert isinstance(a.attempt_id, str)
        assert len(a.attempt_id) > 0

    def test_attempt_id_is_uuid4(self) -> None:
        a = _attempt()
        assert _UUID4_RE.match(a.attempt_id)

    def test_attempt_ids_are_unique(self) -> None:
        ids = {_attempt().attempt_id for _ in range(20)}
        assert len(ids) == 20

    def test_explicit_attempt_id_respected(self) -> None:
        fixed = "ffffffff-0000-4000-8000-000000000001"
        op = _rename_op()
        a = RepairAttempt(
            attempt_number=1, strategy_name="S",
            violations_targeted=[], proposed_operations=[op],
            applied_operations=[op], rejected_operations=[],
            data_before={}, data_after={}, succeeded=True,
            attempt_id=fixed,
        )
        assert a.attempt_id == fixed

    def test_attempted_at_auto_generated(self) -> None:
        a = _attempt()
        assert isinstance(a.attempted_at, datetime)
        assert a.attempted_at.tzinfo is not None

    def test_attempt_number_1_indexed(self) -> None:
        a1 = _attempt(number=1)
        a2 = _attempt(number=2)
        assert a1.attempt_number == 1
        assert a2.attempt_number == 2

    def test_failed_attempt(self) -> None:
        a = _attempt(succeeded=False)
        assert a.succeeded is False

    def test_operations_stored(self) -> None:
        op = _rename_op()
        a = _attempt()
        assert len(a.applied_operations) == 1
        assert a.applied_operations[0] is op or (
            a.applied_operations[0].op_type is FieldOpType.RENAME
        )

    def test_rejected_operations_default_empty(self) -> None:
        a = _attempt()
        assert a.rejected_operations == []

    def test_data_before_and_after(self) -> None:
        a = _attempt()
        assert a.data_before == {"temp_celsius": 31.5}
        assert a.data_after == {"temperature": 31.5}

    def test_violations_targeted_stored(self) -> None:
        a = _attempt()
        assert a.violations_targeted == ["v-001"]

    def test_separate_applied_and_rejected(self) -> None:
        high_conf = FieldOperation(
            op_type=FieldOpType.RENAME, target_path="temperature",
            confidence=0.9, rationale="r", source_path="temp_celsius",
        )
        low_conf = FieldOperation(
            op_type=FieldOpType.REMOVE, target_path="extra",
            confidence=0.3, rationale="r",
        )
        a = RepairAttempt(
            attempt_number=1, strategy_name="S",
            violations_targeted=[],
            proposed_operations=[high_conf, low_conf],
            applied_operations=[high_conf],
            rejected_operations=[low_conf],
            data_before={}, data_after={}, succeeded=True,
        )
        assert len(a.proposed_operations) == 2
        assert len(a.applied_operations) == 1
        assert len(a.rejected_operations) == 1


# ===========================================================================
# RepairResult — construction
# ===========================================================================


class TestRepairResultConstruction:

    def test_success_construction(self) -> None:
        rr = _result(
            status=RepairStatus.SUCCESS,
            repaired_output={"temperature": 31.5},
        )
        assert rr.status is RepairStatus.SUCCESS
        assert rr.repaired_output == {"temperature": 31.5}
        assert rr.contract_id == "abc123"

    def test_failed_construction(self) -> None:
        rr = _result(status=RepairStatus.FAILED, repaired_output=None)
        assert rr.status is RepairStatus.FAILED
        assert rr.repaired_output is None

    def test_partial_construction(self) -> None:
        rr = _result(
            status=RepairStatus.PARTIAL,
            repaired_output={"temperature": 31.5},
        )
        assert rr.status is RepairStatus.PARTIAL
        assert rr.repaired_output is not None

    def test_already_valid_construction(self) -> None:
        rr = RepairResult(
            status=RepairStatus.ALREADY_VALID,
            original_input={"temperature": 31.5},
            initial_violations=[],
            remaining_violations=[],
            attempts=[],
            repair_log=[],
            contract_id="cid",
            repaired_output={"temperature": 31.5},
        )
        assert rr.status is RepairStatus.ALREADY_VALID

    def test_repaired_output_defaults_to_none(self) -> None:
        rr = RepairResult(
            status=RepairStatus.FAILED,
            original_input={},
            initial_violations=[],
            remaining_violations=[],
            attempts=[],
            repair_log=[],
            contract_id="cid",
        )
        assert rr.repaired_output is None

    def test_repaired_at_auto_generated(self) -> None:
        rr = _result()
        assert isinstance(rr.repaired_at, datetime)
        assert rr.repaired_at.tzinfo is not None

    def test_explicit_repaired_at(self) -> None:
        ts = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        rr = RepairResult(
            status=RepairStatus.SUCCESS,
            original_input={},
            initial_violations=[],
            remaining_violations=[],
            attempts=[],
            repair_log=[],
            contract_id="c",
            repaired_at=ts,
        )
        assert rr.repaired_at == ts

    def test_original_input_stored(self) -> None:
        rr = _result()
        assert rr.original_input == {"temp_celsius": 31.5}

    def test_initial_violations_stored(self) -> None:
        v = _violation()
        rr = RepairResult(
            status=RepairStatus.SUCCESS,
            original_input={},
            initial_violations=[v],
            remaining_violations=[],
            attempts=[],
            repair_log=[],
            contract_id="c",
        )
        assert rr.initial_violations[0] is v

    def test_remaining_violations_stored(self) -> None:
        v = _violation()
        rr = RepairResult(
            status=RepairStatus.PARTIAL,
            original_input={},
            initial_violations=[v],
            remaining_violations=[v],
            attempts=[],
            repair_log=[],
            contract_id="c",
        )
        assert rr.remaining_violations[0] is v

    def test_attempts_stored(self) -> None:
        a = _attempt()
        rr = RepairResult(
            status=RepairStatus.SUCCESS,
            original_input={},
            initial_violations=[],
            remaining_violations=[],
            attempts=[a],
            repair_log=[],
            contract_id="c",
        )
        assert rr.attempts[0] is a

    def test_repair_log_stored(self) -> None:
        entry = _log_entry()
        rr = RepairResult(
            status=RepairStatus.SUCCESS,
            original_input={},
            initial_violations=[],
            remaining_violations=[],
            attempts=[],
            repair_log=[entry],
            contract_id="c",
        )
        assert rr.repair_log[0] is entry


# ===========================================================================
# RepairResult — convenience properties
# ===========================================================================


class TestRepairResultProperties:

    @pytest.mark.parametrize(
        ("status", "prop", "expected"),
        [
            (RepairStatus.SUCCESS,       "is_success",       True),
            (RepairStatus.SUCCESS,       "is_partial",       False),
            (RepairStatus.SUCCESS,       "is_failed",        False),
            (RepairStatus.SUCCESS,       "is_already_valid", False),
            (RepairStatus.PARTIAL,       "is_success",       False),
            (RepairStatus.PARTIAL,       "is_partial",       True),
            (RepairStatus.PARTIAL,       "is_failed",        False),
            (RepairStatus.PARTIAL,       "is_already_valid", False),
            (RepairStatus.FAILED,        "is_success",       False),
            (RepairStatus.FAILED,        "is_partial",       False),
            (RepairStatus.FAILED,        "is_failed",        True),
            (RepairStatus.FAILED,        "is_already_valid", False),
            (RepairStatus.ALREADY_VALID, "is_success",       False),
            (RepairStatus.ALREADY_VALID, "is_partial",       False),
            (RepairStatus.ALREADY_VALID, "is_failed",        False),
            (RepairStatus.ALREADY_VALID, "is_already_valid", True),
        ],
    )
    def test_convenience_property(
        self,
        status: RepairStatus,
        prop: str,
        expected: bool,
    ) -> None:
        rr = RepairResult(
            status=status,
            original_input={},
            initial_violations=[],
            remaining_violations=[],
            attempts=[],
            repair_log=[],
            contract_id="c",
        )
        assert getattr(rr, prop) is expected

    def test_exactly_one_property_true_per_status(self) -> None:
        props = ["is_success", "is_partial", "is_failed", "is_already_valid"]
        for status in RepairStatus:
            rr = RepairResult(
                status=status,
                original_input={},
                initial_violations=[],
                remaining_violations=[],
                attempts=[],
                repair_log=[],
                contract_id="c",
            )
            true_count = sum(getattr(rr, p) for p in props)
            assert true_count == 1, (
                f"Expected exactly one True property for {status}, "
                f"got {[p for p in props if getattr(rr, p)]}"
            )


# ===========================================================================
# RepairLogEntry and RepairLogger
# ===========================================================================


class TestRepairLogEntry:

    def test_construction(self) -> None:
        e = _log_entry()
        assert e.level is LogLevel.INFO
        assert e.event == "repair.complete"
        assert e.message == "Test log entry"
        assert isinstance(e.timestamp, datetime)

    def test_data_defaults_to_empty_dict(self) -> None:
        e = RepairLogEntry(
            timestamp=datetime.now(tz=timezone.utc),
            level=LogLevel.DEBUG,
            event="test.event",
            message="msg",
        )
        assert e.data == {}

    def test_data_stores_structured_context(self) -> None:
        e = RepairLogEntry(
            timestamp=datetime.now(tz=timezone.utc),
            level=LogLevel.INFO,
            event="strategy.applied",
            message="Strategy applied.",
            data={"strategy": "FuzzyMatch", "field": "temperature"},
        )
        assert e.data["strategy"] == "FuzzyMatch"
        assert e.data["field"] == "temperature"

    def test_data_not_shared_between_instances(self) -> None:
        e1 = _log_entry()
        e2 = _log_entry()
        e1.data["extra"] = "value"
        assert "extra" not in e2.data

    @pytest.mark.parametrize("level", list(LogLevel))
    def test_every_log_level_constructable(self, level: LogLevel) -> None:
        e = RepairLogEntry(
            timestamp=datetime.now(tz=timezone.utc),
            level=level,
            event=f"event.{level.value}",
            message=f"Message at {level.value}",
        )
        assert e.level is level


class TestRepairLogger:

    def test_starts_empty(self) -> None:
        logger = RepairLogger()
        assert logger.entries == []

    def test_info_creates_entry(self) -> None:
        logger = RepairLogger()
        logger.info("test.event", "Test message", field="temperature")
        assert len(logger.entries) == 1
        e = logger.entries[0]
        assert e.level is LogLevel.INFO
        assert e.event == "test.event"
        assert e.message == "Test message"
        assert e.data["field"] == "temperature"

    def test_debug_creates_debug_entry(self) -> None:
        logger = RepairLogger()
        logger.debug("d.event", "debug msg")
        assert logger.entries[0].level is LogLevel.DEBUG

    def test_warning_creates_warning_entry(self) -> None:
        logger = RepairLogger()
        logger.warning("w.event", "warn msg")
        assert logger.entries[0].level is LogLevel.WARNING

    def test_error_creates_error_entry(self) -> None:
        logger = RepairLogger()
        logger.error("e.event", "error msg")
        assert logger.entries[0].level is LogLevel.ERROR

    def test_entries_are_ordered_by_insertion(self) -> None:
        logger = RepairLogger()
        logger.info("ev.1", "first")
        logger.warning("ev.2", "second")
        logger.error("ev.3", "third")
        events = [e.event for e in logger.entries]
        assert events == ["ev.1", "ev.2", "ev.3"]

    def test_entries_returns_snapshot_copy(self) -> None:
        """Mutating the returned list must not affect the logger."""
        logger = RepairLogger()
        logger.info("e", "msg")
        entries = logger.entries
        entries.clear()
        assert len(logger.entries) == 1

    def test_multiple_entries_accumulate(self) -> None:
        logger = RepairLogger()
        for i in range(5):
            logger.info(f"event.{i}", f"message {i}")
        assert len(logger.entries) == 5

    def test_timestamp_is_utc(self) -> None:
        logger = RepairLogger()
        logger.info("ev", "msg")
        ts = logger.entries[0].timestamp
        assert ts.tzinfo is not None
        assert ts.tzinfo == timezone.utc or ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_data_kwargs_stored_as_dict(self) -> None:
        logger = RepairLogger()
        logger.info("ev", "msg", strategy="Fuzzy", confidence=0.85, field="temp")
        e = logger.entries[0]
        assert e.data == {"strategy": "Fuzzy", "confidence": 0.85, "field": "temp"}


# ===========================================================================
# LogLevel
# ===========================================================================


class TestLogLevel:

    def test_all_expected_values(self) -> None:
        assert {ll.value for ll in LogLevel} == {"debug", "info", "warning", "error"}

    def test_member_count(self) -> None:
        assert len(LogLevel) == 4

    @pytest.mark.parametrize("member", list(LogLevel))
    def test_round_trip_via_value(self, member: LogLevel) -> None:
        assert LogLevel(member.value) is member


# ===========================================================================
# TelemetryEventType and TelemetryEvent
# ===========================================================================


class TestTelemetryEventType:

    def test_all_expected_values_present(self) -> None:
        expected = {
            "validation_started", "violation_detected", "repair_started",
            "strategy_selected", "repair_attempt_started", "operation_applied",
            "operation_rejected", "revalidation_started", "repair_completed",
            "repair_failed",
        }
        assert {t.value for t in TelemetryEventType} == expected

    @pytest.mark.parametrize("member", list(TelemetryEventType))
    def test_round_trip_via_value(self, member: TelemetryEventType) -> None:
        assert TelemetryEventType(member.value) is member


class TestTelemetryEvent:

    def test_construction(self) -> None:
        e = TelemetryEvent(
            event_type=TelemetryEventType.REPAIR_COMPLETED,
            contract_id="abc123",
        )
        assert e.event_type is TelemetryEventType.REPAIR_COMPLETED
        assert e.contract_id == "abc123"
        assert e.data == {}

    def test_emitted_at_auto_generated(self) -> None:
        e = TelemetryEvent(
            event_type=TelemetryEventType.VALIDATION_STARTED,
            contract_id="c",
        )
        assert isinstance(e.emitted_at, datetime)
        assert e.emitted_at.tzinfo is not None

    def test_data_stored(self) -> None:
        e = TelemetryEvent(
            event_type=TelemetryEventType.VIOLATION_DETECTED,
            contract_id="c",
            data={"field": "temperature", "violation": "MISSING_REQUIRED_FIELD"},
        )
        assert e.data["field"] == "temperature"

    @pytest.mark.parametrize("etype", list(TelemetryEventType))
    def test_every_event_type_constructable(self, etype: TelemetryEventType) -> None:
        e = TelemetryEvent(event_type=etype, contract_id="c")
        assert e.event_type is etype


# ===========================================================================
# NoopTelemetry
# ===========================================================================


class TestNoopTelemetry:

    def test_emit_does_not_raise(self) -> None:
        noop = NoopTelemetry()
        event = TelemetryEvent(
            event_type=TelemetryEventType.REPAIR_COMPLETED,
            contract_id="cid",
        )
        noop.emit(event)  # must not raise

    def test_emit_called_multiple_times(self) -> None:
        noop = NoopTelemetry()
        for etype in TelemetryEventType:
            noop.emit(TelemetryEvent(event_type=etype, contract_id="c"))

    def test_satisfies_itelemetryhook_protocol(self) -> None:
        noop = NoopTelemetry()
        assert isinstance(noop, ITelemetryHook)

    def test_custom_hook_satisfies_protocol(self) -> None:
        """Any object with emit() satisfies ITelemetryHook (structural check)."""
        class CustomHook:
            def __init__(self) -> None:
                self.received: list[TelemetryEvent] = []

            def emit(self, event: TelemetryEvent) -> None:
                self.received.append(event)

        hook = CustomHook()
        assert isinstance(hook, ITelemetryHook)

        event = TelemetryEvent(
            event_type=TelemetryEventType.REPAIR_STARTED, contract_id="c"
        )
        hook.emit(event)
        assert len(hook.received) == 1
        assert hook.received[0] is event
