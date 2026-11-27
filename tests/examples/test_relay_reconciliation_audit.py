"""
Regression tests for the Week 3 real-relay audit defects A, B and C.

See ``docs/runtime/WEEK3_REAL_RELAY_VERIFICATION.md`` (the findings) and
``docs/runtime/WEEK3_AUDIT_FIXES.md`` (the fixes). Before the fixes, the first
test of each section failed (it was ``xfail(strict=True)``).

A. Probing needs positive evidence that the original executor can no longer
   act. A restart, a reaped direct child or an empty process group is not
   that evidence: none of them can see a descendant that left the group.
B. A reconciliation being answered while the relay stops is waited for
   (bounded); every client gets exactly one answer, or the relay says in the
   audit log that it could not confirm one.
C. Only a response whose id is exactly a probe the relay issued is treated as
   a probe's; every other response -- however its id looks -- takes the
   normal path.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

import pytest

from tests.examples.test_relay_reconciliation import (
    PEEK,
    READ,
    TOOLS,
    First,
    FsServer,
    World,
    _data,
    _events,
    _move,
    _move_entry,
    _never_resent,
    _policies,
    _result,
    _world,
)

# On sys.path via the module above.
from runtime.harness import (  # noqa: E402
    META,
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    load_relay_module,
)

from stateguard.adapters.mcp import TerminationEvidence  # noqa: E402

pytestmark = pytest.mark.integration

RELAY = Path(__file__).resolve().parents[2] / "examples" / "mcp" / "stdio_proxy.py"
PROBE_ID = re.compile(r"^stateguard-reconcile:[0-9a-f]{16}:\d+$")


def _answers_for(rig: Rig, identifier: Any) -> list[dict[str, Any]]:
    return [
        m
        for m in rig.client_received
        if isinstance(m, dict) and m.get("id") == identifier and "method" not in m
    ]


def _probe_ids_seen_by_servers(rig: Rig) -> list[str]:
    ids = []
    for server in rig.servers:
        for line in server.received:
            message = json.loads(line)
            if message.get("method") == "tools/call" and str(message.get("id", "")).startswith(
                "stateguard-reconcile:"
            ):
                ids.append(str(message["id"]))
    return ids


def _broken_restart_rig() -> tuple[Rig, dict[str, str]]:
    fs = {"a.txt": "order 001"}
    faults = First("move", ServerAction.CLOSE_AFTER_EXECUTE)

    def broken() -> ScriptedServer:
        raise OSError("cannot start the server")

    effects, policy = _policies()
    rig = Rig(
        FsServer(TOOLS, faults=faults, fs=fs),
        RelayMode.AUTO,
        server_factory=broken,
        max_restarts=2,
        restart_policy="after-unknown",
        effects=effects,
        reconcile=policy,
        retry="off",
    )
    return rig, fs


# ---------------------------------------------------------------------------
# A. Executor termination must be established before anything is probed
# ---------------------------------------------------------------------------


def test_a_factory_that_leaves_the_old_executor_running_is_never_observed_satisfied() -> None:
    """The audit's counterexample A, in-process: a restart that proves nothing."""
    fs = {"a.txt": "order 001"}
    faults = First("move", ServerAction.CLOSE_AFTER_EXECUTE)
    undone = threading.Event()

    def careless_factory() -> ScriptedServer:
        # Returns a new server while the old executor is still at work: it
        # undoes the move shortly after, once a probe could have run.
        def old_executor_finishes() -> None:
            fs["a.txt"] = fs.pop("b.txt")
            undone.set()

        threading.Timer(0.5, old_executor_finishes).start()
        return FsServer(TOOLS, faults=faults, fs=fs)

    effects, policy = _policies()
    rig = Rig(
        FsServer(TOOLS, faults=faults, fs=fs),
        RelayMode.AUTO,
        server_factory=careless_factory,
        max_restarts=2,
        restart_policy="after-unknown",
        effects=effects,
        reconcile=policy,
        retry="off",
    )
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        data = _data(_move(World(rig, fs)))
        assert undone.wait(5.0)
        assert fs == {"a.txt": "order 001"}  # ground truth: the move did not last
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert data["io.stateguard/executionState"] == "unknown"
        assert data["io.stateguard/attribution"] == "unknown"
        assert not _events(rig, "reconcile_probe")
        assert not _probe_ids_seen_by_servers(rig)
        (evidence,) = _events(rig, "executor_termination")
        assert evidence["kind"] == "not_reported" and evidence["established"] is False
        _never_resent(World(rig, fs))
    finally:
        rig.close()


