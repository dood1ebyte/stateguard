"""
Executor scopes for a relayed MCP server: a Linux PID namespace per server generation.

Reconciliation may only probe a restarted server once nothing can still perform
the lost call (``reconcile.TerminationEvidence``). A process restart cannot show
that: a descendant the server started may have left its process group. A **PID
namespace** can. This module runs each server generation as the only child of
a small *init* process that is PID 1 of a new PID namespace:

* every process the server starts -- including descendants that call
  ``setsid`` or double-fork -- is in that namespace;
* when init exits, the kernel kills every other process in the namespace and
  init's exit is reported to its parent only after they are all gone;
* init exits as soon as the server process exits, so the scope's lifetime is
  the server's lifetime: a server's descendants never outlive it.

Topology (one per server generation)::

    relay ──(pipes: server stdin/stdout)──────────────┐
      │ control pipe (started / ended events; closing it means "end now")
      ▼                                               │
    helper  (outside the namespace; reaps init)       │
      │ fork after unshare(CLONE_NEWPID)              │
      ▼                                               │
    init    (PID 1 inside; reaps orphans)             │
      │ fork + exec                                   ▼
    server  (stdin/stdout are the relay's pipes -- no wrapper holds them)

The helper and init close their copies of the server's stdio, so the relay sees
the server's stdout close as soon as the *server and its descendants* close it
-- a wrapper never keeps the pipe open.

Ending a scope (``PidNamespaceScope.end``): the relay closes the control pipe;
the helper SIGKILLs init if it is still running (only while it has not been
reaped, so a PID is never signalled after reuse); the kernel empties the
namespace; the helper reaps init, scans ``/proc`` for anything left in the
namespace and reports. The relay scans ``/proc`` again itself. Only if init was
reaped, both scans found the namespace empty, and the helper exited -- all
within ``END_TIMEOUT_SECONDS`` -- is the result ``scope_ended`` for that
generation's executor identity. Otherwise it is ``scope_unconfirmed``, which
never allows a probe.

If the relay dies, the helper reads EOF on its control pipe and kills init. If
the helper dies, init gets SIGKILL (``PR_SET_PDEATHSIG``). Either way the
namespace is emptied by the kernel; no state is left behind (no files, mounts
or cgroups are created).

What this does **not** establish: anything outside the namespace -- remote
APIs, databases, queues, other hosts, other local processes the server talked
to. A process in uninterruptible sleep delays the namespace's end; past the
deadline that is ``scope_unconfirmed``.

Requirements: Linux, ``/proc`` with ``ns/pid`` links, and permission to create
a PID namespace (``CAP_SYS_ADMIN`` in the current user namespace -- root, or a
container granted it). ``check_pid_namespace_capability`` verifies all of this by
creating and tearing down a real namespace before any server starts; nothing is
ever downgraded to a weaker mechanism.

**Architecture B (opt-in, experimental):** passing ``unprivileged=True`` (CLI
``--executor-scope pid-namespace-unprivileged``) adds a *user* namespace to the
same ``unshare``, so the PID namespace is created **without host
``CAP_SYS_ADMIN``**. Every lifecycle guarantee above is unchanged -- init-as-PID-1,
detached-descendant termination, ``/proc`` emptiness confirmation, pidfd teardown,
the capability drop before exec, and the fail-closed reconciliation gate -- and it
is checked by its own probe (``probe-userns``) before any server starts. Its cost
is that it **remaps the server's user identity** to the relay's uid/gid: the server
runs as namespaced root, and files it writes on a shared mount are owned by that
host uid, not root. It also needs unprivileged user namespaces enabled and a
seccomp policy that permits ``unshare`` (Docker's default profile denies it). This
is a privilege-boundary prototype, **not** a production-ready security boundary or
a general-purpose sandbox. See ``docs/runtime/WEEK4G_UNPRIVILEGED_SCOPE_PROTOTYPE.md``.
"""

from __future__ import annotations

import contextlib
import ctypes
import itertools
import json
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from stateguard.adapters.mcp.reconcile import TerminationEvidence

__all__ = [
    "END_TIMEOUT_SECONDS",
    "START_TIMEOUT_SECONDS",
    "PidNamespaceScope",
    "ScopeCapability",
    "ScopeError",
    "check_pid_namespace_capability",
    "namespace_members",
]

