"""
EnumNormalizationStrategy — repairs enum values that differ only in spelling.

Priority 35, between ``TypeCoercionStrategy`` (30) and
``DefaultValueFillStrategy`` (40).  It is the first strategy to target
``VALUE_CONSTRAINT_VIOLATION``: every other strategy repairs a field's
*shape*, and this one repairs its *value*.

It fires when a payload carries a value that is a declared enum member under
a different spelling — ``"IN PROGRESS"`` or ``"in-progress"`` for a member
declared ``"in_progress"``.  Models produce these constantly: the member name
is written for humans in the prompt and echoed back in prose casing.

Where the values come from
--------------------------
The ``ENUM_VALUES`` constraint, whatever declared it.  That is deliberately
one code path for three sources:

* ``Literal["open", "done"]``;
* an ``enum.Enum`` subclass, which the Pydantic adapter now surfaces as its
  member *values* (it previously fell through to ``FieldType.ANY`` with no
  constraint at all, so StateGuard could not see the restriction);
* a JSON Schema ``enum`` array, via ``DictContractAdapter``.

So this lands ready for the MCP adapter without a second implementation.

Normalisation
-------------
A closed, ordered list, applied to both the received value and each declared
member:

1. ``casefold`` (Unicode-aware lowering)
2. ``strip``
3. ``-`` and space -> ``_``

Nothing else.  No Levenshtein, no synonyms, no prefix matching — similarity
scoring over enum *values* is a different and much riskier feature than
normalising their spelling, and the trust model has no way to price it.

Note this **maps** separators to ``_`` rather than removing them, which is
where it differs from ``normalize_field_name`` in ``normalized.py``.  Field
names are identifiers, where ``userid`` and ``user_id`` are the same name;
enum values are data, where collapsing separators entirely would let
``"onhold"`` claim ``"on_hold"`` on thinner evidence than this strategy is
willing to act on.

Ambiguity
---------
Normalisation is many-to-one, so a contract declaring both ``"in_progress"``
and ``"in progress"`` has two members that normalise identically.  Unlike
``NormalizedNameStrategy`` — which drops a collision because
``FuzzyFieldMatchStrategy`` still gets a turn — nothing runs after this
strategy, so dropping it would make the repair vanish silently.

Every colliding member is proposed instead, each carrying ``margin = 0.0``.
They share a ``target_path``, so the engine merges them into a single
``AmbiguousRepair`` with the candidates ranked, and the caller gets the
choice rather than the silence.

Zero external dependencies.
"""

from __future__ import annotations

from typing import Any

from stateguard.core.errors.operations import (
    FIDELITY_EXACT,
    FIDELITY_NORMALISED,
    FIDELITY_WHITESPACE,
    FieldOperation,
    FieldOpType,
    RepairEvidence,
    RepairRisk,
)
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec, FieldSpec, find_field_spec
from stateguard.core.models.field_types import FieldConstraintType
from stateguard.core.paths import NOT_FOUND, get_nested_value

__all__ = ["EnumNormalizationStrategy", "normalize_enum_value"]


#: Separators rewritten to ``_``.  Mapped, not removed -- see the module
#: docstring on why this differs from field-name normalisation.
_SEPARATORS = ("-", " ")

#: Canonical separator every member of ``_SEPARATORS`` collapses to.
_CANONICAL_SEPARATOR = "_"


def normalize_enum_value(value: str) -> str:
    """
    Reduce *value* to its case- and separator-independent form.

    ``casefold`` rather than ``lower``: it is the Unicode-correct operation
    for caseless comparison, so ``"STRASSE"`` and ``"straße"`` agree.

    Because ``-`` and spaces both become ``_``, the four spellings a model
    realistically produces for one member all converge::

        "IN PROGRESS"  -> "in_progress"
        "in-progress"  -> "in_progress"
        "In Progress"  -> "in_progress"
        "in_progress"  -> "in_progress"
    """
    normalized = value.casefold().strip()
    for separator in _SEPARATORS:
        normalized = normalized.replace(separator, _CANONICAL_SEPARATOR)
    return normalized


def _normalisation_fidelity(received: str, member: str) -> float:
    """
    How much of *received* survives being rewritten as *member*.

    The same three-rung ladder ``TypeCoercionStrategy`` measures its casts
    against, so a coercion's ``value_preserved`` and an enum normalisation's
    mean the same thing and the trust scores stay comparable:

    * case alone differed — for a closed set, case carries no information,
      so nothing was lost;
    * surrounding whitespace also differed;
    * separators were rewritten too, which is a reading of the value rather
      than a re-spelling of it.
    """
    if received.casefold() == member.casefold():
        return FIDELITY_EXACT
    if received.strip().casefold() == member.casefold():
        return FIDELITY_WHITESPACE
    return FIDELITY_NORMALISED


