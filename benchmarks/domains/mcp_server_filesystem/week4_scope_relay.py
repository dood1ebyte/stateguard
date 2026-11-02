"""
Week 4A comparative batch: the standard CLI with ``--executor-scope pid-namespace``.

usage: week4_scope_relay.py SERVER_BIN WORKSPACE OUT_JSON [--reps N] [--only ID,...]

Backends, all unchanged from earlier weeks: the pinned
``@modelcontextprotocol/server-filesystem`` behind the audit's fault shim
(``relay_audit_shim.py``), the termination test server (``rollback_server.py``)
and the external hand-off server (``handoff_server.py``). Configurations:

``cli_scope``         ``stdio_proxy.py`` + ``--executor-scope pid-namespace`` + reconcile blocks
``cli_raw``           ``stdio_proxy.py`` without reconcile blocks (the raw relay baseline)
``cli_naive``         ``cli_raw``; on an error the client re-sends once under a new id (retry)
``emb_direct``        ``embedded_relay.py --scope direct-child`` (negative control: a reaped
                      direct child is all the evidence there is)
``cli_shadow``        ``stdio_proxy.py --shadow`` (no reconcile blocks)
``cli_shadow_scope``  ``cli_shadow`` + ``--executor-scope pid-namespace``
``direct``            no relay at all: the client talks to the backend (fidelity baseline)

Ground truth is the sandbox on disk (hashed before, at the answer and at the end)
and the backends' own logs -- never the relay's answer or audit log. The client
keeps every byte the relay writes until end of stream. After each trial, every
process whose command line names the trial directory or the sandbox is an
orphan unless it exits within a bounded wait (awaited on pidfds).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import contract_v2_relay as v2  # noqa: E402
import fixture  # noqa: E402
import relay_audit  # noqa: E402
import workflow  # noqa: E402

PY = v2.PY
RELAY = v2.RELAY
SCOPE = ["--executor-scope", "pid-namespace"]
RESTART = ["--restart", "2", "--restart-policy", "after-unknown"]
ORPHAN_WAIT_SECONDS = 10.0

_BASE = {sc["id"]: sc for sc in v2._scenarios()}
_FAULTS = ["cli_scope", "cli_raw", "cli_naive", "emb_direct", "cli_shadow", "cli_shadow_scope",
           "direct"]  # fmt: skip


def _sc(base: str, configs: list[str], **extra: Any) -> dict[str, Any]:
    return {**_BASE[base], "configs": configs, **extra}


def scenarios() -> list[dict[str, Any]]:
    """Phase 6's fifteen situations plus the external effect, mapped to fixtures."""
    rec = ["cli_scope"]
    return [
        {"id": "N1", "group": 1, "title": "normal operation, no fault", "fault": None,
         "configs": ["cli_scope", "cli_raw", "cli_naive", "cli_shadow", "cli_shadow_scope",
                     "direct"]},
        _sc("S2", _FAULTS, group=2),
        _sc("S1", _FAULTS, group=3),
        _sc("T1", ["cli_scope", "emb_direct"], group=4),
        _sc("T2", ["cli_scope", "emb_direct"], group=5),
        _sc("T3", ["cli_scope", "emb_direct"], group=5),
        {"id": "U1", "group": 6, "title": "scope cannot be confirmed empty (helper stopped)",
         "fault": "drop_response", "stop_helper": True, "configs": rec},
        _sc("S9", ["cli_scope", "cli_raw"], group=7),
        _sc("S4b", ["cli_scope", "cli_raw"], group=8),
        _sc("S4a", ["cli_scope", "cli_raw"], group=9),
        _sc("S4c", rec, group=10),
        _sc("C2", rec, group=10),
        _sc("S4d", rec, group=11),
        _sc("S6d", rec, group=11),
        _sc("C3", rec, group=11),
        _sc("S6b", rec, group=12),
        _sc("S6e", rec, group=12),
        _sc("S7a", rec, group=12),
        {"id": "M1", "group": 13, "title": "two concurrent calls, the server dies on one",
         "fault": "drop_response", "concurrent_now": True, "configs": ["cli_scope", "cli_raw"]},
        {"id": "SR1", "group": 14, "title": "client closes while a reconciliation is held",
         "fault": "drop_response", "close_when_pending": True, "configs": rec},
        _sc("S12e", rec, group=14),
        _sc("X1", ["cli_scope", "emb_direct"], group=16),
    ]  # fmt: skip


