"""
Architecture B: the unprivileged user+PID-namespace executor scope (Week 4G).

Opt-in via ``PidNamespaceScope(..., unprivileged=True)`` /
``check_pid_namespace_capability(unprivileged=True)``. These tests run against the
real kernel and are skipped where unprivileged user namespaces are unavailable.
They assert the same lifecycle and fail-closed guarantees as architecture A, plus
the two properties unique to B: it needs **no host CAP_SYS_ADMIN** (proven by
running as an unprivileged user where A is refused), and it **remaps the server's
user identity** (the server runs as namespaced root; files it writes are owned by
the relay's host uid). The capability-drop (CAP_SYS_ADMIN) still holds in B.

Nothing waits by sleeping: exits are awaited on pidfds, stream EOF on a reader
thread, each bounded. The harness (SERVER, _read_line, _all_exit, _live_members)
is shared with ``test_executor_scope.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from stateguard.adapters.mcp import TerminationKind
from stateguard.adapters.mcp import executor_scope as scope_module
from stateguard.adapters.mcp.executor_scope import (
    END_TIMEOUT_SECONDS,
    PidNamespaceScope,
    ScopeError,
    check_pid_namespace_capability,
    namespace_members,
)
from tests.adapters.mcp.test_executor_scope import (
    ROOT,
    SERVER,
    _all_exit,
    _live_members,
    _read_line,
)

CAPABILITY_B = check_pid_namespace_capability(unprivileged=True)
needs_userns = pytest.mark.skipif(not CAPABILITY_B.available, reason=CAPABILITY_B.reason)
needs_root_tools = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.geteuid() == 0 and shutil.which("setpriv")),
    reason="needs root and setpriv to run as an unprivileged user",
)

#: A server that reports its in-namespace identity, capability state, and writes a
#: file (whose host owner reveals the uid remap). Then serves lines until "exit".
IDENTITY_SERVER = r"""
import json, os, sys
caps = {}
for line in open("/proc/self/status"):
    k, _, v = line.partition(":")
    if k in ("CapAmb", "CapBnd", "CapEff", "NoNewPrivs"):
        caps[k] = v.strip()
made = os.path.join(sys.argv[1], "made_by_server")
open(made, "w").write("x")
print(json.dumps({"uid": os.getuid(), "euid": os.geteuid(),
                  "ns": os.readlink("/proc/self/ns/pid"), "pid": os.getpid(),
                  "caps": caps, "made": made}), flush=True)
for line in sys.stdin:
    if line.strip() == "exit":
        sys.exit(5)
