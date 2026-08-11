"""
The trust model: evidence in, decision out.

Covers what replaced the single global confidence threshold:

* strategies report evidence and declare risk; ``TrustPolicy`` assigns the
  score, so numbers from different strategies are finally comparable;
* the bar varies by *consequence*, not just by likelihood;
* borderline proposals abstain into ``AMBIGUOUS`` instead of being applied on
  a hunch or dropped without trace.

The band values are calibrated, not chosen for looking round --
``TestCalibration`` pins them to the repairs the corpus actually requires.
"""

from __future__ import annotations

import pytest

from stateguard import ContractGuard
from stateguard.core.errors.operations import (
    FieldOperation,
    FieldOpType,
    RepairEvidence,
    RepairRisk,
)
from stateguard.core.errors.results import RepairStatus
from stateguard.core.strategies.fuzzy import jaro_winkler, score_assignments
from stateguard.core.trust import (
    DEFAULT_TRUST_BANDS,
    TrustBand,
    TrustDecision,
    TrustPolicy,
)


@pytest.fixture
def policy() -> TrustPolicy:
    return TrustPolicy()


# ===========================================================================
# Evidence combination
# ===========================================================================


class TestScoring:
    def test_no_applicable_evidence_scores_zero(self, policy: TrustPolicy) -> None:
        """A strategy that measured nothing has demonstrated nothing."""
        assert policy.score(RepairEvidence()) == 0.0

    def test_weakest_signal_caps_the_score(self, policy: TrustPolicy) -> None:
        """
        Signals combine with min(), not max().

        max() was the previous combiner and is why a single hand-tuned
        prefix boost could carry a proposal past the threshold on its own.
        """
        evidence = RepairEvidence(name_match=0.95, value_preserved=0.60)
        assert policy.score(evidence) == pytest.approx(0.60)

    def test_margin_scales_rather_than_vetoes(self, policy: TrustPolicy) -> None:
        strong = policy.score(RepairEvidence(name_match=0.9, margin=1.0))
        tie = policy.score(RepairEvidence(name_match=0.9, margin=0.0))
        assert strong == pytest.approx(0.9)
        assert 0.0 < tie < strong

    def test_absent_margin_earns_full_credit(self, policy: TrustPolicy) -> None:
        """``None`` means there was no competitor at all."""
        assert policy.margin_factor(None) == 1.0

    def test_margin_at_full_credit_threshold_is_not_penalised(self, policy: TrustPolicy) -> None:
        assert policy.margin_factor(0.15) == pytest.approx(1.0)

    def test_a_tie_degrades_into_the_abstain_band_rather_than_to_zero(
        self, policy: TrustPolicy
    ) -> None:
        """
        The tie floor is deliberately well above zero: a contested match
        should be surfaced for review, not deleted.
        """
        trust = policy.score(RepairEvidence(name_match=0.913, margin=0.0))
        assert policy.decide(trust, RepairRisk.INFERRED) is TrustDecision.AMBIGUOUS


# ===========================================================================
# Risk tiers
# ===========================================================================


class TestRiskTiers:
    @pytest.mark.parametrize("risk", list(RepairRisk))
    def test_every_tier_has_a_band(self, risk: RepairRisk, policy: TrustPolicy) -> None:
        assert isinstance(policy.band_for(risk), TrustBand)

    def test_higher_consequence_demands_more_evidence(self) -> None:
        """The whole point of the tiering: cost of being wrong sets the bar."""
        bands = DEFAULT_TRUST_BANDS
        assert (
            bands[RepairRisk.REVERSIBLE].apply_at
            < bands[RepairRisk.INFERRED].apply_at
            < bands[RepairRisk.LOSSY].apply_at
        )

    def test_declared_always_applies(self, policy: TrustPolicy) -> None:
        """The contract is the authority; there is nothing to be unsure of."""
        trust = policy.score(RepairEvidence(schema_authority=1.0))
        assert policy.decide(trust, RepairRisk.DECLARED) is TrustDecision.APPLY

    def test_destructive_never_applies_even_at_perfect_trust(self, policy: TrustPolicy) -> None:
        assert policy.decide(1.0, RepairRisk.DESTRUCTIVE) is TrustDecision.AMBIGUOUS

    @pytest.mark.parametrize(
        ("risk", "trust", "expected"),
        [
            (RepairRisk.INFERRED, 0.59, TrustDecision.REJECT),
            (RepairRisk.INFERRED, 0.60, TrustDecision.AMBIGUOUS),
            (RepairRisk.INFERRED, 0.74, TrustDecision.AMBIGUOUS),
            (RepairRisk.INFERRED, 0.75, TrustDecision.APPLY),
            (RepairRisk.LOSSY, 0.94, TrustDecision.AMBIGUOUS),
            (RepairRisk.LOSSY, 0.95, TrustDecision.APPLY),
            (RepairRisk.REVERSIBLE, 0.49, TrustDecision.REJECT),
            (RepairRisk.REVERSIBLE, 0.70, TrustDecision.APPLY),
        ],
    )
    def test_band_boundaries(
        self, policy: TrustPolicy, risk: RepairRisk, trust: float, expected: TrustDecision
    ) -> None:
        assert policy.decide(trust, risk) is expected

    def test_a_band_cannot_invert(self) -> None:
        with pytest.raises(ValueError, match="no ambiguous band"):
            TrustBand(reject_below=0.9, apply_at=0.5)


