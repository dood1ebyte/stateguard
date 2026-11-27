"""
``stateguard doctor``: check this machine for StateGuard, and optionally one server (Phase 2 S5.2).

    stateguard doctor [-- SERVER COMMAND...]

Each line is ``ok``, ``warn`` or ``FAIL`` with what was checked. Exit 1 if anything failed.
With a server command, it is started behind ``stateguard relay stdio --shadow`` (which changes
nothing), initialized and listed, and ``--keys auto``'s plan for it is shown (nothing is
keyed: the plan is only read).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import stateguard

#: Node's first release that strips TypeScript types without a flag (the preload needs it).
NODE_MINIMUM = (22, 18)


def _line(level: str, what: str, detail: str) -> tuple[str, str, str]:
    return level, what, detail


def check_environment() -> list[tuple[str, str, str]]:
    out = []
    version = sys.version_info
    out.append(
        _line(
            "ok" if version >= (3, 11) else "FAIL",
            "python",
            f"{version.major}.{version.minor}.{version.micro} (3.11 or later needed)",
        )
    )
    out.append(_line("ok", "stateguard", stateguard.__version__))
    missing = [m for m in ("httpx2", "uvicorn", "starlette") if importlib.util.find_spec(m) is None]
    out.append(
        _line(
            "ok" if not missing else "warn",
            "http relay",
            "available"
            if not missing
            else "needs the mcp extra (pip install 'sguard[mcp]'); missing " + ", ".join(missing),
        )
    )
    node = shutil.which("node")
    if node is None:
        out.append(_line("warn", "node", "not on PATH: --keys auto cannot key a Node server"))
    else:
        raw = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
        text = raw.stdout.strip().lstrip("v")
        try:
            found = tuple(int(x) for x in text.split(".")[:2])
        except ValueError:
            found = (0, 0)
        ok = found >= NODE_MINIMUM
        out.append(
            _line(
                "ok" if ok else "warn",
                "node",
                f"{text} ({'enough' if ok else 'below 22.18'} for the preload)",
            )
        )
    from stateguard.relay import keys  # noqa: PLC0415

    preload = keys.preload_path()
    out.append(
        _line(
            "ok" if preload is not None else "warn",
            "preload",
            str(preload) if preload is not None else "not found (set STATEGUARD_PRELOAD)",
        )
    )
    store = keys.DEFAULT_DB.parent
    try:
        store.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=store):
            pass
        out.append(_line("ok", "key store", f"{store} is writable"))
    except OSError as exc:
        out.append(_line("warn", "key store", f"{store}: {exc} (pass --keys-db elsewhere)"))
    return out


def check_server(server: list[str]) -> list[tuple[str, str, str]]:
    """Start *server* behind a shadow relay, initialize and list it; show --keys auto's plan."""
    from stateguard.relay import keys  # noqa: PLC0415
    from stateguard.trial import CALL_TIMEOUT, PROTOCOL, _Session  # noqa: PLC0415

    out = []
    argv = [
        sys.executable,
        "-m",
        "stateguard",
        "relay",
        "stdio",
        "--shadow",
        "--audit",
        os.devnull,
        "--",
        *server,
    ]
    session = _Session(argv, dict(os.environ))
    tools: list[str] = []
    try:
        init = session.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "stateguard-doctor", "version": "1"},
            },
            timeout=CALL_TIMEOUT,
        )
        if init is None or "result" not in init:
            tail = " | ".join(session.stderr[-3:]) or "no answer"
            out.append(
                _line("FAIL", "server", f"did not answer initialize through the relay: {tail}")
            )
            return out
        info: Any = init["result"].get("serverInfo") or {}
        out.append(_line("ok", "server", f"{info.get('name')} {info.get('version', '')}".strip()))
        listed = session.request("tools/list", {}, timeout=CALL_TIMEOUT)
        if listed is None or "result" not in listed:
            out.append(_line("FAIL", "tools/list", "no listing through the relay"))
        else:
            listing = listed["result"].get("tools", [])
            tools = [str(t["name"]) for t in listing if isinstance(t, dict) and t.get("name")]
            out.append(_line("ok", "tools/list", f"{len(tools)} tools through the relay"))
    finally:
        session.close(timeout=20)
    if tools:
        launch = keys.plan(server, tools[:1], probe=keys._probe)
        if isinstance(launch, keys.KeyedLaunch):
            out.append(_line("ok", "--keys auto", f"would start it with the {launch.adapter}"))
        else:
            out.append(_line("warn", "--keys auto", f"would run it unkeyed: {launch.reason}"))
    return out


def main(argv: list[str]) -> int:
    server = argv[argv.index("--") + 1 :] if "--" in argv else []
    lines = check_environment()
    if server:
        lines += check_server(server)
    for level, what, detail in lines:
        print(f"{level:5s} {what:12s} {detail}")
    failed = any(level == "FAIL" for level, _, _ in lines)
    print("stateguard doctor: " + ("problems found" if failed else "ready"))
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
