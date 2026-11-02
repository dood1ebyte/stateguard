"""
The PID-namespace executor scope, against the real kernel.

Every test that starts a scope needs Linux and permission to create a PID
namespace; without it they are skipped (and the refusal itself is tested).
Nothing waits by sleeping: process exits are awaited on pidfds, stream EOF on
a reader thread's event, each with a bounded timeout.
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
import threading
import time
from pathlib import Path
from typing import IO

import pytest

from stateguard.adapters.mcp import TerminationEvidence, TerminationKind
from stateguard.adapters.mcp import executor_scope as scope_module
from stateguard.adapters.mcp.executor_scope import (
    END_TIMEOUT_SECONDS,
    PidNamespaceScope,
    ScopeError,
    check_pid_namespace_capability,
    namespace_members,
)

CAPABILITY = check_pid_namespace_capability()
needs_scope = pytest.mark.skipif(not CAPABILITY.available, reason=CAPABILITY.reason)
needs_root_tools = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.geteuid() == 0 and shutil.which("setpriv")),
    reason="needs root and setpriv to run as another user",
)
ROOT = Path(__file__).resolve().parents[3]

#: A server for these tests. Modes:
#:   stay      -- leave a detached (setsid, double-forked) descendant that ignores
#:                SIGTERM and holds none of the server's stdio; report; serve lines
#:   close     -- as stay, then close stdout and keep running
#:   echo-args -- print argv[2:] as JSON and exit
SERVER = r"""
import json, os, signal, sys, time
mode = sys.argv[1]
if mode == "echo-args":
    print(json.dumps(sys.argv[2:]), flush=True)
    sys.exit(0)
r, w = os.pipe()
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.write(w, str(os.getpid()).encode()); os.close(w)
        while True:
            time.sleep(3600)
    os._exit(0)
os.close(w)
descendant = int(os.read(r, 32))
os.wait()
print(json.dumps({"ns": os.readlink("/proc/self/ns/pid"), "pid": os.getpid(),
                  "descendant": descendant}), flush=True)
if mode == "close":
    sys.stdout.close(); os.close(1)
    while True:
        time.sleep(3600)
for line in sys.stdin:
    if line.strip() == "exit":
        sys.exit(3)