class Client(v2.Client):
    """``contract_v2_relay.Client``, also keeping every raw byte line (shadow fidelity)."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str) -> None:
        self.raw: list[bytes] = []
        super().__init__(argv, env, cwd)

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self.raw.append(raw)
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self.lines.put((time.monotonic(), msg))
        self.lines.put((time.monotonic(), None))


def processes_mentioning(*tokens: str) -> list[int]:
    """Live (non-zombie) processes whose command line contains any of *tokens*."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        with contextlib.suppress(OSError, IndexError):
            cmd = Path(f"/proc/{entry}/cmdline").read_bytes().decode("utf-8", "replace")
            if any(t in cmd for t in tokens):
                state = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[0]
                if state != "Z":
                    found.append(int(entry))
    return found


def wait_exit(pids: list[int], timeout: float) -> list[int]:
    """Wait (on pidfds, bounded) for *pids* to exit; the ones still running."""
    fds: dict[int, int] = {}
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, OSError):
            fds[os.pidfd_open(pid)] = pid
    poller = select.poll()
    for fd in fds:
        poller.register(fd, select.POLLIN)
    waiting = dict(fds)
    deadline = time.monotonic() + timeout
    while waiting and (left := deadline - time.monotonic()) > 0:
        for fd, _ in poller.poll(left * 1000):
            waiting.pop(fd, None)
            poller.unregister(fd)
    for fd in fds:
        os.close(fd)
    return sorted(waiting.values())


def _cmdline(pid: int) -> str:
    with contextlib.suppress(OSError):
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    return ""


def _relay_argv(config: str, sc: dict[str, Any], audit: Path, effects: Path) -> list[str]:
    if config == "direct":
        return []
    if config == "emb_direct":
        return [PY, str(v2.EMBEDDED), "--scope", "direct-child", "--audit", str(audit),
                "--effects", str(effects)]  # fmt: skip
    argv = [PY, str(RELAY), "--audit", str(audit), "--effects", str(effects), "--retry", "off"]
    if config.startswith("cli_shadow"):
        argv.append("--shadow")
    else:
        argv += RESTART
    if config in ("cli_scope", "cli_shadow_scope"):
        argv += SCOPE
    if sc.get("deadline"):
        argv += ["--call-deadline", str(sc["deadline"])]
    return argv


