"""
The standard CLI with ``--executor-scope pid-namespace`` (Week 4A), end to end.

Each test runs ``examples/mcp/stdio_proxy.py`` as a process, with a Python
server that keeps an independent ground truth on disk. Process exits are
awaited on pidfds and stream EOF by reading to the end -- no sleeps. Every
answer is counted at end of stream.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.examples.test_relay_reconciliation_audit import EFFECTS, RELAY, _calls, _Client

from stateguard.adapters.mcp.executor_scope import check_pid_namespace_capability

pytestmark = pytest.mark.integration

CAPABILITY = check_pid_namespace_capability()
needs_scope = pytest.mark.skipif(not CAPABILITY.available, reason=CAPABILITY.reason)
ROOT = Path(__file__).resolve().parents[2]

#: The server under test. Generation 0's ``move``, by mode:
#:   answer    -- renames and answers (no fault)
#:   die-first -- exits before renaming (lost, not executed)
#:   drop      -- renames, exits without answering (lost, executed)
#:   detached  -- as drop, leaving a detached descendant that undoes the rename on "go"
#:   survivor  -- renames, closes stdout, keeps running; undoes the rename on "go"
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
UNDO = (
    "import os, sys, time\n"
    "go, src, dst, done = sys.argv[1:]\n"
    "deadline = time.time() + 60\n"
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
            "serverInfo": {"name": "scope-test", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        calls.write(f"{gen} {name}\n"); calls.flush()
        if name == "read":
            p = Path(args["path"])
            text(rid, p.read_text()) if p.exists() else text(rid, "ENOENT: no such file", True)
            continue
        if gen == 0 and mode == "die-first":
            sys.exit(0)
        os.rename(args["source"], args["destination"])
        if gen == 0 and mode in ("drop", "detached", "survivor"):
            undo = [sys.executable, "-c", UNDO, str(state / "go"), args["source"],
                    args["destination"], str(state / "undone")]
            if mode == "detached":
                subprocess.Popen(undo, start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if mode == "survivor":
                sys.stdout.close(); os.close(1)
                os.execv(sys.executable, undo)  # still running, stdout closed
            sys.exit(0)
        text(rid, "moved")
"""

INIT = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}}
SCOPED = ["--executor-scope", "pid-namespace", "--retry", "off", "--restart", "2",
          "--restart-policy", "after-unknown"]  # fmt: skip
#: Architecture B (Week 4G): the same flags, unprivileged user+PID namespace.
SCOPED_UNPRIV = ["--executor-scope", "pid-namespace-unprivileged", "--retry", "off",
                 "--restart", "2", "--restart-policy", "after-unknown"]  # fmt: skip
CAPABILITY_B = check_pid_namespace_capability(unprivileged=True)
needs_scope_b = pytest.mark.skipif(not CAPABILITY_B.available, reason=CAPABILITY_B.reason)


class _RecordingClient(_Client):
    """Also keeps every line the relay wrote, so answers can be counted at EOF."""

    def __init__(self, argv: list[str]) -> None:
        self.received: list[dict[str, Any]] = []
        self.eof = threading.Event()
        super().__init__(argv)

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            message = json.loads(raw)
            self.received.append(message)
            self.lines.put(message)
        self.eof.set()
        self.lines.put(None)