def test_a_custom_factory_without_evidence_never_probes() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, attest_termination=False)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        (reason,) = data["io.stateguard/reconciliation"]["reasons"]
        assert reason.startswith("executor termination not established")
        assert "nothing was probed" in reason
        assert world.fs == {"b.txt": "order 001"}  # it did run -- and is not claimed
        assert not _events(world.rig, "reconcile_probe")
        assert not _probe_ids_seen_by_servers(world.rig)
        (verdict,) = _events(world.rig, "reconcile_verdict")
        assert verdict["termination"]["kind"] == "not_reported"
        assert verdict["execution_state"] == "unknown" and verdict["attribution"] == "unknown"
        inv = world.rig.relay.ledger.get(world.rig.sent_ids[-1])
        assert inv is not None and inv.reconciliation[-1]["termination"]["established"] is False
        _never_resent(world)
    finally:
        world.rig.close()


def test_a_scope_ended_evidence_is_what_allows_a_probe() -> None:
    """Positive control: an in-process server whose serve thread was joined."""
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, attest_termination=True)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        assert data["io.stateguard/executionState"] == "unknown"
        assert data["io.stateguard/attribution"] == "unknown"
        (evidence,) = _events(world.rig, "executor_termination")
        assert evidence["kind"] == "scope_ended" and evidence["established"] is True
        _never_resent(world)
    finally:
        world.rig.close()


def test_a_restart_that_fails_records_no_evidence_and_never_probes() -> None:
    rig, fs = _broken_restart_rig()
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        response = _move(World(rig, fs))
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert "no probe channel" in data["io.stateguard/reconciliation"]["reasons"][0]
        assert not _events(rig, "executor_termination")
        assert not _events(rig, "reconcile_probe")
        assert len(_answers_for(rig, rig.sent_ids[-1])) == 1
        _never_resent(World(rig, fs))
    finally:
        rig.close()


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX-only")
def test_a_process_group_emptied_by_the_restart_does_not_establish_termination(
    tmp_path: Path,
) -> None:
    """
    A real process tree: the old executor's group leader, and a descendant that
    started its own session. The restart kills the group and verifies it is
    empty -- and the descendant is still running. So that is not evidence.
    """
    pidfile = tmp_path / "descendant.pid"
    leader_code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " start_new_session=True)\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_code], start_new_session=True)
    deadline = time.monotonic() + 10
    while not pidfile.exists() or not pidfile.read_text():
        assert time.monotonic() < deadline, "the descendant never started"
        time.sleep(0.02)
    descendant = int(pidfile.read_text())
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, attest_termination=False)
    relay_module = load_relay_module()
    original = world.rig.relay._restart
    group_empty: list[bool] = []

    def group_killing_restart() -> Any:
        os.killpg(leader.pid, signal.SIGKILL)
        leader.wait()
        try:
            os.killpg(leader.pid, 0)
            group_empty.append(False)
        except ProcessLookupError:
            group_empty.append(True)
        result = original()
        return relay_module.Restarted(
            result.stdin, result.stdout, TerminationEvidence.process_group_empty()
        )

    world.rig.relay._restart = group_killing_restart
    try:
        data = _data(_move(world))
        assert group_empty == [True]  # the group really is empty ...
        os.kill(descendant, 0)  # ... and the descendant is still alive
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert "process group is empty" in data["io.stateguard/reconciliation"]["reasons"][0]
        assert not _events(world.rig, "reconcile_probe")
        assert not _probe_ids_seen_by_servers(world.rig)
        _never_resent(world)
    finally:
        world.rig.close()
        with contextlib.suppress(ProcessLookupError):
            os.kill(descendant, signal.SIGKILL)
        if leader.poll() is None:
            leader.kill()


# -- through the standard entry point (main) ----------------------------------