def trial(server: str, ws: Path, sc: dict[str, Any], config: str, rep: int) -> dict[str, Any]:
    tag = f"{sc['id']}-{config}-{rep}"
    tdir = ws / "trials" / tag
    tdir.mkdir(parents=True, exist_ok=True)
    for f in tdir.iterdir():
        if f.is_file():
            f.unlink()
    sb = ws / "sandbox"
    fixture.reset(sb)
    (sb / fixture.ORDER).write_text(fixture.APPROVED_ORDER, encoding="utf-8")
    if sc.get("setup") == "filed":
        (sb / fixture.ORDER).rename(sb / fixture.DONE)
    before = fixture.snapshot(sb)
    reconciling = config in ("cli_scope", "emb_direct")
    kind = sc.get("kind", "reconcile") if reconciling else "raw"
    effects_file = tdir / "effects.json"
    effects_file.write_text(json.dumps(v2.effects(kind, sc.get("timeout", 5.0))), encoding="utf-8")
    audit = tdir / "audit.jsonl"
    relay = _relay_argv(config, sc, audit, effects_file)
    worker = None
    if sc.get("handoff"):
        backend = [PY, str(v2.HANDOFF), "serve", "--state", str(tdir), "--sandbox", str(sb)]
        worker = subprocess.Popen([PY, str(v2.HANDOFF), "worker", "--state", str(tdir)])
        log = tdir / "events.jsonl"
    elif sc.get("server"):
        backend = [PY, str(relay_audit.ROLLBACK), "--state", str(tdir), "--sandbox", str(sb),
                   "--mode", sc["server"],
                   "--rollback-after", str(sc["rollback_after"])]  # fmt: skip
        log = tdir / "events.jsonl"
    else:
        backend = [PY, str(relay_audit.SHIM), "--state", str(tdir), "--sandbox", str(sb)]
        for flag, key in (("--fault", "fault"), ("--actor-on-restart", "actor"),
                          ("--probe-mode", "probe")):  # fmt: skip
            if sc.get(key):
                backend += [flag, sc[key]]
        if sc.get("fail_restart"):
            backend.append("--fail-restart")
        if sc.get("no_listing"):
            backend.append("--no-listing-on-restart")
        backend += ["--", server, str(sb)]
        log = tdir / "deliveries.jsonl"
    argv = [*relay, "--", *backend] if relay else backend
    client = Client(argv, {"PATH": os.environ["PATH"], "HOME": str(ws)}, str(ws))
    init = client.request(
        "init", "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {},
         "clientInfo": {"name": "sg-week4a", "version": "0"}},
        15.0,
    )  # fmt: skip
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    extra: dict[str, Any] = {}
    if sc.get("stop_helper"):
        own = os.readlink("/proc/self/ns/pid")
        helpers = [p for p in processes_mentioning(str(tdir))
                   if "executor_scope" in _cmdline(p)
                   and os.readlink(f"/proc/{p}/ns/pid") == own]  # fmt: skip
        extra["stopped_helpers"] = len(helpers)
        for pid in helpers:
            os.kill(pid, signal.SIGSTOP)
    tool, args = workflow.arguments("S3", sb)
    call = {"name": tool, "arguments": args}
    ids = ["T-1"]
    t0 = time.monotonic()
    client.send({"jsonrpc": "2.0", "id": "T-1", "method": "tools/call", "params": call})
    if sc.get("concurrent_now"):
        ids.append("T-2")
        client.send({"jsonrpc": "2.0", "id": "T-2", "method": "tools/call",
                     "params": {"name": "read_text_file",
                                "arguments": {"path": str(sb / fixture.LOG)}}})  # fmt: skip
    response: dict[str, Any] | None = None
    if sc.get("close_when_pending"):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(
            e.get("event") == "reconcile_pending" for e in relay_audit._events(audit)
        ):
            time.sleep(0.02)  # polling a file for a condition, bounded; not an ordering sleep
        extra["pending_seen_before_close"] = any(
            e.get("event") == "reconcile_pending" for e in relay_audit._events(audit)
        )
    else:
        response = client.wait_for("T-1", sc.get("timeout_s", 90.0))
    latency = round(time.monotonic() - t0, 3)
    if sc.get("concurrent_now"):
        extra["concurrent_T2"] = workflow.wire_class(client.wait_for("T-2", 30.0))
    at_answer = fixture.snapshot(sb)
    error = response.get("error") if isinstance(response, dict) else None
    if config == "cli_naive" and error:
        ids.append("T-1-resend")
        extra["naive_resend"] = workflow.wire_class(
            client.request("T-1-resend", "tools/call", call, 30.0)
        )
    if sc.get("handoff"):
        (tdir / "go").touch()  # only now may the external worker act
        deadline = time.monotonic() + 15
        while not (tdir / "worker_done").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    if sc.get("late_wait"):
        # Scenario timing (not synchronisation): give a late effect its chance to land.
        time.sleep(max(0.0, t0 + float(sc["late_wait"]) - time.monotonic()))
    closed_at = client.close_and_drain()
    if worker is not None:
        worker.wait(timeout=30)
    if response is None:  # SR1: the answer, if any, came at end of stream
        response = next((m for _, m in client.seen if m.get("id") == "T-1" and "method" not in m),
                        None)  # fmt: skip
        error = response.get("error") if isinstance(response, dict) else None
    at_close = processes_mentioning(str(tdir), str(sb))
    orphans = wait_exit(at_close, ORPHAN_WAIT_SECONDS)
    for pid in orphans:  # leave nothing for the next trial; recorded as an orphan
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)
            os.kill(pid, signal.SIGKILL)
    final = fixture.snapshot(sb)
    events = relay_audit._events(log)
    audit_events = relay_audit._events(audit)
    audit_text = audit.read_text() if audit.exists() else ""
    answers = {i: [at for at, m in client.seen if m.get("id") == i and "method" not in m]
               for i in ids}  # fmt: skip
    data = (error or {}).get("data") or {}
    observation = data.get("io.stateguard/observation") or {}
    window = None
    start, end = v2._iso(observation.get("observed_from")), v2._iso(observation.get("observed_to"))
    if start is not None and end is not None:
        window = round(end - start, 3)
    probes = [e for e in audit_events if e.get("event") == "reconcile_probe"]
    termination = [e for e in audit_events if e.get("event") == "executor_termination"]
    if sc.get("server") or sc.get("handoff"):
        moved_now = [e for e in events if e["event"] == "moved" and e.get("gen") == 0]
        executed = bool(moved_now) or any(e["event"] == "moved_late" for e in events)
        deliveries = len(moved_now) + sum(1 for e in events if e["event"] == "queued")
        late = [e["event"] for e in events
                if e["event"] in ("rolled_back", "moved_late", "worker_moved")]  # fmt: skip
        later_writes = [e for e in events if e["event"] == "moved" and e.get("gen", 0) >= 1]
        resend_ok = False
    else:
        deliveries = sum(1 for e in events if e["event"] == "delivered" and e.get("id") == "T-1")
        answered = {e.get("id"): e for e in events if e["event"] == "answered"}
        # Executed: the shim delivered T-1 to the real server (drop_response executes,
        # swallow_request never delivers), confirmed by the sandbox when it applies.
        executed = deliveries > 0 and sc.get("setup") != "filed"
        late = []
        later_writes = [e for e in events if e["event"] == "delivered" and e.get("gen", 0) >= 1
                        and e.get("tool") not in ("read_text_file", None)
                        and e.get("id") not in ("T-1-resend", "T-2")]  # fmt: skip
        resend = answered.get("T-1-resend")
        resend_ok = resend is not None and not resend.get("is_error")
    kind_pred = sc.get("kind", "reconcile")
    lines = b"".join(client.raw)
    r: dict[str, Any] = {
        "scenario": sc["id"], "group": sc["group"], "config": config, "rep": rep,
        "initialized": bool(init and "result" in init),
        "wire": "none" if response is None else ("error" if error else "result"),
        "error_code": (error or {}).get("code"),
        "postcondition": data.get("io.stateguard/postcondition"),
        "contract": data.get("io.stateguard/reconciliationContract"),
        "claims": {k: data.get(f"io.stateguard/{k}") for k in
                   ("executionState", "attribution", "durability", "operationOutcome",
                    "treatAsSuccess")},
        "predicates": [p.get("strength") for p in observation.get("predicates") or []],
        "content_checked": observation.get("content_checked"),
        "reasons": (data.get("io.stateguard/reconciliation") or {}).get("reasons"),
        "latency_s": latency,
        "observation_window_s": window,
        "probe_count": len(probes),
        "probe_failures": sum(p.get("status") not in ("satisfied", "unsatisfied") for p in probes),
        "termination": [(t.get("kind"), t.get("applies")) for t in termination],
        "budget_exhausted": sum(e.get("event") == "reconcile_budget_exhausted"
                                for e in audit_events),
        "answers_unconfirmed": sum(e.get("event") == "reconcile_answer_unconfirmed"
                                   for e in audit_events),
        # ground truth
        "executed": executed,
        "original_deliveries": deliveries,
        "later_generation_writes": len(later_writes),
        "resend_executed": resend_ok,
        "actor_ops": [e.get("op") for e in events if e.get("event") == "actor"],
        "late_effects": late,
        "predicate_before": v2.predicate(before, kind_pred),
        "predicate_at_answer": v2.predicate(at_answer, kind_pred),
        "predicate_final": v2.predicate(final, kind_pred),
        "intended_at_answer": v2.predicate(at_answer, "reconcile_content"),
        "intended_final": v2.predicate(final, "reconcile_content"),
        "final_checkpoint": fixture.matching_checkpoint(final),
        "answers": {i: len(v) for i, v in answers.items()},
        "answers_after_close": {i: sum(at >= closed_at for at in v) for i, v in answers.items()},
        "processes_at_close": len(at_close),
        "orphans": len(orphans),
        "client_stream_sha256": hashlib.sha256(lines).hexdigest(),
        "client_lines": len(client.raw),
        "sentinel_unchanged": final.get("<sentinel>") == fixture.digest(fixture.SENTINEL_TEXT),
        "audit_leaks_content": any(m in audit_text.lower() for m in relay_audit.LEAK_MARKERS),
        "audit_reconcile_paths": sum(
            1 for e in audit_events if "reconcile" in e.get("event", "")
            and str(sb) in json.dumps(e)),  # fmt: skip
        **extra,
    }  # fmt: skip
    r["classification"] = v2.classify(r)
    return r


