"""
Week 4C — the CLI's executor-scope environment and working-directory policy.

Behavioural tests drive the real relay as a subprocess with a server that
reports its own environment (names only) and cwd. Argument-validation refusals
run `main()` in process and assert nothing starts. No secret value is ever
asserted to be present in a log.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.examples.test_relay_reconciliation_audit import RELAY

from stateguard.adapters.mcp.executor_scope import check_pid_namespace_capability

pytestmark = pytest.mark.integration

CAPABILITY = check_pid_namespace_capability()
needs_scope = pytest.mark.skipif(not CAPABILITY.available, reason=CAPABILITY.reason)
ROOT = Path(__file__).resolve().parents[2]

#: A server that reports its own environment (names only) and cwd via one tool.
ENV_SERVER = r"""
import json, os, sys
def send(m): sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
for raw in sys.stdin:
    msg = json.loads(raw); rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        send({"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":msg["params"]["protocolVersion"],
              "capabilities":{"tools":{}},"serverInfo":{"name":"env","version":"0"}}})
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":rid,"result":{"tools":[
              {"name":"envdump","inputSchema":{"type":"object","properties":{}}}]}})
    elif method == "tools/call":
        body = {"names": sorted(os.environ), "cwd": os.getcwd(),
                "values_present": {k: (k in os.environ) for k in
                                   ("MY_API_TOKEN","DATABASE_PASSWORD","GH_TOKEN","MCP_ROOT","PATH")}}
        send({"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":json.dumps(body)}],
              "isError": False}})
    sys.stdout.flush()
"""

INIT = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}}
PLANTED = {
    "MY_API_TOKEN": "sk-should-not-leak",
    "DATABASE_PASSWORD": "hunter2",
    "GH_TOKEN": "ghp_x",
}


def _drive(argv: list[str], env: dict[str, str], cwd: str | None = None) -> dict[str, Any]:
    """Run the relay to one envdump tool call; return the server's reported dump."""
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, cwd=cwd,
    )  # fmt: skip
    assert proc.stdin is not None and proc.stdout is not None
    try:
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "envdump", "arguments": {}}},
        ):  # fmt: skip
            proc.stdin.write(json.dumps(msg).encode() + b"\n")
            proc.stdin.flush()
        for raw in proc.stdout:
            message = json.loads(raw)
            if message.get("id") == 2 and "result" in message:
                data: dict[str, Any] = json.loads(message["result"]["content"][0]["text"])
                return data
        return {}
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _run(
    tmp_path: Path, flags: list[str], *, scope: bool, env_extra: dict[str, str]
) -> dict[str, Any]:
    """Drive the relay with ENV_SERVER and a planted-secret relay environment."""
    server = tmp_path / "env_server.py"
    server.write_text(ENV_SERVER)
    argv = [sys.executable, str(RELAY), "--retry", "off", *flags, "--",
            sys.executable, str(server)]  # fmt: skip
    return _drive(argv, {**os.environ, **PLANTED, **env_extra})


# ---------------------------------------------------------------------------
# Phase 2 — environment isolation
# ---------------------------------------------------------------------------


@needs_scope
def test_planted_secrets_do_not_reach_a_scoped_server(tmp_path: Path) -> None:
    dump = _run(tmp_path, ["--executor-scope", "pid-namespace"], scope=True, env_extra={})
    assert dump, "the server never answered"
    present = dump["values_present"]
    assert present["MY_API_TOKEN"] is False
    assert present["DATABASE_PASSWORD"] is False
    assert present["GH_TOKEN"] is False
    assert present["PATH"] is True  # the baseline


@needs_scope
def test_explicitly_configured_variables_reach_the_scoped_server(tmp_path: Path) -> None:
    dump = _run(
        tmp_path,
        ["--executor-scope", "pid-namespace", "--env", "MCP_ROOT=/data",
         "--env-passthrough", "MY_API_TOKEN"],  # explicit opt-in of one otherwise-blocked secret
        scope=True,
        env_extra={},
    )  # fmt: skip
    assert dump["values_present"]["MCP_ROOT"] is True
    assert dump["values_present"]["MY_API_TOKEN"] is True  # permitted explicitly
    assert dump["values_present"]["DATABASE_PASSWORD"] is False  # not permitted


@needs_scope
def test_no_scope_still_inherits_the_full_environment(tmp_path: Path) -> None:
    """The pre-4C default is unchanged outside executor-scope mode."""
    dump = _run(tmp_path, [], scope=False, env_extra={})
    assert dump["values_present"]["MY_API_TOKEN"] is True  # inherited, as before
    assert dump["values_present"]["DATABASE_PASSWORD"] is True


