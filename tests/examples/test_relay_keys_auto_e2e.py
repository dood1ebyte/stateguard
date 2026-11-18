"""
``stateguard relay stdio --keys auto``, end to end, from a wheel on a clean environment.

The test builds the wheel, installs it (and nothing else) into a fresh virtual environment
and runs the installed ``stateguard`` command in front of the pinned third-party
``@modelcontextprotocol/server-memory`` (Node). Nothing from this checkout is importable by
the relay: ``PYTHONPATH`` is cleared and the relay's ``stateguard`` is asserted to be the
installed one.

It shows two things (Phase 2 S1 acceptance):

* the write tool (``add_observations``) is keyed: the server, started by ``--keys auto``
  under the wheel's own TypeScript preload, advertises keys for it;
* a crash right after the key's record (TEST-ONLY fault ``after_record``: the server
  SIGKILLs itself once the outcome is recorded, before answering) is answered from that
  record exactly once: the relay restarts the server and resends with the same key, the
  new server replays the recorded result, the client gets one answer, and the observation
  is in the memory file once.

The client is a plain JSON-RPC client over the relay's stdio, so the fresh environment
needs no MCP SDK.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from stateguard.adapters.mcp.idempotency import IDEMPOTENCY_CAPABILITY, IDEMPOTENCY_OUTCOME

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node") or "/opt/node22/bin/node"


def _exists(path: Path) -> bool:
    """Path.exists, but a path the runner may not stat is not there (Python < 3.14 raises)."""
    try:
        return path.exists()
    except OSError:
        return False


MEMORY_JS = Path(
    os.environ.get(
        "MEMORY_JS",
        "/opt/sg-servers/memory/node_modules/@modelcontextprotocol/server-memory/dist/index.js",
    )
)


def _node_ready() -> bool:
    if not _exists(Path(NODE)):
        return False
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True, check=False)
    major, minor = (int(part) for part in out.stdout.strip().lstrip("v").split(".")[:2])
    return (major, minor) >= (22, 18)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _node_ready(), reason="needs Node >= 22.18"),
    pytest.mark.skipif(
        not _exists(MEMORY_JS),
        reason="needs the pinned @modelcontextprotocol/server-memory (set MEMORY_JS)",
    ),
]

WRITE = "add_observations"
READ = "search_nodes"


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh virtual environment with only the freshly built wheel in it."""
    base = tmp_path_factory.mktemp("wheel-e2e")
    wheels = base / "dist"
    venv = base / "venv"
    # The venv's own pip (ensurepip) builds the wheel: the test interpreter's environment
    # may have no pip at all (a uv-made venv). The build is isolated, so the venv still
    # holds only pip until the wheel is installed.
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=300)
    python = venv / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "wheel", str(ROOT), "--no-deps", "-q", "-w", str(wheels)],
        check=True,
        timeout=600,
        env=_clean_env(base),
    )
    (wheel,) = wheels.glob("sguard-*.whl")
    subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "--no-deps", "--no-index", str(wheel)],
        check=True,
        timeout=300,
        env=_clean_env(base),
    )
    return venv


def _clean_env(home: Path, **extra: str) -> dict[str, str]:
    """The test's environment without anything that would reach this checkout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "STATEGUARD_"))}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


class Client:
    """Newline-delimited JSON-RPC over a process's stdio."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            if line.strip():
                self.messages.put(json.loads(line))

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, ident: int, method: str, params: dict[str, Any], timeout: float) -> Any:
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        while True:
            message = self.messages.get(timeout=timeout)
            if message.get("id") == ident:
                return message

    def others(self) -> list[dict[str, Any]]:
        out = []
        while not self.messages.empty():
            out.append(self.messages.get_nowait())
        return out


