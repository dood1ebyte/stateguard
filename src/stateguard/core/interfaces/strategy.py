"""
IRepairStrategy — the extension point for repair algorithms.

Each strategy encapsulates one repair technique (alias matching, fuzzy
rename, type coercion, default fill, …).  Strategies are stateless:
they receive violations and data, return proposed ``FieldOperation`` objects,
and never apply those operations themselves.

Critical contract
-----------------
``propose()`` receives **all current violations at once**, not one at a
time.  This is intentional: some repairs (field renames) are only
identifiable by examining a correlated pair of violations
(MISSING_REQUIRED_FIELD + UNEXPECTED_FIELD together).  Strategies that
receive violations individually cannot detect renames.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from stateguard.core.errors.operations import FieldOperation
from stateguard.core.errors.violations import ContractViolation
from stateguard.core.models.contract import ContractSpec

__all__ = ["IRepairStrategy"]


class IRepairStrategy(ABC):
    """
    Abstract base class for repair strategies.

    Implementations
    ---------------
    V1 ships four built-in strategies (all in ``stateguard.core.strategies``):

    * ``ExactAliasStrategy``    — priority 10
    * ``FuzzyFieldMatchStrategy`` — priority 20
    * ``TypeCoercionStrategy``  — priority 30
    * ``DefaultValueFillStrategy`` — priority 40

    Lower priority numbers run first.  The ``StrategyRegistry`` sorts
    strategies by priority before the repair loop begins.

    Authoring a custom strategy
    ---------------------------
    Subclass ``IRepairStrategy``, implement all four abstract members, and
    register the instance with ``StrategyRegistry``.  No engine code needs
    to change.

    Thread safety
    -------------
    Strategy instances are shared across repair sessions.  Strategies must
    be **stateless** — they must not store per-invocation data on ``self``.
    All context is passed via method parameters.
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Stable, human-readable identifier for this strategy.

        Used in ``RepairAttempt.strategy_name`` and repair log entries.
        Must be unique within a ``StrategyRegistry`` instance.
        Must not change between versions if callers key on it.

        Example: ``"FuzzyFieldMatchStrategy"``
        """

    @property
    @abstractmethod
    def priority(self) -> int:
        """
        Execution order within the registry.  Lower = runs first.

        The engine tries strategies in ascending priority order.  A strategy
        with priority 10 always runs before one with priority 20.
        Strategies with equal priority are run in registration order.
        """

    # ------------------------------------------------------------------
    # Core methods
    # ------------------------------------------------------------------

    @abstractmethod
    def can_handle(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> bool:
        """
        Return ``True`` if this strategy can address at least one violation.

        The engine calls ``can_handle`` before calling ``propose``.  A
        ``False`` return causes the engine to skip this strategy entirely
        for this repair iteration.

        Implementations should be **cheap** — this is called on every
        strategy for every repair loop iteration.  Avoid heavy computation;
        reserve that for ``propose``.

        Parameters
        ----------
        violations:
            All violations present at the start of this repair iteration.
            Includes both ERROR and WARNING severity.
        contract:
            The normalised contract being repaired against.
        data:
            The current working copy of the data dict (after any operations
            applied by earlier strategies in this iteration).  Read-only.

        Returns
        -------
        bool
        """

    @abstractmethod
    def propose(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[FieldOperation]:
        """
        Return a list of ``FieldOperation`` objects that would fix violations.

        The engine applies only operations where
        ``confidence >= RepairConfig.min_confidence_threshold``; operations
        below the threshold are recorded in ``RepairAttempt.rejected_operations``
        but not applied.

        Contract
        --------
        * Must **not** mutate *data*.  Data is a read-only view for the
          duration of ``propose``.
        * Must **not** apply operations — that is the engine's job.
        * May return an empty list if the strategy determines it cannot
          safely propose anything (e.g. score collision detected).
        * The list may contain operations targeting multiple violations;
          the engine applies them atomically in the order they appear.

        Parameters
        ----------
        violations:
            All violations present at the start of this iteration.
        contract:
            The normalised contract.
        data:
            Current working copy of the data.  Read-only.

        Returns
        -------
        list[FieldOperation]
            Proposed operations, ordered by application sequence.
            Empty list is a valid return.
        """
