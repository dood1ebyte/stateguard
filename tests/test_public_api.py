"""
Tests for the top-level ``stateguard`` package's public export contract.

This file exists specifically to catch "missing export" regressions —
e.g. a new public-facing class (like ``RepairHistoryRecorder`` in M9.1)
being fully implemented and tested at its own module path but never
re-exported from ``stateguard`` itself, leaving users to discover the
deeper import path by reading source rather than the documented
top-level API.
"""

from __future__ import annotations

import stateguard


class TestPublicExports:
    """Every name in ``stateguard.__all__`` must be importable and present."""

    def test_all_names_are_actually_present_on_the_module(self) -> None:
        for name in stateguard.__all__:
            assert hasattr(stateguard, name), (
                f"'{name}' is listed in stateguard.__all__ but is not "
                f"actually an attribute of the stateguard module."
            )

    def test_contractguard_exported(self) -> None:
        assert hasattr(stateguard, "ContractGuard")

    def test_repair_result_types_exported(self) -> None:
        for name in ("RepairResult", "RepairStatus", "ValidationResult", "RepairAttempt"):
            assert hasattr(stateguard, name)

    def test_violation_types_exported(self) -> None:
        for name in ("ContractViolation", "ViolationType", "ViolationSeverity"):
            assert hasattr(stateguard, name)

    def test_config_types_exported(self) -> None:
        for name in ("GuardConfig", "RepairConfig"):
            assert hasattr(stateguard, name)

    def test_repair_history_recorder_exported(self) -> None:
        """
        Added in M9.1: RepairHistoryRecorder was fully implemented and
        tested in M9 (Part B) but was only importable via
        stateguard.logging.repair_history, not from the top-level
        stateguard package. This test locks in the fix.
        """
        assert hasattr(stateguard, "RepairHistoryRecorder")

    def test_repair_history_recorder_importable_directly(self) -> None:
        from stateguard import RepairHistoryRecorder

        recorder = RepairHistoryRecorder(enabled=False)
        assert recorder.enabled is False

    def test_version_exported(self) -> None:
        assert hasattr(stateguard, "__version__")
        assert isinstance(stateguard.__version__, str)
