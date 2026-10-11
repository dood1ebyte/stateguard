"""
Recovery workload: what happens to a whole session when its MCP server dies.

    python benchmarks/runtime/recovery.py [--per-type N] [--json OUT]

The single-invocation measurement (``measure.py``) shows what happens to the
call that meets a fault. A server death also decides the fate of every call
*after* it, so this workload runs deterministic multi-call sessions. Each
session is a sequence of calls to a side-effecting tool, with a server
failure injected at a fixed point. It runs in each arm:

=========================  ==================================================
``raw``                    the relay with StateGuard off: the baseline
``shadow``                 StateGuard observing (must equal raw)
``auto``                   StateGuard acting, restart off (the default)
``restart_idle``           ``--restart 3 --restart-policy idle-only``
``restart_after_unknown``  ``--restart 3 --restart-policy after-unknown``
=========================  ==================================================

Every invocation ends in exactly one outcome. They are kept apart on
purpose, so a fall in failures is attributed to recovery, containment,
visibility or plain disappearance:

======================  ======================================================
``succeeded``           executed exactly once, client told it succeeded, on
                        the server process the session started with
``recovered``           the same, but on a *restarted* server, after a
                        transport failure earlier in the session -- the
                        only outcome that counts as recovery
``unknown``             client told "outcome unknown" (it may have run);
                        contained, not resolved
``not_sent``            client told "not sent": certainly did not run
``connection_closed``   no answer; the client saw its server connection end
``silent``              no answer while the connection stayed open: hidden
``other_error``         any other error answer
======================  ======================================================

Counted on top, never folded into the above:

* ``replayed`` -- a request received by a server more than once (any
  server process);
* ``duplicate_execution`` -- a request executed more than once;
* ``stateguard_induced`` -- an invocation that succeeded in ``raw`` and did
  not in this arm (category Z).

Per session: transport failures (server processes that died), reconnects
attempted, succeeded (a restarted server that then served its tool list)
and failed.

**Synthetic.** A scripted in-process server; restarts are new in-process
servers on fresh pipes. Nothing here is a production rate.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    __package__ = "runtime"

from .harness import (  # noqa: E402
    META,
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)

ARMS = ("raw", "shadow", "auto", "restart_idle", "restart_after_unknown")
OUTCOMES = (
    "succeeded",
    "recovered",
    "unknown",
    "not_sent",
    "connection_closed",
    "silent",
    "other_error",
)
CALLS_PER_SESSION = 5
MAX_RESTARTS = 3
CALL_TIMEOUT = 1.5


def _tool(param: str = "text") -> dict[str, Any]:
    return {
        "name": "write_note",
        "description": "Append a note. Not idempotent.",
        "inputSchema": {
            "type": "object",
            "properties": {param: {"type": "string"}},
            "required": [param],
            "additionalProperties": False,
        },
    }


@dataclass(frozen=True)
class Session:
    id: str
    kind: str
    #: Index of the call the fault is attached to.
    at: int
    #: Server action for the call at ``at`` (in-flight deaths, hangs).
    action: ServerAction | None = None
    #: Kill the server while idle, just before call ``at``.
    idle_death: bool = False
    #: The restart factory raises every time.
    restart_fails: bool = False
    #: The first restarted server dies on start; later ones are fine.
    first_restart_dies: bool = False
    #: The restarted server takes this long to start (requests arrive meanwhile).
    restart_delay: float = 0.0
    #: The restarted server's tool renames its parameter.
    renamed_after_restart: bool = False
    #: Two requests are in flight when the server dies (at ``at``, ``at + 1``).
    two_in_flight: bool = False


KINDS = (
    "control",
    "idle_death",
    "death_after_dispatch",
    "death_before_execute",
    "request_during_restart",
    "restart_fails",
    "restart_dies_on_start",
    "listing_changed",
    "two_in_flight",
)


def build(seed: int = 0, per_type: int = 5) -> list[Session]:
    rng = random.Random(seed)
    sessions: list[Session] = []
    for kind in KINDS:
        for n in range(per_type):
            at = rng.randint(1, CALLS_PER_SESSION - 2)
            spec: dict[str, Any] = {"id": f"{kind}:{n}", "kind": kind, "at": at}
            if kind == "idle_death":
                spec["idle_death"] = True
            elif kind == "death_after_dispatch":
                spec["action"] = ServerAction.CLOSE_AFTER_EXECUTE
            elif kind == "death_before_execute":
                spec["action"] = ServerAction.CLOSE_BEFORE_EXECUTE
            elif kind == "request_during_restart":
                spec.update(idle_death=True, restart_delay=0.25)
            elif kind == "restart_fails":
                spec.update(idle_death=True, restart_fails=True)
            elif kind == "restart_dies_on_start":
                spec.update(idle_death=True, first_restart_dies=True)
            elif kind == "listing_changed":
                spec.update(idle_death=True, renamed_after_restart=True)
            elif kind == "two_in_flight":
                spec.update(action=ServerAction.HANG_BEFORE_EXECUTE, two_in_flight=True)
            sessions.append(Session(**spec))
    return sessions


@dataclass
class Invocation:
    session: str
    kind: str
    arm: str
    index: int
    outcome: str
    executions: int
    received: int
    on_generation: int | None


@dataclass
class SessionResult:
    session: str
    kind: str
    arm: str
    transport_failures: int
    reconnect_attempted: int
    reconnect_succeeded: int
    reconnect_failed: int
    invocations: list[Invocation] = field(default_factory=list)


def _state(response: dict[str, Any] | None) -> str | None:
    error = (response or {}).get("error")
    data = error.get("data") if isinstance(error, dict) else None
    value = data.get("io.stateguard/executionState") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def run_session(session: Session, arm: str) -> SessionResult:
    plan: dict[int, ServerAction] = {}
    if session.action is not None:
        plan[session.at] = session.action
        if session.two_in_flight:
            plan[session.at + 1] = session.action
    order = itertools.count()
    order_lock = threading.Lock()

    def faults(call: ServerCall) -> ServerAction:
        with order_lock:
            n = next(order)
        return plan.get(n, ServerAction.NORMAL)

    restarts_made = itertools.count()

    def factory() -> ScriptedServer:
        if session.restart_fails:
            raise OSError("server cannot be started")
        if session.restart_delay:
            time.sleep(session.restart_delay)
        n = next(restarts_made)
        server = ScriptedServer(
            [ServerTool(_tool("body" if session.renamed_after_restart else "text"))],
            faults=faults,
        )
        if session.first_restart_dies and n == 0:
            threading.Timer(0.02, server.kill).start()
        return server

    mode = {
        "raw": RelayMode.PASSTHROUGH,
        "shadow": RelayMode.SHADOW,
        "auto": RelayMode.AUTO,
        "restart_idle": RelayMode.AUTO,
        "restart_after_unknown": RelayMode.AUTO,
    }[arm]
    rig = Rig(
        ScriptedServer([ServerTool(_tool())], faults=faults),
        mode,
        listing_wait=1.0,
        server_factory=factory,
        max_restarts=MAX_RESTARTS if arm.startswith("restart") else 0,
        restart_policy="after-unknown" if arm == "restart_after_unknown" else "idle-only",
    )
    ids: list[Any] = []
    responses: dict[Any, dict[str, Any] | None] = {}
    try:
        rig.request("server/discover")
        if mode is not RelayMode.PASSTHROUGH:
            rig.wait_synced()
        i = 0
        while i < CALLS_PER_SESSION:
            if session.idle_death and i == session.at:
                rig.kill_server()
                _await_loss(rig, mode)
            # After the death, a client that knows the new schema uses its name.
            param = "body" if session.renamed_after_restart and i >= session.at else "text"
            if rig.client_eof.is_set():
                # The client knows its server is gone; it does not send.
                ids.append(f"unsent-{i}")
                responses[ids[-1]] = None
                i += 1
                continue
            if session.two_in_flight and i == session.at:
                pair = [f"s{i}", f"s{i + 1}"]
                for identifier in pair:
                    rig.send(
                        {
                            "jsonrpc": "2.0",
                            "id": identifier,
                            "method": "tools/call",
                            "params": {
                                "name": "write_note",
                                "arguments": {param: identifier},
                                "_meta": dict(META),
                            },
                        }
                    )
                _await_received(rig, pair)
                rig.kill_server()
                for identifier in pair:
                    ids.append(identifier)
                    responses[identifier] = _answer(rig, identifier, CALL_TIMEOUT)
                _await_loss(rig, mode)
                i += 2
                continue
            response = rig.call("write_note", {param: f"note-{i}"}, timeout=CALL_TIMEOUT)
            ids.append(rig.sent_ids[-1])
            responses[ids[-1]] = response
            if session.action is not None and i == session.at:
                _await_loss(rig, mode)
            i += 1
        time.sleep(0.05)
        return _judge(session, arm, rig, ids, responses)
    finally:
        rig.close()


def _answer(rig: Rig, identifier: Any, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for message in rig.client_received:
            if isinstance(message, dict) and message.get("id") == identifier:
                return message
        if rig.client_eof.is_set():
            break
        time.sleep(0.01)
    return None


def _await_received(rig: Rig, ids: list[Any], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        seen = {json.loads(x).get("id") for x in rig.all_received}
        if all(i in seen for i in ids):
            return
        time.sleep(0.01)


def _await_loss(rig: Rig, mode: RelayMode, timeout: float = 2.0) -> None:
    """Deterministic: continue only once the death has been observed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mode is RelayMode.PASSTHROUGH:
            if rig.client_eof.is_set():
                return
        elif any(e.get("event") == "server_lost" for e in rig.audit):
            return
        time.sleep(0.01)


