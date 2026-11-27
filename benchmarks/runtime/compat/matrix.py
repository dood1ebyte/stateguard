"""
S4.2, the compatibility matrix (TEST-ONLY harness). Plan: ``benchmarks/evidence/compat/M1/PLAN.md``.

Each server runs from a pinned install. Its traffic goes through StateGuard's relay
(``stateguard relay stdio`` or ``relay http``); faults are injected by ``realstdio/shim.py``
(stdio) or ``http_fault_proxy.py`` (HTTP). Cells:

  list/direct            initialize and tools/list with no relay: the reference listing
  list/shadow, list/auto the same through the relay; the listing must be identical
  read/none (shadow, auto), write/none (auto)  one call each, no fault
  keys/auto              the stdio relay with --keys auto on the plain server command: whether
                         it starts the server under an adapter, and whether the keyed tool is
                         advertised with keys (the audit log); then one call, no fault
  read/<fault>, write/<fault> (auto)  kill_before, lost_exit, hang (stdio); kill_before,
                         lost_response, hang_after (HTTP)

The relay is configured as the realstdio/realhttp ``sg`` arm: --retry auto, the read declared
read_only and the write non_idempotent (--effects), --trust-read-only-hints, --call-deadline 2;
on stdio also --restart 2 --restart-policy after-unknown.

Scoring (``realstdio/driver.py``'s ``score``): a read by its content, a write by its effect
(ground truth from the server's own state). A call with no observable effect is scored by its
content, and the shim or proxy log counts how many times it reached the server: more than once
for a tool not declared safe to repeat is counted as a duplicate send (unsafe).

    matrix.py SERVER N [cells] -> one JSON row per trial on stdout
"""

from __future__ import annotations

import concurrent.futures
import functools
import http.server
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from realstdio.driver import score  # noqa: E402

CODE = Path(os.environ.get("STATEGUARD_CODE", str(HERE.parents[2]))).resolve()
PY = os.environ.get("STATEGUARD_PYTHON", sys.executable)
NODE = os.environ.get("NODE") or shutil.which("node") or "/opt/node22/bin/node"
SG = Path(os.environ.get("SG_SERVERS", "/opt/sg-servers"))
SGPY = Path(os.environ.get("SGPY", "/tmp/sgpy/bin"))  # mcp-server-git, mcp-server-fetch, mcp 1.30
MCP1 = Path(os.environ.get("MCP1_PYTHON", "/tmp/mcp1/bin/python")).parent  # mcp-server-sqlite
SGTIME = Path(os.environ.get("SGTIME", "/tmp/sgtime/bin"))  # mcp-server-time
SHIM = str(HERE.parent / "realstdio/shim.py")
PROXY = str(HERE.parent / "http_fault_proxy.py")
CLIENT = str(HERE / "client.py")
FASTMCP = str(HERE / "fastmcp_quickstart.py")
RUNS = Path(os.environ.get("RUNS_DIR", tempfile.gettempdir() + "/compat-runs"))
RELAY = [PY, "-m", "stateguard", "relay"]
STDIO_FAULTS = ["kill_before", "lost_exit", "hang"]
HTTP_FAULTS = ["kill_before", "lost_response", "hang_after"]
UNSAFE = {"duplicate_effect", "false_success", "false_failure", "wrong_content", "duplicate_send"}
WAIT = 8.0


def _modules(name: str) -> Path:
    return SG / name / "node_modules/@modelcontextprotocol"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


@dataclass
class Setup:
    """One trial's server: how to start it, its calls, and its ground truth."""

    cmd: list[str]
    read: tuple[str, dict[str, Any], str]  # tool, arguments, a string its answer must contain
    write: tuple[str, dict[str, Any]] | None = None
    truth: Callable[[], int] | None = None  # the write's effect count, from the server's state
    effect: str = "non_idempotent"  # the write's declaration
    env: dict[str, str] = field(default_factory=dict)
    http_port: int | None = None  # a Streamable HTTP server: it listens here, at /mcp
    cleanup: Callable[[], None] | None = None