"""

_CAP_SYS_ADMIN_BIT = 1 << 21


@pytest.fixture
def server(tmp_path: Path) -> list[str]:
    path = tmp_path / "server.py"
    path.write_text(SERVER)
    return [sys.executable, str(path)]


@pytest.fixture
def identity_server(tmp_path: Path) -> list[str]:
    path = tmp_path / "identity.py"
    path.write_text(IDENTITY_SERVER)
    return [sys.executable, str(path), str(tmp_path)]


# ---------------------------------------------------------------------------
# Capability detection for B, separate from A, never substituted
# ---------------------------------------------------------------------------


def test_the_two_architectures_use_separate_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """B's probe command is ``probe-userns``; A's is ``probe``. Never the same subprocess."""
    if not sys.platform.startswith("linux") or not os.path.exists("/proc/self/ns/pid"):
        pytest.skip("needs /proc/self/ns/pid")
    seen: list[str] = []

    def stub(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(cmd[-1])  # record only; do not actually spawn a probe
        return subprocess.CompletedProcess(cmd, 0, stdout=b'{"ok": false, "why": "stub"}\n')

    monkeypatch.setattr(subprocess, "run", stub)
    check_pid_namespace_capability()
    check_pid_namespace_capability(unprivileged=True)
    assert seen == ["probe", "probe-userns"]


@needs_userns
def test_b_capability_is_available_and_describes_the_user_namespace() -> None:
    assert CAPABILITY_B.available
    assert "user+PID namespace (unprivileged)" in CAPABILITY_B.reason


# ---------------------------------------------------------------------------
# The property that justifies B: no host CAP_SYS_ADMIN
# ---------------------------------------------------------------------------


@needs_root_tools
@needs_userns
def test_b_works_unprivileged_where_a_is_refused() -> None:
    """As uid 65534 with no CAP_SYS_ADMIN: A's probe is refused, B's succeeds."""
    probe = subprocess.run(
        ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", sys.executable, "-c",
         "import json; from stateguard.adapters.mcp.executor_scope import "
         "check_pid_namespace_capability as c; "
         "print(json.dumps({'a': c().available, 'b': c(unprivileged=True).available}))"],
        capture_output=True, timeout=60, cwd=ROOT,
    )  # fmt: skip
    result = json.loads(probe.stdout)
    assert result["a"] is False, "architecture A must be refused without CAP_SYS_ADMIN"
    assert result["b"] is True, "architecture B must work without CAP_SYS_ADMIN"


@needs_root_tools
@needs_userns
def test_b_full_lifecycle_unprivileged_ends_scope_and_remaps_identity(tmp_path: Path) -> None:
    """The whole integrated lifecycle as uid 65534: start, confine, confirm empty,
    scope_ended -- and the server's host identity is remapped to 65534."""
    # A world-traversable/readable+writable dir: pytest's tmp_path is 0700, so uid
    # 65534 could neither read the driver nor write the server's file under it.
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="w4g-"))
    work.chmod(0o777)
    driver = work / "driver.py"
    driver.write_text(
        "import json, os, sys\n"
        "from stateguard.adapters.mcp.executor_scope import PidNamespaceScope, namespace_members\n"
        "srv = os.path.join(sys.argv[1], 'identity.py')\n"
        f"open(srv, 'w').write({IDENTITY_SERVER!r})\n"
        "s = PidNamespaceScope([sys.executable, srv, sys.argv[1]], env={'PATH': '/usr/bin:/bin'},"
        " cwd='/', unprivileged=True)\n"
        "rep = json.loads(s.stdout.readline())\n"
        "owner = os.stat(os.path.join(sys.argv[1], 'made_by_server')).st_uid\n"
        "s.stdin.write(b'exit\\n'); s.stdin.flush()\n"
        "ev = s.end()\n"
        "print(json.dumps({'server': rep, 'owner': owner, 'kind': ev.kind.value,\n"
        "  'established': ev.established, 'rc': s.returncode,\n"
        "  'members_after': namespace_members(s.namespace), 'host_uid': os.getuid()}))\n"
    )
    out = subprocess.run(
        ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups",
         sys.executable, str(driver), str(work)],
        capture_output=True, timeout=60, cwd=ROOT,
    )  # fmt: skip
    shutil.rmtree(work, ignore_errors=True)
    assert out.returncode == 0, out.stderr.decode()
    result = json.loads(out.stdout.decode().strip().splitlines()[-1])
    assert result["host_uid"] == 65534
    assert result["kind"] == "scope_ended" and result["established"] is True
    assert result["rc"] == 5 and result["members_after"] == []
    # uid remap: the server runs as namespaced root; the file it wrote is owned by 65534.
    assert result["server"]["uid"] == 0 and result["server"]["euid"] == 0
    assert result["owner"] == 65534
    # the capability drop still holds under B.
    assert result["server"]["caps"]["NoNewPrivs"] == "1"
    assert int(result["server"]["caps"]["CapBnd"], 16) & _CAP_SYS_ADMIN_BIT == 0


# ---------------------------------------------------------------------------
# Lifecycle and confinement (as whoever runs the suite; B maps that uid to 0)
# ---------------------------------------------------------------------------


@needs_userns
def test_a_generation_runs_in_its_own_namespace_and_its_end_is_confirmed(
    server: list[str],
) -> None:
    scope = PidNamespaceScope([*server, "stay"], unprivileged=True)
    try:
        assert scope.unprivileged is True
        line = _read_line(scope.stdout, 10)
        assert line, "the server never reported"
        report = json.loads(line)
        assert report["ns"] == scope.namespace != os.readlink("/proc/self/ns/pid")
        assert report["pid"] == 2  # init is PID 1; the server is its only child
        assert len(namespace_members(scope.namespace)) == 3  # init, server, descendant
        scope.stdin.write(b"exit\n")
        scope.stdin.flush()
        assert _read_line(scope.stdout, 10) == b""
        evidence = scope.end()
        assert evidence.kind is TerminationKind.SCOPE_ENDED and evidence.established
        assert evidence.executor == scope.identity
        assert scope.returncode == 3
        assert namespace_members(scope.namespace) == []
    finally:
        scope.end()


@needs_userns
def test_a_detached_descendant_that_ignores_sigterm_cannot_outlive_the_scope(
    server: list[str],
) -> None:
    scope = PidNamespaceScope([*server, "stay"], unprivileged=True)
    try:
        assert _read_line(scope.stdout, 10)
        members = namespace_members(scope.namespace)
        started = time.monotonic()
        evidence = scope.end()  # server still running: SIGKILL to init empties the ns
        assert time.monotonic() - started < END_TIMEOUT_SECONDS
        assert evidence.kind is TerminationKind.SCOPE_ENDED
        assert scope.returncode == 137
        assert _all_exit(members, 10)
        assert namespace_members(scope.namespace) == []
    finally:
        scope.end()