SERVER = r"""
import json, os, subprocess, sys, time
from pathlib import Path

state, mode = Path(sys.argv[1]), sys.argv[2]
starts = state / "starts"
gen = int(starts.read_text()) if starts.exists() else 0
starts.write_text(str(gen + 1))
calls = open(state / "calls.log", "a")
TOOLS = [
    {"name": "read", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
     "required": ["path"]}, "annotations": {"readOnlyHint": True}},
    {"name": "move", "inputSchema": {"type": "object", "properties": {
     "source": {"type": "string"}, "destination": {"type": "string"}},
     "required": ["source", "destination"]}},
]
ROLLBACK = (
    "import os, sys, time\n"
    "go, src, dst, done = sys.argv[1:]\n"
    "deadline = time.time() + 30\n"
    "while not os.path.exists(go) and time.time() < deadline: time.sleep(0.02)\n"
    "os.rename(dst, src); open(done, 'w').close()\n"
)

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

def text(rid, body, error=False):
    send({"jsonrpc": "2.0", "id": rid,
          "result": {"content": [{"type": "text", "text": body}], "isError": error}})

for raw in sys.stdin:
    msg = json.loads(raw)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
            "serverInfo": {"name": "termination-test", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        calls.write(f"{gen} {name}\n"); calls.flush()
        if name == "read":
            p = Path(args["path"])
            text(rid, p.read_text()) if p.exists() else text(rid, "ENOENT: no such file", True)
        elif name == "move":
            os.rename(args["source"], args["destination"])
            if gen == 0 and mode != "answer":
                if mode == "detached":
                    subprocess.Popen([sys.executable, "-c", ROLLBACK, str(state / "go"),
                                      args["source"], args["destination"],
                                      str(state / "rolled_back")],
                                     start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                sys.exit(0)  # exits normally, without answering
            text(rid, "moved")
"""

EFFECTS = {
    "tools": [
        {"name": "read", "effect": "read_only"},
        {
            "name": "move",
            "effect": "non_idempotent",
            "reconcile": {
                "timeout_seconds": 2,
                "probes": [
                    {
                        "tool": "read",
                        "arguments": {"path": "$source"},
                        "expect": "absent",
                        "absent_marker": "ENOENT",
                    },
                    {
                        "tool": "read",
                        "arguments": {"path": "$destination"},
                        "expect": "present",
                        "absent_marker": "ENOENT",
                    },
                ],
            },
        },
    ]
}