"""


@pytest.fixture
def server(tmp_path: Path) -> list[str]:
    path = tmp_path / "server.py"
    path.write_text(SERVER)
    return [sys.executable, str(path)]


def _all_exit(pids: list[int], timeout: float) -> bool:
    """True once every process in *pids* has exited (awaited on pidfds, bounded)."""
    fds = []
    try:
        for pid in pids:
            try:
                fds.append(os.pidfd_open(pid))
            except ProcessLookupError:
                continue  # already gone
        deadline = time.monotonic() + timeout
        waiting = set(fds)
        poller = select.poll()
        for fd in fds:
            poller.register(fd, select.POLLIN)
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


def _read_line(stream: IO[bytes], timeout: float) -> bytes | None:
    """One line from *stream*, or None if none came in *timeout* (b"" at EOF)."""
    got: list[bytes] = []
    done = threading.Event()

    def read() -> None:
        got.append(stream.readline())
        done.set()

    threading.Thread(target=read, daemon=True).start()
    return got[0] if done.wait(timeout) else None


def _live_members(namespace: str) -> list[int]:
    """Members that are not zombies. An abandoned init is re-parented and its
    zombie waits for that new parent to reap it -- it can no longer act."""
    live = []
    for pid in namespace_members(namespace):
        with contextlib.suppress(OSError):
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state != "Z":
                live.append(pid)
    return live


def _fd_targets(pid: int) -> set[str]:
    targets = set()
    with contextlib.suppress(OSError):
        for fd in os.listdir(f"/proc/{pid}/fd"):
            with contextlib.suppress(OSError):
                targets.add(os.readlink(f"/proc/{pid}/fd/{fd}"))
    return targets


# ---------------------------------------------------------------------------
# Capability detection: actionable, and never downgraded
# ---------------------------------------------------------------------------


def test_capability_is_refused_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    for platform in ("darwin", "win32", "freebsd14"):
        monkeypatch.setattr(sys, "platform", platform)
        capability = check_pid_namespace_capability()
        assert capability.available is False
        assert "PID namespaces need Linux" in capability.reason and platform in capability.reason


def test_capability_is_refused_without_proc_pid_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    real = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: False if p == "/proc/self/ns/pid" else real(p))
    capability = check_pid_namespace_capability()
    assert capability.available is False and "/proc/self/ns/pid is missing" in capability.reason


def test_a_probe_without_a_report_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    if not sys.platform.startswith("linux") or not os.path.exists("/proc/self/ns/pid"):
        pytest.skip("needs /proc/self/ns/pid")
    monkeypatch.setattr(scope_module, "_MODULE", "stateguard.no_such_module_for_this_test")
    capability = check_pid_namespace_capability()
    assert capability.available is False and "no report" in capability.reason


@needs_root_tools
def test_capability_is_refused_without_cap_sys_admin() -> None:
    probe = subprocess.run(
        ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", sys.executable, "-c",
         "import json; from stateguard.adapters.mcp.executor_scope import "
         "check_pid_namespace_capability as c; r = c(); print(json.dumps([r.available, r.reason]))"],
        capture_output=True, timeout=30, cwd=ROOT,
    )  # fmt: skip
    available, reason = json.loads(probe.stdout)
    assert available is False
    assert "Operation not permitted" in reason and "CAP_SYS_ADMIN" in reason
    assert "Unprivileged user namespaces are not used" in reason


@needs_root_tools
@needs_scope
def test_cap_sys_admin_alone_is_enough_root_is_not_required() -> None:
    probe = subprocess.run(
        ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups",
         "--inh-caps=+sys_admin", "--ambient-caps=+sys_admin", sys.executable, "-c",
         "import json; from stateguard.adapters.mcp.executor_scope import "
         "check_pid_namespace_capability as c; r = c(); print(json.dumps([r.available, r.reason]))"],
        capture_output=True, timeout=30, cwd=ROOT,
    )  # fmt: skip
    available, reason = json.loads(probe.stdout)
    assert available is True, reason


@needs_root_tools
def test_starting_a_scope_without_permission_raises_and_leaves_nothing() -> None:
    probe = subprocess.run(
        ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", sys.executable, "-c",
         "from stateguard.adapters.mcp.executor_scope import PidNamespaceScope, ScopeError\n"
         "try:\n    PidNamespaceScope(['true'])\nexcept ScopeError as e:\n    print(e)\n"],
        capture_output=True, timeout=30, cwd=ROOT,
    )  # fmt: skip
    out = probe.stdout.decode()
    assert "did not start" in out and "unshare(CLONE_NEWPID) failed" in out


def test_an_empty_command_is_refused() -> None:
    with pytest.raises(ScopeError, match="no server command"):
        PidNamespaceScope([])


# ---------------------------------------------------------------------------
# Confinement, termination, stdio independence
# ---------------------------------------------------------------------------


@needs_scope
def test_a_generation_runs_in_its_own_namespace_and_its_end_is_confirmed(
    server: list[str],
) -> None:
    scope = PidNamespaceScope([*server, "stay"])
    try:
        line = _read_line(scope.stdout, 10)
        assert line, "the server never reported"
        report = json.loads(line)
        assert report["ns"] == scope.namespace != os.readlink("/proc/self/ns/pid")
        assert report["pid"] == 2  # init is PID 1; the server is its only child
        members = namespace_members(scope.namespace)
        assert len(members) == 3  # init, the server, its detached descendant
        scope.stdin.write(b"exit\n")
        scope.stdin.flush()
        assert _read_line(scope.stdout, 10) == b""  # EOF: the server exited
        evidence = scope.end()
        assert evidence.kind is TerminationKind.SCOPE_ENDED and evidence.established
        assert evidence.executor == scope.identity
        assert evidence.applies_to(scope.identity) is None
        assert scope.namespace in evidence.detail and "no member left" in evidence.detail
        assert scope.returncode == 3
        assert namespace_members(scope.namespace) == []
        assert _all_exit(members, 0)  # every one of them is already gone
    finally:
        scope.end()


@needs_scope
def test_a_detached_descendant_that_ignores_sigterm_cannot_outlive_the_scope(
    server: list[str],
) -> None:
    scope = PidNamespaceScope([*server, "stay"])
    try:
        assert _read_line(scope.stdout, 10)
        members = namespace_members(scope.namespace)
        started = time.monotonic()
        evidence = scope.end()  # the server is still running: SIGKILL to init
        assert time.monotonic() - started < END_TIMEOUT_SECONDS
        assert evidence.kind is TerminationKind.SCOPE_ENDED
        assert scope.returncode == 137  # init exited because SIGKILL ended the server
        assert _all_exit(members, 0)
        assert namespace_members(scope.namespace) == []
    finally:
        scope.end()


@needs_scope
def test_stdout_closed_while_alive_reaches_the_relay_at_once(server: list[str]) -> None:
    """No wrapper holds the server's stdout: EOF arrives while the server still runs."""
    scope = PidNamespaceScope([*server, "close"])
    try:
        assert _read_line(scope.stdout, 10)
        assert _read_line(scope.stdout, 5) == b"", "EOF must not wait for the server to exit"
        assert len(namespace_members(scope.namespace)) == 3  # still alive, all of them
        evidence = scope.end()
        assert evidence.kind is TerminationKind.SCOPE_ENDED
        assert namespace_members(scope.namespace) == []
    finally:
        scope.end()