def refusals(server: str, ws: Path) -> list[dict[str, Any]]:
    """Every configuration that must be refused before any server starts."""
    # Readable by another user (the capability case runs as uid 65534).
    shared = Path(tempfile.mkdtemp(prefix="stateguard-week4a-refusal-"))
    shared.chmod(0o755)
    spec = shared / "refusal-effects.json"
    spec.write_text(json.dumps(v2.effects("reconcile")), encoding="utf-8")
    spec.chmod(0o644)
    marker = ws / "refusal-started"
    starter = [PY, "-c", f"open({str(marker)!r}, 'w').close()"]
    nobody = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"]
    cases: list[tuple[str, list[str], list[str], list[str], str]] = [
        ("no_scope_auto", [], RESTART, starter, "need --executor-scope pid-namespace"),
        ("no_scope_no_restart", [], [], starter, "need --executor-scope pid-namespace"),
        ("no_scope_shadow", [], ["--shadow"], starter, "need --executor-scope pid-namespace"),
        ("no_scope_passthrough", [], ["--passthrough"], starter,
         "need --executor-scope pid-namespace"),
        ("scope_shadow", [], ["--shadow", *SCOPE], starter, "never used in shadow"),
        ("scope_passthrough", [], ["--passthrough", *SCOPE], starter, "never used in shadow"),
        ("scope_no_restart", [], SCOPE, starter, "add --restart N --restart-policy after-unknown"),
        ("scope_idle_only", [], [*SCOPE, "--restart", "2"], starter,
         "add --restart N --restart-policy after-unknown"),
        ("scope_missing_program", [], [*SCOPE, *RESTART], [str(ws / "no-such-server")],
         "was not found"),
        ("scope_without_cap_sys_admin", nobody, [*SCOPE, *RESTART], starter,
         "not available here"),
    ]  # fmt: skip
    out = []
    for name, wrapper, flags, command, expect in cases:
        with contextlib.suppress(FileNotFoundError):
            marker.unlink()
        proc = subprocess.run(
            [*wrapper, PY, str(RELAY), "--effects", str(spec), *flags, "--", *command],
            input=b"", capture_output=True, timeout=60, env={"PATH": os.environ["PATH"]},
            cwd=str(HERE.parents[2]),
        )  # fmt: skip
        err = proc.stderr.decode("utf-8", "replace")
        out.append({
            "case": name, "exit_code": proc.returncode, "server_started": marker.exists(),
            "actionable": expect in err,
            "names_capability": "CAP_SYS_ADMIN" in err if "cap_sys_admin" in name else None,
        })  # fmt: skip
    return out