#: How long starting a scope (helper, namespace, init, exec) may take.
START_TIMEOUT_SECONDS = 5.0
#: How long confirming a scope's end may take.
END_TIMEOUT_SECONDS = 5.0
#: After the relay asks for the end, how long init may take to exit on its own
#: before the helper force-kills it. A clean shutdown (the server has already
#: exited) registers in well under this; it bounds only a server that refuses to
#: exit, and stays within END_TIMEOUT_SECONDS so end() still sees the result.
_END_GRACE_SECONDS = 1.0

_CLONE_NEWPID = 0x20000000
#: Architecture B (opt-in, experimental): a user namespace created in the same
#: ``unshare`` as the PID namespace, so the PID namespace can be made *without*
#: host ``CAP_SYS_ADMIN``. It remaps the server's user identity (see
#: ``_map_userns_identity``); architecture A (default) never uses it.
_CLONE_NEWUSER = 0x10000000
_PR_SET_PDEATHSIG = 1
_PR_CAPBSET_DROP = 24
_PR_SET_NO_NEW_PRIVS = 38
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
#: Creating the PID namespace needs CAP_SYS_ADMIN (bit 21). The supervised
#: server never needs it, so it is dropped before the server is executed (see
#: ``_confine_server``); the scope must not hand the server the capability the
#: feature itself required.
_CAP_SYS_ADMIN = 21
_MODULE = "stateguard.adapters.mcp.executor_scope"
#: ``-P``: never put the working directory on the helper's ``sys.path`` (a
#: ``stateguard`` package planted there must not run as the helper).
_PYTHON_FLAGS = ("-P",)


class ScopeError(RuntimeError):
    """A scope could not be started (or its capability is missing)."""


@dataclass(frozen=True)
class ScopeCapability:
    available: bool
    #: Why not -- or, when available, what was verified.
    reason: str


def namespace_members(namespace: str) -> list[int]:
    """PIDs (as seen here) of processes whose PID namespace is *namespace*."""
    members = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            with contextlib.suppress(OSError):
                if os.readlink(f"/proc/{entry}/ns/pid") == namespace:
                    members.append(int(entry))
    return members


def _libc() -> Any:
    return ctypes.CDLL(None, use_errno=True)


def _map_userns_identity(uid: int, gid: int) -> str | None:
    """
    Self-map *uid*/*gid* to 0 in the just-created user namespace (architecture B).

    The standard single-id rootless mapping: the server then runs as namespaced
    root, whose identity *on the host* is the relay's *uid*/*gid*. A file the
    server creates on a shared mount is owned by that host uid, **not** by root --
    the uid-remapping cost of running without host ``CAP_SYS_ADMIN``. *uid*/*gid*
    must be read **before** the ``unshare`` (afterwards the process is unmapped and
    reads back as the overflow uid). Returns ``None`` on success, else the reason;
    a failure fails the scope closed (nothing is executed).
    """
    try:
        # setgroups must be denied before gid_map may be written from an
        # unprivileged user namespace.
        with open("/proc/self/setgroups", "w", encoding="ascii") as f:
            f.write("deny")
        with open("/proc/self/gid_map", "w", encoding="ascii") as f:
            f.write(f"0 {gid} 1")
        with open("/proc/self/uid_map", "w", encoding="ascii") as f:
            f.write(f"0 {uid} 1")
    except OSError as exc:
        return f"user-namespace uid/gid mapping failed: {exc}"
    return None


def _create_namespace(libc: Any, *, unprivileged: bool) -> str | None:
    """
    Create the PID namespace for the caller's children; return ``None`` or a reason.

    Architecture A (``unprivileged=False``): ``unshare(CLONE_NEWPID)`` -- needs
    ``CAP_SYS_ADMIN`` in the current user namespace. Architecture B
    (``unprivileged=True``, opt-in): ``unshare(CLONE_NEWUSER | CLONE_NEWPID)`` --
    needs *no* host capability, then self-maps the user identity. Either way the
    caller stays in its own PID namespace and its next ``fork`` is PID 1 of the new
    one. Never downgrades: if the requested namespace cannot be made, it fails.
    """
    uid, gid = (os.geteuid(), os.getegid()) if unprivileged else (-1, -1)
    flags = (_CLONE_NEWUSER | _CLONE_NEWPID) if unprivileged else _CLONE_NEWPID
    if libc.unshare(flags) != 0:
        errno = ctypes.get_errno()
        which = "CLONE_NEWUSER|CLONE_NEWPID" if unprivileged else "CLONE_NEWPID"
        return f"unshare({which}) failed: {os.strerror(errno)}"
    if unprivileged:
        return _map_userns_identity(uid, gid)
    return None