def _judge(
    session: Session,
    arm: str,
    rig: Rig,
    ids: list[Any],
    responses: dict[Any, dict[str, Any] | None],
) -> SessionResult:
    received: Counter[Any] = Counter()
    for line in rig.all_received:
        message = json.loads(line)
        if message.get("method") == "tools/call":
            received[json.dumps(message.get("id"))] += 1
    executions: Counter[Any] = Counter()
    generation_of: dict[Any, int] = {}
    for generation, server in enumerate(rig.servers):
        for call in server.executed:
            key = json.dumps(call.message.get("id"))
            executions[key] += 1
            generation_of[key] = generation
    audit = rig.audit
    restarted = [e for e in audit if e.get("event") == "server_restarted"]
    failed = sum(1 for e in audit if e.get("event") in ("restart_failed", "reinit_failed"))
    synced_generations = set()
    generation = 0
    for event in audit:
        if event.get("event") == "server_restarted":
            generation = event["generation"]
        elif event.get("event") == "tools_synced" and generation > 0:
            synced_generations.add(generation)
    died = sum(1 for server in rig.servers if server.dead)
    result = SessionResult(
        session=session.id,
        kind=session.kind,
        arm=arm,
        transport_failures=died,
        reconnect_attempted=len(restarted)
        + sum(1 for e in audit if e.get("event") == "restart_failed"),
        reconnect_succeeded=len(synced_generations),
        reconnect_failed=failed + (len(restarted) - len(synced_generations)),
    )
    for index, identifier in enumerate(ids):
        key = json.dumps(identifier)
        response = responses.get(identifier)
        ok = (
            isinstance(response, dict)
            and isinstance(response.get("result"), dict)
            and response["result"].get("isError") is not True
        )
        state = _state(response)
        if ok and executions[key] == 1:
            # Recovery = it ran exactly once on a *restarted* server process.
            outcome = "recovered" if generation_of.get(key, 0) > 0 else "succeeded"
        elif state == "unknown":
            outcome = "unknown"
        elif state == "not_sent":
            outcome = "not_sent"
        elif response is None:
            outcome = "connection_closed" if rig.client_eof.is_set() else "silent"
        else:
            outcome = "other_error"
        result.invocations.append(
            Invocation(
                session=session.id,
                kind=session.kind,
                arm=arm,
                index=index,
                outcome=outcome,
                executions=executions[key],
                received=received[key],
                on_generation=generation_of.get(key),
            )
        )
    return result


