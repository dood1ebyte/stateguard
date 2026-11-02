"""
Week 4B — privilege boundary of the PID-namespace executor scope.

The scope is a *lifecycle* boundary (termination evidence), hardened so that it
never hands the supervised server the CAP_SYS_ADMIN it required to create the
namespace. It is deliberately NOT a sandbox: these tests also pin the limits
that remain (shared host namespaces, inherited resource limits), so the audit
cannot drift into claiming isolation it does not provide.

Every scope is torn down in a finally. Process exits are awaited on pidfds.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import IO, Any

import pytest

from stateguard.adapters.mcp import TerminationKind
from stateguard.adapters.mcp.executor_scope import (
    PidNamespaceScope,
    _confine_server,
    _confinement_gap,
    _libc,
    _verify_confined,
    check_pid_namespace_capability,
)

CAPABILITY = check_pid_namespace_capability()
needs_scope = pytest.mark.skipif(not CAPABILITY.available, reason=CAPABILITY.reason)
needs_root_tools = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.geteuid() == 0 and shutil.which("setpriv")),
    reason="needs root and setpriv to run as another user",
)
ROOT = Path(__file__).resolve().parents[3]
_CAP_SYS_ADMIN_BIT = 1 << 21

#: A server that reports its own security context as one JSON line, then serves
#: lines until "exit". ``kill-init`` first tries to kill PID 1 of its namespace.
SECURITY_SERVER = r"""
import ctypes, json, os, resource, signal, sys, time
mode = sys.argv[1] if len(sys.argv) > 1 else "report"
def status():
    out = {}
    for line in open("/proc/self/status"):
        k, _, v = line.partition(":")
        if k in ("CapInh","CapPrm","CapEff","CapBnd","CapAmb","NoNewPrivs"):
            out[k] = v.strip()
    return out
def _fds():
    out = {}
    for fd in os.listdir("/proc/self/fd"):  # the listdir handle itself may vanish
        try:
            out[fd] = os.readlink("/proc/self/fd/" + fd)
        except OSError:
            pass
    return out
libc = ctypes.CDLL(None, use_errno=True)
CLONE_NEWNS = 0x00020000
killed = {}
if mode == "kill-init":
    for name in ("SIGKILL", "SIGTERM"):
        try:
            os.kill(1, getattr(signal, name)); killed[name] = "sent"
        except OSError as e:
            killed[name] = f"errno {e.errno}"
    time.sleep(0.2)
