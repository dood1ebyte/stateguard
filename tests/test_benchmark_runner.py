"""
Tests for benchmarks/runner.py.

benchmarks/ lives outside src/stateguard (it's a standalone tool, not part
of the installable package), so it's imported here via an explicit path
insertion rather than a normal package import.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Import benchmarks/runner.py directly from its file path.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNNER_PATH = _REPO_ROOT / "benchmarks" / "runner.py"

_spec = importlib.util.spec_from_file_location("benchmark_runner", _RUNNER_PATH)
assert _spec is not None and _spec.loader is not None
runner = importlib.util.module_from_spec(_spec)
sys.modules["benchmark_runner"] = runner
_spec.loader.exec_module(runner)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_case(directory: Path, filename: str, case: dict[str, Any]) -> Path:
    p = directory / filename
    p.write_text(json.dumps(case))
    return p


SUCCESS_CASE = {
    "name": "fuzzy_rename_test",
    "description": "test case",
    "expected_schema": {
        "fields": [
            {"path": "temperature", "type": "float"},
            {"path": "humidity", "type": "integer"},
        ]
    },
    "broken_payload": {"temp_celsius": 31.5, "humidity": 80},
    "expected_result": {"status": "success", "min_confidence": 0.7},
}

ALREADY_VALID_CASE = {
    "name": "already_valid_test",
    "description": "test case",
    "expected_schema": {
        "fields": [{"path": "x", "type": "string"}],
    },
    "broken_payload": {"x": "hello"},
    "expected_result": {"status": "already_valid"},
}

FAILED_CASE = {
    "name": "unrecoverable_test",
    "description": "test case",
    "expected_schema": {
        "fields": [{"path": "temperature", "type": "float"}],
    },
    "broken_payload": {"xyz": 1.0},
    "expected_result": {"status": "failed"},
}


# ===========================================================================
# load_cases
# ===========================================================================


class TestLoadCases:
    def test_loads_all_json_files(self, tmp_path: Path) -> None:
        _write_case(tmp_path, "a.json", SUCCESS_CASE)
        _write_case(tmp_path, "b.json", FAILED_CASE)
        cases = runner.load_cases(tmp_path)
        assert len(cases) == 2

    def test_sorted_by_filename(self, tmp_path: Path) -> None:
        _write_case(tmp_path, "02_b.json", FAILED_CASE)
        _write_case(tmp_path, "01_a.json", SUCCESS_CASE)
        cases = runner.load_cases(tmp_path)
        assert cases[0]["name"] == "fuzzy_rename_test"
        assert cases[1]["name"] == "unrecoverable_test"

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert runner.load_cases(tmp_path) == []

    def test_ignores_non_json_files(self, tmp_path: Path) -> None:
        _write_case(tmp_path, "a.json", SUCCESS_CASE)
        (tmp_path / "readme.md").write_text("not a case")
        cases = runner.load_cases(tmp_path)
        assert len(cases) == 1

    def test_records_source_file(self, tmp_path: Path) -> None:
        _write_case(tmp_path, "my_case.json", SUCCESS_CASE)
        cases = runner.load_cases(tmp_path)
        assert cases[0]["_source_file"] == "my_case.json"


# ===========================================================================
# run_case
# ===========================================================================


class TestRunCase:
    def test_success_case_passes(self) -> None:
        outcome = runner.run_case(SUCCESS_CASE)
        assert outcome.passed is True
        assert outcome.actual_status == "success"

    def test_already_valid_case_passes(self) -> None:
        outcome = runner.run_case(ALREADY_VALID_CASE)
        assert outcome.passed is True
        assert outcome.actual_status == "already_valid"

    def test_failed_case_passes_when_expected_failed(self) -> None:
        outcome = runner.run_case(FAILED_CASE)
        assert outcome.passed is True
        assert outcome.actual_status == "failed"

    def test_mismatched_expectation_fails(self) -> None:
        case = dict(SUCCESS_CASE)
        case["expected_result"] = {"status": "failed"}
        outcome = runner.run_case(case)
        assert outcome.passed is False
        assert outcome.actual_status == "success"
        assert outcome.expected_status == "failed"

    def test_confidence_floor_respected(self) -> None:
        case = dict(SUCCESS_CASE)
        case["expected_result"] = {"status": "success", "min_confidence": 0.99}
        outcome = runner.run_case(case)
        # fuzzy_rename's actual confidence is ~0.8, below 0.99 -> fails
        assert outcome.passed is False

    def test_confidence_floor_satisfied(self) -> None:
        case = dict(SUCCESS_CASE)
        case["expected_result"] = {"status": "success", "min_confidence": 0.5}
        outcome = runner.run_case(case)
        assert outcome.passed is True

    def test_trusts_recorded(self) -> None:
        outcome = runner.run_case(SUCCESS_CASE)
        assert len(outcome.trusts) >= 1
        assert all(0.0 <= c <= 1.0 for c in outcome.trusts)

    def test_average_trust_property(self) -> None:
        outcome = runner.run_case(SUCCESS_CASE)
        assert outcome.average_trust is not None
        assert outcome.average_trust == sum(outcome.trusts) / len(outcome.trusts)

    def test_average_trust_none_when_no_operations(self) -> None:
        outcome = runner.run_case(ALREADY_VALID_CASE)
        assert outcome.trusts == []
        assert outcome.average_trust is None

    def test_malformed_schema_does_not_crash_runner(self) -> None:
        case = {
            "name": "malformed_test",
            "description": "deliberately broken schema",
            "expected_schema": {"fields": [{"path": "x"}]},  # missing 'type'
            "broken_payload": {"x": 1},
            "expected_result": {"status": "success"},
        }
        outcome = runner.run_case(case)
        assert outcome.passed is False
        assert outcome.actual_status == "error"
        assert outcome.error is not None

    def test_case_name_falls_back_to_source_file(self) -> None:
        case = dict(SUCCESS_CASE)
        del case["name"]
        case["_source_file"] = "fallback.json"
        outcome = runner.run_case(case)
        assert outcome.name == "fallback.json"

    def test_payload_not_mutated(self) -> None:
        case = dict(SUCCESS_CASE)
        original_payload = dict(case["broken_payload"])
        runner.run_case(case)
        assert case["broken_payload"] == original_payload


# ===========================================================================
# run_benchmark (aggregation)
# ===========================================================================


class TestRunBenchmark:
    def test_aggregates_total_cases(self) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE, ALREADY_VALID_CASE, FAILED_CASE])
        assert summary.total_cases == 3

    def test_all_passing_cases(self) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE, ALREADY_VALID_CASE, FAILED_CASE])
        assert summary.passed_cases == 3
        assert summary.failed_cases == 0

    def test_only_repair_scenarios_count_as_repairable(self) -> None:
        """
        An already-valid case has nothing to repair, and the deliberately
        unrecoverable case exists to prove StateGuard refuses to guess --
        repairing it would be a bug. Neither belongs in the denominator.
        """
        summary = runner.run_benchmark([SUCCESS_CASE, ALREADY_VALID_CASE, FAILED_CASE])
        assert summary.repairable_cases == 1
        assert summary.repaired_correctly == 1

    def test_repair_rate_is_recall_over_repairable_cases(self) -> None:
        """
        Dividing by total_cases made 1/3 the *maximum* achievable score on a
        suite where every case behaved correctly.
        """
        summary = runner.run_benchmark([SUCCESS_CASE, ALREADY_VALID_CASE, FAILED_CASE])
        assert summary.repair_rate == pytest.approx(1.0)

    def test_precision_is_reported(self) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE, ALREADY_VALID_CASE, FAILED_CASE])
        assert summary.attempted_repairs == 1
        assert summary.false_positives == 0
        assert summary.precision == pytest.approx(1.0)

    def test_precision_is_none_when_nothing_was_attempted(self) -> None:
        summary = runner.run_benchmark([ALREADY_VALID_CASE, FAILED_CASE])
        assert summary.attempted_repairs == 0
        assert summary.precision is None

    def test_repair_rate_zero_for_empty_list(self) -> None:
        summary = runner.run_benchmark([])
        assert summary.total_cases == 0
        assert summary.repair_rate == 0.0

    def test_average_trust_across_cases(self) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE, ALREADY_VALID_CASE])
        assert summary.average_trust is not None
        assert 0.0 <= summary.average_trust <= 1.0

    def test_average_trust_none_when_nothing_repaired(self) -> None:
        summary = runner.run_benchmark([ALREADY_VALID_CASE, FAILED_CASE])
        assert summary.average_trust is None

    def test_mismatched_case_counted_as_failed(self) -> None:
        bad_case = dict(SUCCESS_CASE)
        bad_case["expected_result"] = {"status": "failed"}
        summary = runner.run_benchmark([bad_case])
        assert summary.failed_cases == 1
        assert summary.passed_cases == 0

    def test_outcomes_list_matches_input_order(self) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE, FAILED_CASE, ALREADY_VALID_CASE])
        names = [o.name for o in summary.outcomes]
        assert names == [
            "fuzzy_rename_test",
            "unrecoverable_test",
            "already_valid_test",
        ]

    def test_timestamp_is_iso_format(self) -> None:
        from datetime import datetime

        summary = runner.run_benchmark([SUCCESS_CASE])
        datetime.fromisoformat(summary.timestamp)  # must not raise


# ===========================================================================
# BenchmarkSummary.to_dict
# ===========================================================================


class TestSummaryToDict:
    def test_to_dict_has_required_keys(self) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE])
        d = summary.to_dict()
        for key in (
            "timestamp",
            "total_cases",
            "passed_cases",
            "failed_cases",
            "repairable_cases",
            "repaired_correctly",
            "repair_rate",
            "attempted_repairs",
            "false_positives",
            "precision",
            "average_trust",
            "outcomes",
        ):
            assert key in d

    def test_to_dict_is_json_serializable(self) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE, FAILED_CASE, ALREADY_VALID_CASE])
        json.dumps(summary.to_dict())  # must not raise

    def test_to_dict_outcome_shape(self) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE])
        outcome_dict = summary.to_dict()["outcomes"][0]
        for key in (
            "name",
            "description",
            "expected_status",
            "actual_status",
            "passed",
            "average_trust",
            "error",
        ):
            assert key in outcome_dict


# ===========================================================================
# write_results
# ===========================================================================


class TestWriteResults:
    def test_creates_results_directory(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        summary = runner.run_benchmark([SUCCESS_CASE])
        runner.write_results(summary, results_dir)
        assert results_dir.exists()

    def test_writes_valid_json_file(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        summary = runner.run_benchmark([SUCCESS_CASE])
        out_path = runner.write_results(summary, results_dir)
        assert out_path.exists()
        data = json.loads(out_path.read_text())
        assert data["total_cases"] == 1

    def test_filename_contains_timestamp(self, tmp_path: Path) -> None:
        results_dir = tmp_path / "results"
        summary = runner.run_benchmark([SUCCESS_CASE])
        out_path = runner.write_results(summary, results_dir)
        assert out_path.name.startswith("run_")
        assert out_path.suffix == ".json"


# ===========================================================================
# print_summary (smoke test — just confirm it doesn't raise)
# ===========================================================================


class TestPrintSummary:
    def test_print_summary_does_not_raise(self, capsys: pytest.CaptureFixture) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE, FAILED_CASE, ALREADY_VALID_CASE])
        runner.print_summary(summary)
        out = capsys.readouterr().out
        assert "StateGuard Benchmark Results" in out
        assert "Total cases:        3" in out

    def test_print_summary_verbose_includes_description(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        summary = runner.run_benchmark([SUCCESS_CASE])
        runner.print_summary(summary, verbose=True)
        out = capsys.readouterr().out
        assert "test case" in out

    def test_print_summary_shows_error_for_crashed_case(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        bad_case = {
            "name": "crash_test",
            "expected_schema": {"fields": [{"path": "x"}]},
            "broken_payload": {"x": 1},
            "expected_result": {"status": "success"},
        }
        summary = runner.run_benchmark([bad_case])
        runner.print_summary(summary)
        out = capsys.readouterr().out
        assert "ERROR" in out


# ===========================================================================
# main() — CLI entry point
# ===========================================================================


class TestMain:
    def test_main_returns_zero_on_all_passing(self, tmp_path: Path) -> None:
        cases_dir = tmp_path / "cases"
        cases_dir.mkdir()
        _write_case(cases_dir, "a.json", SUCCESS_CASE)
        results_dir = tmp_path / "results"

        exit_code = runner.main(
            [
                "--cases-dir",
                str(cases_dir),
                "--results-dir",
                str(results_dir),
            ]
        )
        assert exit_code == 0

    def test_main_returns_one_on_failure(self, tmp_path: Path) -> None:
        cases_dir = tmp_path / "cases"
        cases_dir.mkdir()
        bad_case = dict(SUCCESS_CASE)
        bad_case["expected_result"] = {"status": "failed"}  # will mismatch
        _write_case(cases_dir, "a.json", bad_case)
        results_dir = tmp_path / "results"

        exit_code = runner.main(
            [
                "--cases-dir",
                str(cases_dir),
                "--results-dir",
                str(results_dir),
            ]
        )
        assert exit_code == 1

    def test_main_returns_one_on_empty_cases_dir(self, tmp_path: Path) -> None:
        cases_dir = tmp_path / "empty_cases"
        cases_dir.mkdir()
        exit_code = runner.main(["--cases-dir", str(cases_dir)])
        assert exit_code == 1

    def test_main_writes_results_file(self, tmp_path: Path) -> None:
        cases_dir = tmp_path / "cases"
        cases_dir.mkdir()
        _write_case(cases_dir, "a.json", SUCCESS_CASE)
        results_dir = tmp_path / "results"

        runner.main(["--cases-dir", str(cases_dir), "--results-dir", str(results_dir)])
        result_files = list(results_dir.glob("run_*.json"))
        assert len(result_files) == 1

    def test_main_no_write_skips_results_file(self, tmp_path: Path) -> None:
        cases_dir = tmp_path / "cases"
        cases_dir.mkdir()
        _write_case(cases_dir, "a.json", SUCCESS_CASE)
        results_dir = tmp_path / "results"

        runner.main(
            [
                "--cases-dir",
                str(cases_dir),
                "--results-dir",
                str(results_dir),
                "--no-write",
            ]
        )
        assert not results_dir.exists()

    def test_main_verbose_flag_accepted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        cases_dir = tmp_path / "cases"
        cases_dir.mkdir()
        _write_case(cases_dir, "a.json", SUCCESS_CASE)
        results_dir = tmp_path / "results"

        exit_code = runner.main(
            [
                "--cases-dir",
                str(cases_dir),
                "--results-dir",
                str(results_dir),
                "--verbose",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "test case" in out


# ===========================================================================
# Real benchmark case files — smoke test against the actual shipped cases
# ===========================================================================


class TestRealBenchmarkCases:
    def test_all_shipped_cases_load_without_error(self) -> None:
        cases_dir = _REPO_ROOT / "benchmarks" / "cases"
        cases = runner.load_cases(cases_dir)
        assert len(cases) >= 8

    def test_all_shipped_cases_pass(self) -> None:
        """The 9 cases authored for M9 must all match their own
        expected_result — this is the harness's own regression guard."""
        cases_dir = _REPO_ROOT / "benchmarks" / "cases"
        cases = runner.load_cases(cases_dir)
        summary = runner.run_benchmark(cases)
        failing = [o for o in summary.outcomes if not o.passed]
        assert failing == [], f"Failing cases: {[(o.name, o.error) for o in failing]}"

    def test_every_repairable_shipped_case_is_repaired(self) -> None:
        cases_dir = _REPO_ROOT / "benchmarks" / "cases"
        cases = runner.load_cases(cases_dir)
        summary = runner.run_benchmark(cases)

        assert summary.repair_rate == pytest.approx(1.0)

    def test_no_shipped_case_is_repaired_that_should_not_be(self) -> None:
        """
        Precision, not repair rate, is the number that catches a regression
        that matters: a case repairing when its expected_result says it must
        not is a silently wrong repair.
        """
        cases_dir = _REPO_ROOT / "benchmarks" / "cases"
        summary = runner.run_benchmark(runner.load_cases(cases_dir))

        assert summary.false_positives == 0
        assert summary.precision == pytest.approx(1.0)


