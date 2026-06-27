"""
StrategyRegistry — ordered collection of repair strategies.

The registry sorts strategies by priority once at construction and is
immutable thereafter.  The engine queries it once per repair-loop
iteration via ``get_applicable``.
"""

from __future__ import annotations

from typing import Any

from stateguard.core.errors.violations import ContractViolation
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec

__all__ = ["StrategyRegistry"]


class StrategyRegistry:
    """
    Immutable, priority-ordered collection of ``IRepairStrategy`` instances.

    Strategies are sorted by ``priority`` ascending at construction time
    (lower priority numbers run first).  Python's ``sorted`` is stable, so
    strategies with equal priority retain their relative order from the
    input list — i.e. registration order is the tie-breaker.

    Example
    -------
    ::

        registry = StrategyRegistry([
            FuzzyFieldMatchStrategy(),   # priority 20
            ExactAliasStrategy(),        # priority 10
            TypeCoercionStrategy(),      # priority 30
            DefaultValueFillStrategy(),  # priority 40
        ])
        # registry.strategies is now ordered:
        #   ExactAliasStrategy (10), FuzzyFieldMatchStrategy (20),
        #   TypeCoercionStrategy (30), DefaultValueFillStrategy (40)
    """

    def __init__(self, strategies: list[IRepairStrategy]) -> None:
        self._strategies: tuple[IRepairStrategy, ...] = tuple(
            sorted(strategies, key=lambda s: s.priority)
        )

    @property
    def strategies(self) -> tuple[IRepairStrategy, ...]:
        """All registered strategies, in priority order."""
        return self._strategies

    def get_applicable(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[IRepairStrategy]:
        """
        Return strategies whose ``can_handle`` returns ``True`` for the
        given violation set, in priority order.

        Parameters
        ----------
        violations:
            All violations present at the start of the current repair
            iteration.
        contract:
            The normalised contract being repaired against.
        data:
            The current working copy of the data.  Read-only.

        Returns
        -------
        list[IRepairStrategy]
            Strategies that claim to be able to address at least one
            violation, in priority order.  May be empty.
        """
        return [
            strategy
            for strategy in self._strategies
            if strategy.can_handle(violations, contract, data)
        ]

    def __len__(self) -> int:
        return len(self._strategies)

    def __iter__(self) -> Any:
        return iter(self._strategies)
