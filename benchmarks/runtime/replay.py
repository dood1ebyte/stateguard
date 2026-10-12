"""
Replay recorded MCP traffic through the relay, against a server built from
the recording -- never against a real one.

    python benchmarks/runtime/replay.py [--tapes DIR] [--json OUT]

A recording (``stateguard.adapters.mcp.recording``) holds what the client
sent, what the server answered, what StateGuard decided, and -- for a
fault-injection recording -- which faults were injected. Replay turns it
back into a workload:

* the **client** is the recorded client lines, sent in recorded order with
  their recorded ids, one at a time (each request waits for its answer or a
  timeout before the next is sent);
* the **server** is a ``TapeServer``: a ``ScriptedServer`` that answers each
  ``tools/call`` with exactly what the recording says the server did with it
  -- the recorded response bytes (under the id it was sent), garbage, a
  silent drop, or closing its pipe -- and lists the tools the recording saw
  at that point in the session. An idle server death in the recording is
  replayed before the same client step.

The same recording then runs under every arm -- raw (StateGuard off),
shadow, auto, auto_ops -- and under the configuration it was recorded with
(``as_recorded``, the fidelity check).

Counterfactuals
---------------
A recording only holds what happened. When an arm sends the server something
the recording never saw -- arguments StateGuard repaired, a retry, a call
after a restart the recording never made -- the TapeServer answers it from
its model (the tool exists and its own argument check accepts -> success;
otherwise an error) and marks the answer ``io.stateguard/replay:
counterfactual``. Every such answer is counted and reported per arm: a
number that rests on a counterfactual is a prediction, not an observation.

Provenance
----------
Every report states where its recordings came from. Recordings made by this
repository's own generator (``record_corpus.py``) are fault injection on a
synthetic workload. **Record/replay infrastructure enables real-traffic
measurement but does not itself constitute real-traffic evidence.**

Safety
------
Replay never starts a process and never opens a socket: the TapeServer runs
in-process on OS pipes, and nothing in a recording names a command to run.
Side effects are a list in memory (``executed``).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import queue
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT / "benchmarks"))
    sys.path.insert(0, str(ROOT / "src"))
    __package__ = "runtime"

from stateguard.adapters.mcp import EffectPolicy  # noqa: E402
from stateguard.adapters.mcp.recording import (  # noqa: E402
    Direction,
    Provenance,
    Recorder,
    Recording,
    RecordingError,
    read_recording,
)

from .harness import (  # noqa: E402
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)
from .recovery import _await_loss  # noqa: E402
from .scenarios import SIMULATED_EFFECTS, _with_defaults, equivalent  # noqa: E402

TAPES = Path(__file__).resolve().parent / "tapes"
ARMS = ("raw", "shadow", "auto", "auto_ops")
#: Operator settings of the auto_ops arm (as in measure.py).
DEADLINE_ARM_SECONDS = 0.3
RESTARTS_ARM = 3
#: How long the replaying client waits for each answer.
STEP_TIMEOUT = 1.5
REPLAY_MARK = "io.stateguard/replay"
RESERVED_ID_PREFIXES = ("stateguard-relay:", "stateguard-retry:")
RETRY_PREFIX = "stateguard-retry:"
#: Effects for the auto_ops arm: the drift-corpus declarations plus the
#: replay workload's own tools.
REPLAY_EFFECTS: dict[tuple[str, str | None] | str, str] = {
    **SIMULATED_EFFECTS,
    "search_issues": "read_only",
    "get_weather": "read_only",
    "write_note": "non_idempotent",
}
#: Fault actions under which the scripted server executed the call.
EXECUTING = frozenset(
    {
        ServerAction.NORMAL,
        ServerAction.TOOL_ERROR,
        ServerAction.DROP_RESPONSE,
        ServerAction.CLOSE_AFTER_EXECUTE,
        ServerAction.GARBAGE,
        ServerAction.DUPLICATE_RESPONSE,
        ServerAction.DELAYED_RESPONSE,
        ServerAction.BAD_STRUCTURED,
    }
)
#: An unparseable line, for replaying one the recording could not parse.
GARBAGE_LINE = b'{"io.stateguard/replay": "unparsed line"'


class ReplayRefusedError(RecordingError):
    """The recording cannot be replayed faithfully; nothing was run."""


def _key(identifier: Any) -> str:
    return json.dumps(identifier, sort_keys=True)


def _json(value: Any) -> str:
    """Exact JSON identity: 2 and 2.0, or 1 and true, are different arguments."""
    return json.dumps(value, sort_keys=True)


# ---------------------------------------------------------------------------
# Recording -> environment
# ---------------------------------------------------------------------------


@dataclass
class Step:
    index: int
    seq: int
    line: bytes
    id: Any
    method: str | None
    tool: str | None
    arguments: Any
    expects_answer: bool


@dataclass
class Attempt:
    """One tools/call the recorded server received, and what it did."""

    wire_id: Any
    gen: int
    seq: int
    tool: Any
    arguments: Any
    responses: list[dict[str, Any]] = field(default_factory=list)
    garbage: bool = False
    closed: bool = False
    action: str | None = None
    executes: bool = False


@dataclass
class TapeEnvironment:
    recording: Recording
    steps: list[Step]
    listings: list[tuple[int, list[dict[str, Any]]]]
    attempts: dict[str, list[Attempt]]
    other_responses: dict[str, list[dict[str, Any]]]
    deaths_before: set[int]
    restart_possible: bool
    restart_observed: bool
    labels: dict[str, dict[str, Any]]
    #: (id key, action, auto_action) for every recorded StateGuard call decision.
    recorded_decisions: list[tuple[str, Any, Any]]
    #: Coarse client-visible answer per request id, as recorded.
    recorded_answers: dict[str, str]
    config: dict[str, Any]

    @property
    def session(self) -> str:
        return self.recording.session

    def listing_for_step(self, index: int) -> list[dict[str, Any]]:
        """The tools the server listed while the client's step *index* was handled."""
        if not self.listings:
            return []
        limit = self.steps[index + 1].seq if index + 1 < len(self.steps) else None
        current = self.listings[0][1]
        for seq, tools in self.listings:
            if limit is not None and seq > limit:
                break
            current = tools
        return current

    def attempt(self, client_key: str, n: int) -> Attempt | None:
        attempts = self.attempts.get(client_key, [])
        return attempts[n] if n < len(attempts) else None

    def tool_definition(self, name: Any) -> dict[str, Any]:
        for _, tools in reversed(self.listings):
            for tool in tools:
                if tool.get("name") == name:
                    return tool
        return {}