newns_rc = libc.unshare(CLONE_NEWNS)
report = {
    "uid": os.getuid(), "euid": os.geteuid(), "pid_in_ns": os.getpid(),
    "status": status(),
    "fds": _fds(),
    "cap_sys_admin_live": newns_rc == 0,
    "ns_mnt": os.readlink("/proc/self/ns/mnt"),
    "ns_pid": os.readlink("/proc/self/ns/pid"),
    "rlimit_nproc": resource.getrlimit(resource.RLIMIT_NPROC),
    "rlimit_as": resource.getrlimit(resource.RLIMIT_AS),
    "init_alive_after_kill": os.path.exists("/proc/1") if mode == "kill-init" else None,
    "killed": killed,
}
sys.stdout.write(json.dumps(report) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    if line.strip() == "exit":
        break
"""


@pytest.fixture
def security_server(tmp_path: Path) -> list[str]:
    path = tmp_path / "security_server.py"
    path.write_text(SECURITY_SERVER)
    return [sys.executable, str(path)]


def _read_line(stream: IO[bytes], timeout: float) -> bytes | None:
    got: list[bytes] = []
    done = threading.Event()

    def read() -> None:
        got.append(stream.readline())
        done.set()

    threading.Thread(target=read, daemon=True).start()
    return got[0] if done.wait(timeout) else None


def _report(server: list[str], mode: str = "report") -> dict[str, Any]:
    scope = PidNamespaceScope([*server, mode])
    try:
        line = _read_line(scope.stdout, 10)
        assert line, "the server never reported"
        data: dict[str, Any] = json.loads(line)
        return data
    finally:
        scope.end()


# ---------------------------------------------------------------------------
# The verification logic (pure): every gap that must block the exec
# ---------------------------------------------------------------------------


def test_confinement_gap_flags_live_ambient_capabilities() -> None:
    assert _confinement_gap(_CAP_SYS_ADMIN_BIT, 0, True, False) == (
        "ambient capabilities are not empty after the drop"
    )


def test_confinement_gap_flags_missing_no_new_privs() -> None:
    assert "no_new_privs" in str(_confinement_gap(0, 0, False, False))


def test_confinement_gap_flags_cap_sys_admin_in_a_root_bounding_set() -> None:
    assert "bounding set" in str(_confinement_gap(0, _CAP_SYS_ADMIN_BIT, True, True))


def test_confinement_gap_accepts_a_confined_root_or_nonroot_process() -> None:
    assert _confinement_gap(0, _CAP_SYS_ADMIN_BIT, True, False) is None  # non-root: bnd ignored
    assert _confinement_gap(0, 0, True, True) is None  # root with CAP_SYS_ADMIN dropped


def test_an_unconfined_process_fails_verification() -> None:
    # This test process never dropped privileges, so verification must object.
    assert _verify_confined() is not None


def test_confine_propagates_a_verification_gap_so_the_server_never_execs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: if the boundary cannot be confirmed, _confine_server reports it
    (the server child then exits 126 instead of exec'ing)."""
    monkeypatch.setattr(
        "stateguard.adapters.mcp.executor_scope._verify_confined", lambda: "simulated gap"
    )
    read_fd, write_fd = os.pipe()
    pid = os.fork()  # a child, so the real prctls do not disarm the test process
    if pid == 0:
        os.close(read_fd)
        os.write(write_fd, (_confine_server(_libc()) or "").encode())
        os._exit(0)
    os.close(write_fd)
    reason = os.read(read_fd, 200).decode()
    os.close(read_fd)
    os.waitpid(pid, 0)
    assert reason == "simulated gap"


# ---------------------------------------------------------------------------
# The privilege boundary, against the real kernel
# ---------------------------------------------------------------------------


@needs_scope
def test_a_root_scoped_server_never_holds_cap_sys_admin(security_server: list[str]) -> None:
    report = _report(security_server)
    assert report["euid"] == 0, "this test exercises the root path"
    status = report["status"]
    for field in ("CapEff", "CapPrm", "CapBnd"):
        assert not int(status[field], 16) & _CAP_SYS_ADMIN_BIT, f"{field} still has CAP_SYS_ADMIN"
    assert int(status["CapAmb"], 16) == 0
    assert status["NoNewPrivs"] == "1"


@needs_scope
def test_cap_sys_admin_is_not_live_in_a_scoped_server(security_server: list[str]) -> None:
    # unshare(CLONE_NEWNS) needs CAP_SYS_ADMIN; it must now be refused.
    assert _report(security_server)["cap_sys_admin_live"] is False


@needs_scope
def test_a_scoped_server_cannot_kill_its_own_init(security_server: list[str]) -> None:
    """The server (a namespace member) cannot end the scope's init; the kernel
    discards its signals to PID 1. The scope still ends from the outside."""
    scope = PidNamespaceScope([*security_server, "kill-init"])
    try:
        report = json.loads(_read_line(scope.stdout, 10) or b"{}")
        assert report["init_alive_after_kill"] is True
        evidence = scope.end()
        assert evidence.kind is TerminationKind.SCOPE_ENDED  # the helper ended it, not the server
    finally:
        scope.end()


@needs_scope
def test_a_scoped_server_holds_only_its_stdio(security_server: list[str]) -> None:
    fds = _report(security_server)["fds"]
    # 0 and 1 are the relay's pipes; any fd >= 3 must not be a relay pipe or socket
    # left open (closerange closed them). A transient /proc listing fd may appear.
    leaked = {
        fd: tgt for fd, tgt in fds.items() if int(fd) >= 3 and ("pipe:" in tgt or "socket:" in tgt)
    }
    assert leaked == {}, f"the server kept extra channels open: {leaked}"


@needs_root_tools
@needs_scope
def test_enabling_the_scope_does_not_amplify_a_nonroot_servers_privilege(tmp_path: Path) -> None:
    """A uid-65534 relay with ambient CAP_SYS_ADMIN: without the scope the server
    cannot use CAP_SYS_ADMIN, and with the scope it still cannot (the ambient
    grant is dropped before exec). The scope adds no privilege."""
    # A world-traversable dir: pytest's tmp_path and its parents are 0700, which
    # uid 65534 cannot enter.
    work = Path(tempfile.mkdtemp(prefix="stateguard-w4b-"))
    work.chmod(0o755)
    driver = work / "driver.py"
    driver.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from stateguard.adapters.mcp.executor_scope import PidNamespaceScope\n"
        f"s = PidNamespaceScope([sys.executable, {str(work / 's.py')!r}, 'report'])\n"
        "print(s.stdout.readline().decode().strip()); s.end()\n"
    )
    (work / "s.py").write_text(SECURITY_SERVER)
    for f in (driver, work / "s.py"):
        f.chmod(0o644)
    setpriv = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"]
    # Baseline: the same unprivileged user, no scope, cannot use CAP_SYS_ADMIN.
    probe = "import ctypes; print(ctypes.CDLL(None, use_errno=True).unshare(0x00020000))"
    base = subprocess.run([*setpriv, sys.executable, "-c", probe], capture_output=True, timeout=30)
    assert base.stdout.decode().strip() == "-1", "baseline unprivileged user already had the cap"
    # With the scope (ambient CAP_SYS_ADMIN granted only so the relay can unshare):
    scoped = subprocess.run(
        [*setpriv, "--inh-caps=+sys_admin", "--ambient-caps=+sys_admin",
         sys.executable, str(driver)],
        capture_output=True, timeout=60, cwd=str(ROOT),
    )  # fmt: skip
    line = scoped.stdout.decode().strip().splitlines()
    assert line, scoped.stderr.decode()[-800:]
    report = json.loads(line[-1])
    assert report["euid"] == 65534
    assert report["cap_sys_admin_live"] is False, "the scope amplified the server's privilege"
    assert int(report["status"]["CapEff"], 16) == 0
    assert int(report["status"]["CapAmb"], 16) == 0
    assert report["status"]["NoNewPrivs"] == "1"