def summarise(results: list[SessionResult]) -> dict[str, Any]:
    by_arm: dict[str, dict[str, Any]] = {}
    raw_ok = {
        (inv.session, inv.index)
        for r in results
        if r.arm == "raw"
        for inv in r.invocations
        if inv.outcome in ("succeeded", "recovered")
    }
    for arm in ARMS:
        rs = [r for r in results if r.arm == arm]
        invs = [inv for r in rs for inv in r.invocations]
        outcomes = Counter(inv.outcome for inv in invs)
        by_arm[arm] = {
            "sessions": len(rs),
            "invocations": len(invs),
            "outcomes": {o: outcomes.get(o, 0) for o in OUTCOMES},
            "replayed": sum(max(0, inv.received - 1) for inv in invs),
            "duplicate_execution": sum(1 for inv in invs if inv.executions > 1),
            "stateguard_induced": sum(
                1
                for inv in invs
                if (inv.session, inv.index) in raw_ok
                and inv.outcome not in ("succeeded", "recovered")
            ),
            "transport_failures": sum(r.transport_failures for r in rs),
            "reconnect_attempted": sum(r.reconnect_attempted for r in rs),
            "reconnect_succeeded": sum(r.reconnect_succeeded for r in rs),
            "reconnect_failed": sum(r.reconnect_failed for r in rs),
        }
    by_kind: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    for arm in ARMS:
        for kind in KINDS:
            invs = [
                inv for r in results if r.arm == arm and r.kind == kind for inv in r.invocations
            ]
            counts = Counter(inv.outcome for inv in invs)
            by_kind[kind][arm] = {o: counts.get(o, 0) for o in OUTCOMES}
    return {
        "synthetic": True,
        "workload": {
            "session_kinds": list(KINDS),
            "calls_per_session": CALLS_PER_SESSION,
            "sessions_per_arm": len([r for r in results if r.arm == "raw"]),
            "tool": "write_note (side-effecting, not idempotent; no retry involved)",
        },
        "arms": by_arm,
        "by_kind": dict(by_kind),
        "limitations": [
            "Synthetic: scripted in-process server; restarts are new in-process servers on "
            "fresh pipes, not process spawns (main()'s real factory is tested separately).",
            "One server failure per session at a seeded point; real failures cluster.",
            "Deaths are observed before the next call is sent, except where a test "
            "deliberately races them; real clients race them more often.",
        ],
    }


