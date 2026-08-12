"""
FuzzyFieldMatchStrategy — repairs correlated MISSING/UNEXPECTED field pairs
via approximate name matching.

Priority 20.  This is the strategy that handles the canonical "schema
drift" scenario: a tool returns ``temp_celsius`` when the contract expects
``temperature``.  The validator reports this as one
``MISSING_REQUIRED_FIELD`` (``temperature``) and one ``UNEXPECTED_FIELD``
(``temp_celsius``); this strategy proposes the ``RENAME``.

What this strategy decides, and what it does not
------------------------------------------------
It proposes pairings and reports **evidence**.  It does not score them and
it does not decide whether one is safe to apply — ``TrustPolicy`` does both,
from the ``name_match`` and ``margin`` reported here.

Matching algorithm
------------------
**Similarity is Jaro-Winkler** (``jaro_winkler``).  Winkler's prefix
adjustment rewards a shared opening substring by construction, which is the
same intuition the old token-prefix boost encoded — except it falls out of
the metric instead of a constant hand-picked to clear a threshold.

**Assignment is global** (``score_assignments``).  Every
(missing field × unexpected key) pair is scored, the best pairing overall is
taken first, and both endpoints are consumed.  Previously missing fields were
walked in alphabetical order and each claimed its best remaining candidate,
which made the alphabet the tie-breaker for contested renames.

**Ambiguity is measured, not vetoed.**  Each pairing carries a bipartite
margin: the smaller of "how much better is this candidate than the next
candidate for this field" and "how much better is this field than the next
field for this candidate".  A pairing is only decisive if it wins from both
directions.

**Competition is measured over the whole problem, not the residual.**  The
margin for a pairing counts every field and every key in the payload,
including the ones earlier assignments already consumed.  Measuring against
only what is still unassigned made the *last* pairing in a multi-rename
payload look uncontested by construction — see ``score_assignments``.

Why the margin carries the weight
---------------------------------
Measured against the real corpus, name similarity alone cannot tell a safe
rename from a coin-flip::

    user_email -> user_name     0.913     must NOT be applied unsupervised
    user_email -> user_id       0.891     must NOT be applied unsupervised
    temp_celsius -> temperature 0.809     must be applied

The dangerous pairing scores *higher* than the one that has to succeed.  What
separates them is competition: ``temp_celsius`` beats its runner-up by 0.337,
while ``user_email`` is equally at home in two places (margin 0.021).  That is
why margin is evidence rather than a silent veto — it scales trust, so a
contested match degrades into an *ambiguous* outcome the caller can see
instead of disappearing without trace.

Jaro-Winkler is the only similarity metric in this module.  The Levenshtein
scorer and the token-prefix boost it used to be combined with are gone: the
boost carried a base constant picked to clear the engine's old threshold, and
``max()`` let either signal carry a pairing over the line on its own.  Both
were dead once assignment went global, and keeping them meant two scoring
paths in one file with no way to tell which one ran.

Pure Python, stdlib only — no external fuzzy-matching libraries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stateguard.core.errors.operations import (
    FieldOperation,
    FieldOpType,
    RepairEvidence,
    RepairRisk,
)
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec

__all__ = ["FuzzyFieldMatchStrategy", "jaro_winkler", "score_assignments"]


# ---------------------------------------------------------------------------
# Jaro-Winkler
# ---------------------------------------------------------------------------


def _jaro(s1: str, s2: str) -> float:
    """Jaro similarity of two strings, in ``[0.0, 1.0]``."""
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    window = max(0, max(len1, len2) // 2 - 1)
    matched1 = [False] * len1
    matched2 = [False] * len2

    matches = 0
    for i in range(len1):
        start, end = max(0, i - window), min(i + window + 1, len2)
        for j in range(start, end):
            if matched2[j] or s1[i] != s2[j]:
                continue
            matched1[i] = matched2[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i in range(len1):
        if not matched1[i]:
            continue
        while not matched2[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    half = transpositions // 2
    return (matches / len1 + matches / len2 + (matches - half) / matches) / 3.0


def jaro_winkler(s1: str, s2: str, prefix_weight: float = 0.1, max_prefix: int = 4) -> float:
    """
    Case-insensitive Jaro-Winkler similarity, in ``[0.0, 1.0]``.

    The sole ``name_match`` signal.  Winkler's prefix adjustment rewards a
    shared opening substring *by construction*, which is the same intuition
    the deleted token-prefix boost encoded — except it emerges from the metric
    rather than from a constant chosen to clear the engine's threshold.

    Worth being clear about what this does and does not fix.  Measured against
    the real corpus it scores ``user_email``/``user_name`` at 0.913 and
    ``user_email``/``user_id`` at 0.891 — both *higher* than
    ``temp_celsius``/``temperature`` at 0.809, which is the repair that must
    succeed.  Name similarity alone therefore cannot separate a safe rename
    from a coin-flip; that job belongs to the assignment margin computed in
    ``score_assignments``.
    """
    a, b = s1.lower(), s2.lower()
    similarity = _jaro(a, b)
    prefix = 0
    # strict=False is deliberate: the prefix ends at the shorter string.
    for char_a, char_b in zip(a, b, strict=False):
        if char_a != char_b:
            break
        prefix += 1
        if prefix == max_prefix:
            break
    return similarity + prefix * prefix_weight * (1.0 - similarity)


# ---------------------------------------------------------------------------
# Global assignment
# ---------------------------------------------------------------------------


#: Similarity below which a pair is not proposed at all.
#:
#: This is noise suppression, not a decision: everything above it goes to
#: ``TrustPolicy``, which owns the apply/abstain/reject call. Two names this
#: dissimilar are not a borderline judgement worth surfacing -- they are
#: unrelated, and listing them would bury real near-misses in the audit trail.
_MIN_PLAUSIBLE_NAME_MATCH = 0.5


@dataclass(frozen=True)
class _Assignment:
    """One proposed (missing field <- unexpected key) pairing and its margin."""

    target: str
    candidate: str
    score: float
    margin: float
    alternatives: int
    runner_up: str | None
    #: Which side the runner-up sits on -- "target" when another field
    #: competed for this key, "candidate" when another key competed for this
    #: field. Without it the two are indistinguishable in an explanation.
    runner_up_kind: str | None


def score_assignments(missing: list[str], unexpected: list[str]) -> list[_Assignment]:
    """
    Pair missing fields with unexpected keys by global score, not by iteration
    order.

    The previous implementation walked missing fields alphabetically and let
    each one claim its best remaining candidate.  That makes the *alphabet*
    the tie-breaker for contested renames: with ``user_id`` and ``user_name``
    both missing and a single ``user_email`` present, ``user_id`` won simply
    because "i" sorts before "n".

    Here every (target, candidate) pair is scored, the best pairing overall is
    taken first, and both of its endpoints are consumed — so a candidate is
    assigned where it fits best across the whole problem.

    This is greedy, not maximum-weight bipartite matching, and the two can
    differ: taking the single best pair first can strand a field whose only
    remaining candidate scores below the plausibility floor, where a globally
    optimal assignment would have repaired both. The failure mode is a missed
    repair rather than a wrong one, since the most confident pairing is always
    the one taken. Upgrading to Hungarian matching is a contained change if a
    real case appears.

    Each assignment carries a **bipartite margin**: the smaller of

    * how much better this candidate is than the best *other* candidate for
      the same target, and
    * how much better this target is than the best *other* target for the same
      candidate.

    Taking the minimum means a pairing only counts as decisive if it wins from
    both directions.  That is what distinguishes ``temp_celsius`` (margin
    0.337, one obvious home) from ``user_email`` (margin 0.021, equally at
    home in two places) even though the latter scores higher on raw
    similarity.

    Competition is counted over the **whole** problem
    ------------------------------------------------
    Both margins range over every field in *missing* and every key in
    *unexpected*, including endpoints that earlier iterations already
    consumed.  Restricting them to what was still unassigned made the margin
    an artefact of assignment order: the final pairing in a run had no
    remaining rivals by construction, so it scored ``margin = 1.0`` and
    collected full trust no matter how contested it had actually been.

    Concretely, with ``{user_id, user_name}`` missing and
    ``{user_email, user_names}`` unexpected, ``user_names -> user_name`` is
    taken first and consumes ``user_name``; ``user_email -> user_id`` was then
    left facing nothing and applied at 0.891, writing an email address into
    ``user_id`` — the precise repair the trust model exists to refuse.
    Counting the consumed ``user_name`` keeps that pairing's candidate-side
    margin at 0.0 and lands it in the abstain band, where it belongs.

    A pairing that is *not* the best use of its own endpoints therefore gets a
    margin of 0.0 rather than a negative number: it is maximally contested,
    and 0.0 is the floor the tie factor is defined against.
    """
    if not missing or not unexpected:
        return []

    grid = {(t, c): jaro_winkler(t, c) for t in missing for c in unexpected}

    results: list[_Assignment] = []
    open_targets = set(missing)
    open_candidates = set(unexpected)

    while open_targets and open_candidates:
        # Deterministic ordering: score first, then the names themselves, so
        # an exact tie resolves the same way on every run rather than by dict
        # iteration order.
        target, candidate = max(
            ((t, c) for t in open_targets for c in open_candidates),
            key=lambda pair: (grid[pair], pair[0], pair[1]),
        )
        chosen = grid[(target, candidate)]

        # Rivals are drawn from the full input lists, not the open sets, and
        # are iterated in the caller's (sorted) order so an exact tie always
        # names the same runner-up.
        rival_candidate = max(
            ((c, grid[(target, c)]) for c in unexpected if c != candidate),
            key=lambda pair: pair[1],
            default=None,
        )
        rival_target = max(
            ((t, grid[(t, candidate)]) for t in missing if t != target),
            key=lambda pair: pair[1],
            default=None,
        )

        # ``None`` means there was no competitor at all on that side, which
        # earns full credit rather than zero.
        target_margin = 1.0 if rival_candidate is None else max(0.0, chosen - rival_candidate[1])
        candidate_margin = 1.0 if rival_target is None else max(0.0, chosen - rival_target[1])

        runner_up: str | None = None
        runner_up_kind: str | None = None
        if candidate_margin <= target_margin and rival_target is not None:
            runner_up, runner_up_kind = rival_target[0], "target"
        elif rival_candidate is not None:
            runner_up, runner_up_kind = rival_candidate[0], "candidate"

        results.append(
            _Assignment(
                target=target,
                candidate=candidate,
                score=chosen,
                margin=min(target_margin, candidate_margin),
                # Both sides count. Reporting only the candidate side made the
                # most contested case in the corpus -- two fields competing for
                # one key -- report "1 alternative".
                alternatives=len(missing) + len(unexpected) - 1,
                runner_up=runner_up,
                runner_up_kind=runner_up_kind,
            )
        )
        open_targets.discard(target)
        open_candidates.discard(candidate)

    return results


# ---------------------------------------------------------------------------
# FuzzyFieldMatchStrategy
# ---------------------------------------------------------------------------


class FuzzyFieldMatchStrategy(IRepairStrategy):
    """
    Proposes ``RENAME`` operations for correlated MISSING/UNEXPECTED field
    pairs based on approximate name similarity.

    Deprecated parameters
    ---------------------
    ``min_confidence_threshold`` and ``score_collision_margin`` are accepted
    and **ignored**.  This strategy no longer decides anything: it reports
    ``name_match`` and ``margin`` as evidence and ``TrustPolicy`` owns the
    apply/abstain/reject call.  They remain in the signature so existing
    callers keep constructing, and will be removed with the ``confidence``
    alias.

    The equivalent controls now live on ``TrustPolicy``:

    ============================ ==================================
    was                          now
    ============================ ==================================
    ``min_confidence_threshold`` ``TrustPolicy(minimum_trust=...)``
    ``score_collision_margin``   ``TrustPolicy(margin_full_credit=...)``
    ============================ ==================================

    ``ContractGuard`` wires both from ``RepairConfig`` automatically, so the
    library defaults are unchanged; pass ``ContractGuard(policy=...)`` to
    override the per-risk bands directly.
    """

    def __init__(
        self,
        min_confidence_threshold: float | None = None,
        score_collision_margin: float | None = None,
    ) -> None:
        # Both parameters are accepted and ignored. Thresholding moved to
        # TrustPolicy when strategies stopped scoring their own proposals:
        # this strategy now reports similarity and margin as evidence and the
        # policy decides what clears the bar. Retained so existing callers
        # keep constructing; they have no effect.
        self._min_confidence_threshold = min_confidence_threshold
        self._score_collision_margin = score_collision_margin

    @property
    def name(self) -> str:
        return "FuzzyFieldMatchStrategy"

    @property
    def priority(self) -> int:
        return 20

    # ------------------------------------------------------------------
    # IRepairStrategy
    # ------------------------------------------------------------------

    def can_handle(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> bool:
        missing = self._find_missing_fields(violations)
        unexpected = self._find_unexpected_keys(violations)
        return bool(missing) and bool(unexpected)

    def propose(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[FieldOperation]:
        missing_fields = sorted(self._find_missing_fields(violations))
        unexpected_keys = sorted(self._find_unexpected_keys(violations))

        # Never propose renaming a field to itself.
        candidates = [k for k in unexpected_keys if k not in set(missing_fields)]
        if not missing_fields or not candidates:
            return []

        operations: list[FieldOperation] = []
        for assignment in score_assignments(missing_fields, candidates):
            if assignment.score < _MIN_PLAUSIBLE_NAME_MATCH:
                # Not a close call for the policy to weigh -- these are simply
                # unrelated names. Proposing them would bury the genuine
                # near-misses in the rejected-operations audit trail.
                continue

            notes = [f"jaro-winkler similarity {assignment.score:.3f}"]
            if assignment.runner_up is not None:
                # Name the direction: an unqualified "closest alternative"
                # reads identically whether the competitor was another field
                # to write into or another key to read from.
                where = (
                    "another field also wanted this key"
                    if assignment.runner_up_kind == "target"
                    else "another key also fitted this field"
                )
                notes.append(f"closest alternative '{assignment.runner_up}' ({where})")

            operations.append(
                FieldOperation(
                    op_type=FieldOpType.RENAME,
                    target_path=assignment.target,
                    rationale=(
                        f"Fuzzy name match: '{assignment.candidate}' -> '{assignment.target}'."
                    ),
                    source_path=assignment.candidate,
                    # A name correspondence we worked out, not one the
                    # contract declared.
                    risk=RepairRisk.INFERRED,
                    evidence=RepairEvidence(
                        name_match=assignment.score,
                        margin=assignment.margin,
                        alternatives_considered=assignment.alternatives,
                        notes=tuple(notes),
                    ),
                )
            )

        return operations

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_missing_fields(violations: list[ContractViolation]) -> list[str]:
        """Return ``field_path`` for every MISSING_REQUIRED_FIELD violation."""
        return [
            v.field_path
            for v in violations
            if v.violation_type is ViolationType.MISSING_REQUIRED_FIELD
        ]

    @staticmethod
    def _find_unexpected_keys(violations: list[ContractViolation]) -> list[str]:
        """Return ``field_path`` for every UNEXPECTED_FIELD violation."""
        return [
            v.field_path for v in violations if v.violation_type is ViolationType.UNEXPECTED_FIELD
        ]
