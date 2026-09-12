"""
The contract cache -- and specifically, the two ways it could be wrong.

A cache in front of schema extraction has exactly two failure modes that
matter, and they pull in opposite directions:

* **Serving the wrong contract.** Two tools with coincidentally identical
  parameter shapes must not share an entry. This is why the key is not
  ``contract_id``.
* **Serving a stale contract.** A tool that changes its schema keeps its
  name -- that *is* the drift StateGuard exists to catch -- so keying on the
  name alone would pin the first version seen and quietly repair against it
  forever. This is why the schema content is in the key.

Both are tested here, because a cache that gets either wrong is worse than
no cache at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.adapters.mcp import MCPToolAdapter, SchemaCache
from stateguard.core.models.contract import ContractSpec
from stateguard.core.models.field_types import FieldType


def _tool(name: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


WEATHER = _tool(
    "get_weather", {"location": {"type": "string"}, "days": {"type": "integer"}}, ["location"]
)
#: Deliberately the same *shape* as WEATHER, so the two produce the same
#: ``contract_id``. This is the collision that rules ``contract_id`` out as
#: a cache key, and it is not a contrived one -- tool signatures repeat.
TRAFFIC = _tool(
    "get_traffic", {"location": {"type": "string"}, "days": {"type": "integer"}}, ["location"]
)


class TestKeyCorrectness:
    def test_the_collision_that_rules_out_contract_id_is_real(self) -> None:
        """Guards the premise the cache design rests on."""
        adapter = MCPToolAdapter()
        weather = adapter.extract_contract(WEATHER)
        traffic = adapter.extract_contract(TRAFFIC)
        assert weather.contract_id == traffic.contract_id

    def test_same_shape_different_tools_get_separate_entries(self) -> None:
        adapter = MCPToolAdapter()
        adapter.extract_contract(WEATHER)
        adapter.extract_contract(TRAFFIC)
        assert len(adapter.cache) == 2

    def test_a_changed_schema_is_not_served_from_the_old_entry(self) -> None:
        """
        The failure that would matter most: a server renamed a parameter,
        which is the whole scenario, and the cache kept answering with the
        signature from before the change.
        """
        adapter = MCPToolAdapter()
        adapter.extract_contract(WEATHER)

        renamed = _tool("get_weather", {"city": {"type": "string"}}, ["city"])
        spec = adapter.extract_contract(renamed)

        assert [f.path for f in spec.fields] == ["city"]

    def test_an_unchanged_schema_is_served_from_cache(self) -> None:
        adapter = MCPToolAdapter()
        first = adapter.extract_contract(WEATHER)
        second = adapter.extract_contract(WEATHER)
        assert first is second

    def test_key_is_order_insensitive(self) -> None:
        """
        The same schema with its keys written in a different order is the
        same schema. Canonical JSON, not dict identity.
        """
        adapter = MCPToolAdapter()
        adapter.extract_contract(WEATHER)
        reordered = {
            "inputSchema": {
                "required": ["location"],
                "properties": {"days": {"type": "integer"}, "location": {"type": "string"}},
                "type": "object",
            },
            "name": "get_weather",
        }
        adapter.extract_contract(reordered)
        assert len(adapter.cache) == 1

    def test_a_bare_schema_and_the_tool_holding_it_are_separate_keys(self) -> None:
        """
        Not a bug -- the tool name is part of the identity, and ``None`` is
        a distinct name. They extract to equal contracts either way.
        """
        adapter = MCPToolAdapter()
        adapter.extract_contract(WEATHER)
        adapter.extract_contract(WEATHER["inputSchema"])
        assert len(adapter.cache) == 2


class TestBounding:
    def test_least_recently_used_is_evicted(self) -> None:
        cache = SchemaCache(maxsize=2)
        spec = ContractSpec(fields=[])
        cache.put("a", {"type": "object"}, spec)
        cache.put("b", {"type": "object"}, spec)
        cache.get("a", {"type": "object"})  # 'a' is now the most recent
        cache.put("c", {"type": "object"}, spec)

        assert cache.get("b", {"type": "object"}) is None
        assert cache.get("a", {"type": "object"}) is spec
        assert cache.get("c", {"type": "object"}) is spec

    def test_bound_is_respected_under_churn(self) -> None:
        """
        A proxy sees tool definitions from servers it does not control, so
        an unbounded cache keyed partly on remote content is a slow leak
        with an external party holding the tap.
        """
        cache = SchemaCache(maxsize=4)
        for index in range(100):
            cache.put(f"tool{index}", {"type": "object"}, ContractSpec(fields=[]))
        assert len(cache) == 4

    def test_a_nonsense_size_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            SchemaCache(maxsize=0)

    def test_clear_empties_it(self) -> None:
        adapter = MCPToolAdapter()
        adapter.extract_contract(WEATHER)
        adapter.cache.clear()
        assert len(adapter.cache) == 0

    def test_cache_size_is_configurable_through_the_factory(self) -> None:
        from stateguard import ContractGuard

        guard = ContractGuard.with_mcp(cache_size=1)
        guard.repair(WEATHER, {"location": "Mumbai"})
        guard.repair(TRAFFIC, {"location": "Mumbai"})
        assert len(guard._adapter.cache) == 1


class TestUnhashableSchema:
    def test_an_unserialisable_schema_misses_rather_than_raising(self) -> None:
        """
        A schema that arrived as JSON always serialises; one built in Python
        might not. A cache miss is a far better answer there than an
        exception thrown from inside what the caller asked to be a repair.
        """
        cache = SchemaCache()
        schema = {"type": "object", "x-callback": object()}
        cache.put("t", schema, ContractSpec(fields=[]))
        assert cache.get("t", schema) is None
        assert len(cache) == 0

    def test_extraction_still_works_for_such_a_schema(self) -> None:
        adapter = MCPToolAdapter()
        tool = {
            "name": "t",
            "inputSchema": {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "x-note": object(),
            },
        }
        spec = adapter.extract_contract(tool)
        assert spec.fields[0].field_type is FieldType.STRING
        assert len(adapter.cache) == 0