def check_pid_namespace_capability(
    timeout: float = START_TIMEOUT_SECONDS, *, unprivileged: bool = False
) -> ScopeCapability:
    """
    Can the scope be created *and confirmed empty* here? Never downgrades.

    ``unprivileged=False`` probes architecture A (``CAP_SYS_ADMIN`` PID namespace);
    ``unprivileged=True`` probes architecture B (user+PID namespace with no host
    capability). The two are checked by separate probe subprocesses and are never
    substituted for one another.
    """
    if not sys.platform.startswith("linux"):
        return ScopeCapability(False, f"PID namespaces need Linux; this platform is {sys.platform}")
    if not os.path.exists("/proc/self/ns/pid"):
        return ScopeCapability(
            False, "/proc/self/ns/pid is missing: /proc is not mounted, or this kernel has no PID "
            "namespaces",
        )  # fmt: skip
    probe_command = "probe-userns" if unprivileged else "probe"
    try:
        probe = subprocess.run(
            [sys.executable, *_PYTHON_FLAGS, "-m", _MODULE, probe_command],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ScopeCapability(False, f"the namespace probe did not complete: {exc}")
    try:
        report = json.loads(probe.stdout.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ScopeCapability(
            False, f"the namespace probe gave no report (exit {probe.returncode})"
        )
    if not report.get("ok"):
        return ScopeCapability(False, str(report.get("why", "the namespace probe failed")))
    kind = "user+PID namespace (unprivileged)" if unprivileged else "PID namespace"
    return ScopeCapability(
        True,
        f"created a {kind}, ran a process in it, and confirmed it empty after its init exited",
    )


class PidNamespaceScope:
    """One server generation inside its own PID namespace. See the module docstring."""

    _numbers = itertools.count()

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        unprivileged: bool = False,
        start_timeout: float = START_TIMEOUT_SECONDS,
    ) -> None:
        """
        Run *command* as the only child of init of a new PID namespace.

        ``env``: the server's exact environment. ``None`` inherits the relay's
        environment (the pre-4C behaviour, kept for direct/library callers); the
        CLI always passes an explicit policy env in scope mode. The server's
        environment is delivered over a private pipe, never on the argv or in the
        helper's own environment, so no value appears in ``/proc/<pid>/cmdline``.
        ``cwd``: the server's working directory (``None`` inherits the relay's).
        ``unprivileged``: architecture B (opt-in, experimental). ``False`` (default)
        is architecture A: a PID namespace needing host ``CAP_SYS_ADMIN``, the
        server keeping its host user identity. ``True`` adds a *user* namespace so
        the PID namespace needs no host capability, at the cost of remapping the
        server's user identity to the relay's uid/gid (see ``_map_userns_identity``).
        The choice is never downgraded: a scope asked for one never silently becomes
        the other, direct-child reaping, or unscoped execution.
        """
        if not command:
            raise ScopeError("no server command")
        self.unprivileged = unprivileged
        to_server_r, to_server_w = os.pipe()
        from_server_r, from_server_w = os.pipe()
        control_r, control_w = os.pipe()
        env_r = env_w = -1
        pass_fds = [to_server_r, from_server_w]
        argv = [
            sys.executable, *_PYTHON_FLAGS, "-m", _MODULE, "serve",
            "--to-server-fd", str(to_server_r), "--from-server-fd", str(from_server_w),
        ]  # fmt: skip
        if cwd is not None:
            argv += ["--server-cwd", cwd]
        if env is not None:
            env_r, env_w = os.pipe()
            pass_fds.append(env_r)
            argv += ["--env-fd", str(env_r)]
        if unprivileged:
            # A valued flag so the helper's paired option parsing is unchanged; its
            # presence selects architecture B. Absent for A -- A's argv is untouched.
            argv += ["--userns", "1"]
        argv += ["--", *command]
        try:
            self._helper = subprocess.Popen(
                argv,
                stdin=control_r,
                stdout=subprocess.PIPE,
                pass_fds=tuple(pass_fds),
                # When a policy env is in force, the helper (and so init) runs with
                # a minimal environment of its own; it needs none to create and
                # confirm the namespace. The server's environment is sent over the
                # pipe. With no policy (env is None) the helper inherits, as before.
                env={} if env is not None else None,
            )
        except OSError as exc:
            for fd in (to_server_r, to_server_w, from_server_r, from_server_w, control_r,
                       control_w, env_r, env_w):  # fmt: skip
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            raise ScopeError(f"the scope helper could not start: {exc}") from exc
        # Only the helper (and, after it forks, the server) holds the server's ends.
        for fd in (to_server_r, from_server_w, control_r):
            os.close(fd)
        if env is not None:
            os.close(env_r)
            # The server's environment travels here, never on the argv. The helper
            # drains it concurrently, so a payload larger than the pipe buffer
            # streams through without deadlock. If the helper dies first, the write
            # fails closed: the half-started scope is torn down, not left leaking.
            try:
                self._write_env(env_w, json.dumps(env).encode())
            except OSError as exc:
                for fd in (to_server_w, from_server_r, control_w):
                    with contextlib.suppress(OSError):
                        os.close(fd)
                with contextlib.suppress(OSError):
                    self._helper.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._helper.wait(timeout=END_TIMEOUT_SECONDS)
                raise ScopeError(f"the server environment could not be sent: {exc}") from exc
        self.stdin: IO[bytes] = os.fdopen(to_server_w, "wb")
        self.stdout: IO[bytes] = os.fdopen(from_server_r, "rb")
        self._control: IO[bytes] | None = os.fdopen(control_w, "wb")
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._read_events, daemon=True).start()
        self._ended: dict[str, Any] | None = None
        self._evidence: TerminationEvidence | None = None
        started = self._next_event(time.monotonic() + start_timeout)
        if started is None or started.get("event") != "started" or not started.get("ns"):
            why = (started or {}).get("why") or "no 'started' report in time"
            self._abandon()
            raise ScopeError(f"the PID-namespace scope did not start: {why}")
        self.namespace: str = str(started["ns"])
        self.init_pid: int = int(started["init_pid"])
        #: A reference to the namespace, held until the end has been confirmed: the
        #: kernel never reuses its inode number meanwhile, so the scans in ``end``
        #: can never be about some other, newer namespace.
        self._namespace_fd = self._hold_namespace()
        inode = self.namespace.strip("pid:[]")
        #: Unique per process and per generation, even if the kernel reuses an inode.
        self.identity: str = f"pidns-{inode}-{self._helper.pid}-{next(self._numbers)}"

    def _hold_namespace(self) -> int | None:
        try:
            fd = os.open(f"/proc/{self.init_pid}/ns/pid", os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            return None  # init has already exited: nothing to hold
        try:
            stat = Path(f"/proc/{self.init_pid}/stat").read_text()
            parent = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            parent = -1
        # Only the helper's child is init (the helper forks nothing else), so a
        # parent check rules out a PID reused after init exited.
        if parent != self._helper.pid or os.readlink(f"/proc/self/fd/{fd}") != self.namespace:
            os.close(fd)
            return None
        return fd

    def _release_namespace(self) -> None:
        fd, self._namespace_fd = self.__dict__.get("_namespace_fd"), None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    @staticmethod
    def _write_env(env_w: int, payload: bytes) -> None:
        """Write the server environment to the helper's pipe (extracted for testing)."""
        with os.fdopen(env_w, "wb") as env_pipe:
            env_pipe.write(payload)

    def _read_events(self) -> None:
        assert self._helper.stdout is not None
        for raw in self._helper.stdout:
            with contextlib.suppress(ValueError):
                self._events.put(json.loads(raw))
        self._events.put(None)

    def _next_event(self, deadline: float) -> dict[str, Any] | None:
        try:
            return self._events.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            return None

    def _abandon(self) -> None:
        """Kill the helper (init follows by PDEATHSIG) and release our ends."""
        with contextlib.suppress(OSError):
            self._helper.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._helper.wait(timeout=END_TIMEOUT_SECONDS)
        for stream in (self.__dict__.get("_control"), self.__dict__.get("stdin")):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        self._release_namespace()

    @property
    def returncode(self) -> int | None:
        """The server's exit status as init reported it, once the scope has ended."""
        if self._ended is None:
            return None
        status = self._ended.get("status")
        return int(status) if isinstance(status, int) else None

    def end(self, timeout: float = END_TIMEOUT_SECONDS) -> TerminationEvidence:
        """End the scope; report exactly what was established (idempotent)."""
        if self._evidence is not None:
            return self._evidence
        deadline = time.monotonic() + timeout
        if self._control is not None:
            with contextlib.suppress(OSError):
                self._control.close()  # "end now": the helper SIGKILLs init if it still runs
            self._control = None
        while self._ended is None:
            event = self._next_event(deadline)
            if event is None:
                break
            if event.get("event") == "ended":
                self._ended = event
        try:
            self._helper.wait(timeout=max(0.0, deadline - time.monotonic()))
            helper_exited = True
        except subprocess.TimeoutExpired:
            helper_exited = False
        left = namespace_members(self.namespace)
        self._release_namespace()
        with contextlib.suppress(OSError):
            self.stdin.close()
        ended = self._ended
        if ended is not None and helper_exited and ended.get("members_left") == 0 and not left:
            self._evidence = TerminationEvidence.scope_ended(
                f"linux pid namespace {self.namespace}: init reaped, no member left",
                executor=self.identity,
            )
        else:
            why = (
                "no end report from the scope helper in time"
                if ended is None
                else f"{len(left)} process(es) still in the namespace"
                if left or ended.get("members_left")
                else "the scope helper did not exit in time"
            )
            self._abandon()
            self._evidence = TerminationEvidence.scope_unconfirmed(why)
        return self._evidence


# ---------------------------------------------------------------------------
# The helper process (``python -m stateguard.adapters.mcp.executor_scope``)
# ---------------------------------------------------------------------------


def _emit(event: dict[str, Any]) -> None:
    os.write(1, (json.dumps(event) + "\n").encode())


def _exit_code(status: int) -> int:
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def _confine_server(libc: Any) -> str | None:
    """
    Drop CAP_SYS_ADMIN before the untrusted server is executed; fail closed.

    Enabling the scope forces the relay (and so this process) to hold
    CAP_SYS_ADMIN, which the server must never inherit. Returns ``None`` once no
    path can give the server CAP_SYS_ADMIN, or a reason the boundary could not
    be established -- in which case the caller must not exec the server.

    This is a privilege boundary, not a sandbox: the server still shares the
    host mount, network, IPC and user namespaces, the relay's environment and
    working directory, and inherits its resource limits. See
    ``docs/runtime/WEEK4B_EXECUTOR_SECURITY_AUDIT.md``.
    """
    # Ambient capabilities are a deliberate grant (the non-root path that gives
    # the relay CAP_SYS_ADMIN). Cleared, a non-root server execs with none.
    if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        return f"could not clear ambient capabilities: {os.strerror(ctypes.get_errno())}"
    # no_new_privs stops a non-root server regaining CAP_SYS_ADMIN by exec'ing a
    # setuid-root or file-capability binary (euid stays as it is).
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        return f"could not set no_new_privs: {os.strerror(ctypes.get_errno())}"
    # A root exec is granted the whole bounding set, so CAP_SYS_ADMIN must leave
    # the bounding set too. A root process always holds CAP_SETPCAP to do this;
    # a non-root process cannot, but there the cleared ambient set already means
    # the server execs with no capabilities, so the drop is not needed.
    if os.geteuid() == 0 and libc.prctl(_PR_CAPBSET_DROP, _CAP_SYS_ADMIN, 0, 0, 0) != 0:
        return (
            f"could not drop CAP_SYS_ADMIN from the bounding set: {os.strerror(ctypes.get_errno())}"
        )
    return _verify_confined()


def _confinement_gap(cap_amb: int, cap_bnd: int, no_new_privs: bool, is_root: bool) -> str | None:
    """
    Whether any path could still give the execed server CAP_SYS_ADMIN (pure).

    - ambient caps carry CAP_SYS_ADMIN across execve for a non-root server;
    - without no_new_privs a non-root server regains it via a setuid-root exec;
    - a root exec is granted the whole bounding set, so it must exclude it.
    A non-root exec with no file capabilities has its permitted set recomputed
    to empty regardless, so the bounding set is not checked there.
    """
    if cap_amb != 0:
        return "ambient capabilities are not empty after the drop"
    if not no_new_privs:
        return "no_new_privs is not set after the drop"
    if is_root and cap_bnd & (1 << _CAP_SYS_ADMIN):
        return "CAP_SYS_ADMIN is still in the bounding set after the drop"
    return None


def _verify_confined() -> str | None:
    """From ``/proc`` alone, confirm no path can give the execed server CAP_SYS_ADMIN."""
    fields: dict[str, str] = {}
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                key, _, value = line.partition(":")
                if key in ("CapAmb", "CapBnd", "NoNewPrivs"):
                    fields[key] = value.strip()
    except OSError as exc:
        return f"could not read /proc/self/status: {exc}"
    return _confinement_gap(
        int(fields.get("CapAmb", "f" * 16), 16),
        int(fields.get("CapBnd", "0"), 16),
        fields.get("NoNewPrivs") == "1",
        os.geteuid() == 0,
    )


def _init(
    to_fd: int,
    from_fd: int,
    alive_r: int,
    command: list[str],
    server_env: dict[str, str] | None = None,
    server_cwd: str | None = None,
) -> None:
    """PID 1 of the namespace: exec the server as its child; reap; exit when it exits."""
    libc = _libc()
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    try:  # the helper died before PDEATHSIG was armed: its end of the pipe is closed
        os.set_blocking(alive_r, False)
        if os.read(alive_r, 1) == b"":
            os._exit(1)
    except BlockingIOError:
        pass
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)  # never hold the helper's control pipe
    os.dup2(devnull, 1)
    server = os.fork()
    if server == 0:
        os.dup2(to_fd, 0)
        os.dup2(from_fd, 1)
        os.closerange(3, 1 << 16)
        if server_cwd is not None:
            try:  # fail closed: a server must never silently run in the wrong directory
                os.chdir(server_cwd)
            except OSError:
                os._exit(125)
        if _confine_server(libc) is not None:
            os._exit(126)  # fail closed: never exec the server holding CAP_SYS_ADMIN
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
            signal.signal(sig, signal.SIG_DFL)
        try:
            if server_env is not None:
                os.execvpe(command[0], command, server_env)
            else:
                os.execvp(command[0], command)
        finally:
            os._exit(127)
    for fd in (to_fd, from_fd, devnull):
        os.close(fd)
    while True:
        try:
            pid, status = os.wait()
        except ChildProcessError:
            os._exit(1)
        if pid == server:
            os._exit(_exit_code(status) & 0xFF)  # the kernel now empties the namespace