def test_keys_auto_keys_the_write_and_replays_a_crash_after_the_record_once(
    installed: Path, tmp_path: Path
) -> None:
    python = installed / "bin" / "python"
    command = installed / "bin" / "stateguard"
    env = _clean_env(tmp_path)

    # The relay's stateguard is the wheel's, not this checkout's.
    where = subprocess.run(
        [str(python), "-c", "import stateguard; print(stateguard.__file__)"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=tmp_path,
    ).stdout.strip()
    assert Path(where).is_relative_to(installed), where
    assert not Path(where).is_relative_to(ROOT)

    marker = f"e2e-{uuid.uuid4().hex[:8]}"
    memory = tmp_path / "memory.jsonl"
    memory.write_text(
        json.dumps({"type": "entity", "name": "seed", "entityType": "test", "observations": []})
        + "\n"
    )
    effects = tmp_path / "effects.json"
    effects.write_text(
        json.dumps(
            {
                "tools": [
                    {"name": WRITE, "effect": "non_idempotent"},
                    {"name": READ, "effect": "read_only"},
                ]
            }
        )
    )
    audit = tmp_path / "audit.jsonl"
    fired = tmp_path / "crash.fired"
    relay_env = {
        **env,
        "MEMORY_FILE_PATH": str(memory),
        # TEST-ONLY fault: the server kills itself once, right after recording the outcome.
        "STATEGUARD_FAULT": "after_record",
        "STATEGUARD_FAULT_FILE": str(fired),
        "STATEGUARD_FAULT_TOOLS": WRITE,
    }
    process = subprocess.Popen(
        [
            str(command),
            "relay",
            "stdio",
            "--keys",
            "auto",
            "--keys-db",
            str(tmp_path / "keys.db"),
            "--retry",
            "auto",
            "--restart",
            "2",
            "--restart-policy",
            "after-unknown",
            "--effects",
            str(effects),
            "--audit",
            str(audit),
            "--",
            NODE,
            str(MEMORY_JS),
        ],  # fmt: skip
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=relay_env,
        cwd=tmp_path,
    )
    client = Client(process)
    try:
        init = client.request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "0"},
            },
            timeout=60,
        )
        assert "result" in init, init
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        listing = client.request(2, "tools/list", {}, timeout=60)
        tools = {t["name"]: t for t in listing["result"]["tools"]}
        # 1. The write tool is keyed: the server advertises keys for it, and only for it.
        assert tools[WRITE].get("_meta", {}).get(IDEMPOTENCY_CAPABILITY) == {"version": 1}
        assert IDEMPOTENCY_CAPABILITY not in (tools[READ].get("_meta") or {})

        arguments = {"observations": [{"entityName": "seed", "contents": [marker]}]}
        answer = client.request(
            3, "tools/call", {"name": WRITE, "arguments": arguments}, timeout=120
        )
    finally:
        assert process.stdin is not None
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    late = [m for m in client.others() if m.get("id") == 3]

    # 2. The crash happened, after the record ...
    assert fired.exists(), "the after_record fault never fired"
    # ... and the client was answered from the record: one success, replayed, not re-run.
    assert "result" in answer, answer
    result = answer["result"]
    assert not result.get("isError"), result
    assert (result.get("_meta") or {}).get(IDEMPOTENCY_OUTCOME) == "replayed", result
    assert late == [], "the client got a second answer to the same call"
    assert memory.read_text().count(json.dumps(marker)) == 1, memory.read_text()

    events = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
    started = next(e for e in events if e.get("event") == "relay_started")
    assert started["keys"]["adapter"] == "node-preload"
    assert started["keys"]["keyed_tools"] == [WRITE]
    assert str(installed) in started["server"][2], "the preload is the wheel's own copy"
    assert any(e.get("event") == "keys_advertised" and e.get("tool") == WRITE for e in events)
    assert not any(e.get("event") == "keys_not_advertised" for e in events)
    calls = [
        e for e in events if e.get("event") == "invocation" and e.get("method") == "tools/call"
    ]
    assert len(calls) == 1, calls
    assert calls[0]["resolution"] in ("recovered", "answered"), calls[0]


def test_keys_auto_runs_an_unknown_command_unkeyed_and_says_why(
    installed: Path, tmp_path: Path
) -> None:
    """A server command --keys auto cannot read with certainty runs as given, unkeyed."""
    command = installed / "bin" / "stateguard"
    audit = tmp_path / "audit.jsonl"
    script = tmp_path / "server.sh"
    script.write_text(f'#!/bin/sh\nexec "{NODE}" "{MEMORY_JS}"\n')
    script.chmod(0o755)
    env = _clean_env(tmp_path, MEMORY_FILE_PATH=str(tmp_path / "memory.jsonl"))
    process = subprocess.Popen(
        [
            str(command),
            "relay",
            "stdio",
            "--keys",
            "auto",
            "--keyed-tools",
            WRITE,
            "--keys-db",
            str(tmp_path / "keys.db"),
            "--retry",
            "auto",
            "--audit",
            str(audit),
            "--",
            str(script),
        ],  # fmt: skip
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=tmp_path,
    )
    client = Client(process)
    try:
        init = client.request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "0"},
            },
            timeout=60,
        )
        assert "result" in init, init
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listing = client.request(2, "tools/list", {}, timeout=60)
        tools = {t["name"]: t for t in listing["result"]["tools"]}
        assert IDEMPOTENCY_CAPABILITY not in (tools[WRITE].get("_meta") or {})
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=30)
    assert process.stderr is not None
    stderr = process.stderr.read().decode()
    assert "--keys auto: running unkeyed" in stderr and "/bin/sh" in stderr
    events = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
    started = next(e for e in events if e.get("event") == "relay_started")
    assert started["keys"]["adapter"] is None
    assert "/bin/sh" in started["keys"]["unkeyed_reason"]
    assert not (tmp_path / "keys.db").exists()