# ===========================================================================
# Calibration against the corpus
# ===========================================================================


class TestCalibration:
    """
    Pins the bands to the repairs the corpus requires.

    Name similarity alone cannot separate these: ``user_email`` scores higher
    against both of its targets (0.891 / 0.913) than ``temp_celsius`` does
    against ``temperature`` (0.809). The margin is what distinguishes them, so
    these cases guard the margin handling as much as the thresholds.
    """

    @pytest.mark.parametrize(
        ("targets", "candidates", "expected"),
        [
            (["temperature", "humidity"], ["temp_celsius"], TrustDecision.APPLY),
            (["zip_code"], ["zipcode"], TrustDecision.APPLY),
            (["code"], ["cod"], TrustDecision.APPLY),
            (["user_id", "user_name"], ["user_email"], TrustDecision.AMBIGUOUS),
        ],
    )
    def test_required_outcomes(
        self,
        policy: TrustPolicy,
        targets: list[str],
        candidates: list[str],
        expected: TrustDecision,
    ) -> None:
        assignment = score_assignments(targets, candidates)[0]
        evidence = RepairEvidence(name_match=assignment.score, margin=assignment.margin)
        trust = policy.score(evidence)
        assert policy.decide(trust, RepairRisk.INFERRED) is expected

    def test_the_dangerous_pair_scores_higher_on_similarity_alone(self) -> None:
        """
        Documents *why* margin carries the weight. If this ever stops being
        true, the calibration argument in trust.py needs revisiting.
        """
        assert jaro_winkler("user_name", "user_email") > jaro_winkler("temperature", "temp_celsius")


# ===========================================================================
# Ambiguity end to end
# ===========================================================================


class TestAmbiguousOutcome:
    CONTESTED = {
        "fields": [
            {"path": "user_id", "type": "string"},
            {"path": "user_name", "type": "string"},
        ]
    }

    @pytest.fixture
    def guard(self) -> ContractGuard:
        return ContractGuard.with_dict_schema()

    def test_contested_rename_is_not_applied(self, guard: ContractGuard) -> None:
        """
        The headline bug this phase fixes.

        A single 'user_email' is nearly equally good for 'user_id' and
        'user_name'. It used to be renamed into whichever sorted first --
        so an email address landed in a user id, at 0.82 "confidence".
        """
        result = guard.repair(self.CONTESTED, {"user_email": "a@b.com"})
        assert result.status is RepairStatus.AMBIGUOUS
        assert result.repaired_output is None

    def test_the_candidate_is_surfaced_not_dropped(self, guard: ContractGuard) -> None:
        result = guard.repair(self.CONTESTED, {"user_email": "a@b.com"})

        assert result.has_ambiguous_repairs
        assert len(result.ambiguous) == 1
        candidate = result.ambiguous[0].best
        assert candidate is not None
        assert candidate.source_path == "user_email"
        assert candidate.risk is RepairRisk.INFERRED

    def test_the_reason_names_the_bar_that_was_missed(self, guard: ContractGuard) -> None:
        result = guard.repair(self.CONTESTED, {"user_email": "a@b.com"})
        assert "INFERRED" in result.ambiguous[0].reason

    def test_ambiguous_is_distinguishable_from_failed(self, guard: ContractGuard) -> None:
        """
        FAILED means "no repair found". AMBIGUOUS means "found one, withheld
        it" -- the difference is the only part a caller can act on.
        """
        ambiguous = guard.repair(self.CONTESTED, {"user_email": "a@b.com"})
        failed = guard.repair(self.CONTESTED, {"zzzz": 1, "qqqq": 2})

        assert ambiguous.is_ambiguous and not ambiguous.is_failed
        assert failed.is_failed and not failed.is_ambiguous
        assert failed.ambiguous == []

    def test_an_unambiguous_rename_still_applies(self, guard: ContractGuard) -> None:
        """The safe case must not regress into abstention."""
        schema = {
            "fields": [
                {"path": "temperature", "type": "float"},
                {"path": "humidity", "type": "integer"},
            ]
        }
        result = guard.repair(schema, {"temp_celsius": 31.5, "humidity": 80})

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {"temperature": 31.5, "humidity": 80}
        assert result.ambiguous == []

    def test_a_run_can_repair_one_field_and_abstain_on_another(self, guard: ContractGuard) -> None:
        """``has_ambiguous_repairs`` is not the same as ``is_ambiguous``."""
        schema = {
            "fields": [
                {"path": "temperature", "type": "float"},
                {"path": "user_id", "type": "string"},
                {"path": "user_name", "type": "string"},
            ]
        }
        # 'temp_celsius' has one obvious home and is applied; 'user_email' is
        # contested between two fields and is withheld.
        result = guard.repair(schema, {"temp_celsius": 31.5, "user_email": "a@b.com"})

        assert result.has_ambiguous_repairs
        assert result.status is not RepairStatus.AMBIGUOUS
        assert result.repaired_output is not None
        assert result.repaired_output["temperature"] == 31.5