class _Trial:
    def __init__(self, tmp_path: Path, mode: str, *, flags: list[str] | None = None,
                 effects: dict[str, Any] | None = EFFECTS) -> None:  # fmt: skip
        self.tmp, self.state, self.sandbox = tmp_path, tmp_path / "state", tmp_path / "sandbox"
        self.state.mkdir()
        self.sandbox.mkdir()
        (self.sandbox / "a.txt").write_text("order 001")
        server = tmp_path / "server.py"
        server.write_text(SERVER)
        self.audit = tmp_path / "audit.jsonl"
        argv = [sys.executable, str(RELAY), "--audit", str(self.audit)]
        if effects is not None:
            path = tmp_path / "effects.json"
            path.write_text(json.dumps(effects))
            argv += ["--effects", str(path)]
        argv += [*(SCOPED if flags is None else flags), "--",
                 sys.executable, str(server), str(self.state), mode]  # fmt: skip
        self.client = _RecordingClient(argv)
        self.lines: list[dict[str, Any]] = []

    def start(self) -> None:
        init = self.client.request(1, "initialize", INIT)
        assert init is not None and "result" in init, "no initialize answer"
        self.client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def move(self, rid: Any = "T-1") -> dict[str, Any]:
        arguments = {
            "source": str(self.sandbox / "a.txt"),
            "destination": str(self.sandbox / "b.txt"),
        }
        response = self.client.request(rid, "tools/call", {"name": "move", "arguments": arguments})
        assert response is not None, "the client got no answer"
        return response

    def read(self, rid: Any, name: str) -> dict[str, Any] | None:
        return self.client.request(
            rid, "tools/call", {"name": "read", "arguments": {"path": str(self.sandbox / name)}}
        )

    def close(self) -> dict[Any, int]:
        """Close the client's end; count every answer per id up to the relay's EOF."""
        self.client.close()
        assert self.client.eof.wait(10), "the relay never closed the client's stream"
        counts: dict[Any, int] = {}
        for item in self.client.received:
            if "method" not in item:
                counts[item.get("id")] = counts.get(item.get("id"), 0) + 1
        return counts

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        events = [json.loads(line) for line in self.audit.read_text().splitlines() if line]
        return [e for e in events if name is None or e.get("event") == name]

    def processes(self) -> list[int]:
        """Every live process whose command line names this trial's directory."""
        return _processes_mentioning(str(self.tmp))


def _processes_mentioning(token: str) -> list[int]:
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        with contextlib.suppress(OSError):
            if token in Path(f"/proc/{entry}/cmdline").read_bytes().decode(errors="replace"):
                state = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[0]
                if state != "Z":
                    found.append(int(entry))
    return found


def _all_exit(pids: list[int], timeout: float) -> bool:
    fds = []
    try:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                fds.append(os.pidfd_open(pid))
        poller, waiting = select.poll(), set(fds)
        for fd in fds:
            poller.register(fd, select.POLLIN)
        deadline = time.monotonic() + timeout
        while waiting:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            for fd, _ in poller.poll(left * 1000):
                waiting.discard(fd)
                poller.unregister(fd)
        return True
    finally:
        for fd in fds:
            os.close(fd)


def _verdict(response: dict[str, Any]) -> dict[str, Any]:
    error = response["error"]
    assert error["code"] == -32000
    return dict(error["data"])


# ---------------------------------------------------------------------------
# Reconciliation through the CLI, with the scope
# ---------------------------------------------------------------------------


@needs_scope
@pytest.mark.parametrize("mode", ["detached", "survivor"])
def test_an_executor_left_behind_is_ended_before_the_probe_and_never_acts(
    tmp_path: Path, mode: str
) -> None:
    """T1 (a detached descendant) and T3 (a server alive with stdout closed)."""
    trial = _Trial(tmp_path, mode)
    try:
        trial.start()
        data = _verdict(trial.move())
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        for key, value in (("executionState", "unknown"), ("attribution", "unknown"),
                           ("durability", "not_established"), ("operationOutcome", "unknown"),
                           ("treatAsSuccess", False), ("reconciliationContract", "2")):  # fmt: skip
            assert data[f"io.stateguard/{key}"] == value, key
        assert data["io.stateguard/observation"]["observed_from"]
        (evidence,) = trial.events("executor_termination")
        assert evidence["kind"] == "scope_ended" and evidence["applies"] is True
        assert evidence["executor"].startswith("pidns-")
        # (Not re-scanned here: the kernel may already have reused that namespace's
        # inode number for generation 1. The relay scanned it before generation 1.)
        assert _calls(trial.state) == [("0", "move"), ("1", "read"), ("1", "read")]
        # The old generation's whole scope is gone: nothing is left that could
        # act on "go" (generation 1 is a different scope and has no undo).
        go = str(trial.state / "go")
        assert [p for p in trial.processes() if go in _cmdline(p)] == []
        (trial.state / "go").touch()
        after = trial.read("T-2", "b.txt")
        assert after is not None and "result" in after and "error" not in after  # a real result
        assert after["result"]["content"][0]["text"] == "order 001"
        assert not (trial.state / "undone").exists()
        counts = trial.close()
        assert counts == {1: 1, "T-1": 1, "T-2": 1}
    finally:
        trial.client.close()
    assert _all_exit(trial.processes(), 10), "a process outlived the relay"


