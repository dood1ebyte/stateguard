"""
``--keys auto``'s command detection (``stateguard.relay.keys.plan``), and a real Python server
(the pinned mcp 1.x ``mcp-server-sqlite`` console script) keyed through it.

The detection never guesses: a command it cannot read with certainty runs unkeyed, with the
reason. The probes of the server's interpreter are faked here, except in the last test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from stateguard.adapters.mcp.idempotency import IDEMPOTENCY_CAPABILITY
from stateguard.relay import keys
from stateguard.relay.keys import KeyedLaunch, Unkeyed

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "examples" / "mcp" / "stdio_proxy.py"


class FakeProbe:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.result = (returncode, stdout, stderr)

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        code, out, err = self.result
        return subprocess.CompletedProcess(list(argv), code, out, err)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"PATH": str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""), **extra}


def _program(tmp_path: Path, name: str, text: str = "") -> Path:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def _sdk(root: Path, version: str = "1.32.1", package: str = "sdk") -> None:
    """A TypeScript SDK package where a server's entry resolves it (its node_modules)."""
    pkg = root / "node_modules" / "@modelcontextprotocol" / package / "package.json"
    pkg.parent.mkdir(parents=True, exist_ok=True)
    pkg.write_text(json.dumps({"name": f"@modelcontextprotocol/{package}", "version": version}))


def test_python_module_and_script_run_under_the_injector(tmp_path: Path) -> None:
    probe = FakeProbe()
    for rest in (["-m", "my_server", "--flag"], ["server.py", "--flag"]):
        launch = keys.plan(
            [sys.executable, *rest],
            ["write"],
            db=tmp_path / "k" / "keys.db",
            environ=_env(tmp_path, PYTHONPATH="/srv/lib"),
            probe=probe,
        )
        assert isinstance(launch, KeyedLaunch), launch
        assert launch.adapter == "python-injector"
        assert launch.command == [sys.executable, "-m", "stateguard.adapters.mcp.inject", *rest]
        assert launch.env[keys.DB_VAR] == str((tmp_path / "k" / "keys.db").resolve())
        assert launch.env[keys.KEYED_VAR] == "write"
        assert launch.env[keys.SCOPE_VAR] == keys.default_scope([sys.executable, *rest])
        # stateguard alone is put first on the server's path; the server's own path stays.
        first, rest_path = launch.env["PYTHONPATH"].split(os.pathsep, 1)
        assert rest_path == "/srv/lib"
        assert sorted(p.name for p in Path(first).iterdir()) == ["stateguard"]
        assert (Path(first) / "stateguard" / "relay" / "keys.py").is_file()
    # The interpreter was checked in the server's environment, stateguard importable.
    assert all("stateguard.adapters.mcp.inject" in call[-1] for call in probe.calls)
    assert probe.envs[0]["PYTHONPATH"].endswith("/srv/lib")


def test_a_python_console_script_runs_under_its_own_interpreter(tmp_path: Path) -> None:
    script = _program(tmp_path, "mcp-server-x", f"#!{sys.executable}\nprint('hi')\n")
    launch = keys.plan(
        ["mcp-server-x", "--db", "a.db"],
        ["write"],
        db=tmp_path / "keys.db",
        environ=_env(tmp_path),
        probe=FakeProbe(),
    )
    assert isinstance(launch, KeyedLaunch), launch
    assert launch.command == [
        sys.executable, "-m", "stateguard.adapters.mcp.inject", str(script), "--db", "a.db",
    ]  # fmt: skip


def test_an_env_shebang_is_resolved_on_the_servers_path(tmp_path: Path) -> None:
    python = _program(tmp_path, "python3", "")
    _program(tmp_path, "srv", "#!/usr/bin/env python3\n")
    launch = keys.plan(
        ["srv"], ["write"], db=tmp_path / "keys.db", environ=_env(tmp_path), probe=FakeProbe()
    )
    assert isinstance(launch, KeyedLaunch), launch
    assert launch.command[0] == str(python)


