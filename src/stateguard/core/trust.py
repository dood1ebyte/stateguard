"""
TrustPolicy — turns measured evidence into a decision.

This is the one place in StateGuard where a number is attached to a proposed
repair.  Strategies report what they measured (``RepairEvidence``) and how bad
it would be to be wrong (``RepairRisk``); this module decides what that is
worth and whether to act on it.

Why it is centralised
---------------------
Previously each strategy invented its own score on its own scale and the
engine compared all of them against a single global threshold.  Those numbers
were never commensurable — a fuzzy matcher's 0.80 described name similarity, a
coercion's 0.85 described whether a cast was defined — and one of them was
explicitly hand-tuned to clear the threshold rather than derived from
anything.  Centralising the arithmetic means the model can be recalibrated
without touching a single strategy, and thresholds can differ by consequence.

Three regions, not two
----------------------
The decision is ``APPLY`` / ``AMBIGUOUS`` / ``REJECT``, not a boolean.  This is
the standard three-region model from record linkage (Fellegi & Sunter, 1969):
match, possible-match-requiring-review, non-match.  A binary threshold has to
put every borderline case on one side or the other, so it either guesses or
silently discards; an abstain band lets the engine say "I found a repair but
will not apply it unsupervised" and hand the candidates back to the caller.

Calibration
-----------
The bands below are not round numbers picked for looking reasonable.  They were
fitted to the repairs the test corpus and benchmark suite require, measured with
the same Jaro–Winkler and bipartite-margin code the strategies use:

    must APPLY      temp_celsius -> temperature    name 0.809  margin 0.337  -> 0.809
    must APPLY      zipcode      -> zip_code       name 0.971  margin 1.000  -> 0.971
    must APPLY      cod          -> code           name 0.942  margin 1.000  -> 0.942
    must ABSTAIN    user_email   -> user_name      name 0.913  margin 0.021  -> 0.678
    must REJECT     temp_celsius -> country_code   name 0.472                -> 0.472

Note what that table shows: ``user_email`` scores *higher* on raw name
similarity (0.913) than the repair that must succeed (0.809).  Name similarity
alone cannot separate them.  The margin can — 0.021 against 0.337 — which is
why margin is applied as a multiplier rather than used as a silent veto.

The lowest must-apply score is 0.809 and the highest must-abstain score is
0.678, so ``INFERRED.apply_at`` is set at 0.75, roughly midway, leaving
comparable headroom on both sides.

Zero external dependencies — part of Layer 2 (depends on Layer 1: operations).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from stateguard.core.errors.operations import FieldOperation, RepairEvidence, RepairRisk

__all__ = [
    "DEFAULT_TRUST_BANDS",
    "TrustBand",
    "TrustDecision",
    "TrustPolicy",
]


# ---------------------------------------------------------------------------
# Margin handling
# ---------------------------------------------------------------------------

#: A margin at or above this is treated as decisive — the runner-up is far
#: enough behind that it contributes no doubt.  Matches the historical
#: ``RepairConfig.score_collision_margin`` default, which used the same number
#: as a hard veto.
MARGIN_FULL_CREDIT = 0.15

#: Trust retained when the runner-up is an exact tie.  A near-tie should
#: *degrade* a proposal into the abstain band rather than delete it, so this
#: floor is deliberately well above zero: 0.913 × 0.70 = 0.639, which is
#: ambiguous rather than rejected.
MARGIN_TIE_FACTOR = 0.70


# ---------------------------------------------------------------------------
# TrustDecision
# ---------------------------------------------------------------------------


class TrustDecision(StrEnum):
    """
    What the engine should do with a scored operation.

    Members
    -------
    APPLY:
        Trust clears the bar for this risk tier.  Apply it.
    AMBIGUOUS:
        A real repair was found but the evidence does not justify applying it
        unsupervised.  Recorded on the result so a caller can re-prompt, a
        reviewer can choose, or Shadow Mode can display it.
    REJECT:
        The evidence is too weak to consider this a repair at all.
    """

    APPLY = "apply"
    AMBIGUOUS = "ambiguous"
    REJECT = "reject"


# ---------------------------------------------------------------------------
# TrustBand
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustBand:
    """
    The two cut-points for one ``RepairRisk`` tier.

    ``trust < reject_below`` rejects, ``trust >= apply_at`` applies, and
    anything between the two is ambiguous.  Setting ``apply_at`` above 1.0
    makes a tier never auto-apply; setting both to 0.0 makes it always apply.
    """

    reject_below: float
    apply_at: float

    def __post_init__(self) -> None:
        if self.reject_below > self.apply_at:
            raise ValueError(
                f"reject_below ({self.reject_below}) must not exceed "
                f"apply_at ({self.apply_at}) — that would leave no ambiguous band."
            )


#: Default cut-points per risk tier. Higher consequence demands more evidence.
DEFAULT_TRUST_BANDS: dict[RepairRisk, TrustBand] = {
    # Exactly reversible: the value survives, so a mistake is cheap.
    RepairRisk.REVERSIBLE: TrustBand(reject_below=0.50, apply_at=0.70),
    # The contract said so. There is nothing to be unsure about.
    RepairRisk.DECLARED: TrustBand(reject_below=0.0, apply_at=0.0),
    # A correspondence we inferred. Calibrated — see the module docstring.
    RepairRisk.INFERRED: TrustBand(reject_below=0.60, apply_at=0.75),
    # Information is invented or destroyed; demand near-certainty.
    RepairRisk.LOSSY: TrustBand(reject_below=0.70, apply_at=0.95),
    # Never automatic. apply_at above 1.0 means every proposal abstains.
    RepairRisk.DESTRUCTIVE: TrustBand(reject_below=0.0, apply_at=1.01),
}


# ---------------------------------------------------------------------------
# TrustPolicy
# ---------------------------------------------------------------------------


class TrustPolicy:
    """
    Scores ``RepairEvidence`` and decides what to do with the result.

    Parameters
    ----------
    bands:
        Per-risk cut-points.  Defaults to ``DEFAULT_TRUST_BANDS``.  Supplying
        a partial mapping overrides only the tiers named.
    margin_full_credit:
        Margin at or above which the runner-up contributes no doubt.
    margin_tie_factor:
        Trust multiplier when the runner-up is an exact tie.
    minimum_trust:
        A floor applied across every tier.  Only ever raises a tier's
        ``apply_at``, so the per-risk bars remain the primary control and
        this is the single dial for "be more conservative about everything".
        Fed from ``RepairConfig.min_confidence_threshold``.

    Stateless and safe to share across repairs.
    """

    def __init__(
        self,
        bands: dict[RepairRisk, TrustBand] | None = None,
        margin_full_credit: float = MARGIN_FULL_CREDIT,
        margin_tie_factor: float = MARGIN_TIE_FACTOR,
        minimum_trust: float = 0.0,
    ) -> None:
        merged = dict(DEFAULT_TRUST_BANDS)
        if bands:
            merged.update(bands)
        self._bands = merged
        self._margin_full_credit = margin_full_credit
        self._margin_tie_factor = margin_tie_factor
        self._minimum_trust = minimum_trust

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score(self, evidence: RepairEvidence) -> float:
        """
        Combine *evidence* into a trust score in ``[0.0, 1.0]``.

        Supporting signals are combined with ``min()`` — trust is capped by
        the weakest applicable piece of evidence, because these are necessary
        conditions rather than votes.  ``max()`` was the previous combiner and
        is precisely what let a single hand-tuned signal carry a proposal past
        the threshold on its own.

        The margin then scales the result: a decisive win keeps full credit, a
        near-tie is pulled down toward ``margin_tie_factor``.  Scaling rather
        than vetoing is what turns a contested match into an *ambiguous*
        outcome the caller can see, instead of one that silently disappears.

        Evidence with no applicable signals scores 0.0 — a strategy that
        measured nothing has demonstrated nothing.
        """
        scores = evidence.applicable_scores
        if not scores:
            return 0.0
        return min(scores) * self.margin_factor(evidence.margin)

    def margin_factor(self, margin: float | None) -> float:
        """
        Trust multiplier for a given *margin* over the runner-up.

        ``None`` means there was no competitor at all, which earns full
        credit.
        """
        if margin is None:
            return 1.0
        if self._margin_full_credit <= 0.0:
            return 1.0
        reach = min(1.0, margin / self._margin_full_credit)
        return self._margin_tie_factor + (1.0 - self._margin_tie_factor) * reach

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    def band_for(self, risk: RepairRisk) -> TrustBand:
        """
        The effective cut-points governing *risk*.

        ``minimum_trust`` is folded in here as a floor that can only *raise*
        the bar, never lower it, so a caller who wants StateGuard to be more
        conservative across the board has one dial to turn without having to
        restate every tier.
        """
        band = self._bands[risk]
        if self._minimum_trust <= band.apply_at:
            return band
        return TrustBand(
            reject_below=min(band.reject_below, self._minimum_trust),
            apply_at=self._minimum_trust,
        )

    def decide(self, trust: float, risk: RepairRisk) -> TrustDecision:
        """Classify *trust* against the effective band for *risk*."""
        band = self.band_for(risk)
        if trust >= band.apply_at:
            return TrustDecision.APPLY
        if trust < band.reject_below:
            return TrustDecision.REJECT
        return TrustDecision.AMBIGUOUS

    def evaluate(self, operation: FieldOperation) -> tuple[FieldOperation, TrustDecision]:
        """
        Score *operation* and classify it.

        Returns the operation carrying its computed ``trust`` (a new instance —
        ``FieldOperation`` is frozen) together with the decision.

        Legacy fallback
        ---------------
        ``IRepairStrategy`` is a documented extension point, so third-party
        strategies exist that were written against the pre-evidence API and
        set a score on the operation directly.  When an operation carries no
        applicable evidence at all, its pre-set ``trust`` is honoured rather
        than scored to zero — otherwise upgrading would silently reject every
        repair such a strategy proposes.

        This path is deprecated and disappears with the ``confidence`` alias.
        Built-in strategies never take it: they all report evidence.
        """
        if operation.evidence.applicable_scores:
            trust = self.score(operation.evidence)
        else:
            trust = operation.trust
        return replace(operation, trust=trust), self.decide(trust, operation.risk)

    # ------------------------------------------------------------------
    # Explanation
    # ------------------------------------------------------------------

    def explain(self, operation: FieldOperation, decision: TrustDecision) -> str:
        """
        Render why *operation* received its decision.

        Contains no field values — only paths, scores, and the cut-points that
        were applied — so it is safe to log regardless of
        ``RepairConfig.include_values_in_log``.
        """
        band = self._bands[operation.risk]
        source = f" <- {operation.source_path}" if operation.source_path else ""
        lines = [
            f"{operation.op_type.value.upper()}  {operation.target_path}{source}"
            f"  trust {operation.trust:.2f}  risk {operation.risk.name}"
            f"  -> {decision.value.upper()}"
        ]

        evidence = operation.evidence
        for label, value in (
            ("schema_authority", evidence.schema_authority),
            ("name_match", evidence.name_match),
            ("value_preserved", evidence.value_preserved),
        ):
            if value is not None:
                lines.append(f"  {label:<17} {value:.3f}")
        if evidence.margin is not None:
            lines.append(
                f"  {'margin':<17} {evidence.margin:.3f} "
                f"(x{self.margin_factor(evidence.margin):.2f})"
            )
        if evidence.alternatives_considered:
            lines.append(f"  {'alternatives':<17} {evidence.alternatives_considered}")
        for name, value in evidence.signals:
            lines.append(f"  {name:<17} {value:.3f}")
        for note in evidence.notes:
            lines.append(f"  - {note}")

        lines.append(
            f"  {'decision':<17} {operation.risk.name} applies at {band.apply_at:.2f}, "
            f"rejects below {band.reject_below:.2f}"
        )
        return "\n".join(lines)
