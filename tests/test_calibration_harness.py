"""
Tests for the calibration harness itself.

The harness makes a claim about StateGuard's correctness, so its own scoring
has to be right or the published numbers are worthless. These cover the
judging rules directly, then run the shipped corpus as a regression gate.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Import benchmarks/calibrate.py directly from its file path -- benchmarks/ is
# a standalone tool, not a package. Mirrors tests/test_benchmark_runner.py.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORPUS_DIR = _REPO_ROOT / "benchmarks" / "calibration"
_CALIBRATE_PATH = _REPO_ROOT / "benchmarks" / "calibrate.py"

_spec = importlib.util.spec_from_file_location("calibrate_harness", _CALIBRATE_PATH)
assert _spec is not None and _spec.loader is not None
calibrate = importlib.util.module_from_spec(_spec)
sys.modules["calibrate_harness"] = calibrate
_spec.loader.exec_module(calibrate)


# ===========================================================================
# Comparison
# ===========================================================================


class TestIdentical:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (5, 5),
            (5.0, 5.0),
            ({"a": 1}, {"a": 1}),
            ([1, 2], [1, 2]),
            ({"a": {"b": None}}, {"a": {"b": None}}),
            ("x", "x"),
            (True, True),
        ],
    )
    def test_equal_values_of_equal_type(self, left: Any, right: Any) -> None:
        assert calibrate.identical(left, right) is True

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (5, 5.0),  # the distinction a harness must not lose
            (1, True),
            (0, False),
            ({"a": 5}, {"a": 5.0}),
            ([5], [5.0]),
            ({"a": 1}, {"a": 1, "b": 2}),
            ([1], [1, 2]),
        ],
    )
    def test_type_differences_are_not_equality(self, left: Any, right: Any) -> None:
        """``5 == 5.0`` is True in Python; for a correctness claim it must not be."""
        assert calibrate.identical(left, right) is False

    def test_absent_equals_absent_only(self) -> None:
        absent = calibrate.value_at({}, "nope")
        assert calibrate.identical(absent, calibrate.value_at({}, "other")) is True
        assert calibrate.identical(absent, None) is False
        assert calibrate.identical(None, absent) is False


class TestValueAt:
    def test_reads_a_nested_path(self) -> None:
        assert calibrate.value_at({"a": {"b": 2}}, "a.b") == 2

    def test_a_present_none_is_not_absent(self) -> None:
        assert calibrate.value_at({"a": None}, "a") is None

    @pytest.mark.parametrize("payload", [{}, {"a": 1}, "scalar", None, [1, 2]])
    def test_absent_paths(self, payload: Any) -> None:
        assert calibrate.value_at(payload, "missing") is calibrate._ABSENT


# ===========================================================================
# Judging
# ===========================================================================


def _case(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "t",
        "description": "",
        "verdict": "repair",
        "schema": {"fields": [{"path": "temperature", "type": "float"}]},
        "payload": {"temp_celsius": "31.5"},
        "expected_payload": {"temperature": 31.5},
    }
    base.update(overrides)
    return base


class TestCaseJudging:
    def test_a_correct_repair_passes(self) -> None:
        assert calibrate.run_case(_case()).passed is True

    def test_a_repair_to_the_wrong_value_fails(self) -> None:
        outcome = calibrate.run_case(_case(expected_payload={"temperature": 99.0}))
        assert outcome.passed is False
        assert "expected" in outcome.reason

    def test_a_refuse_case_that_repairs_fails(self) -> None:
        """The false-positive check: a repair happened where none should have."""
        outcome = calibrate.run_case(
            _case(verdict="refuse", expected_payload={"temp_celsius": "31.5"})
        )
        assert outcome.passed is False

    def test_a_refuse_case_that_does_nothing_passes(self) -> None:
        outcome = calibrate.run_case(
            _case(
                verdict="refuse",
                schema={"fields": [{"path": "temperature", "type": "float"}]},
                payload={"humidity": 80.0},
                expected_payload={"humidity": 80.0},
            )
        )
        assert outcome.passed is True
        assert outcome.operations == []

    def test_an_abstain_case_needs_a_surfaced_candidate(self) -> None:
        """
        Leaving the payload alone is necessary but not sufficient -- refusing
        silently is a different (worse) behaviour than abstaining visibly.
        """
        silent = calibrate.run_case(
            _case(
                verdict="abstain",
                schema={"fields": [{"path": "temperature", "type": "float"}]},
                payload={"humidity": 80.0},
                expected_payload={"humidity": 80.0},
            )
        )
        assert silent.passed is False
        assert "ambiguous" in silent.reason

    def test_a_genuine_abstention_passes(self) -> None:
        outcome = calibrate.run_case(
            _case(
                verdict="abstain",
                schema={
                    "fields": [
                        {"path": "user_id", "type": "string"},
                        {"path": "user_name", "type": "string"},
                    ]
                },
                payload={"user_email": "a@b.com"},
                expected_payload={"user_email": "a@b.com"},
            )
        )
        assert outcome.passed is True

    def test_a_crashing_case_is_recorded_not_raised(self) -> None:
        outcome = calibrate.run_case(_case(schema={"fields": "not a list"}))
        assert outcome.passed is False
        assert outcome.status == "error"


class TestOperationScoring:
    def test_a_chained_repair_scores_both_operations_correct(self) -> None:
        """
        The rename leaves "31.5" at temperature and the coercion makes it
        31.5. Scoring the rename against its own intermediate value would
        mark a correct field correspondence wrong.
        """
        outcome = calibrate.run_case(_case())
        assert len(outcome.operations) == 2
        assert {op.op_type for op in outcome.operations} == {"rename", "coerce"}
        assert all(op.correct for op in outcome.operations)

    def test_a_rename_into_a_field_that_should_be_empty_is_incorrect(self) -> None:
        outcome = calibrate.run_case(
            _case(
                verdict="refuse",
                schema={"fields": [{"path": "updated_at", "type": "string"}]},
                payload={"created_at": "2026-01-01"},
                expected_payload={"created_at": "2026-01-01"},
            )
        )
        assert outcome.operations
        assert all(op.correct is False for op in outcome.operations)


# ===========================================================================
# Aggregation
# ===========================================================================


def _op(trust: float, correct: bool) -> calibrate.OperationOutcome:
    return calibrate.OperationOutcome(
        case="c",
        verdict="repair",
        op_type="coerce",
        target_path="x",
        trust=trust,
        risk="REVERSIBLE",
        correct=correct,
    )


class TestReliabilityCurve:
    def test_buckets_by_trust_and_drops_empty_ones(self) -> None:
        buckets = calibrate.reliability_curve([_op(0.82, True), _op(0.84, False)])
        assert len(buckets) == 1
        assert buckets[0].total == 2
        assert buckets[0].correct == 1
        assert buckets[0].accuracy == pytest.approx(0.5)

    def test_trust_of_one_lands_in_the_top_bucket(self) -> None:
        """1.0 / 0.05 == 20, one past the last index."""
        buckets = calibrate.reliability_curve([_op(1.0, True)])
        assert len(buckets) == 1
        assert buckets[0].high == pytest.approx(1.0)

    def test_gap_is_positive_when_overconfident(self) -> None:
        buckets = calibrate.reliability_curve([_op(0.97, False), _op(0.97, True)])
        assert buckets[0].gap > 0

    def test_ece_is_zero_when_accuracy_equals_the_midpoint(self) -> None:
        """Perfect calibration: bucket [0.85, 0.90) has midpoint 0.875, and
        seven of eight correct is exactly 87.5%."""
        perfect = [_op(0.86, True)] * 7 + [_op(0.86, False)]
        buckets = calibrate.reliability_curve(perfect)
        assert buckets[0].accuracy == pytest.approx(buckets[0].midpoint)
        assert calibrate.expected_calibration_error(buckets) == pytest.approx(0.0)

    def test_ece_measures_the_distance_from_the_midpoint(self) -> None:
        """50% accuracy in a bucket whose midpoint is 52.5% is a 0.025 gap."""
        buckets = calibrate.reliability_curve([_op(0.52, True), _op(0.52, False)])
        assert calibrate.expected_calibration_error(buckets) == pytest.approx(0.025)

    def test_ece_weights_buckets_by_population(self) -> None:
        """
        A one-operation bucket must not count as much as a nine-operation
        one, or a single outlier dominates the published number.
        """
        ops = [_op(0.52, True)] * 9 + [_op(0.52, False)] + [_op(0.97, False)]
        buckets = calibrate.reliability_curve(ops)
        # [0.50,0.55): 90% vs 52.5% -> gap 0.375, n=10
        # [0.95,1.00):  0% vs 97.5% -> gap 0.975, n=1
        expected = (10 * 0.375 + 1 * 0.975) / 11
        assert calibrate.expected_calibration_error(buckets) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("trust", "expected_low"),
        [(0.60, 0.60), (0.70, 0.70), (0.85, 0.85), (0.95, 0.95), (1.0, 0.95), (0.849, 0.80)],
    )
    def test_band_boundary_values_land_in_the_right_bucket(
        self, trust: float, expected_low: float
    ) -> None:
        """
        ``0.95 / 0.05`` is 18.999999999999996 in binary float, so a plain
        ``int()`` files these one bucket too low -- and 0.60 and 0.70 are
        exactly INFERRED's reject_below and REVERSIBLE's apply_at.
        """
        buckets = calibrate.reliability_curve([_op(trust, True)])
        assert len(buckets) == 1
        assert buckets[0].low == pytest.approx(expected_low)

    def test_ece_is_none_with_no_operations(self) -> None:
        assert calibrate.expected_calibration_error([]) is None


# ===========================================================================
# The shipped corpus
# ===========================================================================


class TestShippedCorpus:
    @pytest.fixture(scope="class")
    @classmethod
    def outcomes(cls) -> list[calibrate.CaseOutcome]:
        # Class-scoped so the whole 80-case corpus runs once, not once per test.
        return [calibrate.run_case(c) for c in calibrate.load_corpus(_CORPUS_DIR)]

    def test_the_corpus_is_large_enough_to_mean_something(self) -> None:
        cases = calibrate.load_corpus(_CORPUS_DIR)
        # The plan calls for 60-100 labelled cases.
        assert 60 <= len(cases) <= 100

    def test_every_verdict_family_is_represented(self) -> None:
        verdicts = {c["verdict"] for c in calibrate.load_corpus(_CORPUS_DIR)}
        assert verdicts == {"repair", "abstain", "refuse"}

    def test_the_false_positive_block_is_substantial(self) -> None:
        """
        The block the plan says the 9-case benchmark suite had zero of. It is
        the reason the corpus exists, so it must not shrink to a token few.
        """
        refuse = [c for c in calibrate.load_corpus(_CORPUS_DIR) if c["verdict"] == "refuse"]
        assert len(refuse) >= 25

    def test_case_names_are_unique(self) -> None:
        names = [c["name"] for c in calibrate.load_corpus(_CORPUS_DIR)]
        assert len(names) == len(set(names))

    def test_refuse_cases_expect_an_untouched_payload(self) -> None:
        """A refuse case whose ground truth differs from its input is mislabelled."""
        for case in calibrate.load_corpus(_CORPUS_DIR):
            if case["verdict"] == "refuse":
                assert case["expected_payload"] == case["payload"], case["name"]

    def test_no_regressions(self, outcomes: list[calibrate.CaseOutcome]) -> None:
        regressions = [o.name for o in outcomes if o.regression]
        assert regressions == []

    def test_no_stale_known_gap_markers(self, outcomes: list[calibrate.CaseOutcome]) -> None:
        """
        A documented gap that now passes must fail, so the marker gets removed
        and the next real regression cannot hide behind it.
        """
        stale = [o.name for o in outcomes if o.unexpectedly_fixed]
        assert stale == []

    def test_every_known_gap_carries_its_reasoning(self) -> None:
        for case in calibrate.load_corpus(_CORPUS_DIR):
            gap = case.get("known_gap")
            if gap is not None:
                assert len(gap) > 60, case["name"]

    def test_precision_over_applied_operations(self, outcomes: list[calibrate.CaseOutcome]) -> None:
        """
        Guards the headline number. Drops here mean StateGuard started
        applying repairs the corpus says are wrong.
        """
        operations = [op for o in outcomes for op in o.operations]
        correct = sum(1 for op in operations if op.correct)
        assert correct / len(operations) >= 0.90

    def test_expected_calibration_error_stays_low(
        self, outcomes: list[calibrate.CaseOutcome]
    ) -> None:
        operations = [op for o in outcomes for op in o.operations]
        ece = calibrate.expected_calibration_error(calibrate.reliability_curve(operations))
        assert ece is not None
        assert ece <= 0.10

    def test_the_harness_exits_zero_on_the_shipped_corpus(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert calibrate.main([]) == 0
        assert "Calibration Report" in capsys.readouterr().out


class TestCorpusFilesAreWellFormed:
    @pytest.mark.parametrize("path", sorted(_CORPUS_DIR.glob("*.json")), ids=lambda p: p.name)
    def test_required_keys(self, path: Path) -> None:
        block = json.loads(path.read_text(encoding="utf-8"))
        assert block["verdict"] in {"repair", "abstain", "refuse"}
        assert block["description"]
        assert block["cases"]
        for case in block["cases"]:
            assert case["name"]
            assert case["description"]
            assert "schema" in case
            assert "payload" in case
            assert "expected_payload" in case
