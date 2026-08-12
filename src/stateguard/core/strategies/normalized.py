"""
NormalizedNameStrategy — repairs renames that differ only in naming convention.

Priority 15, between ``ExactAliasStrategy`` (10) and
``FuzzyFieldMatchStrategy`` (20).  It fires when a missing field and an
unexpected key are the *same name written differently*: ``UserID`` for
``user_id``, ``firstName`` for ``first_name``, ``ZIP-CODE`` for ``zip_code``.

Why this is not just fuzzy matching with a high threshold
---------------------------------------------------------
``FuzzyFieldMatchStrategy`` already lowercases before scoring, so casing alone
was never the problem.  Separators are: Jaro-Winkler puts ``UserID`` against
``user_id`` at roughly 0.86, which is a *guess* — indistinguishable in the
trust model from a genuine near-miss like ``user_email`` at 0.891, and priced
the same way.

But these are not guesses.  ``UserID`` and ``user_id`` are the same identifier
under two naming conventions, and recognising that is a decision about
*orthography*, not about meaning.  Matching after normalisation makes it an
exact match, which is what it always was.

The practical effect is that fuzzy matching — the riskiest thing the engine
does — stops being asked about cases that were never ambiguous, so its
proposals are drawn from a smaller and genuinely uncertain pool.

Normalisation
-------------
A closed, ordered list, applied to both sides:

1. ``casefold`` (Unicode-aware lowering — handles ``ß``/``ss``, ``İ``)
2. ``strip``
3. remove ``_``, ``-`` and spaces

Nothing else.  No stemming, no synonyms, no abbreviation expansion — every one
of those infers meaning rather than normalising form, and belongs to
``FuzzyFieldMatchStrategy`` or to a future semantic strategy.

Ambiguity
---------
Normalisation is many-to-one, so it can collide: a contract declaring both
``user_id`` and ``userId`` has two fields that normalise identically, and so
does a payload carrying both ``USER_ID`` and ``user-id``.  Either way the
correspondence is genuinely unknowable from the names, so no operation is
proposed at all — the fuzzy strategy still gets its turn and will surface the
pairing as ambiguous if it is worth surfacing.

Zero external dependencies.
"""

from __future__ import annotations

from collections import defaultdict
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

__all__ = ["NormalizedNameStrategy", "normalize_field_name"]


#: Characters removed entirely — the separators that distinguish snake_case,
#: kebab-case, and spaced names from one another.
_SEPARATORS = ("_", "-", " ")


def normalize_field_name(name: str) -> str:
    """
    Reduce *name* to its separator- and case-independent form.

    ``casefold`` rather than ``lower``: it is the Unicode-correct operation
    for caseless comparison, so ``STRASSE`` and ``straße`` agree, as do the
    dotted and dotless Turkish ``I``.

    Only the final path segment is normalised for dotted paths — the segments
    above it locate the field and must keep matching literally, or a rename
    could jump between branches.
    """
    parent, _, local = name.rpartition(".")
    normalized = local.casefold().strip()
    for separator in _SEPARATORS:
        normalized = normalized.replace(separator, "")
    return f"{parent}.{normalized}" if parent else normalized


class NormalizedNameStrategy(IRepairStrategy):
    """
    Renames an unexpected key to a missing field when the two are the same
    name under different naming conventions.

    Reports ``name_match = 1.0`` at ``RepairRisk.INFERRED``.  The score is a
    genuine 1.0 — after normalisation the names are *equal*, not similar — but
    the risk stays INFERRED rather than DECLARED because nothing in the
    contract said these two names refer to the same field; the engine worked
    it out.
    """

    @property
    def name(self) -> str:
        return "NormalizedNameStrategy"

    @property
    def priority(self) -> int:
        return 15

    # ------------------------------------------------------------------
    # IRepairStrategy
    # ------------------------------------------------------------------

    def can_handle(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> bool:
        return bool(self._pairs(violations))

    def propose(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[FieldOperation]:
        operations: list[FieldOperation] = []

        for missing, candidate in sorted(self._pairs(violations).items()):
            operations.append(
                FieldOperation(
                    op_type=FieldOpType.RENAME,
                    target_path=missing,
                    rationale=(
                        f"Same name under a different convention: '{candidate}' -> '{missing}'."
                    ),
                    source_path=candidate,
                    # An orthographic match, not a declared one: the contract
                    # never said these names are the same field.
                    risk=RepairRisk.INFERRED,
                    evidence=RepairEvidence(
                        # Equal after normalisation, so this is exact rather
                        # than approximate -- and unique, or it would not have
                        # survived the collision guard in _pairs().
                        name_match=1.0,
                        margin=1.0,
                        alternatives_considered=1,
                        notes=(
                            f"'{candidate}' and '{missing}' both normalise to "
                            f"'{normalize_field_name(missing)}'",
                        ),
                    ),
                )
            )

        return operations

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _pairs(violations: list[ContractViolation]) -> dict[str, str]:
        """
        Map each missing field to the unexpected key that normalises to it.

        Only unambiguous correspondences are returned.  A normalised form
        claimed by more than one missing field, or matched by more than one
        unexpected key, is dropped entirely: normalisation is many-to-one, so
        a collision means the names genuinely cannot tell us which goes where.
        Leaving it out lets ``FuzzyFieldMatchStrategy`` weigh it instead, with
        the margin machinery that exists for exactly that situation.
        """
        missing_by_form: dict[str, list[str]] = defaultdict(list)
        for violation in violations:
            if violation.violation_type is ViolationType.MISSING_REQUIRED_FIELD:
                missing_by_form[normalize_field_name(violation.field_path)].append(
                    violation.field_path
                )

        if not missing_by_form:
            return {}

        candidates_by_form: dict[str, list[str]] = defaultdict(list)
        for violation in violations:
            if violation.violation_type is ViolationType.UNEXPECTED_FIELD:
                candidates_by_form[normalize_field_name(violation.field_path)].append(
                    violation.field_path
                )

        pairs: dict[str, str] = {}
        for form, missing_fields in missing_by_form.items():
            candidates = candidates_by_form.get(form, [])
            if len(missing_fields) != 1 or len(candidates) != 1:
                continue  # ambiguous, or nothing to pair with
            if missing_fields[0] == candidates[0]:
                continue  # identical names -- nothing to rename
            pairs[missing_fields[0]] = candidates[0]

        return pairs