@needs_scope
def test_neither_wrapper_process_holds_the_servers_stdio(server: list[str]) -> None:
    scope = PidNamespaceScope([*server, "stay"])
    try:
        assert _read_line(scope.stdout, 10)
        pipes = {
            f"pipe:[{os.fstat(stream.fileno()).st_ino}]" for stream in (scope.stdin, scope.stdout)
        }
        helper = scope._helper.pid
        assert not pipes & _fd_targets(helper)
        assert not pipes & _fd_targets(scope.init_pid)
    finally:
        scope.end()


@needs_scope
def test_every_generation_has_a_distinct_scope_and_identity(server: list[str]) -> None:
    first = PidNamespaceScope([*server, "stay"])
    second = PidNamespaceScope([*server, "stay"])
    try:
        assert first.namespace != second.namespace
        assert first.identity != second.identity
        one, two = first.end(), second.end()
        assert one.executor == first.identity and two.executor == second.identity
        # Evidence for one generation never applies to another.
        assert one.applies_to(second.identity) is not None
        assert two.applies_to(first.identity) is not None
    finally:
        first.end()
        second.end()


@needs_scope
def test_ending_is_idempotent(server: list[str]) -> None:
    scope = PidNamespaceScope([*server, "stay"])
    assert scope.end() is scope.end()


@needs_scope
def test_arguments_are_never_interpreted_by_a_shell(server: list[str], tmp_path: Path) -> None:
    marker = tmp_path / "injected"
    hostile = [f"$(touch {marker})", f"; touch {marker}", "`id`", "a b", "*"]
    scope = PidNamespaceScope([*server, "echo-args", *hostile])
    try:
        line = _read_line(scope.stdout, 10)
        assert line and json.loads(line) == hostile
        assert scope.end().kind is TerminationKind.SCOPE_ENDED
        assert not marker.exists()
    finally:
        scope.end()


@needs_scope
def test_a_program_that_cannot_be_executed_exits_127_inside_the_scope(tmp_path: Path) -> None:
    scope = PidNamespaceScope([str(tmp_path / "no-such-program")])
    try:
        assert _read_line(scope.stdout, 10) == b""
        assert scope.end().kind is TerminationKind.SCOPE_ENDED
        assert scope.returncode == 127
    finally:
        scope.end()


# ---------------------------------------------------------------------------
# Incomplete confinement fails closed
# ---------------------------------------------------------------------------


@needs_scope
def test_a_stopped_helper_is_unconfirmed_within_the_bound_and_still_cleaned_up(
    server: list[str],
) -> None:
    scope = PidNamespaceScope([*server, "stay"])
    try:
        assert _read_line(scope.stdout, 10)
        members = namespace_members(scope.namespace)
        os.kill(scope._helper.pid, signal.SIGSTOP)
        started = time.monotonic()
        evidence = scope.end(timeout=0.5)
        elapsed = time.monotonic() - started
        assert evidence.kind is TerminationKind.SCOPE_UNCONFIRMED and not evidence.established
        assert evidence.applies_to(scope.identity) is not None
        assert "no end report" in evidence.detail
        assert elapsed < 0.5 + END_TIMEOUT_SECONDS
        # Cleanup (not evidence): the helper was killed; init died by PDEATHSIG.
        assert _all_exit(members, 10)
        assert _live_members(scope.namespace) == []
    finally:
        scope.end()


@needs_scope
def test_a_killed_helper_empties_the_namespace_and_is_unconfirmed(server: list[str]) -> None:
    scope = PidNamespaceScope([*server, "stay"])
    try:
        assert _read_line(scope.stdout, 10)
        members = namespace_members(scope.namespace)
        os.kill(scope._helper.pid, signal.SIGKILL)
        assert _all_exit(members, 10), "init must die with its helper (PR_SET_PDEATHSIG)"
        evidence = scope.end()
        assert evidence.kind is TerminationKind.SCOPE_UNCONFIRMED
    finally:
        scope.end()