# ===========================================================================
# Explanations
# ===========================================================================


class TestExplain:
    def test_names_the_decision_and_the_bar(self, policy: TrustPolicy) -> None:
        op = FieldOperation(
            op_type=FieldOpType.RENAME,
            target_path="user_id",
            rationale="fuzzy",
            source_path="user_email",
            risk=RepairRisk.INFERRED,
            evidence=RepairEvidence(name_match=0.891, margin=0.021),
        )
        text = policy.explain(*policy.evaluate(op))

        assert "user_id" in text
        assert "user_email" in text
        assert "INFERRED" in text
        assert "AMBIGUOUS" in text
        assert "name_match" in text
        assert "margin" in text

    def test_carries_no_field_values(self, policy: TrustPolicy) -> None:
        """
        Explanations are written into log entries, and
        RepairConfig.include_values_in_log defaults to False. Paths and
        scores are safe to log; payload values are not.
        """
        op = FieldOperation(
            op_type=FieldOpType.COERCE,
            target_path="api_key",
            rationale="coerce",
            risk=RepairRisk.LOSSY,
            evidence=RepairEvidence(value_preserved=1.0),
        )
        assert "sk-live-SECRET" not in policy.explain(*policy.evaluate(op))


# ===========================================================================
# Deprecated alias
# ===========================================================================


class TestConfidenceAlias:
    def test_confidence_still_reads(self) -> None:
        op = FieldOperation(op_type=FieldOpType.REMOVE, target_path="x", rationale="r", trust=0.42)
        assert op.confidence == 0.42
        assert op.confidence == op.trust

    def test_confidence_is_not_a_constructor_argument(self) -> None:
        with pytest.raises(TypeError):
            FieldOperation(  # type: ignore[call-arg]
                op_type=FieldOpType.REMOVE,
                target_path="x",
                rationale="r",
                confidence=0.42,
            )


# ===========================================================================
# Effective bands
# ===========================================================================


class TestExplainReportsEffectiveBands:
    """
    ``explain()`` must render the cut-points that were actually applied.

    Reading the raw per-risk bands made the explanation contradict the
    decision it was explaining as soon as ``minimum_trust`` raised a tier's
    bar -- which reads as an engine bug rather than as the caller's own
    threshold doing its job.
    """

    def _rename(self) -> FieldOperation:
        return FieldOperation(
            op_type=FieldOpType.RENAME,
            target_path="user_id",
            rationale="r",
            source_path="user_email",
            risk=RepairRisk.INFERRED,
            evidence=RepairEvidence(name_match=0.891, margin=1.0),
        )

    def test_raised_bar_is_the_one_explained(self) -> None:
        policy = TrustPolicy(minimum_trust=0.99)
        scored, decision = policy.evaluate(self._rename())

        assert decision is TrustDecision.AMBIGUOUS
        explanation = policy.explain(scored, decision)
        assert "applies at 0.99" in explanation
        assert "applies at 0.75" not in explanation

    def test_declared_tier_shows_the_floor_it_actually_uses(self) -> None:
        # ContractGuard's default wiring: min_confidence_threshold -> floor.
        policy = TrustPolicy(minimum_trust=0.7)
        op = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="humidity",
            rationale="r",
            risk=RepairRisk.DECLARED,
            evidence=RepairEvidence(schema_authority=1.0),
        )
        scored, decision = policy.evaluate(op)
        assert decision is TrustDecision.APPLY
        assert "DECLARED applies at 0.70" in policy.explain(scored, decision)

    def test_unraised_bands_are_unchanged(self) -> None:
        policy = TrustPolicy()
        scored, decision = policy.evaluate(self._rename())
        assert "applies at 0.75" in policy.explain(scored, decision)


class TestPolicyConfiguration:
    def test_partial_band_override_replaces_only_that_tier(self) -> None:
        policy = TrustPolicy(bands={RepairRisk.INFERRED: TrustBand(0.1, 0.2)})
        assert policy.band_for(RepairRisk.INFERRED) == TrustBand(0.1, 0.2)
        # Every other tier keeps its default.
        assert policy.band_for(RepairRisk.LOSSY) == DEFAULT_TRUST_BANDS[RepairRisk.LOSSY]

    def test_zero_margin_full_credit_disables_margin_scaling(self) -> None:
        """A caller who does not want margin to bite sets the reach to zero."""
        policy = TrustPolicy(margin_full_credit=0.0)
        assert policy.margin_factor(0.0) == 1.0
        assert policy.score(RepairEvidence(name_match=0.9, margin=0.0)) == pytest.approx(0.9)

    def test_signals_are_rendered_in_explanations(self) -> None:
        policy = TrustPolicy()
        op = FieldOperation(
            op_type=FieldOpType.COERCE,
            target_path="meta",
            rationale="r",
            risk=RepairRisk.LOSSY,
            evidence=RepairEvidence(value_preserved=1.0, signals=(("structure_preserved", 0.0),)),
        )
        assert "structure_preserved" in policy.explain(*policy.evaluate(op))