# ---------------------------------------------------------------------------
# Documented limits: the scope is NOT a sandbox (fail honestly, not silently)
# ---------------------------------------------------------------------------


@needs_scope
def test_the_scope_shares_the_host_mount_namespace(security_server: list[str]) -> None:
    """No filesystem isolation: the server is in the relay's mount namespace.
    This encodes the documented boundary so the audit cannot overclaim."""
    report = _report(security_server)
    assert report["ns_mnt"] == os.readlink("/proc/self/ns/mnt")  # same as the relay
    assert report["ns_pid"] != os.readlink("/proc/self/ns/pid")  # only the PID ns is new


@needs_scope
def test_the_scope_imposes_no_resource_limit(security_server: list[str]) -> None:
    """No cgroup and no tightened rlimit: the server inherits the relay's limits
    unchanged. Documented limitation, pinned so a future change is deliberate."""
    report = _report(security_server)
    assert tuple(report["rlimit_nproc"]) == tuple(_libc_rlimit_nproc())
    # unlimited address space is inherited, not constrained by the scope
    assert report["rlimit_as"][0] in (-1, _soft_as())


def _libc_rlimit_nproc() -> tuple[int, int]:
    import resource

    return resource.getrlimit(resource.RLIMIT_NPROC)


def _soft_as() -> int:
    import resource

    return resource.getrlimit(resource.RLIMIT_AS)[0]


@needs_scope
def test_the_capability_check_includes_the_privilege_drop() -> None:
    # A kernel that could create the namespace but not drop the capability must be
    # refused. Here both work, so the capability is available and says so.
    capability = check_pid_namespace_capability()
    assert capability.available is True
    assert "confirmed it empty" in capability.reason


# ---------------------------------------------------------------------------
# Week 4D — environment-transfer security (the private-pipe policy)
# ---------------------------------------------------------------------------

