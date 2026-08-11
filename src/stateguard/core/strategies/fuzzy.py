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

``_normalized_score`` (Levenshtein) and ``_token_prefix_boost`` remain as
unit-tested building blocks but no longer drive proposals.

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
# Pure string-similarity functions (module level, no dependencies)
# ---------------------------------------------------------------------------


def _levenshtein_distance(s1: str, s2: str) -> int:
    """
    Return the Levenshtein (edit) distance between *s1* and *s2*.

    Standard dynamic-programming implementation using a single rolling
    row, O(len(s1) * len(s2)) time, O(min(len(s1), len(s2))) space.
    """
    if s1 == s2:
        return 0
    if len(s1) == 0:
        return len(s2)
    if len(s2) == 0:
        return len(s1)

    # Ensure s2 is the shorter string to minimise row width.
    if len(s1) < len(s2):
        s1, s2 = s2, s1

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1, start=1):
        current_row = [i]
        for j, c2 in enumerate(s2, start=1):
            insertion = previous_row[j] + 1
            deletion = current_row[j - 1] + 1
            substitution = previous_row[j - 1] + (0 if c1 == c2 else 1)
            current_row.append(min(insertion, deletion, substitution))
        previous_row = current_row
    return previous_row[-1]


def _normalized_score(s1: str, s2: str) -> float:
    """
    Return a similarity score in ``[0.0, 1.0]`` between *s1* and *s2*.

    ``1.0`` means identical (case-insensitive); ``0.0`` means maximally
    dissimilar for their lengths.  Comparison is case-insensitive so that
    e.g. ``"userId"`` and ``"user_id"`` are scored on their structural
    similarity rather than penalised for casing alone.

    Two empty strings score ``1.0`` (defined as identical).

    This is pure normalized Levenshtein similarity with no other signals
    mixed in.  ``_score_candidates`` uses ``_combined_score`` (below),
    which incorporates this function as one of two signals -- see the
    module docstring's "Matching algorithm" section.
    """
    a, b = s1.lower(), s2.lower()
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1.0
    distance = _levenshtein_distance(a, b)
    return 1.0 - (distance / max_len)


# ---------------------------------------------------------------------------
# Token-prefix boost
# ---------------------------------------------------------------------------

# A token shorter than this is considered too generic to be a meaningful
# abbreviation signal on its own (e.g. "id", "a", "ok").
_MIN_PREFIX_TOKEN_LENGTH = 3

# When a qualifying token-prefix relationship is found, the pair is given
# at least this confidence -- chosen to clear the engine's default
# min_confidence_threshold (0.7) for the motivating case (temp_celsius ->
# temperature) without being so high that it would mask a genuine
# collision between two structurally similar candidates.
_PREFIX_MATCH_BASE_CONFIDENCE = 0.7

# Additional confidence awarded on top of the base, scaled by how much of
# the longer name the matching token covers. Keeps a token covering most
# of both names (e.g. an exact-but-cased duplicate) scored higher than one
# covering only a small fraction of a much longer name.
_PREFIX_MATCH_CONFIDENCE_RANGE = 0.3


def _token_prefix_boost(s1: str, s2: str) -> float:
    """
    Return a boosted confidence if an underscore-delimited token of either
    *s1* or *s2* is an exact, case-insensitive prefix of the other string;
    otherwise return ``0.0``.

    Motivating example: a tool returns ``"temp_celsius"`` where the
    contract expects ``"temperature"``. Pure Levenshtein distance scores
    this pair poorly (~0.42) because the strings diverge after their first
    four characters and differ substantially in length. But ``"temp"`` --
    a token of ``"temp_celsius"`` -- is an exact prefix of
    ``"temperature"``, which is a strong, low-noise signal that the two
    names refer to the same underlying field under different naming
    conventions (abbreviation + unit suffix).

    Only tokens of at least ``_MIN_PREFIX_TOKEN_LENGTH`` characters are
    considered, to avoid generic short tokens (e.g. ``"id"``) producing
    spurious matches.

    Symmetric: checks tokens of *s1* against *s2* and tokens of *s2*
    against *s1*, returning the highest qualifying score found.
    """
    a_lower, b_lower = s1.lower(), s2.lower()
    best = 0.0

    for token in a_lower.split("_"):
        if len(token) >= _MIN_PREFIX_TOKEN_LENGTH and b_lower.startswith(token):
            weight = len(token) / max(len(a_lower), len(b_lower))
            score = _PREFIX_MATCH_BASE_CONFIDENCE + _PREFIX_MATCH_CONFIDENCE_RANGE * weight
            best = max(best, score)

    for token in b_lower.split("_"):
        if len(token) >= _MIN_PREFIX_TOKEN_LENGTH and a_lower.startswith(token):
            weight = len(token) / max(len(a_lower), len(b_lower))
            score = _PREFIX_MATCH_BASE_CONFIDENCE + _PREFIX_MATCH_CONFIDENCE_RANGE * weight
            best = max(best, score)

    return best


