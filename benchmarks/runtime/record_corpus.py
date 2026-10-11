"""
Record the replay corpus: eight MCP sessions (A-H), each driven through the
relay with a recorder attached, against the in-process scripted server.

    python benchmarks/runtime/record_corpus.py [--out DIR]

Provenance: **fault_injection**. Every session is a synthetic workload with
deterministically injected faults; the recordings say so in their header,
and nothing that replays them may describe them as real traffic.

Each session is recorded the way an operator would capture it: in shadow
mode (StateGuard decides, changes nothing; the server sees what the client
sent) -- except the two recovery sessions (D, E), which are recorded with
the relay's recovery settings on, so the recording holds what a restarted
server actually did. The relay configuration goes into the header.

Some arguments and ``_meta`` deliberately carry credential-shaped values
(fake ones, marked ``FAKE``), so every recording exercises redaction; a test
checks that none of them reaches a file.

=====  ================================  =========================================
tape   scenario                          what happens
=====  ================================  =========================================
A      success                           valid calls; secrets in _meta / arguments
B      tool failure                      tool error after a side effect; transient
C      transport failure                 the server dies mid-call, before executing
D      server death / reconnect          the server dies idle; the relay restarts it
E      UNKNOWN after dispatch            a dropped response (deadline), then a
                                         death after executing (restart after-unknown)
F      schema / tool-list change         a parameter renamed, a tool removed, unannounced
G      protocol error                    unknown method, malformed request, auth error,
                                         an unparseable client line
H      StateGuard intervention           the hand-labelled drift corpus, plus a result
                                         that breaks its outputSchema
=====  ================================  =========================================
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import itertools
import json
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    __package__ = "runtime"

from stateguard.adapters.mcp.recording import Provenance, Recorder  # noqa: E402

from .harness import (  # noqa: E402
    META,
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)
from .recovery import _answer, _await_loss  # noqa: E402
from .replay import _await_restart_settled  # noqa: E402
from .scenarios import drift_corpus  # noqa: E402

OUT = Path(__file__).resolve().parent / "tapes"
CALL_TIMEOUT = 1.5

#: Fake credentials. Never real; a test asserts none appears in a recording.
FAKE_SECRETS = (
    "Bearer FAKEtoken0123456789abcdef",
    "ghp_FAKE0123456789abcdefghijklmnopqrstuv",
    "sk-FAKE-0123456789abcdefghijklmnop",
    "FAKE-plain-password-42",
)

SEARCH: dict[str, Any] = {
    "name": "search_issues",
    "description": "Search issues in the tracker",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
            "limit": {"type": "integer", "maximum": 100},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}
SEARCH_RENAMED = copy.deepcopy(SEARCH)
SEARCH_RENAMED["inputSchema"]["properties"]["q"] = SEARCH_RENAMED["inputSchema"]["properties"].pop(
    "query"
)
SEARCH_RENAMED["inputSchema"]["required"] = ["q"]
NOTE: dict[str, Any] = {
    "name": "write_note",
    "description": "Append a note (not idempotent)",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}
WEATHER: dict[str, Any] = {
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
BASIC = [SEARCH, NOTE, WEATHER]


@dataclass
class Call:
    tool: str
    arguments: Any
    #: What should execute exactly once (None: nothing should).
    intended: Any
    #: Server action for this call (by call order across server processes).
    action: ServerAction = ServerAction.NORMAL
    category: str = "A"
    note: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    bad_result: bool = False


@dataclass
class Event:
    """Something done to the environment between client steps."""

    kind: str  # kill_idle | set_tools | raw_line | unparsed_line | request
    tools: list[dict[str, Any]] | None = None
    line: bytes = b""
    method: str = ""
    category: str = ""


@dataclass
class Session:
    name: str
    title: str
    tools: list[dict[str, Any]]
    steps: list[Call | Event]
    mode: RelayMode = RelayMode.SHADOW
    call_deadline: float | None = None
    max_restarts: int = 0
    restart_policy: str = "idle-only"


def sessions() -> list[Session]:
    act = ServerAction
    corpus = drift_corpus()
    drift_calls = [
        Call(
            c["tool"],
            c["arguments"],
            c.get("forward") if c["class"] in ("repairable", "unchanged") else None,
            category="A",
            note=c["name"],
        )
        for c in corpus.calls
        if not c.get("known_gap")
    ]
    return [
        Session(
            "A_success",
            "success",
            BASIC,
            [
                Call(
                    "search_issues",
                    {"query": "crash on login", "state": "open"},
                    {"query": "crash on login", "state": "open"},
                    meta={"authorization": FAKE_SECRETS[0], "io.example/trace": FAKE_SECRETS[1]},
                ),
                Call(
                    "write_note",
                    {"text": f"rotate key {FAKE_SECRETS[2]} today"},
                    {"text": f"rotate key {FAKE_SECRETS[2]} today"},
                ),
                Call("get_weather", {"city": "Lisbon"}, {"city": "Lisbon"}),
            ],
        ),
        Session(
            "B_tool_failure",
            "tool failure",
            BASIC,
            [
                Call("write_note", {"text": "n1"}, {"text": "n1"}, act.TOOL_ERROR, "G"),
                Call("search_issues", {"query": "q"}, {"query": "q"}, act.TRANSIENT_ERROR, "F"),
                Call("search_issues", {"query": "r"}, {"query": "r"}),
            ],
        ),
        Session(
            "C_transport_failure",
            "transport failure",
            BASIC,
            [
                Call("search_issues", {"query": "a"}, {"query": "a"}),
                Call("write_note", {"text": "b"}, {"text": "b"}, act.CLOSE_BEFORE_EXECUTE, "E"),
                Call("write_note", {"text": "c"}, {"text": "c"}, category="E"),
            ],
        ),
        Session(
            "D_server_death_reconnect",
            "server death and reconnect",
            BASIC,
            [
                Call("write_note", {"text": "before"}, {"text": "before"}),
                Event("kill_idle", category="E"),
                Call("write_note", {"text": "after"}, {"text": "after"}, category="E"),
                Call("search_issues", {"query": "after"}, {"query": "after"}, category="E"),
            ],
            mode=RelayMode.AUTO,
            max_restarts=3,
        ),
        Session(
            "E_unknown_after_dispatch",
            "UNKNOWN after dispatch",
            BASIC,
            [
                Call("write_note", {"text": "a"}, {"text": "a"}),
                Call("write_note", {"text": "b"}, {"text": "b"}, act.DROP_RESPONSE, "I"),
                Call("write_note", {"text": "c"}, {"text": "c"}, act.CLOSE_AFTER_EXECUTE, "I"),
                Call("search_issues", {"query": "d"}, {"query": "d"}, category="E"),
            ],
            mode=RelayMode.AUTO,
            call_deadline=0.3,
            max_restarts=3,
            restart_policy="after-unknown",
        ),
        Session(
            "F_schema_change",
            "schema / tool-list change",
            BASIC,
            [
                Call("search_issues", {"query": "x"}, {"query": "x"}),
                Event("set_tools", tools=[SEARCH_RENAMED, WEATHER], category="B"),
                Call("search_issues", {"q": "x"}, {"q": "x"}, category="B"),
                Call("write_note", {"text": "gone"}, None, category="B", note="tool removed"),
            ],
        ),
        Session(
            "G_protocol_error",
            "protocol error",
            BASIC,
            [
                Event("request", method="tools/frobnicate", category="C"),
                Event(
                    "raw_line",
                    line=json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "raw-1",
                            "method": "tools/call",
                            "params": {"name": "search_issues", "arguments": '{"query": "x"'},
                        }
                    ).encode(),
                    category="C",
                ),
                Call("search_issues", {"query": "k"}, {"query": "k"}, act.AUTH_EXPIRED, "K"),
                Event("unparsed_line", line=b'{"jsonrpc": "2.0", "method": ', category="C"),
                Call("search_issues", {"query": "after"}, {"query": "after"}),
            ],
        ),
        Session(
            "H_stateguard_intervention",
            "StateGuard intervention",
            [*corpus.tools.values(), WEATHER],
            [
                *drift_calls,
                Call(
                    "get_weather",
                    {"city": "Oslo"},
                    {"city": "Oslo"},
                    act.BAD_STRUCTURED,
                    "H",
                    bad_result=True,
                ),
            ],
        ),
    ]


def record(session: Session, out: Path) -> Path:
    path = out / f"{session.name}.jsonl"
    sink = path.open("w", encoding="utf-8")
    recorder = Recorder(
        sink,
        provenance=Provenance.FAULT_INJECTION,
        session=session.name,
        deterministic=True,
        config={
            "mode": session.mode.value,
            "retry": "shadow",
            "restart": session.max_restarts,
            "restart_policy": session.restart_policy,
            "call_deadline": session.call_deadline,
            "listing_refresh": True,
            "unreadable": "forward",
        },
    )
    # Faults are keyed by the request id they are injected into, so a call
    # that is not part of the plan (a malformed line, a retry) never takes one.
    plan: dict[Any, ServerAction] = {}
    lock = threading.Lock()

    def faults(call: ServerCall) -> ServerAction:
        with lock:
            return plan.pop(call.message.get("id"), ServerAction.NORMAL)

    def on_fault(call: ServerCall, action: ServerAction) -> None:
        recorder.fault(action.value, id=call.message.get("id"), tool=call.name)

    current = {"tools": session.tools}

    def server() -> ScriptedServer:
        return ScriptedServer(
            [ServerTool(t) for t in current["tools"]], faults=faults, on_fault=on_fault
        )

    rig = Rig(
        server(),
        session.mode,
        listing_wait=1.0,
        call_deadline=session.call_deadline,
        server_factory=server,
        max_restarts=session.max_restarts,
        restart_policy=session.restart_policy,
        recorder=recorder,
    )
    next_id = itertools.count(1)

    def send(message: dict[str, Any] | bytes) -> None:
        line = message if isinstance(message, bytes) else json.dumps(message).encode()
        with contextlib.suppress(OSError, ValueError):
            rig._client_w.write(line + b"\n")
            rig._client_w.flush()

    def request(
        method: str,
        params: dict[str, Any],
        meta: dict[str, Any] | None = None,
        identifier: Any = None,
    ) -> Any:
        identifier = next(next_id) if identifier is None else identifier
        send(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": method,
                "params": {**params, "_meta": {**META, **(meta or {})}},
            }
        )
        return identifier

    def settle() -> None:
        if rig.server.dead:
            _await_loss(rig, session.mode)

    try:
        _answer(rig, request("server/discover", {}), CALL_TIMEOUT)
        rig.wait_synced()
        for step in session.steps:
            # Never send mid-restart: a request that arrives while the server
            # is being replaced is decided on timing (see TASKMAP, T13 findings).
            _await_restart_settled(rig)
            if isinstance(step, Event):
                if step.kind == "kill_idle":
                    recorder.fault("kill_idle")
                    rig.kill_server()
                    _await_loss(rig, session.mode)
                elif step.kind == "set_tools":
                    assert step.tools is not None
                    recorder.fault("set_tools", tools=[t["name"] for t in step.tools])
                    current["tools"] = step.tools
                    rig.server.set_tools([ServerTool(t) for t in step.tools])
                elif step.kind == "request":
                    _answer(rig, request(step.method, {}), CALL_TIMEOUT)
                elif step.kind == "raw_line":
                    send(step.line)
                    _answer(rig, json.loads(step.line)["id"], CALL_TIMEOUT)
                elif step.kind == "unparsed_line":
                    send(step.line)
                continue
            identifier = next(next_id)
            if step.action is not ServerAction.NORMAL:
                with lock:
                    plan[identifier] = step.action
            request(
                "tools/call",
                {"name": step.tool, "arguments": step.arguments},
                step.meta,
                identifier=identifier,
            )
            _answer(rig, identifier, CALL_TIMEOUT)
            recorder.label(
                identifier,
                tool=step.tool,
                intended=step.intended,
                category=step.category,
                kind=step.action.value,
                bad_result=step.bad_result,
            )
            settle()
        time.sleep(0.1)  # late answers, deadline expiries
    finally:
        rig.close()
        recorder.close()
        sink.close()
    if not recorder.healthy:
        raise RuntimeError(f"recording {session.name} failed: {recorder.failure_types}")
    return path


def main(argv: list[str] | None = None, *, select: Callable[[Session], bool] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record the A-H replay corpus.")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for session in sessions():
        if select is None or select(session):
            print(record(session, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
