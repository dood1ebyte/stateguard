"""
What a server inside ``--executor-scope pid-namespace`` inherits (Week 4B, isolated tool).

usage: week4b_scope_audit.py OUT_JSON

Runs a small, harmless probe as the server of a real ``PidNamespaceScope`` and
records what the probe observed about *itself*: credentials, capability sets,
``no_new_privs``, namespaces, open descriptors, resource limits, working
directory, whether a canary environment variable's *name* reached it (its value
is never recorded), and whether it can still exercise ``CAP_SYS_ADMIN`` (a
harmless ``unshare(CLONE_NEWNS)`` in itself). It then records the scope's
termination evidence, so the privilege result is tied to a working lifecycle.

Run it directly (root relay) and under ``setpriv`` to capture the non-root
relay path; the two runs write separate files. Output is deterministic (sorted
keys, no timestamps): re-running on the same code gives identical bytes.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import IO, Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))

from stateguard.adapters.mcp.executor_scope import PidNamespaceScope  # noqa: E402

CANARY_ENV = "SG_W4B_CANARY"
_CAP_SYS_ADMIN_BIT = 1 << 21

#: The probe server: one JSON line about itself, then it serves until "exit".
PROBE = r"""
import ctypes, json, os, resource, sys
libc = ctypes.CDLL(None, use_errno=True)
def status():
    out = {}
    for line in open("/proc/self/status"):
        k, _, v = line.partition(":")
        if k in ("Uid","Gid","CapInh","CapPrm","CapEff","CapBnd","CapAmb","NoNewPrivs","Seccomp"):
            out[k] = v.strip()
    return out
def fds():
    out = {}
    for fd in os.listdir("/proc/self/fd"):
        try:
            out[fd] = os.readlink("/proc/self/fd/" + fd)
        except OSError:
            pass
    return out
report = {
    "uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(),
    "cwd": os.getcwd(),
    "status": status(),
    "fds": fds(),
    "ns": {n: os.readlink("/proc/self/ns/" + n) for n in
           ("pid","mnt","net","user","ipc","uts","cgroup")},
    "rlimits": {n: resource.getrlimit(getattr(resource, n)) for n in
                ("RLIMIT_NPROC","RLIMIT_AS","RLIMIT_CPU","RLIMIT_NOFILE")},
    "canary_env_name_present": "__CANARY__" in os.environ,
    "canary_value_recorded": False,
    "env_var_count": len(os.environ),
    # unshare(CLONE_NEWNS) needs CAP_SYS_ADMIN and changes only this process.
    "cap_sys_admin_live": libc.unshare(0x00020000) == 0,
}
sys.stdout.write(json.dumps(report) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    if line.strip() == "exit":
        break
""".replace("__CANARY__", CANARY_ENV)


def _read_line(stream: IO[bytes], timeout: float) -> bytes | None:
    got: list[bytes] = []
    done = threading.Event()

    def read() -> None:
        got.append(stream.readline())
        done.set()

    threading.Thread(target=read, daemon=True).start()
    return got[0] if done.wait(timeout) else None


def audit(out: Path) -> dict[str, Any]:
    probe = out.parent / "w4b_probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    os.environ[CANARY_ENV] = "canary-value-must-never-be-recorded"
    scope = PidNamespaceScope([sys.executable, str(probe)])
    try:
        line = _read_line(scope.stdout, 15)
        report: dict[str, Any] = json.loads(line) if line else {"error": "no report"}
    finally:
        evidence = scope.end()
    status = report.get("status", {})
    caps = {k: int(status.get(k, "0"), 16) for k in ("CapEff", "CapPrm", "CapBnd", "CapAmb")}
    relay_ns = {n: os.readlink(f"/proc/self/ns/{n}") for n in report.get("ns", {})}
    relay_mnt = os.readlink("/proc/self/ns/mnt")
    # Redact per-run values (inode numbers) so the record is byte-reproducible:
    # namespaces become a relation to the relay's, pipe/socket fds become a kind.
    server_view = {
        "uid": report.get("uid"),
        "euid": report.get("euid"),
        "gid": report.get("gid"),
        "cwd": report.get("cwd"),
        "status": status,
        "rlimits": report.get("rlimits"),
        "env_var_count": report.get("env_var_count"),
        "ns_relation": {
            n: "shared-with-relay" if report["ns"][n] == relay_ns.get(n) else "isolated"
            for n in sorted(report.get("ns", {}))
        },
        # fds 0,1,2 are always the server's stdio (fd 2 is the relay's inherited
        # stderr, whose kind depends on how the relay was launched); only fds >= 3
        # would be a leaked channel, and those are what the record pins.
        "fd_kinds_ge_3": {
            fd: tgt.split(":")[0] if (":" in tgt and "[" in tgt) else "file"
            for fd, tgt in sorted(report.get("fds", {}).items())
            if int(fd) >= 3
        },
    }
    result = {
        "relay_euid": os.geteuid(),
        "server": server_view,
        "findings": {
            "cap_sys_admin_in_eff": bool(caps["CapEff"] & _CAP_SYS_ADMIN_BIT),
            "cap_sys_admin_in_bnd": bool(caps["CapBnd"] & _CAP_SYS_ADMIN_BIT),
            "ambient_nonzero": caps["CapAmb"] != 0,
            "no_new_privs": status.get("NoNewPrivs") == "1",
            "cap_sys_admin_live": report.get("cap_sys_admin_live"),
            "shares_relay_mount_ns": report.get("ns", {}).get("mnt") == relay_mnt,
            "pid_ns_is_new": report.get("ns", {}).get("pid") != os.readlink("/proc/self/ns/pid"),
            "extra_fds": sorted(
                fd
                for fd, tgt in report.get("fds", {}).items()
                if int(fd) >= 3 and ("pipe:" in tgt or "socket:" in tgt)
            ),
            "canary_env_name_present": report.get("canary_env_name_present"),
            "canary_value_recorded": report.get("canary_value_recorded"),
        },
        "termination": {"kind": evidence.kind.value, "established": evidence.established},
    }
    return result


def main() -> None:
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    result = audit(out)
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    f = result["findings"]
    print(f"relay euid={result['relay_euid']} termination={result['termination']['kind']}")
    print(f"  CAP_SYS_ADMIN eff={f['cap_sys_admin_in_eff']} bnd={f['cap_sys_admin_in_bnd']}")
    print(f"  ambient_nonzero={f['ambient_nonzero']} no_new_privs={f['no_new_privs']}")
    print(f"  CAP_SYS_ADMIN live (unshare)={f['cap_sys_admin_live']}")
    print(f"  shares relay mount ns={f['shares_relay_mount_ns']} pid ns new={f['pid_ns_is_new']}")
    print(f"  extra fds held={f['extra_fds']}")
    print(
        f"  canary env NAME present={f['canary_env_name_present']}"
        f" value recorded={f['canary_value_recorded']}"
    )


if __name__ == "__main__":
    main()