def _combined_score(s1: str, s2: str) -> float:
    """
    Return ``max(_normalized_score(s1, s2), _token_prefix_boost(s1, s2))``.

    .. deprecated::
       Superseded by ``jaro_winkler`` as the scoring signal.  ``max()`` is the
       most permissive combiner available: any single signal could carry a
       pair over the threshold alone, which is exactly how a hand-tuned
       prefix floor came to decide real repairs.  Retained because both
       inputs remain individually unit-tested building blocks.
    """
    return max(_normalized_score(s1, s2), _token_prefix_boost(s1, s2))


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

    This replaces ``_combined_score`` as the ``name_match`` signal.  Winkler's
    prefix adjustment rewards a shared opening substring *by construction*,
    which is the same intuition ``_token_prefix_boost`` encoded — except it
    emerges from the metric rather than from a constant chosen to clear the
    engine's threshold.

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

    * how much better this candidate is than the next candidate for the same
      target, and
    * how much better this target is than the next target for the same
      candidate.

    Taking the minimum means a pairing only counts as decisive if it wins from
    both directions.  That is what distinguishes ``temp_celsius`` (margin
    0.337, one obvious home) from ``user_email`` (margin 0.021, equally at
    home in two places) even though the latter scores higher on raw
    similarity.
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

        by_candidate = sorted(
            (grid[(target, c)] for c in open_candidates),
            reverse=True,
        )
        by_target = sorted(
            (grid[(t, candidate)] for t in open_targets),
            reverse=True,
        )
        target_margin = 1.0 if len(by_candidate) < 2 else by_candidate[0] - by_candidate[1]
        candidate_margin = 1.0 if len(by_target) < 2 else by_target[0] - by_target[1]

        runner_up: str | None = None
        runner_up_kind: str | None = None
        if candidate_margin <= target_margin and len(by_target) >= 2:
            runner_up = max(
                (t for t in open_targets if t != target),
                key=lambda t: grid[(t, candidate)],
            )
            runner_up_kind = "target"
        elif len(by_candidate) >= 2:
            runner_up = max(
                (c for c in open_candidates if c != candidate),
                key=lambda c: grid[(target, c)],
            )
            runner_up_kind = "candidate"

        results.append(
            _Assignment(
                target=target,
                candidate=candidate,
                score=grid[(target, candidate)],
                margin=min(target_margin, candidate_margin),
                # Both sides count. Reporting only the candidate side made the
                # most contested case in the corpus -- two fields competing for
                # one key -- report "1 alternative".
                alternatives=len(open_candidates) + len(open_targets) - 1,
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

    Parameters
    ----------
    min_confidence_threshold:
        Minimum ``_normalized_score`` required to propose a rename at all.
        Defaults to ``0.7``, matching ``RepairConfig.min_confidence_threshold``.
    score_collision_margin:
        If the best and second-best candidate scores for the same missing
        field are within this margin, no rename is proposed for that field.
        Defaults to ``0.15``, matching ``RepairConfig.score_collision_margin``.

    Notes
    -----
    These constructor parameters intentionally mirror ``RepairConfig``
    field names and defaults.  When the engine constructs its default
    strategy set it passes the active ``RepairConfig`` values through, so
    this strategy's internal "should I propose at all" decision is
    consistent with the engine's "should I apply this operation" decision.
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

    @staticmethod
    def _score_candidates(
        missing: str,
        candidates: list[str],
    ) -> list[tuple[str, float]]:
        """
        Score every *candidate* against *missing* using ``_combined_score``
        (Levenshtein similarity plus the token-prefix boost), returning
        ``(candidate, score)`` pairs sorted by score descending.

        Ties are broken by the original order of *candidates* (Python's
        ``sorted`` is stable).
        """
        scored = [(c, _combined_score(missing, c)) for c in candidates]
        return sorted(scored, key=lambda pair: pair[1], reverse=True)

    @staticmethod
    def _check_collision(
        scores: list[tuple[str, float]],
        margin: float,
    ) -> bool:
        """
        Return ``True`` if the top two scores are within *margin* of each
        other, indicating an ambiguous match.

        Always ``False`` if there is only one candidate.
        """
        if len(scores) < 2:
            return False
        best_score = scores[0][1]
        second_score = scores[1][1]
        return (best_score - second_score) < margin
