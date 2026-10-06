"""
Micro-benchmarks for the MCP adapter's hot paths.

    python benchmarks/mcp_bench.py [--repeat N]

Times each operation over the real-schema corpus (21 tools captured from four
public MCP servers) plus the drift-corpus tools, and prints the median time
per operation. What matters is the shape: which operations are per *call*
(and so must be cheap) and which are per *listing* (and can afford a walk).

Per listing -- when tools/list changes:
    fingerprint, normalise, diff, register (extract + fingerprint + warn),
    sync of a whole listing.
Per call -- every tools/call:
    validate, repair_tool_call on a valid call (cache hit), on a drifted
    call, with a cold cache (cache miss), the registry's process_call, and
    explain().

No network, no SDK. Numbers are for comparison on one machine, not a
specification.
"""

from __future__ import annotations

import argparse
import copy
import json
import statistics
import sys
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stateguard import ContractGuard  # noqa: E402
from stateguard.adapters.jsonschema import (  # noqa: E402
    SchemaFeatureWarning,
    diff_schemas,
    fingerprint_schema,
    normalize_schema,
)
from stateguard.adapters.mcp import ToolRegistry, repair_tool_call  # noqa: E402


def _tools() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for path in sorted((ROOT / "tests/adapters/jsonschema/corpus/schemas").glob("*.json")):
        data = json.loads(path.read_text())
        tools.extend(data["tools"] if isinstance(data, dict) else data)
    drift = json.loads((ROOT / "tests/adapters/mcp/corpus/drift_corpus.json").read_text())
    tools.extend(drift["tools"].values())
    return tools


def _calls() -> list[tuple[str, Any, Any]]:
    drift = json.loads((ROOT / "tests/adapters/mcp/corpus/drift_corpus.json").read_text())
    return [(c["tool"], c["arguments"], c.get("forward")) for c in drift["calls"]]


def _time(label: str, fn: Callable[[], Any], ops: int, repeat: int) -> tuple[str, float]:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) / ops)
    return label, statistics.median(samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument("--repeat", type=int, default=15)
    args = parser.parse_args(argv)
    warnings.simplefilter("ignore", SchemaFeatureWarning)

    tools = _tools()
    schemas = [t["inputSchema"] for t in tools]
    edited = []
    for schema in schemas:
        after = copy.deepcopy(schema)
        after.setdefault("properties", {})["added_by_bench"] = {"type": "integer"}
        edited.append(after)

    calls = _calls()
    definitions = {t["name"]: t for t in tools}
    valid = [(definitions[n], fwd) for n, _, fwd in calls if fwd is not None]
    drifted = [(definitions[n], a) for n, a, _ in calls]

    guard = ContractGuard.with_mcp()
    registry = ToolRegistry(guard)
    registry.sync(tools)
    for tool, arguments in drifted:  # warm the cache
        repair_tool_call(guard, tool, arguments)
    outcomes = [repair_tool_call(guard, tool, arguments) for tool, arguments in drifted]

    def cold() -> None:
        for tool, arguments in drifted:
            repair_tool_call(ContractGuard.with_mcp(), tool, arguments)

    rows = [
        (
            "per listing",
            _time(
                "fingerprint_schema",
                lambda: [fingerprint_schema(s) for s in schemas],
                len(schemas),
                args.repeat,
            ),
        ),
        (
            "per listing",
            _time(
                "normalize_schema",
                lambda: [normalize_schema(s) for s in schemas],
                len(schemas),
                args.repeat,
            ),
        ),
        (
            "per listing",
            _time(
                "diff_schemas (one added field)",
                lambda: [diff_schemas(a, b) for a, b in zip(schemas, edited, strict=True)],
                len(schemas),
                args.repeat,
            ),
        ),
        (
            "per listing",
            _time(
                "ToolRegistry.register (new registry)",
                lambda: [ToolRegistry(guard).register(t) for t in tools],
                len(tools),
                args.repeat,
            ),
        ),
        (
            "per listing",
            _time(
                f"ToolRegistry.sync ({len(tools)} tools, unchanged)",
                lambda: registry.sync(tools),
                1,
                args.repeat,
            ),
        ),
        (
            "per call",
            _time(
                "guard.validate (cache hit)",
                lambda: [guard.validate(t, a) for t, a in valid],
                len(valid),
                args.repeat,
            ),
        ),
        (
            "per call",
            _time(
                "repair_tool_call, valid call",
                lambda: [repair_tool_call(guard, t, a) for t, a in valid],
                len(valid),
                args.repeat,
            ),
        ),
        (
            "per call",
            _time(
                "repair_tool_call, drift corpus",
                lambda: [repair_tool_call(guard, t, a) for t, a in drifted],
                len(drifted),
                args.repeat,
            ),
        ),
        ("per call", _time("repair_tool_call, cold cache", cold, len(drifted), args.repeat)),
        (
            "per call",
            _time(
                "ToolRegistry.process_call, drift corpus",
                lambda: [registry.process_call(definitions[n]["name"], a) for n, a, _ in calls],
                len(calls),
                args.repeat,
            ),
        ),
        (
            "per call",
            _time(
                "ToolCallOutcome.explain()",
                lambda: [o.explain() for o in outcomes],
                len(outcomes),
                args.repeat,
            ),
        ),
        (
            "per call",
            _time(
                "ToolCallOutcome.to_dict()",
                lambda: [o.to_dict() for o in outcomes],
                len(outcomes),
                args.repeat,
            ),
        ),
    ]

    print(
        f"MCP adapter benchmarks -- {len(tools)} tools, {len(calls)} calls, median of {args.repeat}"
    )
    print(f"Python {sys.version.split()[0]}\n")
    print(f"{'when':<12} {'operation':<46} {'per op':>12}")
    print("-" * 72)
    for when, (label, seconds) in rows:
        print(f"{when:<12} {label:<46} {seconds * 1e6:>9.1f} µs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