def _cmdline(pid: int) -> str:
    with contextlib.suppress(OSError):
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    return ""


@needs_scope
def test_lost_before_executing_is_observed_not_satisfied(tmp_path: Path) -> None:
    trial = _Trial(tmp_path, "die-first")
    try:
        trial.start()
        data = _verdict(trial.move())
        assert data["io.stateguard/postcondition"] == "observed_not_satisfied"
        assert data["io.stateguard/executionState"] == "unknown"
        assert data["io.stateguard/treatAsSuccess"] is False
        assert (trial.sandbox / "a.txt").exists() and not (trial.sandbox / "b.txt").exists()
        assert _calls(trial.state) == [("0", "move"), ("1", "read"), ("1", "read")]
        assert trial.close() == {1: 1, "T-1": 1}
    finally:
        trial.client.close()


@needs_scope
def test_a_scope_that_cannot_be_confirmed_empty_is_undetermined_and_never_probed(
    tmp_path: Path,
) -> None:
    """U1: the old generation's scope helper is stopped, so its end is never confirmed."""
    trial = _Trial(tmp_path, "drop")
    try:
        trial.start()
        own = os.readlink("/proc/self/ns/pid")
        (helper,) = [  # init is a fork of the helper: same command line, other namespace
            p
            for p in trial.processes()
            if "executor_scope" in _cmdline(p) and os.readlink(f"/proc/{p}/ns/pid") == own
        ]
        os.kill(helper, signal.SIGSTOP)
        started = time.monotonic()
        data = _verdict(trial.move())
        elapsed = time.monotonic() - started
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert data["io.stateguard/treatAsSuccess"] is False
        (reason,) = data["io.stateguard/reconciliation"]["reasons"]
        assert "could not be confirmed empty" in reason
        (evidence,) = trial.events("executor_termination")
        assert evidence["kind"] == "scope_unconfirmed" and evidence["applies"] is False
        assert not trial.events("reconcile_probe")
        assert _calls(trial.state) == [("0", "move")]  # nothing probed, nothing resent
        assert elapsed < 30, "bounded by the scope's end timeout and the budget"
        assert trial.close() == {1: 1, "T-1": 1}
    finally:
        trial.client.close()
    assert _all_exit(trial.processes(), 15), "the stopped scope was not cleaned up"


@needs_scope
def test_with_the_scope_and_no_reconcile_blocks_the_relay_works_as_usual(
    tmp_path: Path,
) -> None:
    trial = _Trial(
        tmp_path, "answer",
        flags=["--executor-scope", "pid-namespace", "--retry", "off"],
        effects={"tools": [{"name": "read", "effect": "read_only"},
                           {"name": "move", "effect": "non_idempotent"}]},
    )  # fmt: skip
    try:
        trial.start()
        response = trial.move()
        assert response["result"]["isError"] is False  # a real result, untouched
        (started,) = trial.events("relay_started")
        assert started["executor_scope"] == "pid-namespace"
        servers = [p for p in trial.processes() if "server.py" in _cmdline(p)
                   and "executor_scope" not in _cmdline(p)
                   and "stdio_proxy" not in _cmdline(p)]  # fmt: skip
        assert servers
        own = os.readlink("/proc/self/ns/pid")
        assert all(os.readlink(f"/proc/{p}/ns/pid") != own for p in servers)
        assert trial.close() == {1: 1, "T-1": 1}
    finally:
        trial.client.close()
    assert (trial.sandbox / "b.txt").exists()


@needs_scope
def test_killing_the_relay_leaves_no_process_behind(tmp_path: Path) -> None:
    trial = _Trial(tmp_path, "answer")
    try:
        trial.start()
        before = trial.processes()
        assert len(before) >= 4  # relay, helper, init, server
        trial.client.proc.kill()
        trial.client.proc.wait(10)
        assert _all_exit(before, 10), "the scope must end when its relay dies"
        assert trial.processes() == []
    finally:
        trial.client.close()