_SLEEP = "import sys, time; sys.stdout.write('up\\n'); sys.stdout.flush(); time.sleep(30)"
_REPORT_BIG = (
    "import os, sys; sys.stdout.write(str(len(os.environ.get('BIG','')))+'\\n'); "
    "sys.stdout.flush(); [ln for ln in sys.stdin]"
)


def _proc_bytes(pid: int, name: str) -> bytes:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as handle:
            return handle.read()
    except OSError:
        return b""


@needs_scope
def test_a_planted_secret_reaches_only_the_server_environment_not_argv_or_the_helper() -> None:
    secret = b"sk-PLANTED-4D-SECRET-value"
    scope = PidNamespaceScope(
        [sys.executable, "-c", _SLEEP],
        env={"PATH": "/usr/bin", "API_TOKEN": secret.decode()},
        cwd="/",
    )
    try:
        assert _read_line(scope.stdout, 10) == b"up\n"
        helper = scope._helper.pid
        assert secret not in _proc_bytes(helper, "cmdline"), "secret leaked into helper argv"
        assert secret not in _proc_bytes(helper, "environ"), "secret leaked into helper environ"
        # the helper runs with an empty environment of its own
        assert _proc_bytes(helper, "environ").count(b"\x00") == 0
        # The helper's argv also contains the server command (after "--"); match the
        # server itself, whose argv is only the `python -c ...` and not the helper module.
        server = next(
            int(e)
            for e in os.listdir("/proc")
            if e.isdigit()
            and b"time.sleep(30)" in _proc_bytes(int(e), "cmdline")
            and b"executor_scope" not in _proc_bytes(int(e), "cmdline")
        )
        assert secret not in _proc_bytes(server, "cmdline"), "secret leaked into server argv"
        assert secret in _proc_bytes(server, "environ"), "the permitted secret was not delivered"
    finally:
        scope.end()


@needs_scope
def test_a_large_environment_value_is_delivered_without_deadlock() -> None:
    """A value larger than the pipe buffer streams through (the helper drains it)."""
    value = "v" * (100 * 1024)  # under MAX_ARG_STRLEN, so execvpe accepts it
    scope = PidNamespaceScope(
        [sys.executable, "-c", _REPORT_BIG], env={"PATH": "/usr/bin", "BIG": value}, cwd="/"
    )
    try:
        line = _read_line(scope.stdout, 10)
        assert line is not None and line.strip() == str(len(value)).encode()
        scope.stdin.close()
        assert scope.end().kind is TerminationKind.SCOPE_ENDED
    finally:
        scope.end()


@needs_scope
def test_an_environment_past_the_kernel_exec_limit_fails_closed_without_hanging() -> None:
    """A single value past MAX_ARG_STRLEN makes execvpe fail; the server never runs,
    the scope ends cleanly, and nothing hangs or leaks. (Documented kernel limit.)"""
    value = "v" * (256 * 1024)
    scope = PidNamespaceScope(
        [sys.executable, "-c", _REPORT_BIG], env={"PATH": "/usr/bin", "BIG": value}, cwd="/"
    )
    try:
        assert _read_line(scope.stdout, 10) == b""  # the server exec failed; no output
        evidence = scope.end()
        assert evidence.kind is TerminationKind.SCOPE_ENDED
        assert scope.returncode == 127  # execvpe failure exit code
    finally:
        scope.end()


@needs_scope
def test_a_failure_sending_the_environment_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the environment cannot be written to the helper, the half-started scope is
    torn down and ScopeError is raised -- never an unhandled error or a leak."""
    import stateguard.adapters.mcp.executor_scope as mod

    def boom(env_w: int, payload: bytes) -> None:
        os.close(env_w)  # release the fd as the real writer would
        raise BrokenPipeError("simulated helper death during environment transfer")

    monkeypatch.setattr(mod.PidNamespaceScope, "_write_env", staticmethod(boom))
    with pytest.raises(mod.ScopeError, match="server environment could not be sent"):
        PidNamespaceScope([sys.executable, "-c", "pass"], env={"PATH": "/usr/bin"}, cwd="/")