def main() -> None:
    server, ws, out = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3])
    argv = sys.argv[4:]
    reps = int(argv[argv.index("--reps") + 1]) if "--reps" in argv else 5
    only = set(argv[argv.index("--only") + 1].split(",")) if "--only" in argv else None
    ws.mkdir(parents=True, exist_ok=True)
    ws.chmod(0o755)  # the capability refusal runs as another user
    results = []
    for sc in scenarios():
        if only and sc["id"] not in only:
            continue
        for config in sc["configs"]:
            for rep in range(reps if sc["id"] != "S12e" else min(reps, 3)):
                r = trial(server, ws, sc, config, rep)
                results.append(r)
                print(
                    f"{r['scenario']:5} {config:16} r{rep} {r['classification']:26} "
                    f"post={r['postcondition']!s:22} t={r['latency_s']}s "
                    f"probes={r['probe_count']} answers={r['answers']} "
                    f"term={r['termination']} orphans={r['orphans']} "
                    f"late={r['late_effects']}",
                    flush=True,
                )
    refused = refusals(server, ws) if not only or "REFUSAL" in only else []
    for row in refused:
        print("refusal", json.dumps(row), flush=True)
    fixture.reset(ws / "sandbox")
    out.write_text(json.dumps({"results": results, "refusal": refused}, indent=1),
                   encoding="utf-8")  # fmt: skip
    print("trials:", len(results), "refusal cases:", len(refused))


if __name__ == "__main__":
    main()
