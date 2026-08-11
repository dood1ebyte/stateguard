"""
Field operation types used by repair strategies to describe proposed fixes.

A FieldOperation is the atomic unit of repair.  Strategies propose lists of
FieldOperation objects; the RepairEngine scores each one through
``TrustPolicy`` and applies, defers, or rejects it accordingly.

Evidence vs. trust
------------------
Strategies report **evidence** — what they actually measured — and declare the
**risk** of the operation being wrong.  They do *not* invent a score.  The
single number (``trust``) is computed from that evidence by
``stateguard.core.trust.TrustPolicy``.

This split exists because the previous model had every strategy inventing its
own confidence on its own scale: ``FuzzyFieldMatchStrategy``'s 0.8 meant "these
names look alike", ``TypeCoercionStrategy``'s 0.85 meant "this cast is
defined", and both were compared against one global threshold as though they
were the same quantity.  Worse, the fuzzy strategy's prefix-boost floor was
explicitly chosen to clear that threshold — tuning the evidence to the bar
rather than the bar to the evidence.

Keeping evidence separate also gives Stage 2 (semantic/LLM-backed repair) a
place to contribute: it becomes another evidence source, not another invented
constant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any

__all__ = [
    "FieldOpType",
    "FieldOperation",
    "RepairEvidence",
    "RepairRisk",
]


# ---------------------------------------------------------------------------
# FieldOpType
# ---------------------------------------------------------------------------


class FieldOpType(StrEnum):
    """
    The type of atomic change a repair strategy can propose.

    Members
    -------
    RENAME:
        Move a value from ``source_path`` to ``target_path`` and remove the
        source key.  Requires ``source_path`` to be set.
    COERCE:
        Cast the value already present at ``target_path`` to a different type
        (e.g. ``"30"`` → ``30`` for an INTEGER field).  ``source_path`` and
        ``value`` are unused.
    SET_DEFAULT:
        Insert a missing required field at ``target_path`` using its declared
        ``FieldSpec.default`` value.  The value is carried in
        ``FieldOperation.value``.
    REMOVE:
        Delete the key at ``target_path`` from the data.  Used in strict mode
        to remove unexpected fields.  ``source_path`` and ``value`` are unused.
    SET_VALUE:
        Force a specific value at ``target_path``.  Last-resort operation.
        The forced value is carried in ``FieldOperation.value``.
    """

    RENAME = "rename"
    COERCE = "coerce"
    SET_DEFAULT = "set_default"
    REMOVE = "remove"
    SET_VALUE = "set_value"


# ---------------------------------------------------------------------------
# RepairRisk
# ---------------------------------------------------------------------------


class RepairRisk(IntEnum):
    """
    How bad it is if this operation is **wrong** — consequence, not likelihood.

    Likelihood is what ``RepairEvidence`` measures.  Risk is orthogonal: a
    rename we are 90% sure about and a deletion we are 90% sure about deserve
    different bars, because the cost of the 10% differs enormously.  The
    previous model had no notion of this at all — a lossless ``"5"`` → ``5``
    cast and a lossy ``{...}`` → ``'{"...": ...}'`` serialisation were scored
    on one scale against one threshold.

    Ordered least to most consequential; ``TrustPolicy`` requires a higher
    trust score as the tier rises.

    Members
    -------
    REVERSIBLE:
        The value survives exactly and the operation could be undone —
        ``"5"`` → ``5``, where ``str(5) == "5"`` recovers the input.
    DECLARED:
        The schema itself specified this — a declared alias, a declared
        default.  The contract is the authority, so there is nothing to be
        unsure about.
    INFERRED:
        A correspondence we worked out rather than were told — a fuzzy
        rename, an enum normalisation.  Plausible, but a guess.
    LOSSY:
        Information is invented or destroyed and the operation cannot be
        cleanly undone — serialising a container into a string, wrapping a
        bare value in an array.
    DESTRUCTIVE:
        Data is removed outright.  Never applied automatically.
    """

    REVERSIBLE = 0
    DECLARED = 1
    INFERRED = 2
    LOSSY = 3
    DESTRUCTIVE = 4


# ---------------------------------------------------------------------------
# RepairEvidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairEvidence:
    """
    What a strategy actually measured in support of an operation.

    Every score is in ``[0.0, 1.0]``; ``None`` means "this signal does not
    apply to this kind of repair" and is ignored rather than treated as zero.

    ``TrustPolicy`` combines the applicable signals with ``min()`` — trust is
    capped by the *weakest* piece of supporting evidence, because these are
    necessary conditions rather than votes.  This is deliberately the opposite
    of the ``max()`` the fuzzy matcher used to use, where a single strong
    signal could carry a proposal over the line on its own.

    Attributes
    ----------
    name_match:
        Strength of the correspondence between two field *names* — the
        Jaro–Winkler similarity used by ``FuzzyFieldMatchStrategy``.
    value_preserved:
        Round-trip fidelity of a value transformation: 1.0 when converting
        back reproduces the input exactly, lower when the conversion
        normalises something away (``"05"`` → ``5`` → ``"5"``).
    schema_authority:
        1.0 when the contract explicitly declared this repair — a declared
        alias or a declared default.  Not a guess at all.
    margin:
        How much better this choice was than the runner-up, in the same units
        as ``name_match``.  ``None`` means there was no competitor.

        For a rename this is the *bipartite* margin: the smaller of "how much
        better is this candidate than the next candidate for this field" and
        "how much better is this field than the next field for this
        candidate".  An assignment is only unambiguous if it wins from both
        directions.

        This is the signal that actually separates a safe rename from a
        dangerous one.  Measured against the real corpus, ``user_email``
        scores *higher* against ``user_id`` (0.891) and ``user_name`` (0.913)
        than ``temp_celsius`` does against ``temperature`` (0.809) — so name
        similarity alone cannot tell the safe repair from the coin-flip.  The
        margin can: 0.021 versus 0.337.
    alternatives_considered:
        How many candidates were evaluated.  Context for explanations.
    signals:
        Strategy-specific extras as ``(name, score)`` pairs.  A tuple rather
        than a dict so ``FieldOperation`` stays hashable.  This is the
        extension point for Stage 2 semantic repair.
    notes:
        Human-readable fragments describing what was measured, rendered by
        ``FieldOperation.explain()``.  Must never contain field *values* —
        see ``RepairConfig.include_values_in_log``.
    """

    name_match: float | None = None
    value_preserved: float | None = None
    schema_authority: float | None = None
    margin: float | None = None
    alternatives_considered: int = 0
    signals: tuple[tuple[str, float], ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label in ("name_match", "value_preserved", "schema_authority", "margin"):
            score = getattr(self, label)
            if score is not None and not (0.0 <= score <= 1.0):
                raise ValueError(f"RepairEvidence.{label} must be in [0.0, 1.0], got {score!r}")

    @property
    def applicable_scores(self) -> tuple[float, ...]:
        """The supporting signals that apply, excluding ``margin``.

        ``margin`` is excluded because it is not itself evidence *for* the
        repair — it modulates how much the other evidence can be trusted.
        ``TrustPolicy`` applies it separately.
        """
        return tuple(
            score
            for score in (self.schema_authority, self.name_match, self.value_preserved)
            if score is not None
        )


# ---------------------------------------------------------------------------
# FieldOperation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldOperation:
    """
    An atomic repair operation proposed by a strategy.

    Immutability
    ------------
    ``FieldOperation`` is a **frozen** dataclass.  Once proposed, an operation
    is never modified.  The engine scores, applies, or rejects operations but
    does not alter them, ensuring the ``RepairAttempt`` audit trail is a
    faithful record of what was proposed.  Scoring produces a *new* instance
    via ``with_trust``.

    Hashability
    -----------
    Because this is a frozen dataclass, instances are hashable and can be
    placed in sets or used as dict keys.  This requires ``value`` to be
    hashable.  Passing a mutable container as ``value`` will raise
    ``TypeError`` at hash time.

    Attributes
    ----------
    op_type:
        The type of operation to perform.
    target_path:
        Dot-notation path to the field being written, coerced, or removed.
    rationale:
        Human-readable summary, included in repair log entries and the
        ``RepairAttempt`` audit trail.
    trust:
        How much the engine trusts this operation, in ``[0.0, 1.0]``.
        **Computed by ``TrustPolicy`` from ``evidence`` and ``risk``, not set
        by strategies.**  A freshly proposed operation has ``trust = 0.0``
        until the engine scores it.
    risk:
        Consequence if this operation is wrong.  Declared by the strategy.
    evidence:
        What the strategy measured in support of this operation.
    source_path:
        Dot-notation path to read from.  **Required** when
        ``op_type == FieldOpType.RENAME``; ``None`` for all other op types.
    value:
        Value to write.  Used only by ``SET_DEFAULT`` and ``SET_VALUE``;
        ``None`` for ``RENAME``, ``COERCE``, and ``REMOVE``.
    """

    # Required
    op_type: FieldOpType
    target_path: str
    rationale: str

    # Scoring — trust is assigned by TrustPolicy, never by a strategy.
    trust: float = 0.0
    risk: RepairRisk = RepairRisk.INFERRED
    evidence: RepairEvidence = field(default_factory=RepairEvidence)

    # Operation payload
    source_path: str | None = None
    value: Any = None

    def __post_init__(self) -> None:
        if not (0.0 <= self.trust <= 1.0):
            raise ValueError(f"trust must be in [0.0, 1.0], got {self.trust!r}")
        if self.op_type is FieldOpType.RENAME and self.source_path is None:
            raise ValueError("FieldOperation with op_type=RENAME requires source_path to be set.")

    @property
    def confidence(self) -> float:
        """
        Deprecated read-only alias for :attr:`trust`.

        The name changed because the number changed meaning: it used to be
        whatever a strategy felt like reporting on its own scale, and is now a
        policy-computed trust score comparable across strategies.  Retained
        for one release so existing readers keep working; there is no
        ``confidence=`` constructor argument.
        """
        return self.trust
