"""
Strict per-invocation evaluation of StateGuard over Streamable HTTP (TEST-ONLY harness).

Every row is one logical invocation: one client session, one tools/call, one injected fault (or
none). The client is the official Python SDK (``client.py``). Ground truth is read outside
StateGuard: the notes file the server writes, and the fault proxy's log of what reached it.

The code under test is the checkout at ``STATEGUARD_CODE`` (default: this repository). For
recorded evidence use ``run_batch.py``, which makes that a clean ``git worktree`` at a named
commit, so working-tree edits never reach a run.

Servers (reported separately):
  notes       CONTROLLED: tests/examples/http_notes_server.py (Python mcp 2.x)
  tsnotes     CONTROLLED: adapters/typescript/test/fixtures/http_notes_server.ts (TypeScript SDK)
  everything  THIRD-PARTY @modelcontextprotocol/server-everything (reads only: echo); its entry
              point is EVERYTHING_JS (default /opt/sg-servers/everything/...)

Arms:
  raw                 client -> fault proxy -> server
  naive               raw, plus a blind resend on a new session after any failure (no key)
  sg                  client -> StateGuard HTTP relay (--retry auto) -> fault proxy -> server
  sg_keyed            sg, and the server keyed by StateGuard's injector / preload
  sg_resumable        sg, and the notes server storing its event streams (--resumable)
  sg_deadline         sg with --call-deadline 2
  sg_keyed_deadline   sg_keyed with --call-deadline 2

Faults (TEST-ONLY): benchmarks/runtime/http_fault_proxy.py modes, plus session_loss (the server
process killed and restarted between the client's handshake and its call), and slow (notes
and tsnotes, no fault: add_note takes SLOW_SECONDS). Ops: read, write,
drift (add_note with its field misnamed "Text").

usage: driver.py SERVER N [cells] [arms] [workers] -> JSONL rows on stdout
  TRIAL_OFFSET=K numbers trials K..K+N-1
"""

import concurrent.futures
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from stateguard.adapters.mcp.incidence import read_records

HERE = Path(__file__).resolve().parent
CODE = Path(os.environ.get("STATEGUARD_CODE", str(HERE.parents[2]))).resolve()
PY = os.environ.get("STATEGUARD_PYTHON", sys.executable)
NODE = os.environ.get("NODE") or shutil.which("node") or "/opt/node22/bin/node"
RELAY = str(CODE / "examples/mcp/http_proxy.py")
FAULTS = str(CODE / "benchmarks/runtime/http_fault_proxy.py")
NOTES_SERVER = str(CODE / "tests/examples/http_notes_server.py")
TS_SERVER = str(CODE / "adapters/typescript/test/fixtures/http_notes_server.ts")
TS_PRELOAD = str(CODE / "adapters/typescript/src/preload.ts")
EVERYTHING = os.environ.get(
    "EVERYTHING_JS",
    "/opt/sg-servers/everything/node_modules/@modelcontextprotocol/server-everything/dist/index.js",
)
CLIENT = str(HERE / "client.py")
RUNS = Path(os.environ.get("RUNS_DIR", tempfile.gettempdir() + "/realhttp-runs"))
FAILING = Path(os.environ["FAILING_DIR"]) if os.environ.get("FAILING_DIR") else None
OUTCOME = "io.stateguard/idempotencyOutcome"
STATE = "io.stateguard/executionState"
MODES = ["none", "kill_before", "http_503_before", "lost_response", "http_502_after",
         "cut_stream", "hang_after", "session_loss"]
FULL = [f"read/{m}" for m in MODES] + [f"write/{m}" for m in MODES]
#: Server crashes (notes and tsnotes, write only; not in the default cells): the server process
#: kills itself (SIGKILL) at the point inside add_note -- keyed arms through the key's two phases,
#: unkeyed arms around the unmodified handler (the injector / preload in fault-only mode) -- and
#: a supervisor restarts it on the same port, as systemd's Restart= would.
CRASH = ["after_begin", "after_effect", "after_record"]
CELLS = {"notes": FULL, "tsnotes": FULL, "everything": [f"read/{m}" for m in MODES]}
ARMS = {"notes": ["raw", "naive", "sg", "sg_keyed", "sg_keyed_deadline"],
        "tsnotes": ["raw", "naive", "sg", "sg_keyed", "sg_keyed_deadline"],
        "everything": ["raw", "naive", "sg", "sg_deadline"]}
KEYED = ("sg_keyed", "sg_keyed_deadline")
DEADLINE = "2"
CLIENT_WAIT = 8.0
#: Mode "slow" (no fault): the op's tool takes this long (read/slow: notes only), past
#: deadline + the keyed server's 5 s duplicate wait + deadline. A deadline must not turn it
#: into a failure.
SLOW_SECONDS = 10.0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


class Procs:
    def __init__(self, base):
        self.base, self.procs = base, {}

    def start(self, name, argv, env):
        log = open(self.base / f"{name}.log", "ab")
        self.procs[name] = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=env)

    def stop(self, name):
        p = self.procs.pop(name, None)
        if p is not None:
            p.kill()
            p.wait(timeout=10)

    def stop_all(self):
        for name in list(self.procs):
            self.stop(name)