class _Client:
    """Newline-delimited JSON-RPC over the relay process's stdio."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self.lines: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self.lines.put(json.loads(raw))
        self.lines.put(None)

    def send(self, message: dict[str, Any]) -> None:
        stdin: IO[bytes] | None = self.proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(message).encode() + b"\n")
        stdin.flush()

    def request(self, rid: Any, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                item = self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return None
            if item.get("id") == rid and "method" not in item:
                return item
        return None

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


EMBEDDED = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "domains"
    / "mcp_server_filesystem"
    / "embedded_relay.py"
)


def _pid_namespaces_available() -> bool:
    try:
        probe = subprocess.run(
            ["unshare", "--pid", "--fork", "--kill-child", "--", "true"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _run_embedded(
    tmp_path: Path, mode: str, scope: str
) -> tuple[dict[str, Any], Path, list[dict[str, Any]]]:
    state, sandbox = tmp_path / "state", tmp_path / "sandbox"
    state.mkdir()
    sandbox.mkdir()
    (sandbox / "a.txt").write_text("order 001")
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    effects = tmp_path / "effects.json"
    effects.write_text(json.dumps(EFFECTS))
    audit = tmp_path / "audit.jsonl"
    client = _Client(
        [
            sys.executable, str(EMBEDDED), "--scope", scope, "--audit", str(audit),
            "--effects", str(effects), "--", sys.executable, str(server), str(state), mode,
        ]
    )  # fmt: skip
    try:
        init = client.request(
            1,
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}},
        )
        assert init is not None and "result" in init
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = client.request(
            "T-1",
            "tools/call",
            {
                "name": "move",
                "arguments": {
                    "source": str(sandbox / "a.txt"),
                    "destination": str(sandbox / "b.txt"),
                },
            },
        )
        assert response is not None, "the client got no answer"
    finally:
        client.close()
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    return response, state, events


def _calls(state: Path) -> list[tuple[str, str]]:
    calls = (state / "calls.log").read_text().split()
    return list(zip(calls[::2], calls[1::2], strict=True))


def test_main_refuses_reconciliation_at_startup(tmp_path: Path) -> None:
    """Without an executor scope the CLI cannot show termination, so it refuses reconcile."""
    effects = tmp_path / "effects.json"
    effects.write_text(json.dumps(EFFECTS))
    marker = tmp_path / "server-started"
    for flags in ([], ["--restart", "2", "--restart-policy", "after-unknown"], ["--shadow"]):
        proc = subprocess.run(
            [sys.executable, str(RELAY), "--effects", str(effects), *flags, "--",
             sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
            input=b"", capture_output=True, timeout=30,
        )  # fmt: skip
        assert proc.returncode == 2
        assert not marker.exists(), "no server may start"
        error = proc.stderr.decode()
        assert "reconcile blocks (move) need --executor-scope pid-namespace" in error
        assert "direct child" in error and "TerminationEvidence.scope_ended" in error
        assert "External systems are never covered" in error


def test_main_relays_normally_without_reconciliation(tmp_path: Path) -> None:
    """Ordinary relay behaviour is unchanged -- including an effects file with a repeated key."""
    state, sandbox = tmp_path / "state", tmp_path / "sandbox"
    state.mkdir()
    sandbox.mkdir()
    (sandbox / "a.txt").write_text("order 001")
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    effects = tmp_path / "effects.json"
    # A repeated key outside any reconcile block: read as before (last one wins).
    effects.write_text(
        '{"tools": [{"name": "read", "effect": "idempotent", "effect": "read_only"},'
        ' {"name": "move", "effect": "non_idempotent"}]}'
    )
    client = _Client(
        [sys.executable, str(RELAY), "--effects", str(effects), "--retry", "off", "--",
         sys.executable, str(server), str(state), "answer"]
    )  # fmt: skip
    try:
        init = client.request(
            1,
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}},
        )
        assert init is not None and "result" in init
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = client.request(
            "T-1",
            "tools/call",
            {"name": "move", "arguments": {"source": str(sandbox / "a.txt"),
                                           "destination": str(sandbox / "b.txt")}},
        )  # fmt: skip
        assert response is not None and response["result"]["isError"] is False
    finally:
        client.close()
    assert (sandbox / "b.txt").exists() and not (sandbox / "a.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process model")
def test_a_direct_child_exit_with_a_detached_descendant_alive_is_never_probed(
    tmp_path: Path,
) -> None:
    """The audit's counterexample T1, embedded with what main() can see: a reaped child."""
    response, state, events = _run_embedded(tmp_path, "detached", "direct-child")
    data = response["error"]["data"]
    assert response["error"]["code"] == -32000
    assert data["io.stateguard/postcondition"] == "undetermined"
    assert data["io.stateguard/executionState"] == "unknown"
    assert data["io.stateguard/attribution"] == "unknown"
    (reason,) = data["io.stateguard/reconciliation"]["reasons"]
    assert "executor termination not established" in reason and "direct child" in reason
    assert "io.stateguard/observation" not in data  # nothing was observed
    assert not [e for e in events if e["event"] == "reconcile_probe"]
    (evidence,) = [e for e in events if e["event"] == "executor_termination"]
    assert evidence["kind"] == "direct_child_reaped" and evidence["applies"] is False
    assert _calls(state) == [("0", "move")]  # no later generation got any tools/call
    (state / "go").touch()  # only now may the descendant act
    deadline = time.monotonic() + 15
    while not (state / "rolled_back").exists():  # independent ground truth, on disk
        assert time.monotonic() < deadline, "the detached descendant never acted"
        time.sleep(0.02)
    assert (tmp_path / "sandbox" / "a.txt").exists()  # the old executor acted after the answer


@pytest.mark.skipif(not _pid_namespaces_available(), reason="cannot create a PID namespace")
def test_a_pid_namespace_scope_is_positive_evidence_and_the_descendant_cannot_act(
    tmp_path: Path,
) -> None:
    """
    Positive attestation from a real scope: the embedder ran the server as init
    of a PID namespace, confirmed the namespace empty, and asserted scope_ended
    for that executor. The detached descendant was killed with the namespace.
    """
    response, state, events = _run_embedded(tmp_path, "detached", "pid-namespace")
    data = response["error"]["data"]
    assert data["io.stateguard/postcondition"] == "observed_satisfied"
    assert data["io.stateguard/executionState"] == "unknown"
    assert data["io.stateguard/attribution"] == "unknown"
    assert data["io.stateguard/durability"] == "not_established"
    assert data["io.stateguard/operationOutcome"] == "unknown"
    (evidence,) = [e for e in events if e["event"] == "executor_termination"]
    assert evidence["kind"] == "scope_ended" and evidence["applies"] is True
    assert "pid namespace" in evidence["detail"]
    assert _calls(state) == [("0", "move"), ("1", "read"), ("1", "read")]
    (state / "go").touch()
    # The descendant would undo the move within milliseconds of "go" if it were
    # alive; it died with the namespace before the probes ran.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        assert not (state / "rolled_back").exists()
        time.sleep(0.05)
    assert (tmp_path / "sandbox" / "b.txt").exists()