def test_an_npm_installed_node_bin_runs_with_the_preload(tmp_path: Path) -> None:
    node = _program(tmp_path, "node", "\x7fELF")
    script = _program(tmp_path, "mcp-server-memory", "#!/usr/bin/env node\n")
    _sdk(tmp_path)  # resolved from the bin's directory, upwards, as the preload does
    launch = keys.plan(
        ["mcp-server-memory"],
        ["add_observations"],
        db=tmp_path / "keys.db",
        environ=_env(tmp_path),
        probe=FakeProbe(stdout="v24.1.0\n"),
    )
    assert isinstance(launch, KeyedLaunch), launch
    assert launch.command == [str(node), "--import", str(keys.preload_path()), str(script)]


def test_node_runs_with_the_preload(tmp_path: Path) -> None:
    node = _program(tmp_path, "node")
    entry = tmp_path / "server" / "dist" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("")
    _sdk(tmp_path / "server")
    launch = keys.plan(
        ["node", "--no-warnings", str(entry), "/data"],
        ["move_file"],
        db=tmp_path / "keys.db",
        environ=_env(tmp_path),
        probe=FakeProbe(stdout="v22.18.0\n"),
    )
    assert isinstance(launch, KeyedLaunch), launch
    preload = keys.preload_path()
    assert preload is not None and preload.name == "preload.ts"
    assert launch.command == [
        "node",
        "--import",
        str(preload),
        "--no-warnings",
        str(entry),
        "/data",
    ]
    assert launch.adapter == "node-preload"
    assert node.exists()


@pytest.mark.parametrize(
    ("layout", "reason"),
    [
        ("v2", "no @modelcontextprotocol/sdk (1.x) above"),  # the SDK v2 packages only
        ("sdk2", "the preload wraps the TypeScript SDK 1.x only"),
        ("none", "no @modelcontextprotocol/sdk (1.x) above"),
        ("missing", "the entry script was not found"),
    ],
)
def test_a_node_server_not_on_the_typescript_sdk_1_runs_unkeyed(
    tmp_path: Path, layout: str, reason: str
) -> None:
    """
    M2 (2026-11-27): the reference servers moved to the TypeScript SDK v2. The preload wraps
    SDK 1.x and would stop such a server from starting, so the plan runs it unkeyed.
    """
    _program(tmp_path, "node")
    entry = tmp_path / "server" / "dist" / "index.js"
    if layout != "missing":
        entry.parent.mkdir(parents=True)
        entry.write_text("")
    if layout == "v2":
        _sdk(tmp_path / "server", "2.4.0", package="server")
        _sdk(tmp_path / "server", "2.4.0", package="core")
    if layout == "sdk2":
        _sdk(tmp_path / "server", "2.0.0")
    launch = keys.plan(
        ["node", "--import", "x.mjs", str(entry)], ["write"], db=tmp_path / "keys.db",
        environ=_env(tmp_path), probe=FakeProbe(stdout="v22.18.0\n"),
    )  # fmt: skip
    assert isinstance(launch, keys.Unkeyed), launch
    assert reason in launch.reason


@pytest.mark.parametrize(
    ("command", "probe", "reason"),
    [
        (["npx", "-y", "@modelcontextprotocol/server-memory"], FakeProbe(), "npx starts"),
        (["uvx", "mcp-server-git"], FakeProbe(), "uvx starts"),
        (["server.sh"], FakeProbe(), "script for '/bin/sh'"),
        (["binary"], FakeProbe(), "not a Python or Node"),
        (["missing-program"], FakeProbe(), "was not found"),
        (["node", "x.js"], FakeProbe(stdout="v20.11.1\n"), "needs Node 22.18+"),
        (["python3", "-u", "s.py"], FakeProbe(), "only 'python SCRIPT'"),
        (["python3"], FakeProbe(), "no script or -m module"),
        (
            ["python3", "s.py"],
            FakeProbe(returncode=1, stderr="ModuleNotFoundError: No module named 'mcp'\n"),
            "cannot run the injector",
        ),
    ],
)
def test_what_cannot_be_read_with_certainty_runs_unkeyed(
    tmp_path: Path, command: list[str], probe: FakeProbe, reason: str
) -> None:
    for name in ("npx", "uvx", "binary", "node", "python3"):
        _program(tmp_path, name, "\x7fELF")
    _program(tmp_path, "server.sh", "#!/bin/sh\nexec true\n")
    plan = keys.plan(
        command, ["write"], db=tmp_path / "keys.db", environ=_env(tmp_path), probe=probe
    )
    assert isinstance(plan, Unkeyed), plan
    assert reason in plan.reason