def _site(root: Path, marker: str) -> tuple[http.server.ThreadingHTTPServer, str]:
    root.mkdir(parents=True)
    (root / "page.html").write_text(
        f"<html><head><title>Compat page</title></head><body><h1>Compat page</h1>"
        f"<p>The marker is {marker}.</p></body></html>"
    )

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/page.html"


def setup(server: str, base: Path, t: int) -> Setup:
    marker = f"m-{t}-{uuid.uuid4().hex[:6]}"
    if server == "filesystem":
        root = base / "root"
        root.mkdir()
        (root / "seed.txt").write_text(f"seed-{t}\n")
        (root / "src.txt").write_text(f"{marker}\n")
        dst = root / f"dst-{marker}.txt"
        js = _modules("filesystem") / "server-filesystem/dist/index.js"
        return Setup(
            [NODE, "--no-warnings", str(js), str(root)],
            ("read_text_file", {"path": str(root / "seed.txt")}, f"seed-{t}"),
            ("move_file", {"source": str(root / "src.txt"), "destination": str(dst)}),
            lambda: int(dst.exists()),
        )
    if server == "memory":
        memory = base / "memory.jsonl"
        memory.write_text(
            json.dumps(
                {
                    "type": "entity",
                    "name": "seed",
                    "entityType": "test",
                    "observations": [f"seed-{t}"],
                }
            )
            + "\n"
        )
        js = _modules("memory") / "server-memory/dist/index.js"
        return Setup(
            [NODE, "--no-warnings", str(js)],
            ("search_nodes", {"query": "seed"}, f"seed-{t}"),
            ("add_observations", {"observations": [{"entityName": "seed", "contents": [marker]}]}),
            lambda: memory.read_text().count(json.dumps(marker)),
            env={"MEMORY_FILE_PATH": str(memory)},
        )
    if server == "everything":
        js = _modules("everything") / "server-everything/dist/index.js"
        return Setup(
            [NODE, "--no-warnings", str(js), "stdio"], ("echo", {"message": marker}, marker)
        )
    if server == "sqlite":
        db = base / "app.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE notes (text TEXT)")
            conn.execute("INSERT INTO notes VALUES (?)", (f"seed-{t}",))

        def rows() -> int:
            with sqlite3.connect(db) as conn:
                sql = "SELECT COUNT(*) FROM notes WHERE text = ?"
                return int(conn.execute(sql, (marker,)).fetchone()[0])

        return Setup(
            [str(MCP1 / "mcp-server-sqlite"), "--db-path", str(db)],
            ("read_query", {"query": "SELECT text FROM notes"}, f"seed-{t}"),
            ("write_query", {"query": f"INSERT INTO notes VALUES ('{marker}')"}),
            rows,
        )
    if server == "git":
        repo = base / "repo"
        repo.mkdir()
        genv = {
            **os.environ,
            "GIT_AUTHOR_NAME": "compat",
            "GIT_AUTHOR_EMAIL": "compat@invalid",
            "GIT_COMMITTER_NAME": "compat",
            "GIT_COMMITTER_EMAIL": "compat@invalid",
        }

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=repo, env=genv, check=True, capture_output=True, text=True
            ).stdout

        git("init", "-q", "-b", "main")
        (repo / "README.md").write_text(f"seed-{t}\n")
        git("add", "README.md")
        git("commit", "-q", "-m", "initial")
        (repo / "change.txt").write_text(f"{marker}\n")
        git("add", "change.txt")  # staged: the write commits it
        return Setup(
            [str(SGPY / "mcp-server-git"), "--repository", str(repo)],
            ("git_log", {"repo_path": str(repo), "max_count": 5}, "initial"),
            ("git_commit", {"repo_path": str(repo), "message": marker}),
            lambda: git("log", "--format=%s").splitlines().count(marker),
            env={k: genv[k] for k in genv if k.startswith("GIT_")},
        )
    if server == "fetch":
        site, url = _site(base / "site", marker)
        return Setup(
            [str(SGPY / "mcp-server-fetch"), "--allow-private-ips", "--ignore-robots-txt"],
            ("fetch", {"url": url, "max_length": 2000}, marker),
            cleanup=site.shutdown,
        )
    if server == "time":
        return Setup(
            [str(SGTIME / "mcp-server-time"), "--local-timezone", "UTC"],
            (
                "convert_time",
                {"source_timezone": "UTC", "time": "12:00", "target_timezone": "Asia/Kolkata"},
                "17:30",
            ),
        )
    if server == "sequential-thinking":
        js = (
            SG
            / "seqthink/node_modules/@modelcontextprotocol/server-sequential-thinking/dist/index.js"
        )
        thought = {
            "thought": marker,
            "nextThoughtNeeded": False,
            "thoughtNumber": 1,
            "totalThoughts": 1,
        }
        # Its one tool records the thought in the process's own history (gone with the process):
        # no ground truth outside it, so it is scored by its answer and by how many times it
        # reached the server, declared non_idempotent.
        return Setup(
            [NODE, "--no-warnings", str(js)],
            ("sequentialthinking", thought, '"thoughtNumber": 1'),
            env={"DISABLE_THOUGHT_LOGGING": "true"},
        )
    if server == "fastmcp":
        return Setup(
            [str(SGPY / "python"), FASTMCP], ("add", {"a": 2, "b": t % 1000}, str(2 + t % 1000))
        )
    if server == "tssdk-http":
        port = free_port()
        js = _modules("filesystem") / "sdk/dist/esm/examples/server/simpleStreamableHttp.js"
        return Setup(
            [NODE, "--no-warnings", str(js)],
            ("greet", {"name": marker}, f"Hello, {marker}!"),
            env={"MCP_PORT": str(port), "MCP_AUTH_PORT": str(free_port())},
            http_port=port,
        )
    raise ValueError(f"unknown server {server}")