def render(summary: dict[str, Any]) -> str:
    w = summary["workload"]
    lines = [
        "MCP session recovery after server failure (SYNTHETIC WORKLOAD)",
        f"{w['sessions_per_arm']} sessions x {w['calls_per_session']} calls per arm; "
        f"tool: {w['tool']}",
        "",
        f"{'arm':<22}{'invoc':>6} "
        + " ".join(f"{o[:9]:>9}" for o in OUTCOMES)
        + f" {'replayed':>8} {'dup.exec':>8} {'Z':>4} | {'deaths':>6} {'rc.try':>6}"
        f" {'rc.ok':>6} {'rc.fail':>7}",
        "-" * 142,
    ]
    for arm, row in summary["arms"].items():
        lines.append(
            f"{arm:<22}{row['invocations']:>6} "
            + " ".join(f"{row['outcomes'][o]:>9}" for o in OUTCOMES)
            + f" {row['replayed']:>8} {row['duplicate_execution']:>8}"
            f" {row['stateguard_induced']:>4} | {row['transport_failures']:>6}"
            f" {row['reconnect_attempted']:>6} {row['reconnect_succeeded']:>6}"
            f" {row['reconnect_failed']:>7}"
        )
    lines += [
        "",
        "By session kind (succeeded / recovered / unknown / not_sent / connection_closed "
        "/ silent / other_error):",
    ]
    for kind, arms in summary["by_kind"].items():
        cells = "  ".join(f"{arm}=" + "/".join(str(arms[arm][o]) for o in OUTCOMES) for arm in ARMS)
        lines.append(f"  {kind:<24} {cells}")
    lines += ["", "Limitations:", *[f"  - {x}" for x in summary["limitations"]]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Session recovery after MCP server failure.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-type", type=int, default=5)
    parser.add_argument("--json", metavar="OUT")
    args = parser.parse_args(argv)
    results = [
        run_session(session, arm) for session in build(args.seed, args.per_type) for arm in ARMS
    ]
    summary = summarise(results)
    print(render(summary))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"summary": summary, "sessions": [asdict(r) for r in results]},
                indent=2,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