@needs_scope
def test_a_member_still_visible_to_the_relay_is_unconfirmed(
    server: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relay's own scan must agree with the helper's, or nothing is established."""
    scope = PidNamespaceScope([*server, "stay"])
    try:
        assert _read_line(scope.stdout, 10)
        monkeypatch.setattr(scope_module, "namespace_members", lambda namespace: [424242])
        evidence = scope.end()
        assert evidence.kind is TerminationKind.SCOPE_UNCONFIRMED
        assert "1 process(es) still in the namespace" in evidence.detail
    finally:
        monkeypatch.undo()
        scope.end()


@needs_scope
def test_an_owner_killed_abruptly_leaves_no_process_behind(server: list[str]) -> None:
    """The relay dies with SIGKILL: the helper sees its control pipe close and ends init."""
    owner = subprocess.Popen(
        [sys.executable, "-c",
         "import json, sys\n"
         "from stateguard.adapters.mcp.executor_scope import PidNamespaceScope, "
         "namespace_members\n"
         f"s = PidNamespaceScope({[*server, 'stay']!r})\n"
         "s.stdout.readline()\n"
         "print(json.dumps([s.namespace, namespace_members(s.namespace), s._helper.pid]), "
         "flush=True)\n"
         "sys.stdin.read()\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=ROOT,
    )  # fmt: skip
    try:
        assert owner.stdout is not None
        line = _read_line(owner.stdout, 20)
        assert line, "the owner never reported"
        namespace, members, helper = json.loads(line)
        assert len(members) == 3
        owner.kill()
        owner.wait(10)
        assert _all_exit([*members, helper], 10)
        assert namespace_members(namespace) == []
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()


def test_unconfirmed_evidence_never_establishes_termination() -> None:
    evidence = TerminationEvidence.scope_unconfirmed("stuck")
    assert evidence.kind is TerminationKind.SCOPE_UNCONFIRMED
    assert evidence.established is False
    assert evidence.applies_to("any-executor") is not None
    assert evidence.applies_to(None) is not None


@needs_scope
@pytest.mark.parametrize("hold", [True, False])
def test_the_namespace_is_held_so_its_end_is_never_judged_by_a_newer_one(
    server: list[str], monkeypatch: pytest.MonkeyPatch, hold: bool
) -> None:
    """
    The kernel reuses a freed namespace's inode number at once. The scope keeps a
    reference until its end is confirmed, so the scans in ``end`` are about this
    namespace only. (``hold=False`` shows what happens without it.)
    """
    if not hold:
        monkeypatch.setattr(PidNamespaceScope, "_hold_namespace", lambda self: None)
    first = PidNamespaceScope([*server, "echo-args"])
    second: PidNamespaceScope | None = None
    try:
        assert _read_line(first.stdout, 10)
        assert _all_exit([first.init_pid], 10)  # the first namespace has no member left
        second = PidNamespaceScope([*server, "stay"])
        assert _read_line(second.stdout, 10)
        evidence = first.end()
        if hold:
            assert second.namespace != first.namespace
            assert evidence.kind is TerminationKind.SCOPE_ENDED
        elif second.namespace == first.namespace:  # reused: the newer one's members count
            assert evidence.kind is TerminationKind.SCOPE_UNCONFIRMED
    finally:
        first.end()
        if second is not None:
            second.end()


@needs_scope
def test_a_package_planted_in_the_working_directory_never_runs_as_the_helper(
    server: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = tmp_path / "planted"
    (planted / "stateguard").mkdir(parents=True)
    marker = tmp_path / "planted-ran"
    (planted / "stateguard" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').close()\nraise SystemExit(9)\n"
    )
    monkeypatch.chdir(planted)
    assert check_pid_namespace_capability().available
    scope = PidNamespaceScope([*server, "stay"])
    try:
        assert _read_line(scope.stdout, 10)
        assert scope.end().kind is TerminationKind.SCOPE_ENDED
    finally:
        scope.end()
    assert not marker.exists()


@needs_scope
@pytest.mark.parametrize("code", [0, 3])
def test_a_clean_server_exit_reports_its_status_not_a_kill(code: int) -> None:
    """A server that exits on its own (stdin EOF) just before the scope ends must
    report its real status, never 137 from a teardown-race SIGKILL. Repeated to
    stress the race the end-grace closes."""
    program = f"import sys; [ln for ln in sys.stdin]; sys.exit({code})"
    for _ in range(5):
        scope = PidNamespaceScope([sys.executable, "-c", program])
        try:
            scope.stdin.close()  # EOF: the server reads nothing and exits `code`
            assert _read_line(scope.stdout, 10) == b""  # its stdout closes as it exits
            evidence = scope.end()
            assert evidence.kind is TerminationKind.SCOPE_ENDED
            assert scope.returncode == code, f"clean exit {code} became {scope.returncode}"
        finally:
            scope.end()