@needs_userns
def test_the_server_cannot_keep_cap_sys_admin(identity_server: list[str]) -> None:
    """Even as namespaced root, CAP_SYS_ADMIN is dropped before exec (contract)."""
    scope = PidNamespaceScope(identity_server, env={"PATH": "/usr/bin:/bin"}, unprivileged=True)
    try:
        report = json.loads(_read_line(scope.stdout, 10) or b"{}")
        assert report["euid"] == 0  # namespaced root
        assert report["caps"]["NoNewPrivs"] == "1"
        assert report["caps"]["CapAmb"] == "0000000000000000"
        assert int(report["caps"]["CapBnd"], 16) & _CAP_SYS_ADMIN_BIT == 0
        scope.stdin.write(b"exit\n")
        scope.stdin.flush()
        assert scope.end().kind is TerminationKind.SCOPE_ENDED
    finally:
        scope.end()


@needs_userns
def test_env_allowlist_reaches_the_server_and_no_other(identity_server: list[str]) -> None:
    """Only the policy env is present; a relay-side secret is never inherited."""
    env_server = IDENTITY_SERVER.replace(
        'print(json.dumps({"uid"',
        'import os as _o; print(json.dumps({"env": dict(_o.environ), "uid"',
    )
    path = Path(identity_server[1])
    path.write_text(env_server)
    os.environ["W4G_RELAY_SECRET"] = "do-not-leak"
    try:
        scope = PidNamespaceScope(
            [sys.executable, str(path), str(path.parent)],
            env={"PATH": "/usr/bin:/bin", "W4G_ALLOWED": "yes"},
            unprivileged=True,
        )
        try:
            report = json.loads(_read_line(scope.stdout, 10) or b"{}")
            assert report["env"].get("W4G_ALLOWED") == "yes"
            assert "W4G_RELAY_SECRET" not in report["env"]
            scope.stdin.write(b"exit\n")
            scope.stdin.flush()
            assert scope.end().kind is TerminationKind.SCOPE_ENDED
        finally:
            scope.end()
    finally:
        del os.environ["W4G_RELAY_SECRET"]


@needs_userns
def test_a_stopped_helper_is_unconfirmed_and_still_cleaned_up(server: list[str]) -> None:
    scope = PidNamespaceScope([*server, "stay"], unprivileged=True)
    try:
        assert _read_line(scope.stdout, 10)
        members = namespace_members(scope.namespace)
        os.kill(scope._helper.pid, signal.SIGSTOP)
        evidence = scope.end(timeout=0.5)
        assert evidence.kind is TerminationKind.SCOPE_UNCONFIRMED and not evidence.established
        assert "no end report" in evidence.detail
        assert _all_exit(members, 10)  # cleanup: helper killed, init died by PDEATHSIG
        assert _live_members(scope.namespace) == []
    finally:
        scope.end()


@needs_userns
def test_a_killed_helper_empties_the_namespace_and_is_unconfirmed(server: list[str]) -> None:
    scope = PidNamespaceScope([*server, "stay"], unprivileged=True)
    try:
        assert _read_line(scope.stdout, 10)
        members = namespace_members(scope.namespace)
        os.kill(scope._helper.pid, signal.SIGKILL)
        assert _all_exit(members, 10), "init must die with its helper (PR_SET_PDEATHSIG)"
        assert scope.end().kind is TerminationKind.SCOPE_UNCONFIRMED
    finally:
        scope.end()


@needs_userns
@pytest.mark.parametrize("code", [0, 3])
def test_a_clean_server_exit_reports_its_status_not_a_kill(code: int) -> None:
    program = f"import sys; [ln for ln in sys.stdin]; sys.exit({code})"
    for _ in range(5):
        scope = PidNamespaceScope([sys.executable, "-c", program], unprivileged=True)
        try:
            scope.stdin.close()
            assert _read_line(scope.stdout, 10) == b""
            evidence = scope.end()
            assert evidence.kind is TerminationKind.SCOPE_ENDED
            assert scope.returncode == code
        finally:
            scope.end()


