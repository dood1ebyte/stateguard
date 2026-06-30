"""
FuzzyFieldMatchStrategy — repairs correlated MISSING/UNEXPECTED field pairs
via approximate name matching.

Priority 20.  This is the strategy that handles the canonical "schema
drift" scenario: a tool returns ``temp_celsius`` when the contract expects
``temperature``.  The validator reports this as one
``MISSING_REQUIRED_FIELD`` (``temperature``) and one ``UNEXPECTED_FIELD``
(``temp_celsius``); this strategy proposes the ``RENAME``.

Safety model
------------
Two safeguards prevent silent wrong-field repairs:

1. **Confidence threshold** — a candidate must score at least
   ``min_confidence_threshold`` to be proposed at all.
2. **Collision detection** — if the best candidate and the second-best
   candidate for the same missing field score within
   ``score_collision_margin`` of each other, *no* rename is proposed for
   that field.  An ambiguous match is worse than no match.

Matching algorithm
-------------------
Two complementary signals are combined to score a candidate pair:

1. **Normalized Levenshtein distance** on lowercased strings::

       score(a, b) = 1 - levenshtein(a.lower(), b.lower()) / max(len(a), len(b))

   Identical strings (case-insensitive) score ``1.0``; completely disjoint
   strings of equal length score ``0.0``. This alone handles typos,
   reordering, and casing differences well (e.g. ``"userId"`` vs
   ``"user_id"``).

2. **Token-prefix boost**, which handles the common "abbreviation + unit
   suffix" pattern that pure edit distance scores poorly -- e.g. a tool
   returning ``temp_celsius`` for a contract field named ``temperature``.
   If an underscore-delimited token of either name (of at least 3
   characters) is an exact prefix of the *other* name, the pair receives a
   floor confidence of ``0.7`` plus a small bonus proportional to how much
   of the longer name that token covers. This is intentionally a *floor*,
   not an override: ``max(levenshtein_score, prefix_boost)`` is used, so
   the boost only ever raises a score, never lowers one already earned by
   strong overall similarity.

The two signals are combined via ``max()`` in ``_combined_score``, which is
what ``_score_candidates`` actually uses for matching. The raw
``_normalized_score`` function is left untouched and still reflects pure
Levenshtein similarity on its own -- it remains useful (and is unit-tested)
as a building block, but is no longer the sole signal driving repair
proposals.

Pure Python, stdlib only — no external fuzzy-matching libraries.
"""

from __future__ import annotations

from typing import Any

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec

__all__ = ["FuzzyFieldMatchStrategy"]


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
    Return the matching confidence used by ``_score_candidates``:
    ``max(_normalized_score(s1, s2), _token_prefix_boost(s1, s2))``.

    The token-prefix boost is a floor, not an override -- it can only
    raise a pair's score above what pure Levenshtein similarity would
    give, never lower it. See the module docstring's "Matching algorithm"
    section for the rationale behind combining both signals.
    """
    return max(_normalized_score(s1, s2), _token_prefix_boost(s1, s2))


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
        min_confidence_threshold: float = 0.7,
        score_collision_margin: float = 0.15,
    ) -> None:
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
        unexpected_keys = self._find_unexpected_keys(violations)

        operations: list[FieldOperation] = []
        consumed: set[str] = set()

        for missing in missing_fields:
            available = [k for k in unexpected_keys if k not in consumed]
            # Never propose renaming a field to itself.
            available = [k for k in available if k != missing]
            if not available:
                continue

            scores = self._score_candidates(missing, available)
            if not scores:
                continue

            best_key, best_score = scores[0]

            if best_score < self._min_confidence_threshold:
                continue

            if self._check_collision(scores, self._score_collision_margin):
                # Ambiguous match — do not guess.
                continue

            operations.append(
                FieldOperation(
                    op_type=FieldOpType.RENAME,
                    target_path=missing,
                    confidence=best_score,
                    rationale=(
                        f"Fuzzy name match: '{best_key}' -> '{missing}' (score: {best_score:.2f})."
                    ),
                    source_path=best_key,
                )
            )
            consumed.add(best_key)

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
