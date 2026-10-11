"""
Measurement scenarios: one MCP invocation each, with ground truth decided
before anything is run.

Every scenario names its failure category (A-K, see docs/runtime/TASKMAP.md),
the injector that produces the failure, what the client sends, and what is
*correct*:

* ``intended`` -- the arguments that should execute exactly once, with the
  client told it succeeded; or
* ``None`` -- nothing should execute, and the client must not be told it
  succeeded.

Corpora are pluggable: ``build(seed, corpus=...)`` takes any object with
``tools`` (name -> definition) and ``calls`` (labelled calls, in the drift
corpus's shape). The default is the hand-labelled synthetic drift corpus;
recorded real traffic can be dropped in with the same shape.

Deterministic: the only randomness is ``random.Random(seed)``, used to pick
which valid calls carry each transport / execution fault.
"""

from __future__ import annotations

import copy
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .harness import ServerAction

ROOT = Path(__file__).resolve().parents[2]
DRIFT_CORPUS = ROOT / "tests" / "adapters" / "mcp" / "corpus" / "drift_corpus.json"

#: The request id a scenario's raw line uses; never one ``Rig.request`` assigns.
RAW_LINE_ID = "raw-line-1"

CATEGORIES = {
    "A": "contract / argument schema",
    "B": "tool discovery / staleness",
    "C": "protocol / conformance",
    "D": "session / capability",
    "E": "transport / connection",
    "F": "timeout / transient",
    "G": "tool execution failure",
    "H": "result integrity",
    "I": "ambiguous completion",
    "J": "runtime state inconsistency",
    "K": "auth signal",
}


#: A *simulated* operator effect declaration for the drift-corpus tools, used
#: only by the ``auto_ops`` arm. Plain reads are READ_ONLY; tools that create,
#: send or move something are NON_IDEMPOTENT; tools whose effect depends on
#: arguments or implementation (write_file, set_role, query_db) are left
#: UNKNOWN, as an operator who had not checked would leave them.
SIMULATED_EFFECTS: dict[str, str] = {
    "read_file": "read_only",
    "list_issues": "read_only",
    "search_users": "read_only",
    "get_issue": "read_only",
    "web_search": "read_only",
    "create_issue": "non_idempotent",
    "post_message": "non_idempotent",
    "create_event": "non_idempotent",
    "transfer_funds": "non_idempotent",
    "push_files": "non_idempotent",
    "notify": "non_idempotent",
}


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str
    injector: str
    tools: list[dict[str, Any]]
    tool: str
    arguments: Any
    #: What should execute exactly once; None = nothing should execute.
    intended: dict[str, Any] | None
    #: Server action by call index (0 = first call of the invocation, 1 = a retry).
    plan: dict[int, ServerAction] = field(default_factory=dict)
    #: Listing swapped in after the relay has synced (no notification).
    tools_after: list[dict[str, Any]] | None = None
    #: Raw line the client sends instead of a well-formed request (C).
    raw_line: bytes | None = None
    #: The server process dies while idle, before the call is sent (E).
    kill_before_call: bool = False
    #: Every restart attempt fails (the server cannot be started again).
    restart_fails: bool = False
    timeout: float = 1.5
    note: str = ""


@dataclass(frozen=True)
class Corpus:
    tools: Mapping[str, dict[str, Any]]
    calls: list[dict[str, Any]]
    provenance: str


def drift_corpus() -> Corpus:
    data = json.loads(DRIFT_CORPUS.read_text())
    return Corpus(
        tools=data["tools"],
        calls=data["calls"],
        provenance=(
            "synthetic: tests/adapters/mcp/corpus/drift_corpus.json -- 14 tool definitions "
            "modelled on real MCP servers, 37 hand-labelled drifted/valid calls"
        ),
    )


def _with_defaults(tool: Mapping[str, Any], arguments: Any) -> Any:
    """Top-level declared defaults filled -- what a server applies to omitted args."""
    if not isinstance(arguments, dict):
        return arguments
    properties = (tool.get("inputSchema") or {}).get("properties") or {}
    filled = dict(arguments)
    for name, sub in properties.items():
        if name not in filled and isinstance(sub, dict) and "default" in sub:
            filled[name] = sub["default"]
    return filled


def equivalent(tool: Mapping[str, Any], executed: Any, intended: Any) -> bool:
    """Same call once the server's own defaults are applied."""
    return bool(_with_defaults(tool, executed) == _with_defaults(tool, intended))


