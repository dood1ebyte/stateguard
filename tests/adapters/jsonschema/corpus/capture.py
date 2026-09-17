"""
Capture real ``inputSchema`` blobs from public MCP servers.

``MCP_ADAPTER_PLAN.md`` §6 Phase 4.1: *"Corpus: 15-20 real ``inputSchema``
blobs from public MCP servers as fixtures."*  Phase 1b is the argument for
it -- every one of its seven findings was a schema shape that extracted
cleanly while dropping something, which is what a real corpus surfaces and
what hand-written fixtures do not.

The schemas are captured by **running the servers**, not by transcribing
their source: each one is launched over stdio, asked for ``tools/list``, and
its answer written out verbatim.  What lands in ``schemas/`` is therefore
what a proxy would actually receive on the wire, not an approximation of it.

Running this
------------
Installing four third-party packages is not something a test suite should do,
so it does not: the captured JSON is committed and the corpus tests read it
offline.  This script exists so the corpus can be *re*-captured, and so the
provenance of each fixture is reproducible rather than asserted::

    python -m venv /tmp/corpusenv
    /tmp/corpusenv/bin/pip install mcp-server-git mcp-server-fetch \
        mcp-server-time mcp-server-sqlite
    MCP_CORPUS_BIN=/tmp/corpusenv/bin python tests/adapters/jsonschema/corpus/capture.py

Requires the MCP SDK in *this* environment (``pip install 'sguard[mcp]'``)
for the client half.  The servers live in their own environment; the two
never share a process.
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, StdioServerParameters

HERE = Path(__file__).parent
SCHEMAS = HERE / "schemas"

#: Directory holding the servers' console scripts -- ``Scripts`` on Windows,
#: ``bin`` elsewhere. Kept out of the committed fixtures so the capture is
#: not tied to one machine's paths.
BIN = Path(os.environ.get("MCP_CORPUS_BIN", ""))

#: The servers to capture, as ``(package, executable stem, arguments)``.
#: All four are reference servers from the ``modelcontextprotocol/servers``
#: repository, published to PyPI -- public, widely deployed, and between them
#: covering enough tools to be a corpus rather than a sample.
SERVERS: list[tuple[str, str, list[str]]] = [
    ("mcp-server-git", "mcp-server-git", []),
    ("mcp-server-fetch", "mcp-server-fetch", []),
    ("mcp-server-time", "mcp-server-time", []),
    ("mcp-server-sqlite", "mcp-server-sqlite", ["--db-path", ":memory:"]),
]


def _executable(stem: str) -> str:
    candidate = BIN / (f"{stem}.exe" if sys.platform == "win32" else stem)
    if not candidate.exists():
        raise SystemExit(
            f"No executable at {candidate}. Set MCP_CORPUS_BIN to the "
            f"Scripts/ or bin/ directory of an environment with the corpus "
            f"servers installed -- see this module's docstring."
        )
    return str(candidate)


def _version(package: str) -> str:
    """
    The installed version of *package*, asked of the server environment.

    Recorded into each fixture so a schema that changes upstream can be told
    apart from one this adapter started reading differently.
    """
    python = BIN / ("python.exe" if sys.platform == "win32" else "python")
    result = subprocess.run(
        [
            str(python),
            "-c",
            f"from importlib.metadata import version; print(version({package!r}))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout.strip() or "unknown"


async def _capture(package: str, stem: str, args: list[str]) -> dict[str, Any]:
    """Launch one server, ask for its tools, and return them verbatim."""
    params = StdioServerParameters(command=_executable(stem), args=args)
    async with Client(params) as client:
        listing = await client.list_tools()

    tools = [tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools]
    print(f"{package:20} {len(tools):2} tool(s): {', '.join(t['name'] for t in tools)}")
    return {
        "source": {
            "package": package,
            "version": _version(package),
            "origin": "https://pypi.org/project/" + package,
            "captured": date.today().isoformat(),
            "method": "tools/list over stdio, recorded verbatim",
        },
        "tools": tools,
    }


async def main() -> None:
    SCHEMAS.mkdir(exist_ok=True)
    total = 0
    for package, stem, args in SERVERS:
        captured = await _capture(package, stem, args)
        total += len(captured["tools"])
        path = SCHEMAS / f"{package}.json"
        path.write_text(json.dumps(captured, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"\n{total} tool schemas written to {SCHEMAS}")


if __name__ == "__main__":
    anyio.run(main)