def test_nothing_to_key_or_keys_configured_by_hand_runs_unkeyed(tmp_path: Path) -> None:
    none = keys.plan([sys.executable, "s.py"], [], db=tmp_path / "k.db", probe=FakeProbe())
    assert isinstance(none, Unkeyed) and "no tools to key" in none.reason
    by_hand = keys.plan(
        [sys.executable, "s.py"],
        ["write"],
        db=tmp_path / "k.db",
        environ=_env(tmp_path, STATEGUARD_KEYED_TOOLS="other"),
        probe=FakeProbe(),
    )
    assert isinstance(by_hand, Unkeyed) and "configured by hand" in by_hand.reason


def test_keyed_tools_are_deduplicated_in_order(tmp_path: Path) -> None:
    launch = keys.plan(
        [sys.executable, "s.py"], ["b", "a", "b", ""], db=tmp_path / "k.db", probe=FakeProbe()
    )
    assert isinstance(launch, KeyedLaunch) and launch.keyed_tools == ("b", "a")


def test_the_relay_refuses_contradictory_key_options(tmp_path: Path) -> None:
    for extra in (
        ["--keyed-tools", "w"],
        ["--retry", "auto", "--keys", "auto", "--no-idempotency-keys"],
        ["--keys", "auto", "--keyed-tools", "w"],  # no --retry auto
    ):
        done = subprocess.run(
            [sys.executable, str(RELAY), *extra, "--", sys.executable, "s.py"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 2, done.stderr
        assert "--keys auto" in done.stderr


MCP1 = os.environ.get("STATEGUARD_MCP1_PYTHON")


@pytest.mark.integration
@pytest.mark.skipif(not MCP1, reason="needs STATEGUARD_MCP1_PYTHON (mcp 1.x + mcp-server-sqlite)")
def test_a_real_python_console_script_is_keyed(tmp_path: Path) -> None:
    """The pinned mcp-server-sqlite console script, started by --keys auto, advertises keys."""
    assert MCP1 is not None
    server = Path(MCP1).parent / "mcp-server-sqlite"
    audit = tmp_path / "audit.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith("STATEGUARD_")}
    env["HOME"] = str(tmp_path)
    process = subprocess.Popen(
        [
            sys.executable,
            str(RELAY),
            "--keys",
            "auto",
            "--keyed-tools",
            "write_query",
            "--keys-db",
            str(tmp_path / "keys.db"),
            "--retry",
            "auto",
            "--audit",
            str(audit),
            "--",
            str(server),
            "--db-path",
            str(tmp_path / "app.db"),
        ],  # fmt: skip
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None

    def request(ident: int, method: str, params: dict[str, Any]) -> Any:
        assert process.stdin is not None and process.stdout is not None
        message = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        process.stdin.write(json.dumps(message).encode() + b"\n")
        process.stdin.flush()
        while True:
            answer = json.loads(process.stdout.readline())
            if answer.get("id") == ident:
                return answer

    try:
        request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        )
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.flush()
        tools = {t["name"]: t for t in request(2, "tools/list", {})["result"]["tools"]}
    finally:
        process.stdin.close()
        process.wait(timeout=30)
    assert tools["write_query"].get("_meta", {}).get(IDEMPOTENCY_CAPABILITY) == {"version": 1}
    assert IDEMPOTENCY_CAPABILITY not in (tools["read_query"].get("_meta") or {})
    events = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
    started = next(e for e in events if e.get("event") == "relay_started")
    assert started["keys"]["adapter"] == "python-injector"
    server_command = started["server"]
    assert server_command[1:4] == ["-m", "stateguard.adapters.mcp.inject", str(server)]
    assert Path(server_command[0]).parent == Path(MCP1).parent  # the script's own interpreter