def build(seed: int = 0, *, corpus: Corpus | None = None, per_fault: int = 8) -> list[Scenario]:
    corpus = corpus or drift_corpus()
    rng = random.Random(seed)
    tools = [copy.deepcopy(t) for t in corpus.tools.values()]
    scenarios: list[Scenario] = []

    # -- A: the model's arguments drift from the schema ----------------------
    for case in corpus.calls:
        intended = case.get("forward") if case["class"] in ("repairable", "unchanged") else None
        scenarios.append(
            Scenario(
                id=f"A:{case['name']}",
                category="A",
                injector="model_drift",
                tools=tools,
                tool=case["tool"],
                arguments=copy.deepcopy(case["arguments"]),
                intended=copy.deepcopy(intended),
                note=case["class"] + (" (known gap)" if case.get("known_gap") else ""),
            )
        )

    # Valid calls carry every non-A fault, so the fault is measured alone.
    valid = [
        c
        for c in corpus.calls
        if c["class"] in ("unchanged", "repairable") and c.get("forward") and not c.get("known_gap")
    ]

    def pick() -> dict[str, Any]:
        return copy.deepcopy(rng.choice(valid))

    def fault(category: str, injector: str, plan: dict[int, ServerAction], **extra: Any) -> None:
        for i in range(per_fault):
            case = pick()
            scenarios.append(
                Scenario(
                    id=f"{category}:{injector}:{i}",
                    category=category,
                    injector=injector,
                    tools=tools,
                    tool=case["tool"],
                    arguments=case["forward"],
                    intended=case["forward"],
                    plan=plan,
                    **extra,
                )
            )

    # -- B: the server's schema changes and nobody is told -------------------
    for i, (before, after, arguments) in enumerate(_staleness_pairs()):
        scenarios.append(
            Scenario(
                id=f"B:schema_drift_midrun:{i}",
                category="B",
                injector="schema_drift_midrun",
                tools=[before],
                tools_after=[after],
                tool=after["name"],
                arguments=arguments,
                intended=arguments,
            )
        )
    removed = pick()
    scenarios.append(
        Scenario(
            id="B:tool_removed",
            category="B",
            injector="tool_removed",
            tools=tools,
            tools_after=[t for t in tools if t["name"] != removed["tool"]],
            tool=removed["tool"],
            arguments=removed["forward"],
            intended=None,
            note="the tool no longer exists; nothing may execute",
        )
    )

    # -- C: malformed client envelope -- must pass through untouched ----------
    for i in range(per_fault):
        case = pick()
        line = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": RAW_LINE_ID,
                "method": "tools/call",
                "params": {"name": case["tool"], "arguments": json.dumps(case["forward"])[:-1]},
            }
        ).encode()
        scenarios.append(
            Scenario(
                id=f"C:malformed_arguments:{i}",
                category="C",
                injector="malformed_arguments",
                tools=tools,
                tool=case["tool"],
                arguments=None,
                intended=None,
                raw_line=line,
                note="arguments is a truncated JSON string",
            )
        )

    act = ServerAction
    # The server exits while idle; the next call finds it gone. Nothing was in
    # flight, so a restart (where enabled) is safe.
    fault("E", "server_dies_idle", {}, timeout=1.5, kill_before_call=True)
    fault(
        "E",
        "server_dies_idle_restart_fails",
        {},
        timeout=1.5,
        kill_before_call=True,
        restart_fails=True,
    )
    fault("E", "stdio_kill_before_execute", {0: act.CLOSE_BEFORE_EXECUTE}, timeout=0.6)
    fault("F", "hang_then_recover", {0: act.HANG_BEFORE_EXECUTE}, timeout=0.6)
    fault("F", "transient_error", {0: act.TRANSIENT_ERROR})
    # A slow tool that succeeds: no failure at all, unless something gives up on it.
    fault("F", "slow_success", {0: act.DELAYED_RESPONSE}, timeout=0.6)
    fault("G", "execution_error_after_side_effect", {0: act.TOOL_ERROR})
    fault("H", "truncated_response", {0: act.GARBAGE}, timeout=0.6)
    fault("I", "response_lost_after_execute", {0: act.DROP_RESPONSE}, timeout=0.6)
    fault("I", "stdio_kill_after_execute", {0: act.CLOSE_AFTER_EXECUTE}, timeout=0.6)
    fault("J", "duplicate_response", {0: act.DUPLICATE_RESPONSE})
    # A result that breaks the tool's own outputSchema, delivered as a success.
    weather = {
        "name": "get_weather",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"temp_c": {"type": "number"}, "summary": {"type": "string"}},
            "required": ["temp_c"],
        },
        "x-structured": {"temp_c": 21.5, "summary": "clear"},
        "x-bad-structured": {"temp_c": "warm", "summary": 3},
    }
    for i in range(per_fault):
        scenarios.append(
            Scenario(
                id=f"H:output_schema_violation:{i}",
                category="H",
                injector="output_schema_violation",
                tools=[*tools, weather],
                tool="get_weather",
                arguments={"city": f"city-{i}"},
                intended={"city": f"city-{i}"},
                plan={0: act.BAD_STRUCTURED},
            )
        )

    # An expired credential stays expired: a retry sees the same answer.
    fault("K", "auth_expired", {0: act.AUTH_EXPIRED, 1: act.AUTH_EXPIRED})
    return scenarios


def _staleness_pairs() -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    s = {"type": "string"}

    def tool(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "name": "search_docs",
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }

    return [
        (tool({"query": s}, ["query"]), tool({"q": s}, ["q"]), {"q": "x"}),
        (
            tool({"query": s}, ["query"]),
            tool({"query": s, "repo": s}, ["query", "repo"]),
            {"query": "x", "repo": "a/b"},
        ),
        (
            tool({"query": s, "state": {"type": "string", "enum": ["open", "closed"]}}, ["query"]),
            tool(
                {"query": s, "state": {"type": "string", "enum": ["open", "closed", "all"]}},
                ["query"],
            ),
            {"query": "x", "state": "all"},
        ),
        (
            tool({"query": s, "page": {"type": "integer"}}, ["query"]),
            tool({"query": s, "page": {"type": "string"}}, ["query"]),
            {"query": "x", "page": "next"},
        ),
        (
            tool({"query": s, "limit": {"type": "integer", "maximum": 50}}, ["query"]),
            tool({"query": s, "limit": {"type": "integer", "maximum": 500}}, ["query"]),
            {"query": "x", "limit": 200},
        ),
    ]