@needs_userns
def test_every_generation_has_a_distinct_scope_and_identity(server: list[str]) -> None:
    first = PidNamespaceScope([*server, "stay"], unprivileged=True)
    second = PidNamespaceScope([*server, "stay"], unprivileged=True)
    try:
        assert first.namespace != second.namespace
        assert first.identity != second.identity
        one, two = first.end(), second.end()
        assert one.applies_to(second.identity) is not None
        assert two.applies_to(first.identity) is not None
    finally:
        first.end()
        second.end()


@needs_userns
def test_arguments_are_never_interpreted_by_a_shell(server: list[str], tmp_path: Path) -> None:
    marker = tmp_path / "injected"
    hostile = [f"$(touch {marker})", f"; touch {marker}", "`id`", "a b", "*"]
    scope = PidNamespaceScope([*server, "echo-args", *hostile], unprivileged=True)
    try:
        line = _read_line(scope.stdout, 10)
        assert line and json.loads(line) == hostile
        assert scope.end().kind is TerminationKind.SCOPE_ENDED
        assert not marker.exists()
    finally:
        scope.end()


@needs_userns
def test_repeated_starts_and_stops_leave_no_orphaned_descendants(server: list[str]) -> None:
    identities = []
    for _ in range(4):
        scope = PidNamespaceScope([*server, "stay"], unprivileged=True)
        assert _read_line(scope.stdout, 10)
        identities.append(scope.identity)
        members = namespace_members(scope.namespace)
        assert scope.end().kind is TerminationKind.SCOPE_ENDED
        assert _all_exit(members, 10)
        assert namespace_members(scope.namespace) == []
    # The kernel may reuse a freed namespace's inode, so the namespace string can
    # repeat; the generation identity (helper pid + counter) never does.
    assert len(set(identities)) == len(identities)


def test_an_empty_command_is_refused_for_b() -> None:
    with pytest.raises(ScopeError, match="no server command"):
        PidNamespaceScope([], unprivileged=True)


@needs_userns
def test_uid_gid_mapping_failure_fails_closed_with_a_reason() -> None:
    """
    Week 4H gap closure: force a real UID/GID mapping failure and confirm
    ``_map_userns_identity`` / ``_create_namespace`` report it (fail-closed), rather
    than proceeding with an unmapped namespace. A user namespace's ``uid_map`` may be
    written only once, so a second write genuinely fails with no mocking.
    """
    from stateguard.adapters.mcp.executor_scope import (
        _CLONE_NEWUSER,
        _create_namespace,
        _libc,
        _map_userns_identity,
    )

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # isolate the irreversible unshare in a throwaway child
        os.close(read_fd)
        try:
            uid, gid = os.geteuid(), os.getegid()
            libc = _libc()
            rc = libc.unshare(_CLONE_NEWUSER)
            first = None if rc == 0 else "unshare failed"
            if rc == 0:
                first = _map_userns_identity(uid, gid)  # succeeds -> None
                second = _map_userns_identity(uid, gid)  # map already written -> reason
            else:
                second = "unshare failed"
            os.write(write_fd, json.dumps({"first": first, "second": second}).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    raw = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    os.waitpid(child, 0)
    result = json.loads(raw)
    assert result["first"] is None, "the first mapping must succeed"
    assert isinstance(result["second"], str) and "mapping failed" in result["second"], (
        "a failed mapping must return a reason (fail-closed), not proceed"
    )


@needs_userns
def test_create_namespace_reports_mapping_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_create_namespace(unprivileged=True)`` surfaces a mapping failure as its
    reason, so the helper emits an error and the scope never forks a server."""
    from stateguard.adapters.mcp import executor_scope as mod

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            # Make the identity self-map fail deterministically.
            monkeypatch.setattr(mod, "_map_userns_identity", lambda uid, gid: "mapping blocked")
            reason = mod._create_namespace(mod._libc(), unprivileged=True)
            os.write(write_fd, (reason or "").encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    reason = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    os.waitpid(child, 0)
    assert reason == "mapping blocked", "creation must fail closed with the mapping reason"


@needs_userns
def test_b_helper_argv_carries_the_userns_flag_and_a_does_not(server: list[str]) -> None:
    """No silent fallback: the selected architecture is exactly what runs."""
    a = PidNamespaceScope([*server, "stay"])
    b = PidNamespaceScope([*server, "stay"], unprivileged=True)
    try:
        a_argv = Path(f"/proc/{a._helper.pid}/cmdline").read_bytes().split(b"\0")
        b_argv = Path(f"/proc/{b._helper.pid}/cmdline").read_bytes().split(b"\0")
        assert b"--userns" not in a_argv
        assert b"--userns" in b_argv
    finally:
        a.end()
        b.end()