# ---------------------------------------------------------------------------
# B. Shutdown: every reconciliation answered exactly once, or reported unconfirmed
# ---------------------------------------------------------------------------


def _hooked_shutdown(rig: Rig, *, block: threading.Event | None = None) -> threading.Event:
    """
    Order the race without sleeps: the worker takes the entry (``claimed``), then
    -- before it writes -- waits for the pump thread to finish (bounded). The
    stopping relay waits for ``claimed`` before it settles. With the fix the
    pump thread cannot finish while an answer is being written, so the join
    times out and the worker writes; without it, the relay stops first.
    """
    relay = rig.relay
    pump_thread = rig._threads[3]  # runs relay.pump_server, then closes the client's stdout
    record = relay._record_verdict
    settle_name = (
        "_settle_reconciliations"
        if hasattr(relay, "_settle_reconciliations")
        else "_abandon_reconciliations"
    )
    settle = getattr(relay, settle_name)
    claimed = threading.Event()

    def record_after_claim(*args: Any, **kwargs: Any) -> None:
        if threading.current_thread() is not pump_thread:
            claimed.set()
            if block is not None:
                block.wait(10)
            else:
                pump_thread.join(timeout=1.0)
        record(*args, **kwargs)

    def settle_after_claim(reason: str) -> None:
        claimed.wait(5)
        settle(reason)

    relay._record_verdict = record_after_claim
    setattr(relay, settle_name, settle_after_claim)
    return claimed


def _client_answers_at_end_of_stream(rig: Rig, identifier: Any) -> int:
    rig.close()  # joins the client-side reader: every line the relay wrote has been read
    return len(_answers_for(rig, identifier))


def test_a_pending_reconciliation_is_answered_before_the_relay_stops() -> None:
    """The audit's race B, ordered deterministically: the worker is mid-answer at stop."""
    rig, fs = _broken_restart_rig()
    _hooked_shutdown(rig)
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        response = _move(World(rig, fs), timeout=10.0)
        assert response is not None, "the client got no answer"
        assert _data(response)["io.stateguard/postcondition"] == "undetermined"
        assert _client_answers_at_end_of_stream(rig, rig.sent_ids[-1]) == 1
        (answered,) = _events(rig, "reconcile_answered")
        assert answered["written_to_stream"] is True
        assert not _events(rig, "reconcile_answer_unconfirmed")
        _never_resent(World(rig, fs))
    finally:
        rig.close()


def test_a_worker_that_finishes_just_before_shutdown_is_answered_once() -> None:
    rig, fs = _broken_restart_rig()
    relay = rig.relay
    settle = relay._settle_reconciliations
    finished = threading.Event()
    finish = relay._finish_reconciliation

    def finish_and_signal(*args: Any, **kwargs: Any) -> bool:
        took = bool(finish(*args, **kwargs))
        finished.set()
        return took

    def settle_after_finish(reason: str) -> None:
        finished.wait(5)  # the worker's answer is fully written before the stop begins
        settle(reason)

    relay._finish_reconciliation = finish_and_signal
    relay._settle_reconciliations = settle_after_finish
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        assert _move(World(rig, fs), timeout=10.0) is not None
        assert _client_answers_at_end_of_stream(rig, rig.sent_ids[-1]) == 1
        assert len(_events(rig, "reconcile_answered")) == 1
        assert not _events(rig, "reconcile_answer_unconfirmed")
    finally:
        rig.close()


