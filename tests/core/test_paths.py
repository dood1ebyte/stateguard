"""
Tests for stateguard.core.paths.

The sentinel's job is to keep "absent" apart from "present but ``None``".
That only works if there is exactly one of it, which is what most of these
pin: two modules each defining their own would produce values that are never
``is``-equal across a module boundary.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.paths import NOT_FOUND, get_nested_value
from stateguard.core.paths import _NotFound  # noqa: PLC2701


class TestSentinel:
    def test_is_a_singleton(self) -> None:
        assert _NotFound() is NOT_FOUND

    def test_reprs_readably(self) -> None:
        """It shows up in assertion diffs and log lines."""
        assert repr(NOT_FOUND) == "NOT_FOUND"

    def test_truthiness_raises_rather_than_lying(self) -> None:
        """
        Falsy would alias the sentinel to 0, "", [] and None -- the exact
        conflation it exists to prevent -- and truthy would make "nothing
        here" read as a value. Neither answer is right, so there is no answer.
        """
        with pytest.raises(TypeError, match="is NOT_FOUND"):
            bool(NOT_FOUND)
        with pytest.raises(TypeError, match="is NOT_FOUND"):
            if NOT_FOUND:  # noqa: SIM103
                pass

    def test_is_distinguishable_from_none(self) -> None:
        assert NOT_FOUND is not None


class TestGetNestedValue:
    @pytest.mark.parametrize(
        ("data", "path", "expected"),
        [
            ({"a": 1}, "a", 1),
            ({"a": {"b": 2}}, "a.b", 2),
            ({"a": {"b": {"c": 3}}}, "a.b.c", 3),
            ({"a": None}, "a", None),  # present, and None is a real value
            ({"a": {"b": None}}, "a.b", None),
            ({"a": 0}, "a", 0),  # falsy but present
            ({"a": ""}, "a", ""),
        ],
    )
    def test_returns_the_value_at_the_path(self, data: Any, path: str, expected: Any) -> None:
        assert get_nested_value(data, path) == expected

    @pytest.mark.parametrize(
        ("data", "path"),
        [
            ({}, "a"),
            ({"a": 1}, "b"),
            ({"a": {"b": 1}}, "a.c"),
            ({"a": {"b": 1}}, "a.b.c"),  # cannot descend into a scalar
            ({"a": [1, 2]}, "a.0"),  # lists are not traversed
            ({"a": 1}, ""),
        ],
    )
    def test_returns_not_found_for_an_absent_path(self, data: Any, path: str) -> None:
        assert get_nested_value(data, path) is NOT_FOUND

    def test_a_present_none_is_not_not_found(self) -> None:
        """The distinction the whole module exists for."""
        assert get_nested_value({"a": None}, "a") is None
        assert get_nested_value({}, "a") is NOT_FOUND

    def test_a_non_dict_root_is_not_found(self) -> None:
        assert get_nested_value("string", "a") is NOT_FOUND
        assert get_nested_value(None, "a") is NOT_FOUND
