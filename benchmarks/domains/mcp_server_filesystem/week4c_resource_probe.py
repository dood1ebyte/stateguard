"""
Week 4C resource-containment probe (isolated, read-mostly, bounded).

usage: week4c_resource_probe.py OUT_JSON

Records, without implementing any containment:
* the resource limits a scoped server inherits (rlimits; RLIMIT_NPROC is per-UID);
* whether cgroup v2 is mounted and which controllers are available;
* whether this process could create a delegated child cgroup and write pids.max
  (a bounded probe: it makes one empty cgroup, writes pids.max=16, removes it);
* a bounded fork demonstration (8 short-lived children) to confirm the scope
  imposes no process-count ceiling of its own.

Nothing unbounded or destructive runs. Output is deterministic except for the
cgroup path, which is redacted to its leaf name.
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import sys
from pathlib import Path
from typing import Any

CG_ROOT = Path("/sys/fs/cgroup")


def _rlimits() -> dict[str, list[int]]:
    return {
        name: list(resource.getrlimit(getattr(resource, name)))
        for name in ("RLIMIT_NPROC", "RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_NOFILE", "RLIMIT_FSIZE")
    }


def _cgroup_v2() -> dict[str, Any]:
    info: dict[str, Any] = {"mounted": (CG_ROOT / "cgroup.controllers").exists()}
    if not info["mounted"]:
        return info
    info["controllers"] = (CG_ROOT / "cgroup.controllers").read_text().split()
    try:
        own = Path("/proc/self/cgroup").read_text().strip().split(":")[-1]
        info["own_cgroup_leaf"] = own.rsplit("/", 1)[-1] or "(root)"
    except OSError:
        info["own_cgroup_leaf"] = "unavailable"
    return info


def _can_delegate_cgroup() -> dict[str, Any]:
    """Can a child cgroup with pids.max be created here? Bounded; cleaned up."""
    if not (CG_ROOT / "cgroup.controllers").exists():
        return {"attempted": False, "why": "cgroup v2 not mounted"}
    probe = CG_ROOT / "stateguard-w4c-probe"
    try:
        probe.mkdir()
    except OSError as exc:
        return {"attempted": True, "created_child_cgroup": False, "why": str(exc)}
    wrote_pids_max = False
    why = ""
    try:
        try:
            (probe / "pids.max").write_text("16")
            wrote_pids_max = True
        except OSError as exc:
            why = f"pids.max not writable: {exc}"
    finally:
        with contextlib.suppress(OSError):
            probe.rmdir()
    return {
        "attempted": True,
        "created_child_cgroup": True,
        "wrote_pids_max": wrote_pids_max,
        "why": why,
    }


def _bounded_fork() -> dict[str, Any]:
    """Fork exactly 8 short-lived children; confirm none is blocked. Harmless."""
    pids = []
    for _ in range(8):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        pids.append(pid)
    reaped = 0
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        reaped += os.waitstatus_to_exitcode(status) == 0
    return {"forked": len(pids), "reaped_ok": reaped, "ceiling_hit": False}


def main() -> None:
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "euid": os.geteuid(),
        "rlimits": _rlimits(),
        "rlimit_nproc_is_per_uid": True,
        "cgroup_v2": _cgroup_v2(),
        "cgroup_delegation": _can_delegate_cgroup(),
        "bounded_fork": _bounded_fork(),
    }
    out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
