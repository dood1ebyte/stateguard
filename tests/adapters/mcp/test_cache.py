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

from stateguard import ContractGuard
from stateguard.adapters.jsonschema.errors import JSONSchemaError, SchemaReferenceError
from stateguard.adapters.mcp import MCPToolAdapter, SchemaCache
from stateguard.adapters.mcp import cache as cache_module
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


class TestSchemaTooDeepToKey:
    """
    A cache lookup must not be the thing that crashes on untrusted input.

    ``json.dumps`` recurses per level, so a schema nested deeper than the
    interpreter allows raises ``RecursionError`` from inside ``_key`` -- and
    that runs *before* ``JSONSchemaExtractor``, so it pre-empted
    ``RefResolver``'s depth bound, whose entire purpose is to stop that error
    escaping as an unhandled crash. The MCP path therefore died with a
    ``RecursionError`` on a schema that ``with_json_schema()`` refused
    cleanly, which is the wrong way round: the MCP path is the one facing
    servers nobody controls.

    **How deep is "too deep" is not this package's property.** It moves with
    the interpreter: CPython 3.12 gave C-level recursion its own limit, so a
    depth that raises on one version serialises fine on the next. Measured
    here, 3.13 on Windows raises from depth 1499; CI's 3.12 on Linux and
    macOS serialises 2000 without complaint. The first version of these tests
    asserted an empty cache after a 2000-deep ``put`` and duly passed on 3.11
    and failed on 3.12.

    So the depth-dependent test asserts only what is true at every depth --
    that nothing escapes -- and the ``RecursionError`` branch is forced
    rather than provoked.
    """

    @staticmethod
    def _nested(depth: int) -> dict[str, Any]:
        root: dict[str, Any] = {"type": "object", "properties": {}}
        current = root
        for _ in range(depth):
            child: dict[str, Any] = {"type": "object", "properties": {}}
            current["properties"]["child"] = child
            current = child
        return root

    def test_a_deeply_nested_schema_never_raises_from_the_cache(self) -> None:
        """
        Neither call may blow up, whatever this interpreter makes of the depth.

        Deliberately no assertion about whether the entry landed: that is a
        fact about ``json.dumps`` on the running CPython, not about the cache,
        and asserting it is what made this test portable only by accident.
        """
        cache = SchemaCache()
        schema = self._nested(2000)

        cache.get("t", schema)
        cache.put("t", schema, ContractSpec(fields=[]))

    def test_a_key_that_recurses_too_deep_is_a_miss(self, monkeypatch: Any) -> None:
        """
        The ``RecursionError`` branch, forced rather than hoped for.

        Raising it directly pins the behaviour on every platform and version,
        including the ones where no reachable depth would trigger it -- and
        the guard has to hold there too, because the *next* release may move
        the limit back.
        """

        def too_deep(*_args: Any, **_kwargs: Any) -> str:
            raise RecursionError("maximum recursion depth exceeded while encoding a JSON object")

        monkeypatch.setattr(cache_module.json, "dumps", too_deep)

        cache = SchemaCache()
        schema = {"type": "object", "properties": {}}

        assert cache.get("t", schema) is None
        cache.put("t", schema, ContractSpec(fields=[]))
        assert len(cache) == 0

    def test_the_miss_lets_the_extractor_refuse_it_properly(self) -> None:
        """The point of the miss: a catchable error, in the right vocabulary."""
        adapter = MCPToolAdapter()
        tool = {"name": "deep", "inputSchema": self._nested(2000)}
        with pytest.raises(SchemaReferenceError, match="nests deeper than"):
            adapter.extract_contract(tool)

    def test_both_entrypoints_refuse_it_the_same_way(self) -> None:
        """
        The asymmetry this fixes, pinned directly. Both are ``JSONSchemaError``
        (and so ``ValueError``); neither is ``RecursionError``, which is a
        ``RuntimeError`` and escapes every caller catching the former.
        """
        schema = self._nested(2000)
        arguments = {"child": {}}

        with pytest.raises(JSONSchemaError):
            ContractGuard.with_mcp().repair({"name": "deep", "inputSchema": schema}, arguments)
        with pytest.raises(JSONSchemaError):
            ContractGuard.with_json_schema().repair(schema, arguments)
