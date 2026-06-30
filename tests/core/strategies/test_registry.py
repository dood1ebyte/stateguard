"""Tests for stateguard.core.strategies.registry."""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.errors.violations import ContractViolation
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec
from stateguard.core.strategies.registry import StrategyRegistry
from tests.conftest import MockRepairStrategy, make_contract_spec, make_violation


# ---------------------------------------------------------------------------
# Construction and ordering
# ---------------------------------------------------------------------------


class TestConstructionAndOrdering:

    def test_empty_registry(self) -> None:
        registry = StrategyRegistry([])
        assert len(registry) == 0
        assert registry.strategies == ()

    def test_single_strategy(self) -> None:
        s = MockRepairStrategy(name="A", priority=10)
        registry = StrategyRegistry([s])
        assert len(registry) == 1
        assert registry.strategies == (s,)

    def test_sorted_by_priority_ascending(self) -> None:
        s30 = MockRepairStrategy(name="C", priority=30)
        s10 = MockRepairStrategy(name="A", priority=10)
        s20 = MockRepairStrategy(name="B", priority=20)
        registry = StrategyRegistry([s30, s10, s20])
        names = [s.name for s in registry.strategies]
        assert names == ["A", "B", "C"]

    def test_already_sorted_input_unchanged(self) -> None:
        s10 = MockRepairStrategy(name="A", priority=10)
        s20 = MockRepairStrategy(name="B", priority=20)
        registry = StrategyRegistry([s10, s20])
        assert registry.strategies == (s10, s20)

    def test_reverse_sorted_input_corrected(self) -> None:
        s40 = MockRepairStrategy(name="D", priority=40)
        s30 = MockRepairStrategy(name="C", priority=30)
        s20 = MockRepairStrategy(name="B", priority=20)
        s10 = MockRepairStrategy(name="A", priority=10)
        registry = StrategyRegistry([s40, s30, s20, s10])
        names = [s.name for s in registry.strategies]
        assert names == ["A", "B", "C", "D"]

    def test_equal_priority_preserves_registration_order(self) -> None:
        """Python's sorted() is stable; equal priorities keep input order."""
        s_first = MockRepairStrategy(name="First", priority=10)
        s_second = MockRepairStrategy(name="Second", priority=10)
        s_third = MockRepairStrategy(name="Third", priority=10)
        registry = StrategyRegistry([s_first, s_second, s_third])
        names = [s.name for s in registry.strategies]
        assert names == ["First", "Second", "Third"]

    def test_mixed_equal_and_distinct_priorities(self) -> None:
        a = MockRepairStrategy(name="A", priority=10)
        b = MockRepairStrategy(name="B", priority=20)
        c = MockRepairStrategy(name="C", priority=10)
        d = MockRepairStrategy(name="D", priority=5)
        registry = StrategyRegistry([a, b, c, d])
        names = [s.name for s in registry.strategies]
        # D(5), then A(10), C(10) in registration order, then B(20)
        assert names == ["D", "A", "C", "B"]

    def test_strategies_property_returns_tuple(self) -> None:
        registry = StrategyRegistry([MockRepairStrategy()])
        assert isinstance(registry.strategies, tuple)


# ---------------------------------------------------------------------------
# Iteration and length
# ---------------------------------------------------------------------------


class TestIterationAndLength:

    def test_len(self) -> None:
        registry = StrategyRegistry([
            MockRepairStrategy(name="A", priority=10),
            MockRepairStrategy(name="B", priority=20),
        ])
        assert len(registry) == 2

    def test_iteration_order_matches_priority(self) -> None:
        s20 = MockRepairStrategy(name="B", priority=20)
        s10 = MockRepairStrategy(name="A", priority=10)
        registry = StrategyRegistry([s20, s10])
        names = [s.name for s in registry]
        assert names == ["A", "B"]

    def test_iteration_yields_all_strategies(self) -> None:
        strategies = [
            MockRepairStrategy(name=f"S{i}", priority=i * 10) for i in range(5)
        ]
        registry = StrategyRegistry(strategies)
        assert len(list(registry)) == 5


# ---------------------------------------------------------------------------
# get_applicable
# ---------------------------------------------------------------------------


class TestGetApplicable:

    def test_no_strategies_returns_empty(self) -> None:
        registry = StrategyRegistry([])
        contract = make_contract_spec()
        result = registry.get_applicable([], contract, {})
        assert result == []

    def test_all_handle_returns_all_in_priority_order(self) -> None:
        s20 = MockRepairStrategy(name="B", priority=20, handle=True)
        s10 = MockRepairStrategy(name="A", priority=10, handle=True)
        registry = StrategyRegistry([s20, s10])
        contract = make_contract_spec()
        result = registry.get_applicable([], contract, {})
        assert [s.name for s in result] == ["A", "B"]

    def test_none_handle_returns_empty(self) -> None:
        s1 = MockRepairStrategy(name="A", priority=10, handle=False)
        s2 = MockRepairStrategy(name="B", priority=20, handle=False)
        registry = StrategyRegistry([s1, s2])
        contract = make_contract_spec()
        result = registry.get_applicable([], contract, {})
        assert result == []

    def test_partial_handle_returns_only_applicable(self) -> None:
        s1 = MockRepairStrategy(name="A", priority=10, handle=True)
        s2 = MockRepairStrategy(name="B", priority=20, handle=False)
        s3 = MockRepairStrategy(name="C", priority=30, handle=True)
        registry = StrategyRegistry([s1, s2, s3])
        contract = make_contract_spec()
        result = registry.get_applicable([], contract, {})
        assert [s.name for s in result] == ["A", "C"]

    def test_get_applicable_preserves_priority_order_with_subset(self) -> None:
        s1 = MockRepairStrategy(name="Low", priority=5, handle=True)
        s2 = MockRepairStrategy(name="Mid", priority=15, handle=False)
        s3 = MockRepairStrategy(name="High", priority=25, handle=True)
        registry = StrategyRegistry([s3, s1, s2])  # registered out of order
        result = registry.get_applicable([], make_contract_spec(), {})
        assert [s.name for s in result] == ["Low", "High"]

    def test_get_applicable_called_with_real_violation(self) -> None:
        v = make_violation()
        contract = make_contract_spec()
        registry = StrategyRegistry([MockRepairStrategy(handle=True)])
        result = registry.get_applicable([v], contract, {"some": "data"})
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Conformance: registry only holds IRepairStrategy instances
# ---------------------------------------------------------------------------


class TestConformance:

    def test_mock_strategy_satisfies_irepairstrategy(self) -> None:
        assert isinstance(MockRepairStrategy(), IRepairStrategy)

    def test_registry_strategies_are_irepairstrategy_instances(self) -> None:
        registry = StrategyRegistry([
            MockRepairStrategy(name="A", priority=10),
            MockRepairStrategy(name="B", priority=20),
        ])
        for s in registry:
            assert isinstance(s, IRepairStrategy)