def test_an_answer_that_cannot_be_written_in_time_is_reported_unconfirmed() -> None:
    relay_module = load_relay_module()
    rig, fs = _broken_restart_rig()
    release = threading.Event()
    _hooked_shutdown(rig, block=release)
    pump_thread = rig._threads[3]
    done = threading.Event()
    finish = rig.relay._finish_reconciliation

    def finish_and_signal(*args: Any, **kwargs: Any) -> bool:
        try:
            return bool(finish(*args, **kwargs))
        finally:
            done.set()

    rig.relay._finish_reconciliation = finish_and_signal
    saved = relay_module.RECONCILE_STOP_WAIT_SECONDS
    relay_module.RECONCILE_STOP_WAIT_SECONDS = 0.3
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        started = time.monotonic()
        rig.send(
            {"jsonrpc": "2.0", "id": "T-1", "method": "tools/call",
             "params": {"name": "move", "arguments": {"source": "a.txt",
                                                      "destination": "b.txt"}, "_meta": META}}
        )  # fmt: skip
        pump_thread.join(timeout=5)
        assert not pump_thread.is_alive(), "the stopping relay must not wait unbounded"
        assert time.monotonic() - started < 5
        (unconfirmed,) = _events(rig, "reconcile_answer_unconfirmed")
        assert unconfirmed["state"] == "answering"
        release.set()  # the worker resumes against a relay that has stopped
        assert done.wait(5)
        rig.close()
        (answered,) = _events(rig, "reconcile_answered")
        assert answered["written_to_stream"] is False  # never reported as delivered
        assert _answers_for(rig, "T-1") == []
    finally:
        release.set()
        relay_module.RECONCILE_STOP_WAIT_SECONDS = saved
        rig.close()


def test_a_repeated_shutdown_answers_nothing_twice() -> None:
    rig, fs = _broken_restart_rig()
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        assert _move(World(rig, fs)) is not None
        rig._threads[3].join(timeout=5)
        rig.relay._settle_reconciliations("stopping again")
        rig.relay._settle_reconciliations("and again")
        assert _client_answers_at_end_of_stream(rig, rig.sent_ids[-1]) == 1
        assert len(_events(rig, "reconcile_answered")) == 1
        assert not _events(rig, "reconcile_answer_unconfirmed")
    finally:
        rig.close()


def test_concurrent_finishers_answer_exactly_once() -> None:
    world = _world(action=ServerAction.NORMAL)
    rig = world.rig
    try:
        response = rig.call("read", {"path": "a.txt"})
        assert response is not None and "result" in response
        invocation = rig.relay.ledger.get(rig.sent_ids[-1])
        assert invocation is not None
        relay_module = load_relay_module()
        pending: dict[str, Any] = {"client_id": "X", "invocation": invocation, "state": "held"}
        with rig.relay._reconcile_lock:
            rig.relay._pending_reconcile[relay_module._id_key("X")] = pending
        barrier = threading.Barrier(8)
        results: list[bool] = []
        lock = threading.Lock()
        verdict = relay_module.Verdict(relay_module.Postcondition.UNDETERMINED, ("race",))

        def finisher() -> None:
            barrier.wait()
            took = rig.relay._finish_reconciliation(pending, verdict, probe_generation=None)
            with lock:
                results.append(took)

        threads = [threading.Thread(target=finisher) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert sorted(results) == [False] * 7 + [True]
        assert not rig.relay._pending_reconcile
        # Its real answer, plus exactly one from the eight finishers -- counted at EOF.
        assert _client_answers_at_end_of_stream(rig, rig.sent_ids[-1]) == 2
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# C. Probe responses are matched by exact issued id, never by prefix
# ---------------------------------------------------------------------------


def test_a_client_id_with_the_probe_prefix_is_still_answered() -> None:
    world = _world(action=ServerAction.NORMAL)
    rig = world.rig
    try:
        for identifier in ("stateguard-reconcile:1", "stateguard-reconcile:0123456789abcdef:1"):
            rig.send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": "tools/call",
                    "params": {"name": "read", "arguments": {"path": "a.txt"}, "_meta": META},
                }
            )
            response = rig.wait_for(identifier, timeout=3.0)
            assert response is not None, f"the client's own request {identifier} went unanswered"
            assert "result" in response
        assert not _events(rig, "reconcile_probe_dropped")
    finally:
        rig.close()


def test_client_ids_that_look_like_relay_owned_ids_are_answered_normally() -> None:
    """
    Defect D (found in the Week 3 contract-v2 audit): relay-owned tools/list ids
    and retry ids were matched by prefix too, so a client request with such an
    id lost its response -- and a client tools/list could be synced as the
    relay's own. Now only ids the relay issued are treated as its own.
    """
    world = _world(action=ServerAction.NORMAL)
    rig = world.rig
    try:
        cases = [
            ("stateguard-relay:1", "tools/list", {"_meta": META}),
            ("stateguard-relay:999", "tools/list", {"_meta": META}),
            (
                "stateguard-retry:1",
                "tools/call",
                {"name": "read", "arguments": {"path": "a.txt"}, "_meta": META},
            ),
        ]
        for identifier, method, params in cases:
            rig.send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
            response = rig.wait_for(identifier, timeout=3.0)
            assert response is not None, f"the client's own request {identifier} went unanswered"
            assert "result" in response
        assert rig.relay.registry.find("read") is not None  # the relay's own listing synced
        assert not _events(rig, "own_response_dropped")
        assert not _events(rig, "response_dropped")
        rig.close()
        for identifier, _, _ in cases:
            assert len(_answers_for(rig, identifier)) == 1  # exactly one, counted at EOF
    finally:
        rig.close()