def _coarse(message: dict[str, Any]) -> str:
    """A client-visible answer class, from a (redacted) response message."""
    if "error" in message:
        error = message.get("error")
        data = error.get("data") if isinstance(error, dict) else None
        state = data.get("io.stateguard/executionState") if isinstance(data, dict) else None
        return state if state in ("unknown", "not_sent") else "protocol_error"
    result = message.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        return "tool_error"
    return "success"


def derive(recording: Recording, *, allow_incomplete: bool = False) -> TapeEnvironment:
    """
    Build a replay environment. Refuses (``ReplayRefusedError``) a recording
    that cannot be replayed faithfully: incomplete, a client request id that
    collides with the relay's own id namespace, or a request id used twice.
    """
    if not recording.complete and not allow_incomplete:
        raise ReplayRefusedError("recording is incomplete (the recorder stopped or was not closed)")
    steps: list[Step] = []
    seen_ids: set[str] = set()
    for record in recording.messages(Direction.CLIENT_TO_RELAY):
        message = record.get("message")
        identifier = record.get("id")
        if isinstance(identifier, str) and identifier.startswith(RESERVED_ID_PREFIXES):
            raise ReplayRefusedError(
                f"client request id at seq {record['seq']} uses the relay's reserved id prefix"
            )
        is_request = record["kind"] == "request"
        if is_request:
            key = _key(identifier)
            if key in seen_ids:
                raise ReplayRefusedError(
                    f"client request id reused at seq {record['seq']}: replay keys by id"
                )
            seen_ids.add(key)
        if record["kind"] == "unparsed":
            line = GARBAGE_LINE
        else:
            line = json.dumps(message, separators=(",", ":")).encode()
        params = message.get("params") if isinstance(message, dict) else None
        steps.append(
            Step(
                index=len(steps),
                seq=record["seq"],
                line=line,
                id=identifier,
                method=record.get("method"),
                tool=record.get("tool"),
                arguments=params.get("arguments") if isinstance(params, dict) else None,
                expects_answer=is_request,
            )
        )

    listings: list[tuple[int, list[dict[str, Any]]]] = []
    pages: list[dict[str, Any]] = []
    for record in recording.messages(Direction.SERVER_TO_RELAY):
        result = (record.get("message") or {}).get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            continue
        pages.extend(t for t in result["tools"] if isinstance(t, dict))
        if not result.get("nextCursor"):
            listings.append((record["seq"], pages))
            pages = []

    audits = [(r["seq"], r["event"]) for r in recording.of_type("audit")]
    faults = recording.of_type("fault")
    fault_by_wire = {_key(f.get("id")): f["fault"] for f in faults if "id" in f}
    fault_injection = bool(faults)
    lost = [(seq, e) for seq, e in audits if e.get("event") == "server_lost"]

    attempts: dict[str, list[Attempt]] = defaultdict(list)
    by_wire: dict[str, Attempt] = {}
    responses_by_wire: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    unparsed_from_server: list[tuple[int, int]] = []
    for record in recording.messages(Direction.SERVER_TO_RELAY):
        if record["kind"] in ("response", "error"):
            responses_by_wire[(_key(record.get("id")), record["gen"])].append(record)
        elif record["kind"] == "unparsed":
            unparsed_from_server.append((record["seq"], record["gen"]))

    for record in recording.messages(Direction.RELAY_TO_SERVER):
        if record["kind"] != "request" or record.get("method") != "tools/call":
            continue
        message = record["message"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        wire = record.get("id")
        attempt = Attempt(
            wire_id=wire,
            gen=record["gen"],
            seq=record["seq"],
            tool=params.get("name"),
            arguments=params.get("arguments"),
        )
        if isinstance(wire, str) and wire.startswith(RETRY_PREFIX):
            # A retry carries the original request's params under a new id.
            client_key = next(
                (
                    k
                    for k, previous in reversed(list(attempts.items()))
                    if previous[-1].tool == attempt.tool
                    and previous[-1].arguments == attempt.arguments
                ),
                _key(wire),
            )
        else:
            client_key = _key(wire)
        later = [
            r
            for r in responses_by_wire.get((_key(wire), attempt.gen), [])
            if r["seq"] > attempt.seq
        ]
        attempt.responses = [r["message"] for r in later]
        attempts[client_key].append(attempt)
        by_wire[_key(wire)] = attempt

    pending = sorted(
        (a for a in by_wire.values() if not a.responses), key=lambda attempt: attempt.seq
    )
    for seq, gen in unparsed_from_server:
        for attempt in pending:
            if attempt.gen == gen and attempt.seq < seq and not attempt.garbage:
                attempt.garbage = True
                break
    for attempt in by_wire.values():
        action = fault_by_wire.get(_key(attempt.wire_id))
        attempt.action = action
        if attempt.responses or attempt.garbage:
            pass
        elif action is not None:
            # Ground truth: the injected fault says whether the pipe closed.
            attempt.closed = action in (
                ServerAction.CLOSE_BEFORE_EXECUTE.value,
                ServerAction.CLOSE_AFTER_EXECUTE.value,
            )
        else:
            # Observed only: the server died (server_lost for this generation)
            # with this request unanswered. Several may be unanswered; by
            # convention the most recent one is the request the server died
            # on, and earlier ones were dropped.
            death = next(
                (
                    seq
                    for seq, e in lost
                    if seq > attempt.seq and e.get("generation") == attempt.gen
                ),
                None,
            )
            attempt.closed = death is not None and not any(
                other.gen == attempt.gen and attempt.seq < other.seq < death
                for other in by_wire.values()
            )
        if action is not None and action in {a.value for a in ServerAction}:
            attempt.executes = ServerAction(action) in EXECUTING
        elif fault_injection:
            # No fault on this call: the scripted server acted normally, and
            # executed exactly when it answered success.
            attempt.executes = bool(attempt.responses) and _coarse(attempt.responses[0]) == (
                "success"
            )
        else:
            # Real traffic: whether it executed is not observable. Assume it
            # did unless the server refused it at the protocol level -- the
            # conservative side for counting duplicate executions.
            attempt.executes = not (
                attempt.responses and _coarse(attempt.responses[0]) == "protocol_error"
            )

    other: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        if step.expects_answer and step.method not in ("tools/call", "tools/list"):
            found = [
                r["message"]
                for r in recording.messages(Direction.SERVER_TO_RELAY)
                if r.get("id") == step.id and r["seq"] > step.seq
            ]
            if found:
                other[_key(step.id)] = found[:1]

    deaths: set[int] = set()
    for seq, event in lost:
        if event.get("in_flight_unknown") == 0:
            after = next((s.index for s in steps if s.seq > seq), None)
            if after is not None:
                deaths.add(after)

    events = [e for _, e in audits]
    labels = {_key(r["id"]): r for r in recording.of_type("label")}
    answers: dict[str, str] = {}
    for record in recording.messages(Direction.RELAY_TO_CLIENT):
        if record["kind"] in ("response", "error"):
            answers.setdefault(_key(record.get("id")), _coarse(record["message"]))
    return TapeEnvironment(
        recording=recording,
        steps=steps,
        listings=listings,
        attempts=dict(attempts),
        other_responses=other,
        deaths_before=deaths,
        restart_possible=not any(e.get("event") == "restart_failed" for e in events)
        and not any(f["fault"] == "restart_fails" for f in faults),
        restart_observed=any(e.get("event") == "server_restarted" for e in events),
        labels=labels,
        recorded_decisions=[
            (_key(e.get("id")), e.get("action"), e.get("auto_action"))
            for e in events
            if e.get("event") == "call"
        ],
        recorded_answers=answers,
        config=dict(recording.header.get("config") or {}),
    )


# ---------------------------------------------------------------------------
# The server, from the recording
# ---------------------------------------------------------------------------


@dataclass
class ReplayState:
    """Shared by every TapeServer of one replay run (one per server process)."""

    resolve: Any = None  # wire id -> client id (the relay's retry mapping)
    step: int = 0
    attempts_seen: Counter[str] = field(default_factory=Counter)
    #: client id key -> [(generation, arguments)] for every execution.
    executed: dict[str, list[tuple[int, Any]]] = field(default_factory=lambda: defaultdict(list))
    counterfactual: Counter[str] = field(default_factory=Counter)
    servers_started: int = 0
    counterfactual_restarts: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class TapeServer(ScriptedServer):
    env: Any = None
    state: Any = None
    generation: int = 0

    def _client_key(self, identifier: Any) -> str:
        resolve = self.state.resolve
        return _key(resolve(identifier) if resolve is not None else identifier)

    def _handle(self, line: bytes, writer: IO[bytes]) -> bool:
        with contextlib.suppress(ValueError):
            message = json.loads(line)
            if (
                isinstance(message, dict)
                and "id" in message
                and message.get("method") not in (None, "tools/call", "tools/list")
            ):
                recorded = self.env.other_responses.get(_key(message["id"]))
                if recorded:
                    self._write(writer, json.dumps({**recorded[0], "id": message["id"]}).encode())
                    return True
        return super()._handle(line, writer)

    def _call(
        self, identifier: Any, params: dict[str, Any], message: dict[str, Any], writer: IO[bytes]
    ) -> bool:
        name, arguments = params.get("name"), params.get("arguments")
        key = self._client_key(identifier)
        with self.state.lock:
            n = self.state.attempts_seen[key]
            self.state.attempts_seen[key] += 1
        recorded = self.env.attempt(key, n)
        if recorded is not None and recorded.tool == name and self._same_call(recorded, arguments):
            return self._replay(recorded, identifier, name, arguments, message, writer, key)
        return self._counterfactual(identifier, name, arguments, message, writer, key)

    def _same_call(self, recorded: Attempt, arguments: Any) -> bool:
        """
        The call the recording saw, as the server would see it: equal once the
        tool's declared defaults are applied (a relay that fills a default
        sends the server nothing new).
        """
        if _json(recorded.arguments) == _json(arguments):
            return True
        tool = self.env.tool_definition(recorded.tool)
        return bool(tool) and _json(_with_defaults(tool, recorded.arguments)) == _json(
            _with_defaults(tool, arguments)
        )

    def _execute(self, name: Any, arguments: Any, message: dict[str, Any], key: str) -> None:
        with self.state.lock:
            self.executed.append(ServerCall(len(self.executed), str(name), arguments, message))
            self.state.executed[key].append((self.generation, arguments))

    def _replay(
        self,
        recorded: Attempt,
        identifier: Any,
        name: Any,
        arguments: Any,
        message: dict[str, Any],
        writer: IO[bytes],
        key: str,
    ) -> bool:
        if recorded.executes:
            self._execute(name, arguments, message, key)
        if recorded.closed:
            self.dead = True
            return False
        if recorded.garbage:
            self._write(writer, GARBAGE_LINE)
            return True
        for body in recorded.responses:
            self._write(writer, json.dumps({**body, "id": identifier}).encode())
        return True

    def _counterfactual(
        self,
        identifier: Any,
        name: Any,
        arguments: Any,
        message: dict[str, Any],
        writer: IO[bytes],
        key: str,
    ) -> bool:
        with self.state.lock:
            self.state.counterfactual[key] += 1
        tool = next((t for t in self.tools if t.name == name), None)
        mark = {REPLAY_MARK: "counterfactual"}
        if tool is None or not tool.accepts(arguments):
            text = f"Unknown tool: {name}" if tool is None else f"Invalid arguments for {name}"
            result: dict[str, Any] = {
                "resultType": "complete",
                "content": [{"type": "text", "text": text}],
                "isError": True,
                "_meta": mark,
            }
        else:
            self._execute(name, arguments, message, key)
            result = {
                "resultType": "complete",
                "content": [
                    {"type": "text", "text": json.dumps({"tool": name, "arguments": arguments})}
                ],
                "isError": False,
                "_meta": mark,
            }
            if "outputSchema" in tool.definition:
                result["structuredContent"] = tool.definition.get("x-structured", {})
        self._reply(writer, identifier, result)
        return True


# ---------------------------------------------------------------------------
# Running one arm
# ---------------------------------------------------------------------------


@dataclass
class StepOutcome:
    index: int
    id: Any
    method: str | None
    tool: str | None
    answer: str
    executions: int
    executed_on: list[int]
    counterfactual: int
    duplicate_answers: bool
    latency_ms: float | None
    retries: int
    #: Ground truth from a label: True / False, or None without one.
    ok: bool | None
    #: Raw-comparable hidden failure: silent, phantom, wrong or repeated execution.
    hidden: bool


@dataclass
class ArmRun:
    session: str
    arm: str
    provenance: str
    steps: list[StepOutcome]
    internal_errors: int
    restarts: int
    reconnect_attempted: int
    reconnect_failed: int
    retries: int
    result_findings: int
    counterfactual_restarts: int
    decisions: list[tuple[str, Any, Any]]
    wall_ms: float
    recording_bytes: int = 0
    #: The relay's audit events for the run (for the T14 race measurement).
    audit: list[dict[str, Any]] = field(default_factory=list)
    #: tools/call messages that reached any server process.
    tools_call_sent: int = 0


def _arm_settings(arm: str, env: TapeEnvironment) -> dict[str, Any]:
    if arm == "as_recorded":
        config = env.config
        mode = {"passthrough": RelayMode.PASSTHROUGH, "shadow": RelayMode.SHADOW}.get(
            str(config.get("mode")), RelayMode.AUTO
        )
        return {
            "mode": mode,
            "call_deadline": config.get("call_deadline"),
            "effects": None,
            "retry": config.get("retry", "shadow"),
            "max_restarts": int(config.get("restart") or 0),
            "restart_policy": config.get("restart_policy", "idle-only"),
        }
    ops = arm == "auto_ops"
    return {
        "mode": {"raw": RelayMode.PASSTHROUGH, "shadow": RelayMode.SHADOW}.get(arm, RelayMode.AUTO),
        "call_deadline": DEADLINE_ARM_SECONDS if ops else None,
        "effects": EffectPolicy(REPLAY_EFFECTS) if ops else None,
        "retry": "auto" if ops else "shadow",
        "max_restarts": RESTARTS_ARM if ops else 0,
        "restart_policy": "idle-only",
    }


def _classify(rig: Rig, identifier: Any, answer: dict[str, Any] | None, eof_before: bool) -> str:
    if eof_before:
        return "unsent"
    if answer is None:
        return "connection_closed" if rig.client_eof.is_set() else "silent"
    if "error" in answer:
        return _coarse(answer)
    result = answer.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        invocation = rig.relay.ledger.get(identifier)
        if invocation is not None and invocation.state.value == "answered_locally":
            return "refused_by_stateguard"
        return "tool_error"
    return "success"


def run_arm(
    env: TapeEnvironment,
    arm: str,
    *,
    recorder: Recorder | None = None,
    step_timeout: float = STEP_TIMEOUT,
    race_provoking: bool = False,
) -> ArmRun:
    """
    Replay *env* under *arm*. ``race_provoking`` sends each client line as
    soon as the previous answer arrived -- even while the relay is still
    restarting a server, as a real client would -- instead of waiting for the
    restart to settle. That is the ordering under which T13 found the
    mid-restart race (TASKMAP, T14).
    """
    settings = _arm_settings(arm, env)
    mode: RelayMode = settings["mode"]
    state = ReplayState()

    def server() -> TapeServer:
        with state.lock:
            generation = state.servers_started
            state.servers_started += 1
        return TapeServer(
            [ServerTool(t) for t in env.listing_for_step(state.step)],
            env=env,
            state=state,
            generation=generation,
        )

    def factory() -> TapeServer:
        if not env.restart_possible:
            raise OSError("the recording shows the server could not be restarted")
        if not env.restart_observed:
            with state.lock:
                state.counterfactual_restarts += 1
        return server()

    started = time.perf_counter()
    rig = Rig(
        server(),
        mode,
        listing_wait=1.0,
        call_deadline=settings["call_deadline"],
        effects=settings["effects"],
        retry=settings["retry"],
        server_factory=factory,
        max_restarts=settings["max_restarts"],
        restart_policy=settings["restart_policy"],
        recorder=recorder,
    )
    relay = rig.relay
    state.resolve = lambda wire: relay._client_of.get(wire, wire)
    answers: dict[int, tuple[dict[str, Any] | None, bool, float | None]] = {}
    try:
        for step in env.steps:
            state.step = step.index
            if step.index in env.deaths_before and not rig.server.dead:
                rig.kill_server()
                _await_loss(rig, mode)
            if not race_provoking:
                _await_restart_settled(rig)
            rig.server.set_tools([ServerTool(t) for t in env.listing_for_step(step.index)])
            eof_before = rig.client_eof.is_set()
            began = time.perf_counter()
            with contextlib.suppress(OSError, ValueError):  # the relay may be gone
                rig._client_w.write(step.line + b"\n")
                rig._client_w.flush()
            if not step.expects_answer:
                time.sleep(0.02)
                continue
            answer = None if eof_before else _await_answer(rig, step.id, step_timeout)
            latency = (time.perf_counter() - began) * 1000 if answer is not None else None
            answers[step.index] = (answer, eof_before, latency)
            if rig.server.dead:
                _await_loss(rig, mode)
        time.sleep(0.05)  # late or duplicate answers
    finally:
        rig.close()
    wall = (time.perf_counter() - started) * 1000

    audit = rig.audit
    retries_by_id: Counter[str] = Counter(
        _key(e.get("id")) for e in audit if e.get("event") == "retry_decision" and e.get("acted")
    )
    outcomes: list[StepOutcome] = []
    for step in env.steps:
        if not step.expects_answer:
            continue
        answer, eof_before, latency = answers.get(step.index, (None, True, None))
        key = _key(step.id)
        executions = state.executed.get(key, [])
        duplicate = (
            sum(
                1
                for m in rig.client_received
                if isinstance(m, dict) and m.get("id") == step.id and "method" not in m
            )
            > 1
        )
        kind = _classify(rig, step.id, answer, eof_before)
        ok, hidden = _judge(env, step, kind, executions, duplicate)
        outcomes.append(
            StepOutcome(
                index=step.index,
                id=step.id,
                method=step.method,
                tool=step.tool,
                answer=kind,
                executions=len(executions),
                executed_on=[g for g, _ in executions],
                counterfactual=state.counterfactual.get(key, 0),
                duplicate_answers=duplicate,
                latency_ms=latency,
                retries=retries_by_id.get(key, 0),
                ok=ok,
                hidden=hidden,
            )
        )
    return ArmRun(
        session=env.session,
        arm=arm,
        provenance=env.recording.provenance.value,
        steps=outcomes,
        internal_errors=sum(1 for e in audit if e.get("category") == "Z"),
        restarts=sum(1 for e in audit if e.get("event") == "server_restarted"),
        reconnect_attempted=sum(
            1 for e in audit if e.get("event") in ("server_restarted", "restart_failed")
        ),
        reconnect_failed=sum(
            1 for e in audit if e.get("event") in ("restart_failed", "reinit_failed")
        ),
        retries=sum(retries_by_id.values()),
        result_findings=sum(
            1 for e in audit if e.get("event") == "result_check" and e.get("category") == "H"
        ),
        counterfactual_restarts=state.counterfactual_restarts,
        decisions=[
            (_key(e.get("id")), e.get("action"), e.get("auto_action"))
            for e in audit
            if e.get("event") == "call"
        ],
        wall_ms=wall,
        audit=list(audit),
        tools_call_sent=sum(1 for line in rig.all_received if b'"tools/call"' in line),
    )


def unsafe_bypasses(audit: list[dict[str, Any]]) -> int:
    """
    T14's bypass, counted from a relay audit log: a ``tools/call`` decision
    taken as "forward unvalidated, listing unconfirmed" after the server was
    lost or a listing request could not be written since the previous
    decision. That is the path on which a call skipped StateGuard's repair
    because a restart was under way.
    """
    count = 0
    lost_since_last_call = False
    for event in audit:
        name = event.get("event")
        if name in ("server_lost", "tools_list_unsent"):
            lost_since_last_call = True
        elif name == "call":
            if event.get("action") == "unvalidated" and event.get("listing") == "unconfirmed":
                count += 1 if lost_since_last_call else 0
            lost_since_last_call = False
    return count


def _await_restart_settled(rig: Rig, timeout: float = 3.0) -> None:
    """
    Sequential replay: never send the next client line while the relay is
    still restarting a server, or while it is stopping -- the recording's
    order (restart and re-listing, or the relay's exit, then the next
    request) is kept. A request that arrives mid-restart can be
    decided differently (see the T13 findings in TASKMAP), which would make
    replay depend on thread timing.
    """
    deadline = time.monotonic() + timeout
    while rig.relay._link == "restarting" and time.monotonic() < deadline:
        time.sleep(0.005)
    if rig.relay._link == "down" and not rig.relay._client_gone:
        # The relay is stopping (no restart): let it finish, so the next line
        # meets a closed relay every time, not sometimes a "not sent" answer.
        rig.client_eof.wait(max(0.0, deadline - time.monotonic()))
    elif rig.relay.generation > 0:
        rig.relay._listing_ready.wait(max(0.0, deadline - time.monotonic()))


def _await_answer(rig: Rig, identifier: Any, timeout: float) -> dict[str, Any] | None:
    """
    The answer to *identifier*, taken off the client's queue the moment it
    arrives (so latency is not quantised by polling). Other messages --
    late answers to earlier requests -- are skipped; they stay in
    ``client_received`` for duplicate counting.
    """
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            message = rig._responses.get(timeout=min(remaining, 0.02))
        except queue.Empty:
            if rig.client_eof.is_set():
                return None
            continue
        if (
            isinstance(message, dict)
            and message.get("id") == identifier
            and "method" not in message
        ):
            return message
    return None


def _judge(
    env: TapeEnvironment,
    step: Step,
    kind: str,
    executions: list[tuple[int, Any]],
    duplicate: bool,
) -> tuple[bool | None, bool]:
    """(ok against the label, hidden failure) for one request."""
    label = env.labels.get(_key(step.id))
    success = kind == "success"
    silent = kind in ("silent", "connection_closed", "unsent")
    if label is None or step.method != "tools/call":
        return None, silent or duplicate or len(executions) > 1
    intended = label.get("intended")
    if intended is None:
        wrong = bool(executions)
        return (not executions and not success and not silent), silent or wrong or success
    tool = env.tool_definition(step.tool)
    matching = [a for _, a in executions if equivalent(tool, a, intended)]
    bad_result = bool(label.get("bad_result"))
    ok = (
        len(matching) == 1 and len(executions) == 1 and success and not duplicate and not bad_result
    )
    phantom = success and not matching
    wrong = len(executions) > len(matching)
    hidden = (
        silent or phantom or wrong or len(matching) > 1 or duplicate or (success and bad_result)
    )
    return ok, hidden


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _p50(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def summarise(
    envs: list[TapeEnvironment],
    runs: list[ArmRun],
    *,
    determinism: dict[str, Any],
    overhead: dict[str, Any],
    parse_ms: dict[str, float],
) -> dict[str, Any]:
    raw = {(r.session, s.index): s for r in runs if r.arm == "raw" for s in r.steps}
    arms: dict[str, Any] = {}
    for arm in (*ARMS, "as_recorded"):
        mine = [r for r in runs if r.arm == arm]
        steps = [(r.session, s) for r in mine for s in r.steps]
        labelled = [(sess, s) for sess, s in steps if s.ok is not None]
        block: dict[str, Any] = {
            "invocations": len(steps),
            "labelled": len(labelled),
            "answers": dict(sorted(Counter(s.answer for _, s in steps).items())),
            "unknown": sum(1 for _, s in steps if s.answer == "unknown"),
            "not_sent": sum(1 for _, s in steps if s.answer == "not_sent"),
            "duplicate_executions": sum(max(0, s.executions - 1) for _, s in steps),
            "duplicate_answers": sum(1 for _, s in steps if s.duplicate_answers),
            "failures": sum(1 for _, s in labelled if not s.ok),
            "hidden_failures": sum(1 for _, s in steps if s.hidden),
            "counterfactual_answers": sum(s.counterfactual for _, s in steps),
            "counterfactual_restarts": sum(r.counterfactual_restarts for r in mine),
            "restarts": sum(r.restarts for r in mine),
            "reconnect_attempted": sum(r.reconnect_attempted for r in mine),
            "reconnect_failed": sum(r.reconnect_failed for r in mine),
            "retries": sum(r.retries for r in mine),
            "result_findings": sum(r.result_findings for r in mine),
            "internal_errors_z": sum(r.internal_errors for r in mine),
            "latency_p50_ms": _p50([s.latency_ms for _, s in steps if s.latency_ms is not None]),
        }
        if arm != "raw":
            prevented = recovered = escalated = induced = 0
            prevented_cf = recovered_cf = 0
            for session, s in labelled:
                base = raw.get((session, s.index))
                if base is None or base.ok is None:
                    continue
                if not base.ok and s.ok:
                    if s.retries or any(g > 0 for g in s.executed_on):
                        recovered += 1
                        recovered_cf += 1 if s.counterfactual else 0
                    else:
                        prevented += 1
                        prevented_cf += 1 if s.counterfactual else 0
                elif not base.ok and not s.ok and base.hidden and not s.hidden:
                    escalated += 1
                elif base.ok and not s.ok:
                    induced += 1
            block.update(
                prevented=prevented,
                prevented_on_counterfactual=prevented_cf,
                recovered=recovered,
                recovered_on_counterfactual=recovered_cf,
                safely_escalated=escalated,
                stateguard_induced=induced,
            )
        arms[arm] = block
    raw_p50 = arms["raw"]["latency_p50_ms"]
    for arm in arms:
        p50 = arms[arm]["latency_p50_ms"]
        arms[arm]["latency_overhead_p50_ms"] = (
            round(p50 - raw_p50, 2) if p50 is not None and raw_p50 is not None else None
        )

    fidelity = _fidelity(envs, runs)
    by_session: dict[str, dict[str, Any]] = {}
    for run in runs:
        labelled_steps = [s for s in run.steps if s.ok is not None]
        by_session.setdefault(run.session, {})[run.arm] = {
            "failures": sum(1 for s in labelled_steps if not s.ok),
            "labelled": len(labelled_steps),
            "hidden": sum(1 for s in run.steps if s.hidden),
            "unknown": sum(1 for s in run.steps if s.answer == "unknown"),
            "counterfactual": sum(s.counterfactual for s in run.steps),
        }
    return {
        "corpus": _corpus(envs, parse_ms),
        "arms": arms,
        "by_session": by_session,
        "fidelity": fidelity,
        "determinism": determinism,
        "overhead": overhead,
        "limitations": limitations(envs),
    }


def _fidelity(envs: list[TapeEnvironment], runs: list[ArmRun]) -> dict[str, Any]:
    """Replaying under the recorded configuration: does it reproduce the recording?"""
    by_session = {e.session: e for e in envs}
    answers_same = answers_total = decisions_same = decisions_total = 0
    diverged: list[str] = []
    for run in runs:
        if run.arm != "as_recorded":
            continue
        env = by_session[run.session]
        for s in run.steps:
            recorded = env.recorded_answers.get(_key(s.id))
            if _reproduced(recorded, s.answer):
                answers_same += 1
            else:
                diverged.append(f"{run.session}#{s.index}: {recorded} -> {s.answer}")
            answers_total += 1
        replayed = Counter(run.decisions)
        recorded_decisions = Counter(env.recorded_decisions)
        decisions_total += sum(recorded_decisions.values())
        decisions_same += sum((replayed & recorded_decisions).values())
    return {
        "answers_reproduced": answers_same,
        "answers_total": answers_total,
        "decisions_reproduced": decisions_same,
        "decisions_total": decisions_total,
        "diverged": diverged,
    }


def _reproduced(recorded: str | None, replayed: str) -> bool:
    """Same client-visible answer; a relay refusal is recorded as a tool error."""
    if recorded is None:
        return replayed in ("silent", "connection_closed", "unsent")
    return recorded == replayed or (
        recorded == "tool_error" and replayed == "refused_by_stateguard"
    )


def _corpus(envs: list[TapeEnvironment], parse_ms: dict[str, float]) -> dict[str, Any]:
    provenances = Counter(e.recording.provenance.value for e in envs)
    real = all(e.recording.provenance is Provenance.REAL_CAPTURE for e in envs) and envs
    categories: Counter[str] = Counter()
    for env in envs:
        for label in env.labels.values():
            if isinstance(label.get("category"), str):
                categories[label["category"]] += 1
    return {
        "provenance": dict(provenances),
        "evidence": (
            Provenance.REAL_CAPTURE.evidence
            if real
            else "NOT real-world evidence: "
            + "; ".join(sorted({Provenance(p).evidence for p in provenances}))
        ),
        "sessions": len(envs),
        "invocations": sum(1 for e in envs for s in e.steps if s.expects_answer),
        "tools_call_invocations": sum(1 for e in envs for s in e.steps if s.method == "tools/call"),
        "labelled_categories": dict(sorted(categories.items())),
        "redaction": {
            "modes": sorted({e.recording.header["redaction"]["mode"] for e in envs}),
            "values_redacted": sum(
                int((e.recording.end or {}).get("values_redacted", 0)) for e in envs
            ),
            "complete": all(e.recording.complete for e in envs),
        },
        "parse_and_derive_ms": {k: round(v, 2) for k, v in sorted(parse_ms.items())},
    }


LIMITATIONS = [
    "The corpus is generated by record_corpus.py: deterministic fault injection on a synthetic "
    "workload, recorded through the relay. It is not real traffic and these numbers are not "
    "real-world evidence.",
    "Replay is sequential: each client request waits for its answer before the next is sent. "
    "Concurrent client traffic is serialised.",
    "Timing is not replayed: a slow response in the recording is answered immediately, so "
    "deadline-related outcomes (and their Z cost) are not reproduced.",
    "A recording holds only what happened. Calls an arm sends that the recording never saw "
    "(repaired arguments, retries, calls after a restart that never happened) are answered "
    "from the TapeServer's model and counted as counterfactual -- predictions, not observations.",
    "Redaction masks free-form values; a StateGuard decision that depends on a masked value can "
    "differ on replay. The fidelity block counts how many recorded decisions and answers replay "
    "reproduced.",
    "Whether a call executed is ground truth only for fault-injection recordings; for real "
    "captures the TapeServer assumes execution unless the server refused at the protocol level.",
]


#: Replaces the first limitation when the corpus is observed traffic.
OBSERVED_CORPUS = (
    "The corpus is observed traffic ({provenances}). It has no ground truth: no request is "
    "labelled, so no failure is counted as prevented or recovered, and a call StateGuard would "
    "have repaired is not shown to have needed it. Fidelity and determinism are measured; "
    "outcome deltas are not. This is evidence collection, not proof of reliability improvement."
)


def limitations(envs: list[TapeEnvironment]) -> list[str]:
    """LIMITATIONS, with the corpus line describing the corpus actually replayed."""
    provenances = sorted({e.recording.provenance.value for e in envs})
    if envs and all(e.recording.provenance.observed for e in envs):
        return [OBSERVED_CORPUS.format(provenances=", ".join(provenances)), *LIMITATIONS[1:]]
    return list(LIMITATIONS)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def load_one(path: Path) -> TapeEnvironment:
    """Read, validate and derive one recording. Raises ``RecordingError`` only."""
    return derive(read_recording(path))


def load(tapes: Path) -> tuple[list[TapeEnvironment], dict[str, float]]:
    envs: list[TapeEnvironment] = []
    parse_ms: dict[str, float] = {}
    for path in sorted(tapes.glob("*.jsonl")):
        began = time.perf_counter()
        env = load_one(path)
        parse_ms[env.session] = (time.perf_counter() - began) * 1000
        envs.append(env)
    return envs, parse_ms


def _signature(run: ArmRun) -> list[tuple[Any, ...]]:
    return [
        (
            s.index,
            s.answer,
            s.executions,
            tuple(s.executed_on),
            s.counterfactual,
            s.duplicate_answers,
        )
        for s in run.steps
    ]


def measure(
    envs: list[TapeEnvironment],
    parse_ms: dict[str, float],
    *,
    repeat_for_determinism: bool = True,
    overhead_runs: int = 3,
) -> dict[str, Any]:
    runs: list[ArmRun] = []
    for env in envs:
        for arm in (*ARMS, "as_recorded"):
            runs.append(run_arm(env, arm))

    determinism: dict[str, Any] = {"checked": False}
    if repeat_for_determinism:
        mismatched = [
            f"{r.session}/{r.arm}"
            for r in runs
            if _signature(run_arm(next(e for e in envs if e.session == r.session), r.arm))
            != _signature(r)
        ]
        determinism = {"checked": True, "runs": len(runs), "mismatched": mismatched}

    overhead = recording_overhead(envs, runs=overhead_runs) if overhead_runs else {}
    return summarise(envs, runs, determinism=determinism, overhead=overhead, parse_ms=parse_ms)


def recording_overhead(envs: list[TapeEnvironment], *, runs: int = 3) -> dict[str, Any]:
    """
    Per-call latency of the auto arm without a recorder, with a synchronous
    one (redacts and writes in the call path) and with a background one
    (what ``--record`` uses).
    """
    latencies: dict[str, list[float]] = {"none": [], "synchronous": [], "background": []}
    size = 0
    for _ in range(runs):
        for env in envs:
            latencies["none"] += [s.latency_ms for s in run_arm(env, "auto").steps if s.latency_ms]
            for kind in ("synchronous", "background"):
                sink = io.StringIO()
                recorder = Recorder(
                    sink,
                    # What it records is a replay of ``env``, not new traffic.
                    provenance=Provenance.REPLAY,
                    session=env.session,
                    background=kind == "background",
                )
                run = run_arm(env, "auto", recorder=recorder)
                recorder.close()
                latencies[kind] += [s.latency_ms for s in run.steps if s.latency_ms]
                if kind == "synchronous":
                    size += len(sink.getvalue().encode())
    p50 = {kind: _p50(values) for kind, values in latencies.items()}
    base = p50["none"]
    return {
        "runs": runs,
        "calls_measured": len(latencies["none"]),
        "latency_p50_ms": p50,
        "overhead_p50_ms": {
            kind: round(value - base, 2) if value is not None and base is not None else None
            for kind, value in p50.items()
            if kind != "none"
        },
        "recording_bytes_per_session": round(size / max(1, runs * len(envs))),
        "note": "in-process pipes on one machine; the absolute values are not a network latency",
    }


def render(summary: dict[str, Any]) -> str:
    corpus = summary["corpus"]
    out = [
        "StateGuard record/replay measurement",
        "=" * 36,
        f"CORPUS: {corpus['evidence']}",
        f"provenance {corpus['provenance']}  sessions {corpus['sessions']}  "
        f"requests {corpus['invocations']} (tools/call {corpus['tools_call_invocations']})",
        f"redaction {corpus['redaction']}",
        f"labelled categories {corpus['labelled_categories']}",
        "",
        f"{'arm':12} {'fail':>4} {'hidden':>6} {'prev':>4} {'recov':>5} {'escal':>5} "
        f"{'Z':>3} {'unkn':>4} {'dupEx':>5} {'cf':>3} {'rst':>3} {'rtry':>4} {'p50ms':>6}",
    ]
    for arm, b in summary["arms"].items():
        out.append(
            f"{arm:12} {b['failures']:>4} {b['hidden_failures']:>6} "
            f"{b.get('prevented', '-'):>4} {b.get('recovered', '-'):>5} "
            f"{b.get('safely_escalated', '-'):>5} {b.get('stateguard_induced', '-'):>3} "
            f"{b['unknown']:>4} {b['duplicate_executions']:>5} {b['counterfactual_answers']:>3} "
            f"{b['restarts']:>3} {b['retries']:>4} {b['latency_p50_ms'] or '-':>6}"
        )
    out += [
        "",
        "of which on counterfactual answers: "
        + ", ".join(
            f"{arm} prevented {b['prevented_on_counterfactual']}/{b['prevented']} "
            f"recovered {b['recovered_on_counterfactual']}/{b['recovered']}"
            for arm, b in summary["arms"].items()
            if arm != "raw"
        ),
        "",
        "failures / labelled per session (raw -> shadow -> auto -> auto_ops | as_recorded):",
    ]
    for session, arms in summary["by_session"].items():
        cells = " -> ".join(
            f"{arms[a]['failures']}/{arms[a]['labelled']}" for a in ARMS if a in arms
        )
        recorded = arms.get("as_recorded", {})
        out.append(
            f"  {session:28} {cells} | {recorded.get('failures')}/{recorded.get('labelled')}"
        )
    f = summary["fidelity"]
    out += [
        "",
        f"fidelity (as_recorded): answers {f['answers_reproduced']}/{f['answers_total']}, "
        f"decisions {f['decisions_reproduced']}/{f['decisions_total']}",
        f"determinism: {summary['determinism']}",
        f"overhead: {summary['overhead']}",
        "",
        "Limitations:",
        *[f"- {line}" for line in summary["limitations"]],
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay recorded MCP traffic under every arm.")
    parser.add_argument("--tapes", type=Path, default=TAPES)
    parser.add_argument("--json", type=Path, help="Write the summary as JSON.")
    parser.add_argument("--no-determinism", action="store_true", help="Skip the second run.")
    parser.add_argument("--overhead-runs", type=int, default=3)
    args = parser.parse_args(argv)
    envs, parse_ms = load(args.tapes)
    if not envs:
        print(f"no recordings in {args.tapes}", file=sys.stderr)
        return 1
    summary = measure(
        envs,
        parse_ms,
        repeat_for_determinism=not args.no_determinism,
        overhead_runs=args.overhead_runs,
    )
    print(render(summary))
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
