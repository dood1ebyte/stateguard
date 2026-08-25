"""
Screening of schema-supplied regular expressions.

``pattern`` is the only supported keyword whose value is *executed* rather
than compared against, and on this adapter it arrives from a third-party MCP
server. Before the screen existed, ``(a+)+$`` against 40 ``a``s and a ``!``
did not finish in 15 seconds -- a denial of service reachable from a tool
definition, and one that bounding the input length does not fix.

The screen is a heuristic, not a proof (see ``patterns.py``), so the tests
below pin both halves of the trade: the nested-quantifier family is refused,
and patterns that merely *look* like syntax -- quantifiers inside a character
class, escaped parentheses, bounded braces -- are not.
"""

from __future__ import annotations

import pytest

from stateguard.adapters.jsonschema import UnsupportedSchemaError
from stateguard.adapters.jsonschema.extractor import JSONSchemaExtractor
from stateguard.adapters.jsonschema.patterns import screen_pattern

# The nested-quantifier family: a group containing an unbounded quantifier,
# itself unboundedly quantified.
CATASTROPHIC = [
    "(a+)+$",
    "(a*)*",
    "(a+)*",
    "(a*)+",
    "((a+))+",
    r"(\d+\.)+",
    "(a+){2,}",
    r"([a-z]+\.)*",
]

# Safe, and specifically chosen to catch an over-eager scanner.
SAFE = [
    "^[0-9]+$",
    "(abc)+",
    "(a|b)+",
    "(a+)?",  # '?' is bounded
    "(a+){2,5}",  # bounded brace
    "[*+]+",  # quantifier chars inside a character class
    r"\(a+\)+",  # escaped parentheses are not a group
    "a+b+",  # no group at all
    "(?:foo)+",
    r"^\d{3}-\d{4}$",
]


def _schema(pattern: object) -> dict:
    return {
        "type": "object",
        "properties": {"c": {"type": "string", "pattern": pattern}},
    }


class TestCatastrophicPatternsAreRefused:
    @pytest.mark.parametrize("pattern", CATASTROPHIC)
    def test_screen_refuses(self, pattern: str) -> None:
        with pytest.raises(UnsupportedSchemaError, match="unbounded quantifier"):
            screen_pattern(pattern, "c")

    @pytest.mark.parametrize("pattern", CATASTROPHIC)
    def test_extraction_refuses(self, pattern: str) -> None:
        """Refused at extraction, so no match is ever attempted on data."""
        with pytest.raises(UnsupportedSchemaError):
            JSONSchemaExtractor().extract(_schema(pattern))

    def test_error_suggests_a_fix(self) -> None:
        with pytest.raises(UnsupportedSchemaError) as exc:
            screen_pattern("(a+)+$", "c")
        assert "bounded quantifier" in str(exc.value)


class TestSafePatternsAreAllowed:
    @pytest.mark.parametrize("pattern", SAFE)
    def test_screen_allows(self, pattern: str) -> None:
        assert screen_pattern(pattern, "c") == pattern

    @pytest.mark.parametrize("pattern", SAFE)
    def test_extraction_allows(self, pattern: str) -> None:
        spec = JSONSchemaExtractor().extract(_schema(pattern))
        assert spec.fields[0].path == "c"


class TestMalformedPatterns:
    def test_uncompilable_pattern_is_refused(self) -> None:
        with pytest.raises(UnsupportedSchemaError, match="not a valid regular expression"):
            screen_pattern("([", "c")

    @pytest.mark.parametrize(("value", "name"), [(5, "int"), (None, "NoneType"), (["a"], "list")])
    def test_non_string_pattern_is_refused(self, value: object, name: str) -> None:
        with pytest.raises(UnsupportedSchemaError, match=name):
            screen_pattern(value, "c")

    def test_malformed_pattern_is_refused_at_extraction(self) -> None:
        with pytest.raises(UnsupportedSchemaError):
            JSONSchemaExtractor().extract(_schema("(["))


class TestScreenIsReachableFromTheAdapter:
    def test_the_original_hang_never_runs(self) -> None:
        """
        The regression: this exact schema previously hung validation. It must
        now fail during extraction, before any data is matched.
        """
        with pytest.raises(UnsupportedSchemaError):
            JSONSchemaExtractor().extract(
                {
                    "type": "object",
                    "properties": {"c": {"type": "string", "pattern": "(a+)+$"}},
                    "required": ["c"],
                }
            )