def _declared_enum_values(field_spec: FieldSpec) -> tuple[Any, ...] | None:
    """
    Return the field's ``ENUM_VALUES`` set, or ``None`` if it declares none.

    The constraint's value must be a real collection.  ``tuple()`` over a bare
    ``str`` splits it into *characters*, so a contract that declares
    ``{"type": "enum_values", "value": "open"}`` -- a plausible authoring slip
    that ``DictContractAdapter`` accepts without complaint -- would be read as
    the four members ``('o', 'p', 'e', 'n')``.  A received ``"O"`` then
    normalises cleanly onto ``'o'`` and StateGuard rewrites the payload and
    reports SUCCESS.  Silently rewriting data because a schema was malformed
    is worse than doing nothing, so a non-collection is treated as declaring
    no enum at all.
    """
    for constraint in field_spec.constraints:
        if constraint.constraint_type is FieldConstraintType.ENUM_VALUES:
            if isinstance(constraint.value, (list, tuple, set, frozenset)):
                return tuple(constraint.value)
            return None
    return None


class EnumNormalizationStrategy(IRepairStrategy):
    """
    Rewrites a payload value to the declared enum member it spells.

    Reports ``value_preserved`` at ``RepairRisk.INFERRED``.  INFERRED rather
    than DECLARED because the contract named the *members*, not the claim
    that ``"IN PROGRESS"`` is one of them — recognising that is StateGuard's
    inference, and a wrong one silently changes a value the caller will act
    on.
    """

    @property
    def name(self) -> str:
        return "EnumNormalizationStrategy"

    @property
    def priority(self) -> int:
        return 35

    # ------------------------------------------------------------------
    # IRepairStrategy
    # ------------------------------------------------------------------

    def can_handle(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> bool:
        return any(self._enum_values_for(v, contract) is not None for v in violations)

    def propose(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[FieldOperation]:
        operations: list[FieldOperation] = []

        for violation in violations:
            allowed = self._enum_values_for(violation, contract)
            if allowed is None:
                continue

            received = get_nested_value(data, violation.field_path)
            # Only strings are normalisable, and a value that already *is* a
            # member is somebody else's violation to explain.
            if received is NOT_FOUND or not isinstance(received, str) or received in allowed:
                continue

            target = normalize_enum_value(received)
            matches = [
                member
                for member in allowed
                if isinstance(member, str) and normalize_enum_value(member) == target
            ]
            if not matches:
                continue

            operations.extend(self._proposals(violation.field_path, received, matches))

        return operations

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _enum_values_for(
        violation: ContractViolation,
        contract: ContractSpec,
    ) -> tuple[Any, ...] | None:
        """
        The declared enum set governing *violation*, or ``None``.

        ``None`` for any violation this strategy has no business touching:
        the wrong violation type, a path the contract does not declare, or a
        declared field carrying no ``ENUM_VALUES`` constraint.
        """
        if violation.violation_type is not ViolationType.VALUE_CONSTRAINT_VIOLATION:
            return None
        field_spec = find_field_spec(contract, violation.field_path)
        if field_spec is None:
            return None
        return _declared_enum_values(field_spec)

    @staticmethod
    def _proposals(
        field_path: str,
        received: str,
        matches: list[str],
    ) -> list[FieldOperation]:
        """
        Build one ``SET_VALUE`` per member *received* normalises onto.

        More than one means the contract's own members collide under
        normalisation, which is a defect in the schema rather than in the
        payload — no evidence about the value can resolve it.  Every
        colliding proposal therefore reports ``margin = 0.0`` **and the same
        ``value_preserved``**, so the whole group is scored identically and
        lands in one band together.

        Scoring them individually is what the obvious implementation does,
        and it is wrong here.  Against members ``{"in progress",
        "in_progress"}`` a received ``"IN PROGRESS"`` differs from the first
        by case alone (1.0) and from the second by a separator (0.85), so the
        two candidates land 0.105 apart — straddling the abstain floor.  One
        is surfaced on ``RepairResult.ambiguous`` and the other is quietly
        rejected, leaving the caller looking at a one-item list for a
        decision that had two options.  Tied candidates have to be scored as
        tied.

        The group's score is the *best* fidelity available, because that is
        what the payload actually demonstrates: the received value does spell
        one of these members that faithfully; what is unknown is only which.
        """
        contested = len(matches) > 1
        # One score for the whole group -- see the docstring.
        fidelity = max(_normalisation_fidelity(received, member) for member in matches)
        operations: list[FieldOperation] = []

        for member in matches:
            note = (
                f"received value normalises onto the declared member '{member}'"
                if not contested
                else (
                    f"'{member}' is one of {len(matches)} declared members that "
                    f"normalise identically; the contract cannot say which was meant"
                )
            )
            operations.append(
                FieldOperation(
                    op_type=FieldOpType.SET_VALUE,
                    target_path=field_path,
                    # The declared member is schema data and safe to log; the
                    # received value is runtime data and is not named here.
                    # See RepairConfig.include_values_in_log.
                    rationale=(
                        f"Normalise the value at '{field_path}' to the declared "
                        f"enum member '{member}'."
                    ),
                    value=member,
                    risk=RepairRisk.INFERRED,
                    evidence=RepairEvidence(
                        value_preserved=fidelity,
                        margin=0.0 if contested else 1.0,
                        alternatives_considered=len(matches),
                        notes=(note,),
                    ),
                )
            )

        return operations