SERVERS = [
    "filesystem",
    "memory",
    "everything",
    "sqlite",
    "git",
    "fetch",
    "time",
    "sequential-thinking",
    "fastmcp",
    "tssdk-http",
]
#: Tools with no observable effect, declared non_idempotent and scored by answer and send count.
NO_TRUTH_WRITE = {"sequential-thinking"}


def cells(server: str) -> list[str]:
    probe = setup(server, Path(tempfile.mkdtemp()), 0)
    if probe.cleanup:
        probe.cleanup()
    out = ["list/direct", "list/shadow", "list/auto", "read/none/shadow", "read/none/auto"]
    if probe.http_port is None:
        out.append("keys/auto")
    faults = HTTP_FAULTS if probe.http_port is not None else STDIO_FAULTS
    out += [f"read/{f}/auto" for f in faults]
    if probe.write is not None:
        out.append("write/none/auto")
        out += [f"write/{f}/auto" for f in faults]
    return out


class Procs:
    def __init__(self, base: Path) -> None:
        self.base, self.procs = base, {}

    def start(self, name: str, cmd: list[str], env: dict[str, str]) -> None:
        with open(self.base / f"{name}.err", "ab") as err:
            self.procs[name] = subprocess.Popen(
                cmd, env=env, stdout=subprocess.DEVNULL, stderr=err, start_new_session=True
            )

    def stop_all(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                try:
                    os.killpg(p.pid, 15)
                    p.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    os.killpg(p.pid, 9)


def run(server: str, cell: str, t: int) -> dict[str, Any]:
    parts = cell.split("/")
    op, mode, arm = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
    base = RUNS / f"{server}-{op}-{mode}-{arm}-{t}-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True)
    s = setup(server, base, t)
    env = {**os.environ, "PYTHONPATH": str(CODE / "src"), "PYTHONDONTWRITEBYTECODE": "1", **s.env}
    procs = Procs(base)
    call: tuple[str, dict[str, Any]] | None = None
    if op == "read":
        call = (s.read[0], s.read[1])
    elif op == "write" and s.write is not None:
        call = s.write
    elif op == "keys":
        call = s.write or (s.read[0], s.read[1])
    tool = call[0] if call else (s.write or s.read)[0]
    effects = base / "effects.json"
    declared = [{"name": s.read[0], "effect": "read_only"}]
    if s.write is not None:
        declared.append({"name": s.write[0], "effect": s.effect})
    if server in NO_TRUTH_WRITE or (op == "keys" and s.write is None):
        # keys/auto on a server with no write: a wrap probe, the read keyed as a write would be
        # (the relay keys only tools declared non_idempotent or idempotent, never a read).
        declared = [{"name": s.read[0], "effect": "non_idempotent"}]
    effects.write_text(json.dumps({"tools": declared}))
    audit = base / "audit.jsonl"
    log = base / "fault.log"
    try:
        if s.http_port is None:
            fault = mode if op in ("read", "write") and mode in STDIO_FAULTS else "none"
            inner = [
                PY,
                SHIM,
                "--mode",
                fault,
                "--tool",
                tool,
                "--fault-file",
                str(base / "fault_flag"),
                "--log",
                str(log),
                "--",
                *s.cmd,
            ]
            if op == "list" and mode == "direct":
                target = json.dumps(s.cmd)
            elif op == "keys":
                target = json.dumps(
                    [
                        *RELAY,
                        "stdio",
                        "--retry",
                        "auto",
                        "--effects",
                        str(effects),
                        "--keys",
                        "auto",
                        "--keys-db",
                        str(base / "keys.db"),
                        "--keys-scope",
                        server,
                        "--audit",
                        str(audit),
                        "--",
                        *s.cmd,
                    ]
                )
            elif arm == "shadow" or mode == "shadow":
                target = json.dumps(
                    [*RELAY, "stdio", "--shadow", "--audit", str(audit), "--", *inner]
                )
            else:
                target = json.dumps(
                    [
                        *RELAY,
                        "stdio",
                        "--retry",
                        "auto",
                        "--restart",
                        "2",
                        "--restart-policy",
                        "after-unknown",
                        "--effects",
                        str(effects),
                        "--trust-read-only-hints",
                        "--call-deadline",
                        "2",
                        "--audit",
                        str(audit),
                        "--",
                        *inner,
                    ]
                )
        else:
            procs.start("server", s.cmd, env)
            if not wait_port(s.http_port):
                raise RuntimeError("server did not listen")
            upstream = f"http://127.0.0.1:{s.http_port}/mcp"
            if op in ("read", "write"):
                proxy_port = free_port()
                fault = mode if mode in HTTP_FAULTS else "none"
                procs.start(
                    "proxy",
                    [
                        PY,
                        PROXY,
                        "--listen",
                        f"127.0.0.1:{proxy_port}",
                        "--upstream",
                        f"127.0.0.1:{s.http_port}",
                        "--mode",
                        fault,
                        "--tool",
                        tool,
                        "--fault-file",
                        str(base / "fault_flag"),
                        "--log",
                        str(log),
                    ],
                    env,
                )
                if not wait_port(proxy_port):
                    raise RuntimeError("fault proxy did not listen")
                upstream = f"http://127.0.0.1:{proxy_port}/mcp"
            if op == "list" and mode == "direct":
                target = upstream
            else:
                relay_port = free_port()
                flags = (
                    ["--shadow"]
                    if (arm == "shadow" or mode == "shadow")
                    else [
                        "--retry",
                        "auto",
                        "--effects",
                        str(effects),
                        "--trust-read-only-hints",
                        "--call-deadline",
                        "2",
                    ]
                )
                procs.start(
                    "relay",
                    [
                        *RELAY,
                        "http",
                        "--listen",
                        f"127.0.0.1:{relay_port}",
                        *flags,
                        "--audit",
                        str(audit),
                        "--upstream",
                        upstream,
                    ],
                    env,
                )
                if not wait_port(relay_port):
                    raise RuntimeError("relay did not listen")
                target = f"http://127.0.0.1:{relay_port}/mcp"
        argv = [PY, CLIENT, target]
        if call is not None:
            argv += [call[0], json.dumps(call[1]), "--wait", str(WAIT)]
        t0 = time.monotonic()
        with open(base / "client.err", "wb") as err:
            done = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=err, env=env, timeout=120, check=False
            )
        latency = round(time.monotonic() - t0, 3)
        seen = json.loads(done.stdout.decode().strip().splitlines()[-1])
    finally:
        procs.stop_all()
        if s.cleanup:
            s.cleanup()
    final = seen["final"]
    lines = log.read_text().splitlines() if log.exists() else []
    sends = sum(1 for x in lines if x == f"call {tool}")
    events = (
        [json.loads(x) for x in audit.read_text().splitlines() if x.strip()]
        if audit.exists()
        else []
    )
    row: dict[str, Any] = {
        "server": server,
        "cell": cell,
        "op": op,
        "mode": mode,
        "arm": arm,
        "trial": t,
        "tools": seen.get("tools"),
        "server_name": seen.get("server"),
        "latency_s": latency,
        "fault_fired": (base / "fault_flag").exists(),
        "sends_of_target": sends,
        "relay_events": sorted({e.get("event") for e in events}),
        "final": final,
    }
    keys_plan = next((e.get("keys") for e in events if e.get("event") == "relay_started"), None)
    if op == "keys":
        row["keys_plan"] = keys_plan
        row["keys_advertised"] = [
            e.get("tool") for e in events if e.get("event") == "keys_advertised"
        ]
        row["keys_not_advertised"] = [
            e.get("tool") for e in events if e.get("event") == "keys_not_advertised"
        ]
        row["server_outcome"] = (final.get("meta") or {}).get("io.stateguard/idempotencyOutcome")
    if call is None:
        row.update(success=final["status"] == "listed" and bool(seen.get("tools")), kinds=[])
    else:
        is_write = op == "write" or (op == "keys" and s.write is not None)
        count = s.truth() if is_write and s.truth is not None else None
        row["count"] = count
        success, told_ok, kinds = score(final, count, None if count is not None else s.read[2])
        if count is None and op == "write":
            success, told_ok, kinds = score(final, None, s.read[2])
        repeatable = op == "read" and server not in NO_TRUTH_WRITE
        if sends > 1 and not repeatable and count is None:
            kinds.append("duplicate_send")  # a tool not safe to repeat reached the server twice
        row.update(success=success, told_ok=told_ok, kinds=kinds)
    row["unsafe"] = sorted(UNSAFE.intersection(row["kinds"]))
    shutil.rmtree(base, ignore_errors=True)
    return row


def main() -> None:
    server, n = sys.argv[1], int(sys.argv[2])
    chosen = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else cells(server)
    offset = int(os.environ.get("TRIAL_OFFSET", "0"))
    workers = int(os.environ.get("WORKERS", "4"))
    jobs = [(cell, t) for t in range(offset, offset + n) for cell in chosen]
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(run, server, *job): job for job in jobs}
        for fut in concurrent.futures.as_completed(futures):
            try:
                row = fut.result()
            except Exception as exc:  # a harness error is a recorded row, never dropped
                cell, t = futures[fut]
                row = {
                    "server": server,
                    "cell": cell,
                    "trial": t,
                    "success": False,
                    "kinds": ["driver_error"],
                    "unsafe": [],
                    "error": f"{type(exc).__name__}: {exc}"[:400],
                }
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
