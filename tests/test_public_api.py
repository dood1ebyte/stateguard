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

import pytest

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


class TestPackageMetadata:
    """
    The distribution is ``sguard``; the import package is ``stateguard``.

    Those two names differing is ordinary, but it made it easy to write the
    import name where the distribution name belongs -- and ``stateguard`` on
    PyPI is an unrelated project, so every such slip installs someone else's
    code. These pin the places that slip has already happened once.
    """

    @staticmethod
    def _pyproject() -> dict[str, object]:
        import tomllib
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with open(path, "rb") as f:
            return tomllib.load(f)

    def test_the_version_is_declared_once(self) -> None:
        project = self._pyproject()["project"]
        assert isinstance(project, dict)
        assert stateguard.__version__ == project["version"]

    def test_the_cli_reports_the_package_version(self, capsys: pytest.CaptureFixture[str]) -> None:

        from stateguard.cli import main

        with pytest.raises(SystemExit):
            main(["--version"])
        assert capsys.readouterr().out.strip() == f"stateguard {stateguard.__version__}"

    def test_self_referencing_extras_name_this_distribution(self) -> None:
        project = self._pyproject()["project"]
        assert isinstance(project, dict)
        name = project["name"]
        for extra, requirements in project["optional-dependencies"].items():
            for requirement in requirements:
                if requirement.split("[", 1)[0] in ("stateguard", name):
                    assert requirement.startswith(f"{name}["), (
                        f"extra {extra!r} requires {requirement!r}, which names a "
                        f"different distribution than this one ({name!r})"
                    )