def _serve(argv: list[str]) -> None:
    split = argv.index("--")
    opts = dict(zip(argv[:split:2], argv[1:split:2], strict=True))
    command = argv[split + 1 :]
    to_fd, from_fd = int(opts["--to-server-fd"]), int(opts["--from-server-fd"])
    server_cwd = opts.get("--server-cwd")
    # The server's environment arrives over a private pipe, never on the argv, so
    # no value is visible in /proc/<pid>/cmdline. Read it before the fork so init
    # inherits it in memory. A malformed env fails closed: nothing is started.
    server_env: dict[str, str] | None = None
    if "--env-fd" in opts:
        try:
            with os.fdopen(int(opts["--env-fd"]), "rb") as env_pipe:
                server_env = json.loads(env_pipe.read() or b"{}")
            if not isinstance(server_env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in server_env.items()
            ):
                raise ValueError("the server environment must be a map of strings")
        except (OSError, ValueError) as exc:
            _emit({"event": "error", "why": f"the server environment could not be read: {exc}"})
            os._exit(4)
    libc = _libc()
    # Architecture A (default): unshare(CLONE_NEWPID), needs host CAP_SYS_ADMIN.
    # Architecture B (``--userns``): unshare(CLONE_NEWUSER|CLONE_NEWPID) + identity
    # self-map, needs no host capability. Either failing fails the scope closed:
    # nothing is forked, no fallback to a weaker mechanism.
    reason = _create_namespace(libc, unprivileged=opts.get("--userns") == "1")
    if reason is not None:
        _emit({"event": "error", "why": reason})
        os._exit(3)
    alive_r, alive_w = os.pipe()
    init = os.fork()  # the first child after unshare(CLONE_NEWPID) is PID 1 of the namespace
    if init == 0:
        os.close(alive_w)
        _init(to_fd, from_fd, alive_r, command, server_env, server_cwd)
    os.close(alive_r)
    os.close(to_fd)  # the helper never holds the server's stdio
    os.close(from_fd)
    try:
        namespace: str | None = os.readlink(f"/proc/{init}/ns/pid")
    except OSError:
        namespace = None
    _emit({"event": "started", "init_pid": init, "ns": namespace})
    # No threads here: after unshare(CLONE_NEWPID) this process cannot create
    # any. One poll loop over the control pipe and init's pidfd instead; the
    # pidfd also makes the kill race-free (it can never reach a reused PID).
    pidfd = os.pidfd_open(init)
    poller = select.poll()
    poller.register(0, select.POLLIN | select.POLLHUP | select.POLLERR)
    poller.register(pidfd, select.POLLIN)
    exited = False
    while not exited:
        for fd, _ in poller.poll():
            if fd == pidfd:
                exited = True  # init has exited -- and the kernel has emptied the namespace
                continue
            try:
                data = os.read(0, 4096)
            except OSError:
                data = b""
            if not data:  # EOF: the relay asked for the end, or died
                poller.unregister(0)
                # A clean shutdown closes this pipe only after the server has
                # already exited; give init a brief grace to report that exit on
                # its own (status 0) before forcing it, so a clean shutdown is
                # never turned into a SIGKILL. A server that does not exit within
                # the grace is force-killed, bounding the wait.
                grace = select.poll()
                grace.register(pidfd, select.POLLIN)
                if not grace.poll(_END_GRACE_SECONDS * 1000):
                    with contextlib.suppress(ProcessLookupError):
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    _, status = os.waitpid(init, 0)
    left = len(namespace_members(namespace)) if namespace else -1
    _emit({"event": "ended", "status": _exit_code(status), "members_left": left})
    os.close(alive_w)
    os._exit(0)


