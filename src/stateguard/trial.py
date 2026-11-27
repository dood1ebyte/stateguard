"""
``stateguard trial``: try StateGuard on one MCP server, before and after (Phase 2 S5.1).

    stateguard trial --dir DIR --calls CALLS.jsonl [--label TEXT] [--effects FILE]
                     [--sandbox PATH] [--fault kill_before|lost_answer --fault-tool TOOL]
                     -- SERVER COMMAND...

The same scripted calls run twice through ``stateguard capture stdio``: first in **shadow**
mode (StateGuard observes and proposes, the traffic is unchanged), then in **auto** mode
(``--retry auto``: StateGuard acts where it is safe). Each run leaves a redacted capture
bundle (``DIR/shadow/bundle``, ``DIR/auto/bundle``: S2's bundle, masked, rebuilt from itself),
and ``DIR/trial_report.json`` / ``.txt`` put the two side by side: outcomes per call,
UNKNOWN, retries proposed and made, relay overhead, and whether the denominator checks hold.
Everything stays in ``DIR``, on this machine: nothing is sent anywhere.

``CALLS.jsonl``: one call per line, ``{"tool": NAME, "arguments": {...}}``. The calls run in
order, one at a time, on one session.

``--sandbox PATH``: the server works on PATH (a directory or a file named in the server
command). Each run gets a fresh copy (``DIR/<mode>/sandbox``) and the command's PATH is
replaced with it, so both runs start from the same state and the original is never touched.

``--fault``: TEST-ONLY fault injection, refused without ``--sandbox``: the first call of
``--fault-tool`` in each run is cut, by a shim between the relay and the server:
``kill_before`` (the call never reaches the server, and the server exits) or ``lost_answer``
(the call runs, its answer is dropped, and the server exits). The relay is started with
``--restart 1`` so a later call can go on.

What a trial does **not** show: a rate for any other server, workload or period; a
scripted workload is not production traffic, and the report's label says what ran.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

TRIAL_SCHEMA = "stateguard.trial/1"
MODES = ("shadow", "auto")
FAULTS = ("kill_before", "lost_answer")
CALL_TIMEOUT = 60.0
PROTOCOL = "2025-06-18"

USAGE = """\
usage: stateguard trial --dir DIR --calls CALLS.jsonl [--label TEXT] [--effects FILE]
                        [--sandbox PATH] [--fault kill_before|lost_answer --fault-tool TOOL]
                        -- SERVER COMMAND...

Run the same scripted calls through StateGuard twice -- shadow, then auto -- and write a
before/after report and two redacted capture bundles to DIR (kept on this machine)."""


class TrialError(Exception):
    """A trial that cannot run as asked (bad input, a server that does not start)."""


def read_calls(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """``{"tool", "arguments"}`` per line; blank lines and ``#`` comments are skipped."""
    calls = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TrialError(f"{path}:{number}: not JSON ({exc.msg})") from exc
        if not isinstance(item, dict) or not isinstance(item.get("tool"), str):
            raise TrialError(f'{path}:{number}: needs a "tool" name')
        arguments = item.get("arguments", {})
        if not isinstance(arguments, dict):
            raise TrialError(f'{path}:{number}: "arguments" must be an object')
        calls.append({"tool": item["tool"], "arguments": arguments})
    if not calls:
        raise TrialError(f"{path}: no calls")
    return calls


