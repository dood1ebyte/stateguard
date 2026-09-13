"""
The real-schema corpus: 21 ``inputSchema`` blobs from four public MCP servers.

Captured by running the servers, not by transcribing their source -- see
``capture.py``. The JSON in ``schemas/`` is what ``tools/list`` actually put
on the wire, so a test that passes against it is evidence about real servers
rather than about fixtures written by the same person who wrote the adapter.

That distinction is the whole reason Phase 4.1 exists. Every one of Phase
1b's seven findings was a schema shape that extracted cleanly while dropping
something -- constraints behind an ``Optional[$ref]``, keywords inside a
union branch -- and none of them came from a hand-written fixture, because a
hand-written fixture only contains what its author already thought to test.

The corpus is committed, so the tests need no network and no third-party
server installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["CorpusTool", "SCHEMA_DIR", "load_corpus"]

SCHEMA_DIR = Path(__file__).parent / "schemas"


@dataclass(frozen=True)
class CorpusTool:
    """One captured tool definition, with enough provenance to re-find it."""

    package: str
    version: str
    name: str
    definition: dict[str, Any]

    @property
    def input_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = self.definition["inputSchema"]
        return schema

    def __str__(self) -> str:
        # Used as the pytest parameter id, so a failure names the server and
        # the tool rather than an index into a list.
        return f"{self.package}:{self.name}"


def load_corpus() -> list[CorpusTool]:
    """Every captured tool, across every server, in a stable order."""
    tools: list[CorpusTool] = []
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        captured = json.loads(path.read_text(encoding="utf-8"))
        source = captured["source"]
        for definition in captured["tools"]:
            tools.append(
                CorpusTool(
                    package=source["package"],
                    version=source["version"],
                    name=definition["name"],
                    definition=definition,
                )
            )
    return tools