class TestRepairRateDetectsDegradation:
    """
    ``repair_rate`` counts a repairable case only when it *passed*.

    Asking only whether the outcome landed somewhere in {success, partial}
    meant a case degrading from success to partial still counted, so the
    headline number stayed at 100% through the regression it exists to catch.
    """

    def test_a_case_that_degrades_to_partial_does_not_count(self) -> None:
        degraded = dict(SUCCESS_CASE)
        # The payload can be renamed but not coerced, so the case that expects
        # SUCCESS actually lands on PARTIAL.
        degraded["expected_schema"] = {
            "fields": [
                {"path": "temperature", "type": "float"},
                {
                    "path": "station",
                    "type": "string",
                    "constraints": [{"type": "min_length", "value": 99}],
                },
            ]
        }
        degraded["broken_payload"] = {"temp_celsius": 31.5, "station": "KBOS"}

        summary = runner.run_benchmark([degraded])
        assert summary.outcomes[0].actual_status == "partial"
        assert summary.outcomes[0].passed is False
        assert summary.repairable_cases == 1
        assert summary.repaired_correctly == 0
        assert summary.repair_rate == pytest.approx(0.0)

    def test_a_case_below_its_declared_min_confidence_does_not_count(self) -> None:
        strict = dict(SUCCESS_CASE)
        strict["expected_result"] = {"status": "success", "min_confidence": 0.999}
        summary = runner.run_benchmark([strict])
        assert summary.outcomes[0].actual_status == "success"
        assert summary.outcomes[0].passed is False
        assert summary.repair_rate == pytest.approx(0.0)