def _probe() -> None:
    """Create a namespace, run a process in it, and confirm it empties. Prints one report."""
    try:
        own = os.readlink("/proc/self/ns/pid")
        libc = _libc()
    except OSError as exc:
        print(json.dumps({"ok": False, "why": f"cannot read /proc/self/ns/pid: {exc}"}))
        return
    # Confirm the server can be stripped of CAP_SYS_ADMIN on this kernel, in a
    # throwaway child (the drops are irreversible). Done before the unshare: once
    # the namespace's init is killed below, no further fork into it is possible.
    confine_r, confine_w = os.pipe()
    kid = os.fork()
    if kid == 0:
        os.close(confine_r)
        reason = _confine_server(libc)
        os.write(confine_w, (reason or "").encode()[:200])
        os._exit(0)
    os.close(confine_w)
    confine_reason = os.read(confine_r, 200).decode()
    os.close(confine_r)
    os.waitpid(kid, 0)
    if libc.unshare(_CLONE_NEWPID) != 0:
        errno = ctypes.get_errno()
        why = (
            "creating a PID namespace was refused (unshare(CLONE_NEWPID): "
            f"{os.strerror(errno)}); it needs CAP_SYS_ADMIN in this user namespace "
            "(root, or a container granted that capability). Unprivileged user namespaces "
            "are not used."
        )
        print(json.dumps({"ok": False, "why": why}))
        return
    ready_r, ready_w = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_r)
        if os.fork() == 0:  # a detached descendant, as a server might leave behind
            os.setsid()
            time.sleep(60)
            os._exit(0)
        os.write(ready_w, b"x")
        time.sleep(60)
        os._exit(0)
    os.close(ready_w)
    os.read(ready_r, 1)
    try:
        theirs = os.readlink(f"/proc/{child}/ns/pid")
    except OSError as exc:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        print(json.dumps({"ok": False, "why": f"cannot read the new namespace: {exc}"}))
        return
    before = len(namespace_members(theirs))
    os.kill(child, signal.SIGKILL)
    os.waitpid(child, 0)
    after = len(namespace_members(theirs))
    try:
        os.close(os.pidfd_open(os.getpid()))
        has_pidfd = True
    except (OSError, AttributeError):
        has_pidfd = False
    ok = theirs != own and before >= 2 and after == 0 and has_pidfd and not confine_reason
    why = ""
    if not has_pidfd:
        why = "pidfd_open is unavailable (it needs Linux 5.3 or later)"
    elif confine_reason:
        why = f"the server's CAP_SYS_ADMIN could not be dropped: {confine_reason}"
    elif not ok:
        why = f"namespace {theirs} (own {own}): {before} members, {after} after its end"
    print(json.dumps({"ok": ok, "why": why}))