def setup(server, base, t, arm, op, slow=0.0, crash=None):
    env = {**os.environ, "PYTHONPATH": str(CODE / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    port = free_port()
    read = write = None
    if server in ("notes", "tsnotes"):
        notes = base / "notes.txt"
        notes.write_text(f"seed-{t}\n")
        if arm in KEYED:
            env.update(STATEGUARD_IDEMPOTENCY_DB=str(base / "keys.db"),
                       STATEGUARD_IDEMPOTENCY_SCOPE="notes", STATEGUARD_KEYED_TOOLS="add_note")
        if crash:
            env.update(STATEGUARD_FAULT=crash, STATEGUARD_FAULT_FILE=str(base / "crash.fired"),
                       STATEGUARD_FAULT_TOOLS="add_note")
        adapted = arm in KEYED or bool(crash)
        if server == "notes":
            argv = [NOTES_SERVER, str(notes), str(port)]
            if slow:  # the op's own tool is the slow one
                argv += ["--slow-read" if op == "read" else "--slow", str(slow)]
            if arm == "sg_resumable":
                argv.append("--resumable")
            if adapted:
                argv = ["-m", "stateguard.adapters.mcp.inject", *argv]
            cmd = [PY, *argv]
        else:
            cmd = [NODE, "--no-warnings"]
            if adapted:
                cmd += ["--import", TS_PRELOAD]
            cmd += [TS_SERVER, str(notes), str(port)]
            if slow:
                if op == "read":
                    raise ValueError("read/slow needs the notes server (--slow-read)")
                cmd += ["--slow", str(slow)]
        read = ("list_notes", {}, f"seed-{t}")
        write = ("add_note", {"Text" if op == "drift" else "text": f"w-{t}"})
    else:
        env["PORT"] = str(port)
        cmd = [NODE, "--no-warnings", EVERYTHING, "streamableHttp"]
        read = ("echo", {"message": f"seed-{t}"}, f"seed-{t}")
    return port, cmd, env, read, write


def truth_count(base, t):
    path = base / "notes.txt"
    return path.read_text().split("\n").count(f"w-{t}") if path.exists() else 0


def run(server, cell, arm, t):
    op, mode = cell.split("/")
    uid = uuid.uuid4().hex[:10]
    base = RUNS / f"{server}-{op}-{mode}-{arm}-{t}-{uid}"
    base.mkdir(parents=True)
    procs = Procs(base)
    try:
        if mode == "slow" and server not in ("notes", "tsnotes"):
            raise ValueError("mode slow needs a notes server (--slow)")
        slow = SLOW_SECONDS if mode == "slow" else 0.0
        if mode in CRASH and (server not in ("notes", "tsnotes") or op != "write"):
            raise ValueError("crash cells are write cells on notes and tsnotes")
        crash = mode if mode in CRASH else None
        port, server_cmd, server_env, read, write = setup(server, base, t, arm, op, slow, crash)
        name, args = (read[0], read[1]) if op == "read" else write
        procs.start("server", server_cmd, server_env)
        if not wait_port(port):
            raise RuntimeError("server did not listen")
        proxy_port = free_port()
        proxy_mode = mode if mode not in ("none", "session_loss", "slow", *CRASH) else "none"
        procs.start("faults", [PY, FAULTS, "--listen", f"127.0.0.1:{proxy_port}", "--upstream",
                               f"127.0.0.1:{port}", "--mode", proxy_mode, "--tool", name,
                               "--fault-file", str(base / "fault_flag"), "--log",
                               str(base / "proxy.log")], server_env)
        if not wait_port(proxy_port):
            raise RuntimeError("fault proxy did not listen")
        if crash:
            # The fault is inside the server, and a proxy in front of a dead server accepts the
            # connection and closes it: "lost" where a client would see "refused" (not sent).
            # So crash cells reach the server directly.
            proxy_port = port
        url = f"http://127.0.0.1:{proxy_port}/mcp"
        if arm.startswith("sg"):
            effects = base / "effects.json"
            effects.write_text(json.dumps({"tools": [{"name": "add_note", "effect": "non_idempotent"}]}))
            relay_port = free_port()
            deadline = ["--call-deadline", DEADLINE] if arm.endswith("_deadline") else []
            procs.start("relay", [PY, RELAY, "--listen", f"127.0.0.1:{relay_port}", "--retry", "auto",
                                  "--trust-read-only-hints", "--effects", str(effects), *deadline,
                                  "--audit", str(base / "audit.jsonl"), "--",
                                  f"http://127.0.0.1:{proxy_port}/mcp"], server_env)
            if not wait_port(relay_port):
                raise RuntimeError("relay did not listen")
            url = f"http://127.0.0.1:{relay_port}/mcp"
        client = [PY, CLIENT, url, name, json.dumps(args), "--wait", str(CLIENT_WAIT + slow)]
        if arm == "naive":
            client.append("--naive")
        if mode == "session_loss":
            client += ["--pause-file", str(base / "ready"), "--go-file", str(base / "go")]
        t0 = time.monotonic()
        proc = subprocess.Popen(client, stdout=subprocess.PIPE, stderr=open(base / "client.err", "wb"),
                                env=server_env)
        if crash:
            def supervise():  # restart the server once when it dies, on the same port
                while proc.poll() is None:
                    current = procs.procs.get("server")
                    if current is not None and current.poll() is not None:
                        procs.start("server", server_cmd, server_env)
                        wait_port(port)
                        return
                    time.sleep(0.02)

            threading.Thread(target=supervise, daemon=True).start()
        if mode == "session_loss":
            deadline_at = time.monotonic() + 30
            while not (base / "ready").exists() and time.monotonic() < deadline_at and proc.poll() is None:
                time.sleep(0.05)
            procs.stop("server")  # its sessions die with it
            procs.start("server", server_cmd, server_env)
            wait_port(port)
            (base / "go").write_text("go")
            (base / "fault_flag").write_text("session_loss")
        out, _ = proc.communicate(timeout=120)
        latency = time.monotonic() - t0
        seen = json.loads(out.decode().strip().splitlines()[-1])
    finally:
        procs.stop_all()
    final = seen["final"]
    told_ok = final["status"] == "result" and not final["is_error"]
    data = final.get("data") or {}
    unknown = data.get(STATE) == "unknown"
    not_sent = data.get(STATE) == "not_sent"
    server_outcome = (final.get("meta") or {}).get(OUTCOME) if final["status"] == "result" else None
    kinds = []
    if final["status"] in ("timeout", "crash"):
        kinds.append("no_answer_" + final["status"])
    elif unknown:
        kinds.append("surfaced_unknown")
    elif not_sent:
        kinds.append("surfaced_not_sent")
    elif not told_ok:
        kinds.append("error_answer")
    if op == "read":
        right = told_ok and read[2] in final.get("text", "")
        if told_ok and not right:
            kinds.append("wrong_content")
        success, count = bool(right), None
    else:  # write and drift: scored by the effect
        count = truth_count(base, t)
        if count > 1:
            kinds.append("duplicate_effect")
        if count == 0 and told_ok:
            kinds.append("false_success")
        # A keyed server's "in doubt" / "still running" (-32010 / -32011) claims no failure:
        # unresolved, as in the stdio driver, never a false failure.
        if (count >= 1 and final["status"] == "mcp_error" and not unknown
                and final.get("code") not in (-32010, -32011)):
            kinds.append("false_failure")
        if count >= 1 and final["status"] == "result" and final["is_error"]:
            kinds.append("false_failure")
        if count == 0:
            kinds.append("not_executed")
        success = count == 1 and told_ok
    audit = []
    if (base / "audit.jsonl").exists():
        audit = [json.loads(line) for line in (base / "audit.jsonl").read_text().splitlines() if line.strip()]
    proxy_log = (base / "proxy.log").read_text().splitlines() if (base / "proxy.log").exists() else []
    # Finished records, and an ``unfinished`` one for each request the relay never finished.
    invocations = [e for e in (read_records([base / "audit.jsonl"]) if audit else [])
                   if e.get("method") == "tools/call"]
    row = {
        "server": server, "op": op, "tool": name, "mode": mode,
        "family": {"session_loss": "session", "slow": "slow"}.get(
            mode, "crash" if mode in CRASH else "transport"),
        "arm": arm, "trial": t,
        "run": base.name, "success": success, "kinds": kinds, "latency_s": round(latency, 3),
        "count": count, "told_ok": told_ok, "server_outcome": server_outcome,
        "first_status": seen["first"]["status"], "client_retried": seen["retried"],
        "fault_fired": (base / "fault_flag").exists() or (base / "crash.fired").exists(),
        "server_calls_of_target": sum(1 for ln in proxy_log if ln == f"call {name}"),
        "relay_events": sorted({e["event"] for e in audit}),
        "relay_invocation": invocations[-1] if invocations else None,
        "final": final,
    }
    if not success and FAILING is not None:
        FAILING.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base, FAILING / base.name)
    shutil.rmtree(base, ignore_errors=True)
    return row


def main():
    server, n = sys.argv[1], int(sys.argv[2])
    cells = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else CELLS[server]
    arms = sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4] else ARMS[server]
    workers = int(sys.argv[5]) if len(sys.argv) > 5 else 4
    offset = int(os.environ.get("TRIAL_OFFSET", "0"))
    jobs = [(cell, arm, t) for t in range(offset, offset + n) for cell in cells for arm in arms]
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(run, server, *job): job for job in jobs}
        for fut in concurrent.futures.as_completed(futures):
            try:
                row = fut.result()
            except Exception as exc:  # a driver error is a recorded row, never dropped
                cell, arm, t = futures[fut]
                op, mode = cell.split("/")
                row = {"server": server, "op": op, "mode": mode, "arm": arm, "trial": t,
                       "success": False, "kinds": ["driver_error"],
                       "error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