def test_genuine_probe_responses_use_exact_issued_ids() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        ids = _probe_ids_seen_by_servers(world.rig)
        assert len(ids) == 2 and all(PROBE_ID.match(i) for i in ids)
        assert len({i.rsplit(":", 1)[0] for i in ids}) == 1  # one nonce per relay
        for identifier in ids:
            assert _answers_for(world.rig, identifier) == []  # never relayed to the client
        assert not _events(world.rig, "reconcile_probe_dropped")
        _never_resent(world)
    finally:
        world.rig.close()


class _ReadAnswers(FsServer):
    def _answer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        if path in self.fs:
            return _result(self.fs[path])
        return _result("ENOENT: no such file", error=True)


class _OutOfOrderServer(_ReadAnswers):
    """Holds the first read's answer; sends it after the second read's (late)."""

    held: tuple[Any, dict[str, Any]] | None = None

    def _read(self, identifier: Any, arguments: dict[str, Any], writer: IO[bytes]) -> bool:
        self.reads += 1
        answer = self._answer(arguments)
        if self.reads == 1:
            self.held = (identifier, answer)
            return True
        self._reply(writer, identifier, answer)
        if self.held is not None:
            self._reply(writer, *self.held)
            self.held = None
        return True


class _DuplicatingServer(_ReadAnswers):
    def _read(self, identifier: Any, arguments: dict[str, Any], writer: IO[bytes]) -> bool:
        self.reads += 1
        answer = self._answer(arguments)
        self._reply(writer, identifier, answer)
        self._reply(writer, identifier, answer)
        return True


class _UnsolicitedServer(_ReadAnswers):
    """Before each read's answer, sends a response for an id nobody issued."""

    STRAY = "stateguard-reconcile:ffffffffffffffff:1"

    def _read(self, identifier: Any, arguments: dict[str, Any], writer: IO[bytes]) -> bool:
        self.reads += 1
        self._reply(writer, self.STRAY, _result("stray"))
        self._reply(writer, identifier, self._answer(arguments))
        return True


def test_late_and_out_of_order_probe_responses_are_dropped() -> None:
    world = _world(
        action=ServerAction.CLOSE_AFTER_EXECUTE,
        entries=(READ, PEEK, _move_entry(timeout_seconds=0.5)),
        server_cls=_OutOfOrderServer,
    )
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "undetermined"
        probes = _events(world.rig, "reconcile_probe")
        assert [p["status"] for p in probes] == ["unavailable", "satisfied"]
        world.rig.close()  # end of stream: every server line has been processed
        (dropped,) = _events(world.rig, "reconcile_probe_dropped")
        assert dropped["why"] == "late"
        for identifier in _probe_ids_seen_by_servers(world.rig):
            assert _answers_for(world.rig, identifier) == []
        _never_resent(world)
    finally:
        world.rig.close()


def test_duplicate_probe_responses_are_dropped() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, server_cls=_DuplicatingServer)
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        world.rig.close()  # end of stream: every server line has been processed
        assert [d["why"] for d in _events(world.rig, "reconcile_probe_dropped")] == [
            "duplicate",
            "duplicate",
        ]
        for identifier in _probe_ids_seen_by_servers(world.rig):
            assert _answers_for(world.rig, identifier) == []
        _never_resent(world)
    finally:
        world.rig.close()


def test_a_response_for_an_unknown_id_takes_the_normal_path() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, server_cls=_UnsolicitedServer)
    try:
        data = _data(_move(world))
        assert (
            data["io.stateguard/postcondition"] == "observed_satisfied"
        )  # the real answers decided
        assert not _events(world.rig, "reconcile_probe_dropped")
        # Not consumed as a probe answer: it went where any server response goes.
        assert _answers_for(world.rig, _UnsolicitedServer.STRAY)
        _never_resent(world)
    finally:
        world.rig.close()
