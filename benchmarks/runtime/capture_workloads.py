"""
C1: real servers, scripted agent-like workloads, through ``stateguard capture`` (TEST-ONLY harness).
Plan: ``benchmarks/evidence/capture/C1/PLAN.md``. Label: **real server, scripted workload**.

    capture_workloads.py SERVER SESSIONS [arms] [calls] -> one JSON row per session on stdout

SERVER is git, filesystem or fetch. SESSIONS seeds 1..SESSIONS (TRIAL_OFFSET shifts them); each
seed runs once per arm (shadow, auto), on a fresh server state, so a pair is the same workload,
seed and server. Bundles go to $CAPTURE_OUT/<server>-<arm>-s<seed>/bundle (default: the runs
directory); the raw directory (unredacted audit log, key store) stays in the runs directory.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CODE = Path(os.environ.get("STATEGUARD_CODE", str(HERE.parents[1]))).resolve()
PY = os.environ.get("STATEGUARD_PYTHON", sys.executable)
NODE = os.environ.get("NODE") or shutil.which("node") or "/opt/node22/bin/node"
SGPY = Path(os.environ.get("SGPY", "/tmp/sgpy/bin"))
FS_JS = os.environ.get(
    "FS_JS",
    "/opt/sg-servers/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js",
)
RUNS = Path(os.environ.get("RUNS_DIR", tempfile.gettempdir() + "/capture-runs"))
OUT = Path(os.environ.get("CAPTURE_OUT", str(RUNS)))
LABEL = "real server, scripted workload"
ARMS = ("shadow", "auto")
CALLS = 50
MISS = 0.05  # a deliberate miss: a missing file, revision or page (a realistic tool error)
READS = {
    "git": ["git_status", "git_log", "git_diff_unstaged", "git_diff_staged", "git_show",
            "git_branch", "git_diff"],
    "filesystem": ["read_text_file", "list_directory", "get_file_info", "search_files",
                   "directory_tree", "read_multiple_files", "list_allowed_directories"],
    "fetch": ["fetch"],
}  # fmt: skip
WRITES = {
    "git_commit": "non_idempotent", "git_add": "idempotent", "git_create_branch": "non_idempotent",
    "git_checkout": "non_idempotent", "write_file": "non_idempotent",
    "edit_file": "non_idempotent", "move_file": "non_idempotent",
    "create_directory": "idempotent",
}  # fmt: skip


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:  # no request log on stderr
        pass


def _site(root: Path, rng: random.Random) -> tuple[http.server.ThreadingHTTPServer, str]:
    root.mkdir(parents=True)
    for i in range(12):
        words = " ".join(rng.choice(["alpha", "beta", "gamma", "delta", "relay", "tool"])
                         for _ in range(400))  # fmt: skip
        (root / f"page{i}.html").write_text(
            f"<html><head><title>Page {i}</title></head><body><h1>Page {i}</h1><p>{words}</p>"
            f"<a href='page{(i + 1) % 12}.html'>next</a></body></html>"
        )
    port = _free_port()
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def _git(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


class Workload:
    """The next call (tool, arguments) of a seeded agent-like session, and its side edits."""

    def __init__(self, server: str, base: Path, seed: int, env: dict[str, str]) -> None:
        self.server, self.base, self.rng, self.env = server, base, random.Random(seed), env
        self.n = 0
        self.notes: list[str] = []
        self.token: dict[str, str] = {}  # each note's one unique token, for edit_file
        self.url = ""
        self.http: http.server.ThreadingHTTPServer | None = None
        if server == "git":
            self.repo = base / "repo"
            self.repo.mkdir()
            _git(self.repo, env, "init", "-q", "-b", "main")
            (self.repo / "README.md").write_text("# scratch\n")
            _git(self.repo, env, "add", "README.md")
            _git(self.repo, env, "commit", "-q", "-m", "initial")
        elif server == "filesystem":
            self.root = base / "root"
            (self.root / "notes").mkdir(parents=True)
            (self.root / "notes" / "start.md").write_text("start tok-0\n")
            self.notes.append(str(self.root / "notes" / "start.md"))
            self.token[self.notes[0]] = "tok-0"
        else:
            self.http, self.url = _site(base / "site", self.rng)

    def command(self) -> list[str]:
        if self.server == "git":
            return [str(SGPY / "mcp-server-git"), "--repository", str(self.repo)]
        if self.server == "filesystem":
            return [NODE, FS_JS, str(self.root)]
        return [str(SGPY / "mcp-server-fetch"), "--allow-private-ips", "--ignore-robots-txt"]

    def close(self) -> None:
        if self.http is not None:
            self.http.shutdown()

    def next(self) -> tuple[str, dict[str, Any]]:
        self.n += 1
        miss = self.rng.random() < MISS
        pick = self.rng.random()
        return getattr(self, f"_{self.server}")(miss, pick)

    def _git(self, miss: bool, pick: float) -> tuple[str, dict[str, Any]]:
        repo = str(self.repo)
        if miss:
            return "git_show", {"repo_path": repo, "revision": f"no-such-rev-{self.n}"}
        if pick < 0.20:
            return "git_status", {"repo_path": repo}
        if pick < 0.32:
            return "git_log", {"repo_path": repo, "max_count": self.rng.randint(1, 5)}
        if pick < 0.42:
            name = f"file{self.rng.randint(0, 9)}.txt"
            path = self.repo / name
            path.write_text((path.read_text() if path.exists() else "") + f"line {self.n}\n")
            return "git_diff_unstaged", {"repo_path": repo}
        if pick < 0.55:
            name = f"file{self.rng.randint(0, 9)}.txt"
            path = self.repo / name
            path.write_text((path.read_text() if path.exists() else "") + f"edit {self.n}\n")
            return "git_add", {"repo_path": repo, "files": [name]}
        if pick < 0.62:
            return "git_diff_staged", {"repo_path": repo}
        if pick < 0.74:
            return "git_commit", {"repo_path": repo, "message": f"change {self.n}"}
        if pick < 0.86:
            return "git_show", {"repo_path": repo, "revision": "HEAD"}
        if pick < 0.93:
            return "git_branch", {"repo_path": repo, "branch_type": "local"}
        return "git_diff", {"repo_path": repo, "target": "HEAD"}

    def _filesystem(self, miss: bool, pick: float) -> tuple[str, dict[str, Any]]:
        root = self.root
        if miss:
            return "read_text_file", {"path": str(root / "notes" / f"missing-{self.n}.md")}
        if pick < 0.18 or not self.notes:
            path = str(root / "notes" / f"note-{self.n}.md")
            self.notes.append(path)
            self.token[path] = f"tok-{self.n}"
            return "write_file", {"path": path, "content": f"note {self.n}\nbody tok-{self.n}\n"}
        if pick < 0.38:
            return "read_text_file", {"path": self.rng.choice(self.notes)}
        if pick < 0.50:
            return "list_directory", {"path": str(root / "notes")}
        if pick < 0.60:
            return "get_file_info", {"path": self.rng.choice(self.notes)}
        if pick < 0.68:
            return "search_files", {"path": str(root), "pattern": "*.md"}
        if pick < 0.76:
            path = self.rng.choice(self.notes)
            old, new = self.token[path], f"tok-e{self.n}"
            self.token[path] = new
            return "edit_file", {"path": path, "edits": [{"oldText": old, "newText": new}]}
        if pick < 0.84:
            source = self.notes.pop(self.rng.randrange(len(self.notes)))
            destination = str(root / "notes" / f"moved-{self.n}.md")
            self.notes.append(destination)
            self.token[destination] = self.token.pop(source)
            return "move_file", {"source": source, "destination": destination}
        if pick < 0.90:
            return "create_directory", {"path": str(root / f"dir{self.rng.randint(0, 3)}")}
        if pick < 0.95:
            return "directory_tree", {"path": str(root)}
        return "read_multiple_files", {"paths": self.rng.sample(self.notes, min(3, len(self.notes)))}

    def _fetch(self, miss: bool, pick: float) -> tuple[str, dict[str, Any]]:
        if miss:
            return "fetch", {"url": f"{self.url}/missing-{self.n}.html"}
        page = f"{self.url}/page{self.rng.randint(0, 11)}.html"
        if pick < 0.6:
            return "fetch", {"url": page, "max_length": 2000}
        if pick < 0.85:
            return "fetch", {"url": page, "max_length": 1000, "start_index": 1000}
        return "fetch", {"url": page, "raw": True, "max_length": 3000}


class Client:
    """One relay session over stdio, one request at a time."""

    def __init__(self, command: list[str], env: dict[str, str], err: Path) -> None:
        self._err = open(err, "wb")  # noqa: SIM115 -- closed in close()
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._err, env=env
        )
        self.ident = 0
        self.noise = 0  # lines on the relay's stdout that are not JSON (a client skips them)

    def request(self, method: str, params: dict[str, Any], timeout: float = 60.0) -> Any:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.ident += 1
        ident = self.ident
        line = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        self.process.stdin.write(json.dumps(line).encode() + b"\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.process.stdout.readline()
            if not raw:
                return None  # the relay went away
            try:
                answer = json.loads(raw)
            except json.JSONDecodeError:
                self.noise += 1  # e.g. a blank line the server wrote, passed through
                continue
            if answer.get("id") == ident:
                return answer
        return None

    def notify(self, method: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n")
        self.process.stdin.flush()

    def close(self) -> int:
        assert self.process.stdin is not None
        self.process.stdin.close()
        code = self.process.wait(timeout=120)
        self._err.close()
        return code


def _effects(path: Path, server: str) -> None:
    tools = [{"name": t, "effect": "read_only"} for t in READS[server]]
    tools += [{"name": t, "effect": e} for t, e in WRITES.items()]
    path.write_text(json.dumps({"tools": tools}))


def run(server: str, arm: str, seed: int, calls: int) -> dict[str, Any]:
    base = RUNS / f"c1-{server}-{arm}-s{seed}-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True)
    home = base / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    env.update(PYTHONPATH=str(CODE / "src"), PYTHONDONTWRITEBYTECODE="1", HOME=str(home),
               GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="scripted", GIT_AUTHOR_EMAIL="s@local",
               GIT_COMMITTER_NAME="scripted", GIT_COMMITTER_EMAIL="s@local",
               NO_PROXY="*", no_proxy="*")  # fmt: skip
    workload = Workload(server, base, seed, env)
    effects = base / "effects.json"
    _effects(effects, server)
    capture_dir = OUT / f"{server}-{arm}-s{seed}"
    if capture_dir.exists():
        raise SystemExit(f"{capture_dir} exists: a capture is never overwritten")
    capture_dir.parent.mkdir(parents=True, exist_ok=True)
    relay = ["--retry", "auto", "--effects", str(effects), "--trust-read-only-hints",
             "--call-deadline", "30", *(["--shadow"] if arm == "shadow" else [])]  # fmt: skip
    command = [
        PY, "-m", "stateguard", "capture", "stdio", "--dir", str(base / "capture"),
        "--label", LABEL, "--provenance", "local_development", *relay,
        "--", *workload.command(),
    ]  # fmt: skip
    client = Client(command, env, base / "client.err")
    sent = answered = tool_errors = errors = 0
    by_tool: dict[str, int] = {}
    t0 = time.monotonic()
    try:
        init = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "c1", "version": "1"}})  # fmt: skip
        if init is None:
            raise RuntimeError("no answer to initialize")
        client.notify("notifications/initialized")
        client.request("tools/list", {})
        for _ in range(calls):
            tool, arguments = workload.next()
            by_tool[tool] = by_tool.get(tool, 0) + 1
            sent += 1
            answer = client.request("tools/call", {"name": tool, "arguments": arguments})
            if answer is None:
                continue
            answered += 1
            if "error" in answer:
                errors += 1
            elif (answer.get("result") or {}).get("isError") is True:
                tool_errors += 1
    finally:
        exit_code = client.close()
        workload.close()
    wall = time.monotonic() - t0
    bundle = base / "capture" / "bundle"
    row: dict[str, Any] = {
        "server": server, "arm": arm, "seed": seed, "label": LABEL,
        "client": {"tool_calls": sent, "answered": answered, "tool_errors": tool_errors,
                   "jsonrpc_errors": errors, "by_tool": by_tool},
        "relay_exit": exit_code, "wall_s": round(wall, 2), "client_noise_lines": client.noise,
    }  # fmt: skip
    if not bundle.is_dir():
        row["bundle"] = None
        row["invalid"] = "no bundle written"
        row["capture_stderr"] = (base / "client.err").read_text(errors="replace")[-2000:]
        return row
    sys.path.insert(0, str(CODE / "src"))
    from stateguard.capture import load_bundle  # noqa: PLC0415

    loaded = load_bundle(bundle)
    shutil.copytree(bundle, capture_dir / "bundle")
    report = loaded["report"]
    row.update(
        bundle=str((capture_dir / "bundle").relative_to(OUT)),
        rebuilt_equal=loaded["rebuilt_equal"],
        report={k: report[k] for k in ("denominators", "calls", "retries", "latency_ms",
                                         "overhead_ms", "integrity", "server")},  # fmt: skip
    )
    row["client_calls_equal_relay_calls"] = sent == report["denominators"]["client_tool_calls"]
    row["client_tool_errors_equal_report"] = (
        tool_errors + errors == report["calls"]["tool_error"]["k"]
    )
    shutil.rmtree(base, ignore_errors=True)
    return row


def main() -> None:
    server, sessions = sys.argv[1], int(sys.argv[2])
    arms = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else list(ARMS)
    calls = int(sys.argv[4]) if len(sys.argv) > 4 else CALLS
    offset = int(os.environ.get("TRIAL_OFFSET", "0"))
    for seed in range(offset + 1, offset + sessions + 1):
        for arm in arms:
            try:
                row = run(server, arm, seed, calls)
            except Exception as exc:  # a driver error is a row, never dropped
                row = {"server": server, "arm": arm, "seed": seed, "label": LABEL,
                       "invalid": f"driver error: {type(exc).__name__}: {exc}"}  # fmt: skip
            print(json.dumps(row, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
