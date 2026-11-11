"""
The TypeScript server adapter (``adapters/typescript``) against the Python
reference: the same wire contract, the same store file and lock, the same
fingerprints (with one documented divergence), and the real relay in front
of the TypeScript reference server and of the pinned, unmodified third-party
TypeScript servers with the adapter injected at process start.

Skipped when Node 22 or the adapter's installed dependencies are absent.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from stateguard.adapters.mcp.idempotency import (
    IDEMPOTENCY_CAPABILITY,
    IDEMPOTENCY_KEY,
    IDEMPOTENCY_OUTCOME,
    NOT_EXECUTED_OUTCOMES,
    VERSION,
    BeginStatus,
    IdempotencyStore,
    Outcome,
    StoreUnavailable,
    error_code,
    error_message,
    fingerprint,
)

ROOT = Path(__file__).resolve().parents[2]
TS = ROOT / "adapters" / "typescript"
NODE = shutil.which("node") or "/opt/node22/bin/node"
PROBE = TS / "test" / "fixtures" / "contract_probe.ts"
TS_SERVER = TS / "examples" / "keyed_reference_server.ts"
PRELOAD = TS / "src" / "preload.ts"
RELAY = ROOT / "examples" / "mcp" / "stdio_proxy.py"


def _pinned(env: str, *candidates: str) -> Path:
    """A pinned third-party server: $env, else the first install location that exists."""
    if os.environ.get(env):
        return Path(os.environ[env])
    paths = [Path(c) for c in candidates]
    return next((p for p in paths if p.exists()), paths[0])


FS_SERVER = _pinned(
    "STATEGUARD_FS_SERVER",
    "/opt/sg-servers/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js",
    "/root/stateguard-captures/domain2-fs/server/node_modules/@modelcontextprotocol/"
    "server-filesystem/dist/index.js",
)
MEM_SERVER = _pinned(
    "STATEGUARD_MEM_SERVER",
    "/opt/sg-servers/memory/node_modules/@modelcontextprotocol/server-memory/dist/index.js",
    "/root/stateguard-captures/domain2-candidates/mem/node_modules/@modelcontextprotocol/"
    "server-memory/dist/index.js",
)
INIT = {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "t", "version": "0"},
}


def _node_ready() -> bool:
    if not Path(NODE).exists() or not (TS / "node_modules" / "@modelcontextprotocol").exists():
        return False
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True, check=False)
    major, minor = (int(part) for part in out.stdout.strip().lstrip("v").split(".")[:2])
    return (major, minor) >= (22, 18)  # type stripping without a flag


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _node_ready(), reason="needs Node >= 22.18 and adapters/typescript deps"
    ),
]


def _node(
    *args: str, stdin: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [NODE, "--no-warnings", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=TS,
        check=False,
    )


# -- the adapter's own gates ---------------------------------------------------------------


def test_the_typescript_suite_passes() -> None:
    out = _node("--test", "--test-concurrency=1", "test/*.test.ts", timeout=900)
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-2000:]
    assert "# fail 0" in out.stdout


def test_the_typescript_adapter_type_checks_strictly() -> None:
    tsc = TS / "node_modules" / "typescript" / "bin" / "tsc"
    out = _node(str(tsc), "-p", "tsconfig.json", "--noEmit", timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr


# -- the wire contract and fingerprints ---------------------------------------------------------


def test_the_wire_contract_is_the_same_in_both_languages() -> None:
    ts = json.loads(_node(str(PROBE), "contract").stdout)
    assert (ts["capability"], ts["key"], ts["outcome"], ts["version"]) == (
        IDEMPOTENCY_CAPABILITY,
        IDEMPOTENCY_KEY,
        IDEMPOTENCY_OUTCOME,
        VERSION,
    )
    assert set(ts["outcomes"]) == {o.value for o in Outcome}
    assert ts["errors"] == {
        o.value: {"code": error_code(o), "message": error_message(o)} for o in NOT_EXECUTED_OUTCOMES
    }


def _corpus() -> list[Any]:
    import random

    rng = random.Random(11)
    floats = [0.1, 1.5, -2.25, 1e-4, 9.99e-5, 1e-5, 1.234e-7, 6.02214076e23, 3.14159265358979]
    floats += [rng.uniform(-1e6, 1e6) for _ in range(30)]
    floats += [rng.uniform(-1, 1) * 10 ** rng.randint(-12, 25) for _ in range(40)]
    strings = [
        "plain",
        "",
        'q"b\\s',
        "nl\ncr\rt\tb\bf\f",
        "ctl\x01\x1f",
        "del\x7f",
        "café",
        "日本語",
    ]
    strings += ["emoji 😀", " ", "z\x00"]
    corpus: list[Any] = [{"x": f} for f in floats]
    corpus += [{"s": s, s or "k": 1} for s in strings]
    corpus += [
        {"b": True, "a": None, "c": [1, -2, 0, 2**53 - 1, -(2**53 - 1)]},
        {"z": {"y": [{"b": 2, "a": 1}]}},
        {"é": 1, "e": 2, "😀": 3, "￿": 4, "Z": 5},
        {},
        [],
        None,
        {"n": -0.0},
    ]
    return corpus


def _integral_float(value: Any) -> bool:
    """Python holds an integral value as a float: JavaScript cannot tell it from an int."""
    if isinstance(value, float):
        return value == int(value) if value == value and abs(value) != float("inf") else False
    if isinstance(value, dict):
        return any(_integral_float(v) for v in value.values())
    if isinstance(value, list):
        return any(_integral_float(v) for v in value)
    return False


def test_fingerprints_agree_except_for_the_documented_integral_float_case() -> None:
    corpus = _corpus()
    rows = "\n".join(json.dumps({"tool": "pay", "args": args}) for args in corpus)
    out = _node(str(PROBE), "fingerprints", stdin=rows)
    ts = [json.loads(line) for line in out.stdout.splitlines()]
    assert len(ts) == len(corpus)
    diverged = [
        args
        for args, t in zip(corpus, ts, strict=True)
        if t["fingerprint"] != fingerprint("pay", args)
    ]
    # Every divergence is the documented one (an integral value Python holds as a
    # float, which JavaScript cannot tell from an int) ...
    assert diverged and all(_integral_float(args) for args in diverged), diverged
    # ... and every other value -- strings, key order, ints, non-integral floats -- matches.
    for args, t in zip(corpus, ts, strict=True):
        if not _integral_float(args):
            assert t["canonical"] == json.dumps(
                ["pay", args], sort_keys=True, separators=(",", ":")
            ), args
    assert len(corpus) - len(diverged) >= 90


# -- one store file, one lock, two languages ---------------------------------------------------


def test_a_key_recorded_in_typescript_replays_in_python_and_back(tmp_path: Path) -> None:
    db = str(tmp_path / "shared.db")
    args = {"amount": 5, "to": "acme", "note": "café"}
    recorded = json.loads(
        _node(str(PROBE), "store", db, "shared", "begin-complete", "k-ts", json.dumps(args)).stdout
    )
    assert recorded["outcome"] == "executed"
    py = IdempotencyStore(db, scope="shared")
    replay = py.begin("k-ts", fingerprint("pay", args), tool="pay")
    assert replay.status is BeginStatus.REPLAY and replay.result == {"receipt": "ts-k-ts"}
    assert py.begin("k-py", fingerprint("pay", args), tool="pay").status is BeginStatus.EXECUTE
    py.complete("k-py", {"receipt": "py"})
    back = json.loads(
        _node(str(PROBE), "store", db, "shared", "replay", "k-py", json.dumps(args)).stdout
    )
    assert back["outcome"] == "replayed" and back["result"] == {"receipt": "py"}
    other = {"amount": 6, "to": "acme", "note": "café"}
    mismatch = json.loads(
        _node(str(PROBE), "store", db, "shared", "replay", "k-py", json.dumps(other)).stdout
    )
    assert mismatch["outcome"] == "rejected_mismatch"


def test_the_lock_path_is_the_same_file(tmp_path: Path) -> None:
    db = str(tmp_path / "shared.db")
    ts = json.loads(_node(str(PROBE), "store", db, "scope é", "state", "k").stdout)
    assert ts["lockPath"] == IdempotencyStore(db, scope="scope é").lock_path


def test_the_exclusive_lock_excludes_across_languages(tmp_path: Path) -> None:
    db = str(tmp_path / "shared.db")
    held = IdempotencyStore(db, scope="s", exclusive=True)
    refused = _node(str(PROBE), "store", db, "s", "hold-exclusive", "1")
    assert refused.returncode != 0 and "exclusively" in refused.stderr
    held.close()
    holder = subprocess.Popen(
        [NODE, "--no-warnings", str(PROBE), "store", db, "s", "hold-exclusive", "30"],
        stdout=subprocess.PIPE,
        text=True,
        cwd=TS,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(StoreUnavailable, match="exclusively"):
            IdempotencyStore(db, scope="s", exclusive=True, timeout_seconds=0.3)
        holder.send_signal(signal.SIGKILL)  # the holder dies: the kernel drops its lock
        holder.wait(timeout=10)
        IdempotencyStore(db, scope="s", exclusive=True, timeout_seconds=2).close()
    finally:
        if holder.poll() is None:
            holder.kill()


# -- the TypeScript reference server, from Python ---------------------------------------------


class Client:
    """A raw JSON-RPC line client (no SDK): every message it sees is kept."""

    def __init__(self, argv: list[str], env: dict[str, str] | None = None) -> None:
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
        )
        self.seen: list[Any] = []
        self._q: queue.Queue[Any] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            self.seen.append(item)
        self.seen.append(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def wait(self, identifier: Any, timeout: float = 30.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in list(self.seen):
                if item is None:
                    return None
                if isinstance(item, dict) and item.get("id") == identifier and "method" not in item:
                    return item
            time.sleep(0.02)
        return None

    def start(self) -> Client:
        self.send({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": INIT})
        assert self.wait("init") is not None
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, identifier: Any, name: str, args: dict[str, Any], key: str | None = None) -> Any:
        params: dict[str, Any] = {"name": name, "arguments": args}
        if key:
            params["_meta"] = {IDEMPOTENCY_KEY: key}
        self.send({"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": params})
        return self.wait(identifier)

    def close(self) -> None:
        assert self.proc.stdin is not None
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def _ts_server(tmp: Path, *extra: str) -> list[str]:
    return [
        NODE,
        "--no-warnings",
        str(TS_SERVER),
        "--db",
        str(tmp / "orders.db"),
        "--ledger",
        str(tmp / "ledger.txt"),
        "--outbox",
        str(tmp / "outbox.txt"),
        "--fault-file",
        str(tmp / "fault.fired"),
        *extra,
    ]


def _outcome(answer: Any) -> Any:
    if not isinstance(answer, dict):
        return None
    if "result" in answer:
        return (answer["result"].get("_meta") or {}).get(IDEMPOTENCY_OUTCOME)
    return ((answer.get("error") or {}).get("data") or {}).get(IDEMPOTENCY_OUTCOME)


def _unknown(answer: Any) -> bool:
    data = (answer or {}).get("error", {}).get("data", {}) if isinstance(answer, dict) else {}
    return data.get("io.stateguard/executionState") == "unknown"


def _ledger(tmp: Path) -> list[str]:
    path = tmp / "ledger.txt"
    return [ln.split("\t")[0] for ln in path.read_text().split("\n")[:-1]] if path.exists() else []


def test_a_python_operator_releases_a_typescript_key_in_doubt(tmp_path: Path) -> None:
    first = Client(_ts_server(tmp_path, "--fault", "after_begin")).start()
    assert first.call(1, "send_notice", {"to": "ops"}, key="k-op") is None  # died after claiming
    first.close()
    second = Client(_ts_server(tmp_path)).start()
    try:
        assert _outcome(second.call(2, "send_notice", {"to": "ops"}, key="k-op")) == "in_doubt"
        assert not (tmp_path / "outbox.txt").exists()  # the operator checks: nothing was sent
        assert IdempotencyStore(tmp_path / "orders.db", scope="keyed-reference").release("k-op")
        assert _outcome(second.call(3, "send_notice", {"to": "ops"}, key="k-op")) == "executed"
    finally:
        second.close()
    assert (tmp_path / "outbox.txt").read_text() == "ops\n"


def _relay(
    tmp: Path, server: list[str], *relay_extra: str, env: dict[str, str] | None = None
) -> Client:
    argv = [
        sys.executable,
        str(RELAY),
        "--audit",
        str(tmp / "audit.jsonl"),
        "--retry",
        "auto",
        "--restart",
        "2",
        *relay_extra,
        "--",
        *server,
    ]
    return Client(argv, env=env).start()


@pytest.mark.parametrize(
    "tool, args, point, expected",
    [
        ("append_ledger", {"entry": "pay-1"}, "after_effect", "recovered"),
        ("append_ledger", {"entry": "pay-2"}, "mid_effect", "executed"),
        ("create_order", {"item": "w", "qty": 1}, "after_effect", "executed"),
        ("create_order", {"item": "w", "qty": 1}, "after_record", "replayed"),
    ],
)
def test_the_relay_recovers_the_typescript_server(
    tmp_path: Path, tool: str, args: dict[str, Any], point: str, expected: str
) -> None:
    client = _relay(tmp_path, _ts_server(tmp_path, "--fault", point))
    try:
        answer = client.call(1, tool, args)
        assert answer is not None and "result" in answer, answer
        assert _outcome(answer) == expected
    finally:
        client.close()
    assert (tmp_path / "fault.fired").exists()
    if tool == "append_ledger":
        assert _ledger(tmp_path) == [args["entry"]]


def test_the_relay_replays_a_hung_typescript_answer_after_a_deadline(tmp_path: Path) -> None:
    client = _relay(
        tmp_path,
        _ts_server(tmp_path, "--fault", "hang_after_record"),
        "--call-deadline",
        "0.5",
        "--deadline-scope",
        "recoverable",
    )
    try:
        answer = client.call(1, "append_ledger", {"entry": "hung"})
        assert _outcome(answer) == "replayed", answer
    finally:
        client.close()
    assert _ledger(tmp_path) == ["hung"]


def test_a_typescript_key_left_in_doubt_reaches_the_client_as_unknown(tmp_path: Path) -> None:
    client = _relay(tmp_path, _ts_server(tmp_path, "--fault", "after_effect"))
    try:
        answer = client.call(1, "send_notice", {"to": "ops"})
        assert _unknown(answer), answer  # sent once, then in doubt: never re-sent
    finally:
        client.close()
    assert (tmp_path / "outbox.txt").read_text() == "ops\n"


# -- the pinned, unmodified third-party servers with the adapter injected ---------------------------


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(file.relative_to(path).as_posix().encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def _preloaded(
    server: Path, *server_args: str, env: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    argv = [NODE, "--no-warnings", "--import", str(PRELOAD), str(server), *server_args]
    return argv, {**os.environ, **env}


@pytest.mark.skipif(not FS_SERVER.exists(), reason="pinned server-filesystem not installed")
def test_the_adapter_keys_the_unmodified_filesystem_server(tmp_path: Path) -> None:
    package = FS_SERVER.parents[1]
    before = _tree_hash(package)
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("order")
    argv, env = _preloaded(
        FS_SERVER,
        str(root),
        env={
            "STATEGUARD_IDEMPOTENCY_DB": str(tmp_path / "keys.db"),
            "STATEGUARD_IDEMPOTENCY_SCOPE": "filesystem",
            "STATEGUARD_KEYED_TOOLS": "move_file",
        },
    )
    client = Client(argv, env=env).start()
    try:
        client.send({"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}})
        tools = {t["name"]: t for t in client.wait("list")["result"]["tools"]}
        assert tools["move_file"]["_meta"] == {IDEMPOTENCY_CAPABILITY: {"version": 1}}
        assert IDEMPOTENCY_CAPABILITY not in (tools["read_text_file"].get("_meta") or {})
        move = {"source": str(root / "a.txt"), "destination": str(root / "b.txt")}
        first = client.call(1, "move_file", move, key="k-move")
        again = client.call(2, "move_file", move, key="k-move")
        plain = client.call(3, "move_file", move)  # unkeyed: the server's own behaviour
    finally:
        client.close()
    assert _outcome(first) == "executed" and not first["result"].get("isError")
    assert (
        _outcome(again) == "replayed" and again["result"]["content"] == first["result"]["content"]
    )
    assert plain["result"].get("isError") is True  # a real second move fails: the source is gone
    assert not (root / "a.txt").exists() and (root / "b.txt").read_text() == "order"
    assert _tree_hash(package) == before  # the server's files are untouched


@pytest.mark.skipif(not MEM_SERVER.exists(), reason="pinned server-memory not installed")
def test_the_adapter_keys_the_unmodified_memory_server(tmp_path: Path) -> None:
    package = MEM_SERVER.parents[1]
    before = _tree_hash(package)
    store = tmp_path / "mem.jsonl"
    store.write_text(
        json.dumps({"type": "entity", "name": "alice", "entityType": "person", "observations": []})
        + "\n"
    )
    argv, env = _preloaded(
        MEM_SERVER,
        env={
            "MEMORY_FILE_PATH": str(store),
            "STATEGUARD_IDEMPOTENCY_DB": str(tmp_path / "keys.db"),
            "STATEGUARD_IDEMPOTENCY_SCOPE": "memory",
            "STATEGUARD_KEYED_TOOLS": "add_observations",
        },
    )
    client = Client(argv, env=env).start()
    obs = {"observations": [{"entityName": "alice", "contents": ["obs-1"]}]}
    try:
        first = client.call(1, "add_observations", obs, key="k-obs")
        again = client.call(2, "add_observations", obs, key="k-obs")
    finally:
        client.close()
    assert _outcome(first) == "executed" and _outcome(again) == "replayed"
    assert (
        again["result"]["content"] == first["result"]["content"]
    )  # the original answer, not "added []"
    entity = json.loads(store.read_text().splitlines()[0])
    assert entity["observations"] == ["obs-1"]
    assert _tree_hash(package) == before


@pytest.mark.skipif(not MEM_SERVER.exists(), reason="pinned server-memory not installed")
@pytest.mark.parametrize(
    ("point", "declared", "expected"),
    [
        ("after_begin", True, "executed"),  # abandoned before the effect: run once, now
        ("after_effect", True, "executed"),  # abandoned after it: run again, harmlessly
        ("after_effect", False, None),  # not declared: in doubt, as before -> unknown
    ],
)
def test_an_abandoned_call_to_a_declared_idempotent_tool_runs_again(
    tmp_path: Path, point: str, declared: bool, expected: str | None
) -> None:
    """
    The memory server's add_observations drops an observation it already has: the operator can
    declare it idempotent. A crash between claiming the key and recording it then no longer
    leaves the call in doubt: the restarted server, holding the exclusive store, runs it again,
    and the observation exists once. Undeclared, the same crash stays in doubt.
    """
    store = tmp_path / "mem.jsonl"
    store.write_text(
        json.dumps({"type": "entity", "name": "alice", "entityType": "person", "observations": []})
        + "\n"
    )
    env = {
        "MEMORY_FILE_PATH": str(store),
        "STATEGUARD_IDEMPOTENCY_DB": str(tmp_path / "keys.db"),
        "STATEGUARD_IDEMPOTENCY_SCOPE": "memory",
        "STATEGUARD_KEYED_TOOLS": "add_observations",
        "STATEGUARD_IDEMPOTENCY_EXCLUSIVE": "1",
        "STATEGUARD_FAULT": point,
        "STATEGUARD_FAULT_FILE": str(tmp_path / "fault.fired"),
    }
    if declared:
        env["STATEGUARD_IDEMPOTENT_TOOLS"] = "add_observations"
    argv, full_env = _preloaded(MEM_SERVER, env=env)
    client = _relay(tmp_path, argv, env=full_env)
    try:
        answer = client.call(
            1, "add_observations", {"observations": [{"entityName": "alice", "contents": ["o1"]}]}
        )
    finally:
        client.close()
    assert (tmp_path / "fault.fired").exists()
    if expected is None:
        assert _unknown(answer), answer
    else:
        assert answer is not None and "result" in answer, answer
        assert _outcome(answer) == expected
    observations = json.loads(store.read_text().splitlines()[0])["observations"]
    assert observations == (["o1"] if expected or point == "after_effect" else [])


@pytest.mark.skipif(not MEM_SERVER.exists(), reason="pinned server-memory not installed")
def test_an_idempotent_declaration_without_an_exclusive_store_is_refused(tmp_path: Path) -> None:
    argv, env = _preloaded(
        MEM_SERVER,
        env={
            "MEMORY_FILE_PATH": str(tmp_path / "mem.jsonl"),
            "STATEGUARD_IDEMPOTENCY_DB": str(tmp_path / "keys.db"),
            "STATEGUARD_KEYED_TOOLS": "add_observations",
            "STATEGUARD_IDEMPOTENT_TOOLS": "add_observations",
        },
    )
    done = subprocess.run(argv, env=env, input=b"", capture_output=True, timeout=30, check=False)
    assert done.returncode != 0
    assert b"STATEGUARD_IDEMPOTENT_TOOLS needs STATEGUARD_IDEMPOTENCY_EXCLUSIVE=1" in done.stderr


@pytest.mark.skipif(not FS_SERVER.exists(), reason="pinned server-filesystem not installed")
def test_the_fault_only_preload_keys_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("x")
    argv, env = _preloaded(
        FS_SERVER,
        str(root),
        env={
            "STATEGUARD_FAULT": "after_effect",
            "STATEGUARD_FAULT_FILE": str(tmp_path / "fired"),
            "STATEGUARD_FAULT_TOOLS": "move_file",
        },
    )
    client = Client(argv, env=env).start()
    try:
        client.send({"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}})
        tools = client.wait("list")["result"]["tools"]
        assert all(IDEMPOTENCY_CAPABILITY not in (t.get("_meta") or {}) for t in tools)
        died = client.call(
            1, "move_file", {"source": str(root / "a.txt"), "destination": str(root / "b.txt")}
        )
    finally:
        client.close()
    assert died is None  # the process died after the move, before answering
    assert (tmp_path / "fired").exists() and (root / "b.txt").exists()
