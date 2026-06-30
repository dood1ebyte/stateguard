"""Repair strategy implementations and registry."""

from stateguard.core.strategies.alias import ExactAliasStrategy
from stateguard.core.strategies.coerce import TypeCoercionStrategy
from stateguard.core.strategies.default_fill import DefaultValueFillStrategy
from stateguard.core.strategies.fuzzy import FuzzyFieldMatchStrategy
from stateguard.core.strategies.registry import StrategyRegistry

__all__ = [
    "DefaultValueFillStrategy",
    "ExactAliasStrategy",
    "FuzzyFieldMatchStrategy",
    "StrategyRegistry",
    "TypeCoercionStrategy",
]