class _Session:
    """A raw JSON-RPC client over a process's stdio: one request at a time."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        self.lines: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.next_id = 0

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.lines.put(message)
        self.lines.put(None)

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", "replace").rstrip())

    def send(self, message: dict[str, Any]) -> None:
        stream: IO[bytes] | None = self.proc.stdin
        assert stream is not None
        stream.write(json.dumps(message).encode() + b"\n")
        stream.flush()

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        self.next_id += 1
        ident = self.next_id
        try:
            self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        except (BrokenPipeError, OSError, ValueError):
            return None  # the session is gone (in shadow, a dead server is not restarted)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                message = self.lines.get(timeout=left)
            except queue.Empty:
                return None
            if message is None:
                return None
            if message.get("id") == ident and "method" not in message:
                return message

    def close(self, timeout: float = 60.0) -> int:
        if self.proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            return self.proc.wait(timeout=10)


def _status(answer: dict[str, Any] | None) -> str:
    """What the client saw, as one word (never the content: the trial keeps no results)."""
    if answer is None:
        return "no_answer"
    if "error" in answer:
        data = (answer.get("error") or {}).get("data") or {}
        state = data.get("io.stateguard/executionState") if isinstance(data, dict) else None
        return f"error:{state}" if state else "error"
    result = answer.get("result")
    if isinstance(result, dict) and result.get("isError"):
        return "tool_error"
    return "ok"


def _substitute(argv: list[str], original: str, copy: str) -> list[str]:
    return [arg.replace(original, copy) if original in arg else arg for arg in argv]


def _in_sandbox(value: Any, original: str, copy: str) -> Any:
    """*value* (call arguments) with the sandbox's path replaced by its copy's, recursively."""
    if isinstance(value, str):
        return value.replace(original, copy)
    if isinstance(value, list):
        return [_in_sandbox(item, original, copy) for item in value]
    if isinstance(value, dict):
        return {key: _in_sandbox(item, original, copy) for key, item in value.items()}
    return value


def _run_mode(
    mode: str,
    server: list[str],
    calls: list[dict[str, Any]],
    out: Path,
    *,
    label: str,
    effects: str | None,
    sandbox: str | None,
    fault: str | None,
    fault_tool: str | None,
) -> dict[str, Any]:
    run_dir = out / mode
    run_dir.mkdir(parents=True)
    command = list(server)
    if sandbox is not None:
        source = Path(sandbox)
        copy = run_dir / "sandbox" / source.name
        copy.parent.mkdir()
        if source.is_dir():
            shutil.copytree(source, copy)
        else:
            shutil.copy2(source, copy)
        command = _substitute(command, str(source), str(copy))
        calls = [
            {**call, "arguments": _in_sandbox(call["arguments"], str(source), str(copy))}
            for call in calls
        ]
    relay: list[str] = ["--shadow"] if mode == "shadow" else ["--retry", "auto"]
    if effects is not None:
        relay += ["--effects", effects]
    if fault is not None:
        relay += ["--restart", "1", "--restart-policy", "after-unknown"]
        command = [
            sys.executable,
            "-m",
            "stateguard.trial_shim",
            "--mode",
            fault,
            "--tool",
            str(fault_tool),
            "--flag",
            str(run_dir / "fault.fired"),
            "--",
            *command,
        ]
    argv = [
        sys.executable,
        "-m",
        "stateguard",
        "capture",
        "stdio",
        "--dir",
        str(run_dir / "capture"),
        "--label",
        label,
        *relay,
        "--",
        *command,
    ]
    session = _Session(argv, dict(os.environ))
    statuses: list[str] = []
    started = time.monotonic()
    try:
        init = session.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "stateguard-trial", "version": "1"},
            },
            timeout=CALL_TIMEOUT,
        )
        if init is None or "result" not in init:
            raise TrialError(
                f"{mode}: the server did not answer initialize: " + " | ".join(session.stderr[-5:])
            )
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        session.request("tools/list", {}, timeout=CALL_TIMEOUT)
        for call in calls:
            answer = session.request(
                "tools/call",
                {"name": call["tool"], "arguments": call["arguments"]},
                timeout=CALL_TIMEOUT,
            )
            statuses.append(_status(answer))
    finally:
        code = session.close()
    seconds = round(time.monotonic() - started, 3)
    bundle = run_dir / "capture" / "bundle"
    if not (bundle / "report.json").exists():
        raise TrialError(
            f"{mode}: no bundle written (exit {code}): " + " | ".join(session.stderr[-5:])
        )
    from stateguard.capture import load_bundle  # noqa: PLC0415

    loaded = load_bundle(bundle)
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return {
        "report": loaded["report"],
        "rebuilt_equal": loaded["rebuilt_equal"],
        "client": {"statuses": statuses, "counts": counts},
        "seconds": seconds,
        "fault_fired": (run_dir / "fault.fired").exists(),
    }


def _side(run: dict[str, Any]) -> dict[str, Any]:
    report = run["report"]
    calls = report.get("calls") or {}

    def k(name: str) -> int:
        return int((calls.get(name) or {}).get("k", 0))

    retries = report.get("retries") or {}
    overhead = report.get("overhead_ms") or {}
    return {
        "calls": int(calls.get("n", 0)),
        "succeeded": k("succeeded"),
        "tool_error": k("tool_error"),
        "failed": k("failed"),
        "unknown": k("unknown"),
        "not_successful": k("not_successful"),
        "would_retry": int(retries.get("would_retry", 0)),
        "calls_resent": int(retries.get("calls_resent", 0)),
        "overhead_ms_p50": overhead.get("p50"),
        "overhead_ms_p95": overhead.get("p95"),
        "denominators_hold": bool((report.get("denominators") or {}).get("checks_hold")),
        "bundle_rebuilds": bool(run["rebuilt_equal"]),
        "client": run["client"]["counts"],
        "seconds": run["seconds"],
        "fault_fired": run["fault_fired"],
    }


def compare(runs: dict[str, dict[str, Any]], *, label: str, calls: int) -> dict[str, Any]:
    """The before/after report from the two runs."""
    shadow, auto = runs["shadow"], runs["auto"]
    before, after = shadow["client"]["statuses"], auto["client"]["statuses"]
    changed = [
        {"call": index, "shadow": b, "auto": a}
        for index, (b, a) in enumerate(zip(before, after, strict=True))
        if a != b
    ]
    server = shadow["report"].get("server") or {}
    return {
        "schema": TRIAL_SCHEMA,
        "label": label,
        "server": {"name": server.get("name"), "version": server.get("version")},
        "calls": calls,
        "shadow": _side(shadow),
        "auto": _side(auto),
        "changed_calls": changed,
        "not_claimed": [
            "One server, this scripted workload, this machine: not a rate for anything else.",
            "Shadow's would_retry counts resends auto would have made, not resends made.",
            "UNKNOWN is never counted as success; it is a residual failure of the call.",
            "Bundles are redacted (masked, numbers masked) and stay in this directory.",
        ],
    }


def render(trial: dict[str, Any]) -> str:
    s, a = trial["shadow"], trial["auto"]
    server = trial["server"]
    rows = [
        ("calls", s["calls"], a["calls"]),
        ("succeeded", s["succeeded"], a["succeeded"]),
        ("tool errors", s["tool_error"], a["tool_error"]),
        ("failed", s["failed"], a["failed"]),
        ("UNKNOWN (residual failure)", s["unknown"], a["unknown"]),
        ("resends proposed (shadow) / made (auto)", s["would_retry"], a["calls_resent"]),
        ("relay overhead p50 ms", s["overhead_ms_p50"], a["overhead_ms_p50"]),
        ("relay overhead p95 ms", s["overhead_ms_p95"], a["overhead_ms_p95"]),
        ("denominator checks hold", s["denominators_hold"], a["denominators_hold"]),
        ("bundle rebuilds from itself", s["bundle_rebuilds"], a["bundle_rebuilds"]),
    ]
    lines = [
        f"StateGuard trial: {trial['label']}",
        f"server: {server.get('name')} {server.get('version') or ''}".rstrip(),
        "",
        f"{'':44s}{'shadow (before)':>18s}{'auto (after)':>16s}",
    ]
    lines += [f"{name:44s}{str(b):>18s}{str(x):>16s}" for name, b, x in rows]
    lines.append("")
    lines.append(f"client saw, shadow: {s['client']}")
    lines.append(f"client saw, auto:   {a['client']}")
    if trial["changed_calls"]:
        lines.append(f"calls answered differently: {len(trial['changed_calls'])}")
        for change in trial["changed_calls"][:20]:
            lines.append(
                f"  call {change['call']}: shadow {change['shadow']} -> auto {change['auto']}"
            )
    else:
        lines.append("calls answered differently: none")
    lines.append("")
    lines += [f"not claimed: {line}" for line in trial["not_claimed"]]
    return "\n".join(lines) + "\n"


def run_trial(
    server: list[str],
    calls: list[dict[str, Any]],
    out: Path,
    *,
    label: str,
    effects: str | None = None,
    sandbox: str | None = None,
    fault: str | None = None,
    fault_tool: str | None = None,
) -> dict[str, Any]:
    if not server:
        raise TrialError("no server command after --")
    if out.exists() and any(out.iterdir()):
        raise TrialError(f"--dir {out}: not empty; a trial is never overwritten")
    if fault is not None and sandbox is None:
        raise TrialError("--fault needs --sandbox: faults run only against a sandbox copy")
    if fault is not None and not fault_tool:
        raise TrialError("--fault needs --fault-tool")
    if sandbox is not None and not Path(sandbox).exists():
        raise TrialError(f"--sandbox {sandbox}: does not exist")
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    runs = {
        mode: _run_mode(
            mode,
            server,
            calls,
            out,
            label=label,
            effects=effects,
            sandbox=sandbox,
            fault=fault,
            fault_tool=fault_tool,
        )
        for mode in MODES
    }
    trial = compare(runs, label=label, calls=len(calls))
    (out / "trial_report.json").write_text(json.dumps(trial, indent=1, sort_keys=True) + "\n")
    (out / "trial_report.txt").write_text(render(trial))
    return trial


def main(argv: list[str]) -> int:
    split = argv.index("--") if "--" in argv else len(argv)
    head, server = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser(prog="stateguard trial", usage=USAGE, allow_abbrev=False)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--calls", required=True)
    parser.add_argument("--label", default="trial, scripted workload")
    parser.add_argument("--effects")
    parser.add_argument("--sandbox")
    parser.add_argument("--fault", choices=FAULTS)
    parser.add_argument("--fault-tool")
    args = parser.parse_args(head)
    try:
        trial = run_trial(
            server,
            read_calls(args.calls),
            Path(args.dir),
            label=args.label,
            effects=args.effects,
            sandbox=args.sandbox,
            fault=args.fault,
            fault_tool=args.fault_tool,
        )
    except TrialError as exc:
        print(f"stateguard trial: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(render(trial))
    return 0