@needs_scope
def test_shadow_with_and_without_the_scope_is_byte_identical(tmp_path: Path) -> None:
    """Shadow Mode stays non-intervening: same bytes as no relay, scope or not."""
    outputs = {}
    for name, prefix in (
        ("raw", None),
        ("shadow", [sys.executable, str(RELAY), "--shadow", "--"]),
        ("shadow_scope", [sys.executable, str(RELAY), "--shadow", "--executor-scope",
                          "pid-namespace", "--"]),
    ):  # fmt: skip
        run = tmp_path / name
        state, sandbox = run / "state", run / "sandbox"
        state.mkdir(parents=True)
        sandbox.mkdir()
        (sandbox / "a.txt").write_text("order 001")
        (run / "server.py").write_text(SERVER)
        server = [sys.executable, str(run / "server.py"), str(state), "answer"]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "read", "arguments": {"path": str(sandbox / "a.txt")}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "move", "arguments": {"source": str(sandbox / "a.txt"),
                                              "destination": str(sandbox / "b.txt")}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "read", "arguments": {"path": str(sandbox / "a.txt")}}},
        ]  # fmt: skip
        data = b"".join(json.dumps(r).encode() + b"\n" for r in requests)
        proc = subprocess.run((prefix or []) + server, input=data, capture_output=True, timeout=60)
        assert proc.returncode == 0, proc.stderr.decode()[-500:]
        outputs[name] = proc.stdout
    assert outputs["raw"].count(b"\n") == 5
    assert outputs["shadow"] == outputs["raw"]
    assert outputs["shadow_scope"] == outputs["raw"]


# ---------------------------------------------------------------------------
# Refusals: checked before any server starts; never downgraded
# ---------------------------------------------------------------------------


def _refused(tmp_path: Path, flags: list[str], command: list[str] | None = None,
             wrapper: list[str] | None = None) -> str:  # fmt: skip
    if wrapper:  # another user must be able to read the files: not under pytest's 0700 root
        tmp_path = Path(tempfile.mkdtemp(prefix="stateguard-scope-"))
        tmp_path.chmod(0o777)
    effects = tmp_path / "effects.json"
    effects.write_text(json.dumps(EFFECTS))
    marker = tmp_path / "server-started"
    command = command or [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    proc = subprocess.run(
        [*(wrapper or []), sys.executable, str(RELAY), "--effects", str(effects), *flags, "--",
         *command],
        input=b"", capture_output=True, timeout=60, cwd=ROOT,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr.decode()
    assert not marker.exists(), "no server may start"
    return proc.stderr.decode()


def test_reconcile_without_the_scope_is_refused(tmp_path: Path) -> None:
    error = _refused(tmp_path, ["--restart", "2", "--restart-policy", "after-unknown"])
    assert "need --executor-scope pid-namespace" in error


def test_reconcile_with_the_scope_but_no_restart_is_refused(tmp_path: Path) -> None:
    for flags in (
        ["--executor-scope", "pid-namespace"],
        ["--executor-scope", "pid-namespace", "--restart", "2"],  # policy idle-only
    ):
        assert "add --restart N --restart-policy after-unknown" in _refused(tmp_path, flags)


def test_reconcile_in_shadow_or_passthrough_is_refused_even_with_the_scope(
    tmp_path: Path,
) -> None:
    for mode in ("--shadow", "--passthrough"):
        error = _refused(tmp_path, [mode, *SCOPED])
        assert "never used in shadow or passthrough mode" in error


@needs_scope
def test_a_missing_server_program_is_refused(tmp_path: Path) -> None:
    error = _refused(tmp_path, SCOPED, command=[str(tmp_path / "no-such-server")])
    assert "the server program" in error and "was not found" in error


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.geteuid() == 0 and shutil.which("setpriv")),
    reason="needs root and setpriv to run as another user",
)
def test_without_cap_sys_admin_the_scope_is_refused_and_nothing_starts(tmp_path: Path) -> None:
    error = _refused(
        tmp_path, SCOPED, wrapper=["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"]
    )
    assert "--executor-scope pid-namespace is not available here" in error
    assert "CAP_SYS_ADMIN" in error and "Nothing was started" in error