def _probe_userns() -> None:
    """
    Architecture B probe: create a user+PID namespace UNPRIVILEGED, confine as the
    server will be, run a detached ``setsid`` descendant, and confirm the namespace
    empties. Mirrors ``_probe`` but needs no host ``CAP_SYS_ADMIN``; prints one
    report. The confinement check runs inside the namespace, as namespaced root --
    the exact context the server is execed in -- so it proves the real drop.
    """
    try:
        own = os.readlink("/proc/self/ns/pid")
        libc = _libc()
    except OSError as exc:
        print(json.dumps({"ok": False, "why": f"cannot read /proc/self/ns/pid: {exc}"}))
        return
    reason = _create_namespace(libc, unprivileged=True)
    if reason is not None:
        why = (
            f"creating an unprivileged user+PID namespace was refused ({reason}); it needs "
            "unprivileged user namespaces enabled (e.g. sysctl kernel.unprivileged_userns_clone, "
            "user.max_user_namespaces > 0) and a seccomp policy that permits unshare of a user "
            "namespace (Docker's default profile denies it without CAP_SYS_ADMIN). Nothing is "
            "downgraded to a weaker mechanism."
        )
        print(json.dumps({"ok": False, "why": why}))
        return
    # Now namespaced root in a new user+PID namespace; the next fork is PID 1.
    ready_r, ready_w = os.pipe()
    confine_r, confine_w = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_r)
        os.close(confine_r)
        # Confine exactly as _init will before exec'ing the server (namespaced root).
        reason = _confine_server(libc)
        os.write(confine_w, (reason or "").encode()[:200])
        os.close(confine_w)
        if os.fork() == 0:  # a detached descendant, as a server might leave behind
            os.setsid()
            time.sleep(60)
            os._exit(0)
        os.write(ready_w, b"x")
        time.sleep(60)
        os._exit(0)
    os.close(ready_w)
    os.close(confine_w)
    confine_reason = os.read(confine_r, 200).decode()
    os.close(confine_r)
    os.read(ready_r, 1)
    os.close(ready_r)
    try:
        theirs = os.readlink(f"/proc/{child}/ns/pid")
    except OSError as exc:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        print(json.dumps({"ok": False, "why": f"cannot read the new namespace: {exc}"}))
        return
    before = len(namespace_members(theirs))
    os.kill(child, signal.SIGKILL)
    os.waitpid(child, 0)
    after = len(namespace_members(theirs))
    try:
        os.close(os.pidfd_open(os.getpid()))
        has_pidfd = True
    except (OSError, AttributeError):
        has_pidfd = False
    ok = theirs != own and before >= 2 and after == 0 and has_pidfd and not confine_reason
    why = ""
    if not has_pidfd:
        why = "pidfd_open is unavailable (it needs Linux 5.3 or later)"
    elif confine_reason:
        why = f"the server's CAP_SYS_ADMIN could not be dropped: {confine_reason}"
    elif not ok:
        why = f"namespace {theirs} (own {own}): {before} members, {after} after its end"
    print(json.dumps({"ok": ok, "why": why}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        _serve(sys.argv[2:])
    elif sys.argv[1:2] == ["probe"]:
        _probe()
    elif sys.argv[1:2] == ["probe-userns"]:
        _probe_userns()
    else:
        raise SystemExit("usage: executor_scope.py serve ... | probe | probe-userns")
