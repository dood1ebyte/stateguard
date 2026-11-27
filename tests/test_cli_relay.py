"""``stateguard relay stdio|http``: the CLI hands everything after the transport to the relay."""

from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from stateguard import cli

ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "from stateguard.cli import main; main()", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_relay_is_listed_in_help() -> None:
    done = _run("--help")
    assert done.returncode == 0
    assert "relay" in done.stdout


@pytest.mark.parametrize(
    ("args", "code", "stream", "text"),
    [
        ((), 2, "stderr", "usage: stateguard relay stdio"),
        (("--help",), 0, "stdout", "usage: stateguard relay stdio"),
        (("tcp",), 2, "stderr", "unknown transport 'tcp'"),
        (("stdio", "-h"), 0, "stdout", "usage: stateguard relay stdio [-h]"),
        (("stdio",), 2, "stderr", "give the server command after --"),
        (("http", "-h"), 0, "stdout", "usage: stateguard relay http [-h]"),
        (("http",), 2, "stderr", "--upstream URL"),
        (("http", "--upstream", "http://a/mcp", "http://b/mcp"), 2, "stderr", "once"),
    ],
)
def test_relay_dispatch(args: tuple[str, ...], code: int, stream: str, text: str) -> None:
    done = _run("relay", *args)
    assert done.returncode == code, (done.stdout, done.stderr)
    assert text in getattr(done, stream)


def test_relay_stdio_runs_the_relay(tmp_path: Path) -> None:
    """The package command relays: a server that exits at once ends the relay with its code."""
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "from stateguard.cli import main; main()",
            "relay",
            "stdio",
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(3)",
        ],  # fmt: skip
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 3, done.stderr


def test_relay_http_without_the_mcp_extra_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_import = builtins.__import__

    def no_httpx2(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "httpx2" or name.startswith("httpx2."):
            raise ModuleNotFoundError("No module named 'httpx2'", name="httpx2")
        return real_import(name, *args, **kwargs)

    import stateguard.relay

    # Imported earlier in this process, the package attribute would answer the import.
    monkeypatch.delitem(sys.modules, "stateguard.relay.http", raising=False)
    monkeypatch.delattr(stateguard.relay, "http", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_httpx2)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["relay", "http", "--upstream", "http://127.0.0.1:1/mcp"])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "httpx2" in err and "sguard[mcp]" in err


def test_the_example_wrappers_are_the_package_modules() -> None:
    sys.path.insert(0, str(ROOT / "examples" / "mcp"))
    try:
        import http_proxy
        import stdio_proxy

        from stateguard.relay import http, stdio

        assert stdio_proxy is stdio
        assert http_proxy is http
    finally:
        sys.path.remove(str(ROOT / "examples" / "mcp"))