# ---------------------------------------------------------------------------
# Architecture B (Week 4G): the CLI selection is explicit and reconciliation works
# ---------------------------------------------------------------------------


@needs_scope_b
def test_b_reconcile_end_to_end_ends_scope_before_probe_and_labels_the_audit(
    tmp_path: Path,
) -> None:
    """The unprivileged scope satisfies reconcile blocks, ends the old generation's
    scope before probing, and records the exact architecture in the audit."""
    trial = _Trial(tmp_path, "detached", flags=SCOPED_UNPRIV)
    try:
        trial.start()
        (started,) = trial.events("relay_started")
        assert started["executor_scope"] == "pid-namespace-unprivileged"
        data = _verdict(trial.move())
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        assert data["io.stateguard/treatAsSuccess"] is False
        (evidence,) = trial.events("executor_termination")
        assert evidence["kind"] == "scope_ended" and evidence["applies"] is True
        assert evidence["executor"].startswith("pidns-")
        # No replay of the lost move; the detached descendant is gone with the scope.
        assert _calls(trial.state) == [("0", "move"), ("1", "read"), ("1", "read")]
        (trial.state / "go").touch()
        after = trial.read("T-2", "b.txt")
        assert after is not None and "result" in after and "error" not in after
        assert not (trial.state / "undone").exists()
        assert trial.close() == {1: 1, "T-1": 1, "T-2": 1}
    finally:
        trial.client.close()
    assert _all_exit(trial.processes(), 10)


@needs_scope_b
def test_b_scope_that_cannot_be_confirmed_empty_is_undetermined_and_never_probed(
    tmp_path: Path,
) -> None:
    """Fail-closed under B: a stopped scope helper leaves the end unconfirmed."""
    trial = _Trial(tmp_path, "drop", flags=SCOPED_UNPRIV)
    try:
        trial.start()
        own = os.readlink("/proc/self/ns/pid")
        (helper,) = [
            p
            for p in trial.processes()
            if "executor_scope" in _cmdline(p) and os.readlink(f"/proc/{p}/ns/pid") == own
        ]
        os.kill(helper, signal.SIGSTOP)
        data = _verdict(trial.move())
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert data["io.stateguard/treatAsSuccess"] is False
        (evidence,) = trial.events("executor_termination")
        assert evidence["kind"] == "scope_unconfirmed" and evidence["applies"] is False
        assert not trial.events("reconcile_probe")
        assert _calls(trial.state) == [("0", "move")]  # nothing probed, nothing resent
    finally:
        trial.client.close()
    assert _all_exit(trial.processes(), 15)


def test_the_executor_scope_choices_are_exactly_the_three_known_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown architecture is rejected by argparse: the selection is explicit."""
    from runtime.harness import load_relay_module

    relay = load_relay_module()
    with pytest.raises(SystemExit) as exit_info:
        relay.main(["--executor-scope", "userns-magic", "--", sys.executable, "-c", "pass"])
    assert exit_info.value.code == 2


def test_on_a_platform_without_pid_namespaces_the_scope_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In-process main(), with the platform reported as macOS: refused, nothing started."""
    from runtime.harness import load_relay_module

    relay = load_relay_module()
    effects = tmp_path / "effects.json"
    effects.write_text(json.dumps(EFFECTS))
    marker = tmp_path / "server-started"
    started: list[Any] = []
    monkeypatch.setattr(relay, "PidNamespaceScope", lambda *a, **k: started.append(a))
    monkeypatch.setattr(relay.subprocess, "Popen", lambda *a, **k: started.append(a))
    for platform in ("darwin", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        with pytest.raises(SystemExit) as exit_info:
            relay.main(["--effects", str(effects), *SCOPED, "--", sys.executable, "-c", "pass"])
        assert exit_info.value.code == 2
        error = capsys.readouterr().err
        assert "is not available here" in error and f"this platform is {platform}" in error
    assert started == [] and not marker.exists()
