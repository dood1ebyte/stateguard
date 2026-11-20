"""
``stateguard capture`` and ``stateguard report``: a relay session recorded into a bundle that
can be shared, and a report rebuilt from the bundle alone (Phase 2 S2.1 and S2.2).

The server is the SDK-free scripted fixture (``tests/examples/fake_mcp_server.py``), which
echoes each call's arguments back in its result, so a value typed into a call would be in the
recording twice if redaction failed. Everything here is a synthetic fixture, not traffic.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from stateguard.adapters.mcp.recording import read_recording
from stateguard.capture import (
    INVOCATIONS,
    BundleError,
    build_bundle,
    compute_report,
    find_values,
    load_bundle,
    percentiles,
    project_invocation,
    read_invocations,
)

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "examples" / "fake_mcp_server.py"
CLI = [sys.executable, "-c", "from stateguard.cli import main; main()"]
SECRET = "hunter2-Zq9"
TYPED = 482913


def _session(command: list[str], calls: int, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """A client: initialize, wait, initialized, list, then *calls* tool calls, one at a time."""
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd
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

    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}
    request(0, "initialize", init)
    process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    process.stdin.flush()
    request(1, "tools/list", {})
    for i in range(calls):
        arguments = {"query": f"{SECRET} note {i}", "limit": 7, "token": f"tok-{SECRET}"}
        request(10 + i, "tools/call", {"name": "search", "arguments": arguments})
    request(99, "tools/call", {"name": "search", "arguments": {"query": str(TYPED), "limit": 3}})
    process.stdin.close()
    process.wait(timeout=60)
    stderr = process.stderr.read() if process.stderr else b""
    return subprocess.CompletedProcess(command, process.returncode, b"", stderr)


def _capture(tmp_path: Path, *relay: str, calls: int = 4, name: str = "cap") -> Path:
    directory = tmp_path / name
    done = _session(
        [
            *CLI,
            "capture",
            "stdio",
            "--dir",
            str(directory),
            "--label",
            "test fixture (synthetic)",
            *relay,
            "--",
            sys.executable,
            str(FAKE),
            str(tmp_path / f"{name}-server.log"),
        ],  # fmt: skip
        calls,
        tmp_path,
    )
    assert done.returncode == 0, done.stderr.decode()
    assert b"checks hold" in done.stderr, done.stderr.decode()
    return directory


@pytest.mark.parametrize(
    ("relay", "provenance", "mode"),
    [(("--shadow",), "real_capture", "shadow"), (("--retry", "auto"), "local_development", "auto")],
)
def test_a_capture_writes_a_bundle_whose_report_rebuilds_from_the_bundle_alone(
    tmp_path: Path, relay: tuple[str, ...], provenance: str, mode: str
) -> None:
    directory = _capture(tmp_path, *relay)
    bundle = directory / "bundle"
    assert sorted(p.name for p in bundle.iterdir()) == [
        "invocations.jsonl", "manifest.json", "recording.jsonl", "report.json", "report.txt",
    ]  # fmt: skip
    # The raw directory is private; the unredacted audit log stays there, never in the bundle.
    assert stat.S_IMODE((directory / "raw").stat().st_mode) == 0o700
    assert (directory / "raw" / "audit.jsonl").is_file()

    loaded = load_bundle(bundle)
    report = loaded["report"]
    assert loaded["rebuilt_equal"] is True
    assert report["provenance"] == provenance and report["mode"] == mode
    assert report["label"] == "test fixture (synthetic)"
    d = report["denominators"]
    assert d["client_tool_calls"] == d["tool_call_records"] == 5
    assert d["tool_calls_answered_to_client"] == 5 and d["unfinished"] == 0
    assert d["checks_hold"] is True
    assert report["calls"]["succeeded"]["k"] == 5
    assert report["calls"]["failed"]["k"] == report["calls"]["unknown"]["k"] == 0
    assert report["integrity"]["ground_truth_records"] == 0
    assert report["latency_ms"]["n"] == 5 and report["overhead_ms"]["n"] == 5

    checked = subprocess.run(
        [*CLI, "report", "--check", str(bundle)], capture_output=True, text=True, timeout=60
    )
    assert checked.returncode == 0, checked.stderr
    assert "rebuilt from the bundle alone: equal" in checked.stdout


def test_the_bundle_carries_no_typed_value_and_masks_numbers(tmp_path: Path) -> None:
    bundle = _capture(tmp_path, "--shadow") / "bundle"
    found = find_values(bundle, [SECRET, f"tok-{SECRET}", str(TYPED), "note 0"])
    assert found == dict.fromkeys(found, 0), found
    header = read_recording(bundle / "recording.jsonl").header
    assert header["redaction"]["mode"] == "mask"
    assert "mask_numbers" in header["redaction"]["rules"]
    # The projection keeps only allowlisted, identifier-shaped fields: no ids, no text.
    for item in read_invocations(bundle / INVOCATIONS):
        assert set(item) == {
            "method", "tool", "attempts", "faults", "resolution", "server_outcome", "ms",
        }  # fmt: skip


def test_an_altered_bundle_is_refused(tmp_path: Path) -> None:
    bundle = _capture(tmp_path, "--shadow") / "bundle"
    path = bundle / INVOCATIONS
    path.write_text(path.read_text().replace('"answered"', '"unknown"', 1))
    with pytest.raises(BundleError, match="does not match the manifest"):
        load_bundle(bundle)
    done = subprocess.run([*CLI, "report", str(bundle)], capture_output=True, timeout=60)
    assert done.returncode == 2


def test_a_recording_without_number_masking_is_never_bundled(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    done = _session(
        [
            sys.executable,
            str(ROOT / "examples" / "mcp" / "stdio_proxy.py"),
            "--shadow",
            "--record",
            str(raw / "recording.jsonl"),
            "--record-redaction",
            "mask",
            "--audit",
            str(raw / "audit.jsonl"),
            "--",
            sys.executable,
            str(FAKE),
            str(tmp_path / "server.log"),
        ],  # fmt: skip
        1,
        tmp_path,
    )
    assert done.returncode == 0, done.stderr.decode()
    with pytest.raises(BundleError, match="does not mask numbers"):
        build_bundle(raw, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_capture_refuses_options_it_owns_and_a_used_directory(tmp_path: Path) -> None:
    owned = subprocess.run(
        [*CLI, "capture", "stdio", "--dir", str(tmp_path / "a"), "--audit", "x.jsonl",
         "--", sys.executable, str(FAKE), "log"],  # fmt: skip
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert owned.returncode == 2 and "set by capture" in owned.stderr
    used = tmp_path / "used"
    used.mkdir()
    (used / "something").write_text("x")
    again = subprocess.run(
        [*CLI, "capture", "stdio", "--dir", str(used), "--", sys.executable, str(FAKE), "log"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert again.returncode == 2 and "never overwritten" in again.stderr


# -- S2.2: the denominators ------------------------------------------------------------------


def test_an_unfinished_call_is_counted_and_a_missing_record_breaks_the_checks(
    tmp_path: Path,
) -> None:
    bundle = _capture(tmp_path, "--shadow") / "bundle"
    recording = read_recording(bundle / "recording.jsonl")
    invocations = read_invocations(bundle / INVOCATIONS)
    calls = [i for i in invocations if i["method"] == "tools/call"]
    assert len(calls) == 5

    # The relay stopped before answering the last call: its answer is not in the recording and
    # its record is the "unfinished" one read_records makes from the invocation_open line.
    last_call = max(
        (
            r
            for r in recording.records
            if r["type"] == "message" and r["dir"] == "client_to_relay"
            and r.get("method") == "tools/call"
        ),
        key=lambda r: r["seq"],
    )  # fmt: skip
    trimmed = type(recording)(
        header=recording.header,
        records=tuple(
            r
            for r in recording.records
            if not (
                r["type"] == "message"
                and r["dir"] == "relay_to_client"
                and r.get("id") == last_call["id"]
            )
        ),  # fmt: skip
        end=recording.end,
    )
    unfinished = [
        *invocations[:-1],
        project_invocation({**invocations[-1], "resolution": "unfinished", "ms": None}),
    ]  # noqa: E501
    report = compute_report(trimmed, unfinished)
    d = report["denominators"]
    assert d["client_tool_calls"] == d["tool_call_records"] == 5  # never dropped
    assert d["unfinished"] == 1 and d["checks_hold"] is True
    assert report["calls"]["unknown"]["k"] == 1  # unfinished is unresolved: a residual failure
    assert report["calls"]["failed"]["k"] == 1 and report["calls"]["succeeded"]["k"] == 4

    # A call the records do not account for is a broken denominator, reported, never hidden.
    missing = compute_report(recording, invocations[:-1])
    assert missing["denominators"]["checks"]["tool_calls_equal_records"] is False
    assert missing["denominators"]["checks_hold"] is False


def test_percentiles_are_nearest_rank() -> None:
    assert percentiles([]) == {"n": 0, "p50": None, "p90": None, "p95": None, "p99": None,
                               "max": None}  # fmt: skip
    out = percentiles([float(v) for v in range(1, 101)])
    assert (out["p50"], out["p90"], out["p95"], out["p99"], out["max"]) == (50, 90, 95, 99, 100)


def test_the_projection_replaces_anything_not_identifier_shaped() -> None:
    item = project_invocation(
        {
            "id": "user@example.com", "session": "abc", "inv": "deadbeef",
            "method": "tools/call", "tool": "write note", "attempts": 2,
            "faults": ["deadline", "a b"], "resolution": "recovered",
            "server_outcome": "executed", "ms": 12, "text": "secret",
        }
    )  # fmt: skip
    assert item == {
        "method": "tools/call", "tool": "[redacted]", "attempts": 2,
        "faults": ["deadline", "[redacted]"], "resolution": "recovered",
        "server_outcome": "executed", "ms": 12,
    }  # fmt: skip
    assert os.sep  # (keeps the import list honest on every platform)


# -- S2.2: duplicates from the key store -----------------------------------------------------


def test_the_key_store_counts_an_identical_call_executed_under_two_keys(tmp_path: Path) -> None:
    from stateguard.adapters.mcp.idempotency import IdempotencyStore, fingerprint
    from stateguard.capture import key_store_summary

    store = IdempotencyStore(tmp_path / "keys.db", scope="s")
    one = fingerprint("write", {"text": "a"})
    for key, fp in (("k1", one), ("k2", one), ("k3", fingerprint("write", {"text": "b"}))):
        store.begin(key, fp, tool="write")
        store.complete(key, {"content": []})
    store.begin("k4", fingerprint("write", {"text": "c"}), tool="write")  # started, not done
    summary = key_store_summary(tmp_path / "keys.db", scope="s")
    assert summary["keys"] == 4 and summary["executions"] == 3
    assert summary["max_executions_per_key"] == 1
    assert summary["identical_calls_executed_more_than_once"] == 1  # the deliberate duplicate
    assert summary["extra_executions"] == 1
    assert summary["by_state"] == {"done": 3, "started": 1}
    assert summary["by_tool"] == {"write": {"executions": 3, "repeated_calls": 1, "extra": 1}}
    assert key_store_summary(tmp_path / "keys.db", scope="other")["keys"] == 0
    store.complete("k4", {"content": []})
    store.close()


MEMORY_JS = Path(
    os.environ.get(
        "MEMORY_JS",
        "/opt/sg-servers/memory/node_modules/@modelcontextprotocol/server-memory/dist/index.js",
    )
)


def _memory_installed() -> bool:
    import shutil

    try:
        return MEMORY_JS.is_file() and shutil.which("node") is not None
    except OSError:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _memory_installed(), reason="needs node and the pinned server-memory")
def test_a_deliberately_duplicated_keyed_call_is_detected_from_the_key_store(
    tmp_path: Path,
) -> None:
    """Three calls once each, and one sent twice (two requests: two keys, two executions)."""
    import shutil

    memory = tmp_path / "memory.jsonl"
    memory.write_text(
        json.dumps({"type": "entity", "name": "seed", "entityType": "t", "observations": []}) + "\n"
    )
    effects = tmp_path / "effects.json"
    effects.write_text(
        json.dumps({"tools": [{"name": "add_observations", "effect": "non_idempotent"}]})
    )
    directory = tmp_path / "cap"
    command = [
        *CLI, "capture", "stdio", "--dir", str(directory), "--label", "test fixture (scripted)",
        "--retry", "auto", "--effects", str(effects), "--keys", "auto",
        "--keys-db", str(tmp_path / "keys.db"),
        "--", shutil.which("node") or "node", str(MEMORY_JS),
    ]  # fmt: skip
    env = {**os.environ, "MEMORY_FILE_PATH": str(memory)}
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    assert process.stdin is not None and process.stdout is not None

    def request(ident: int, method: str, params: dict[str, Any]) -> Any:
        assert process.stdin is not None and process.stdout is not None
        line = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        process.stdin.write(json.dumps(line).encode() + b"\n")
        process.stdin.flush()
        while True:
            answer = json.loads(process.stdout.readline())
            if answer.get("id") == ident:
                return answer

    request(0, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "t", "version": "0"}})  # fmt: skip
    process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    process.stdin.flush()
    request(1, "tools/list", {})
    markers = ["m-one", "m-two", "m-three", "m-dup", "m-dup"]  # the last call sent twice
    outcomes = []
    for i, marker in enumerate(markers):
        arguments = {"observations": [{"entityName": "seed", "contents": [marker]}]}
        answer = request(10 + i, "tools/call", {"name": "add_observations", "arguments": arguments})
        assert "result" in answer, answer
        outcomes.append(
            (answer["result"].get("_meta") or {}).get("io.stateguard/idempotencyOutcome")
        )
    process.stdin.close()
    process.wait(timeout=60)
    assert process.returncode == 0, process.stderr.read() if process.stderr else ""

    loaded = load_bundle(directory / "bundle")
    assert loaded["rebuilt_equal"] is True
    store = loaded["report"]["key_store"]
    assert store["executions"] == 5 and store["max_executions_per_key"] == 1
    assert store["identical_calls_executed_more_than_once"] == 1
    assert store["extra_executions"] == 1
    # The ground truth agrees: the keyed server says it executed every one of the five calls
    # (two keys for the duplicated call, so neither is a replay). server-memory skips an
    # observation it already holds, so this duplicate execution left the file unchanged: the
    # key store counts executions, the conservative signal, not their effect.
    assert outcomes == ["executed"] * 5
    assert memory.read_text().count(json.dumps("m-dup")) == 1
    # Counts only: no key, fingerprint or result in the bundle.
    text = (directory / "bundle" / "keystore.json").read_text()
    assert "m-dup" not in text and "fingerprint" not in text