@needs_scope
def test_no_env_value_appears_in_the_audit_log(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    _run(
        tmp_path,
        ["--executor-scope", "pid-namespace", "--env", "MCP_ROOT=/data",
         "--env-passthrough", "MY_API_TOKEN", "--audit", str(audit)],
        scope=True,
        env_extra={},
    )  # fmt: skip
    text = audit.read_text()
    assert "sk-should-not-leak" not in text  # the value of MY_API_TOKEN
    assert "hunter2" not in text
    started = next(json.loads(ln) for ln in text.splitlines() if '"relay_started"' in ln)
    # names are logged, values are not
    assert "MY_API_TOKEN" in started["server_env_names"]
    assert "MCP_ROOT" in started["server_env_names"]
    assert started["server_cwd"] == "/"


# -- argument validation: refused before any server starts (in-process main) --


def _refused(tmp_path: Path, flags: list[str]) -> str:
    """Run main() in process; assert it exits 2 and starts nothing. Returns stderr."""
    from runtime.harness import load_relay_module

    relay = load_relay_module()
    marker = tmp_path / "started"
    program = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    argv = [*flags, "--", *program]
    import io
    from contextlib import redirect_stderr

    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exit_info:
        relay.main(argv)
    assert exit_info.value.code == 2, err.getvalue()
    assert not marker.exists(), "no server may start on a refused configuration"
    return err.getvalue()


SCOPE = ["--executor-scope", "pid-namespace", "--restart", "0"]


@needs_scope
@pytest.mark.parametrize(
    "flags, needle",
    [
        (["--env", "NOEQUALS"], "expected NAME=VALUE"),
        (["--env", "=novalue"], "non-empty NAME"),
        (["--env", "A=1", "--env", "A=2"], "given more than once"),
        (["--env", "A=1", "--env-passthrough", "A"], "one source per variable"),
        (["--env-passthrough", "DEFINITELY_NOT_SET_12345"], "not set in the relay's environment"),
    ],
)
def test_malformed_or_conflicting_env_is_refused(
    tmp_path: Path, flags: list[str], needle: str
) -> None:
    error = _refused(tmp_path, [*SCOPE, *flags])
    assert needle in error


def test_env_flags_without_a_scope_are_refused(tmp_path: Path) -> None:
    for flags in (["--env", "A=1"], ["--env-passthrough", "PATH"], ["--server-cwd", "/tmp"]):
        error = _refused(tmp_path, flags)
        assert "--executor-scope pid-namespace" in error


# ---------------------------------------------------------------------------
# Phase 3 — working-directory policy
# ---------------------------------------------------------------------------


@needs_scope
def test_default_working_directory_is_neutral_not_the_relays(tmp_path: Path) -> None:
    dump = _run(tmp_path, ["--executor-scope", "pid-namespace"], scope=True, env_extra={})
    assert dump["cwd"] == "/"  # deterministic neutral default
    assert dump["cwd"] != os.getcwd()  # not the relay's repo


@needs_scope
def test_an_explicit_working_directory_is_honoured(tmp_path: Path) -> None:
    target = tmp_path / "work"
    target.mkdir()
    target.chmod(0o755)
    dump = _run(
        tmp_path,
        ["--executor-scope", "pid-namespace", "--server-cwd", str(target)],
        scope=True,
        env_extra={},
    )
    assert dump["cwd"] == str(target)


@needs_scope
def test_a_nonexistent_working_directory_is_refused(tmp_path: Path) -> None:
    error = _refused(tmp_path, [*SCOPE, "--server-cwd", str(tmp_path / "nope")])
    assert "not an existing directory" in error


@needs_scope
def test_a_relative_server_program_still_runs_under_the_neutral_cwd(tmp_path: Path) -> None:
    """The relay resolves the program to an absolute path, so changing the server
    cwd to '/' does not break a relative command."""
    server = tmp_path / "env_server.py"
    server.write_text(ENV_SERVER)
    # Relative interpreter path, resolved against the relay's cwd (ROOT).
    rel = os.path.relpath(sys.executable, ROOT)
    argv = [sys.executable, str(RELAY), "--retry", "off", "--executor-scope", "pid-namespace",
            "--", rel, str(server)]  # fmt: skip
    got = _drive(argv, {**os.environ}, cwd=str(ROOT))
    assert got and got["cwd"] == "/"
