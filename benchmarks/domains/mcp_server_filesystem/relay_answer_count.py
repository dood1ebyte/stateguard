"""
Client-side exactly-once check for the Week 3 audit fixes (isolated tool).

usage: relay_answer_count.py SERVER_BIN WORKSPACE OUT_JSON [--reps N]

``relay_audit.py`` reads the first answer to each request; this counts *every*
line the relay writes to the client, per request id, until the relay's stdout
closes. Each trial runs the production relay (``stdio_proxy.py main()``) in
front of ``relay_audit_shim.py`` and the pinned server, or ``rollback_server.py``
for the detached-descendant case. Only ids and counts are recorded.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixture  # noqa: E402
import relay_audit  # noqa: E402
import workflow  # noqa: E402
from rawclient import RawSession  # noqa: E402

CASES: list[dict[str, Any]] = [
    {"id": "drop", "shim": ["--fault", "drop_response"]},
    {"id": "fail_restart", "shim": ["--fault", "drop_response", "--fail-restart"]},
    {"id": "detached", "server": "detached"},
    {"id": "colliding_id", "shim": [], "colliding": True},
    {"id": "shadow_drop", "shim": ["--fault", "drop_response"], "shadow": True},
]


def trial(server: str, ws: Path, case: dict[str, Any], rep: int) -> dict[str, Any]:
    trial_dir = ws / "trials" / f"{case['id']}-{rep}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    for f in trial_dir.iterdir():
        f.unlink()
    sb = ws / "sandbox"
    fixture.reset(sb)
    (sb / fixture.ORDER).write_text(fixture.APPROVED_ORDER, encoding="utf-8")
    effects = trial_dir / "effects.json"
    effects.write_text(json.dumps(relay_audit.effects("reconcile")), encoding="utf-8")
    relay = [relay_audit.PY, str(relay_audit.RELAY), "--audit", str(trial_dir / "audit.jsonl"),
             "--effects", str(effects), "--retry", "off"]  # fmt: skip
    relay += ["--shadow"] if case.get("shadow") else [
        "--restart", "2", "--restart-policy", "after-unknown"]  # fmt: skip
    if case.get("server"):
        backend = [relay_audit.PY, str(relay_audit.ROLLBACK), "--state", str(trial_dir),
                   "--sandbox", str(sb), "--mode", case["server"],
                   "--rollback-after", "1.0"]  # fmt: skip
    else:
        backend = [relay_audit.PY, str(relay_audit.SHIM), "--state", str(trial_dir),
                   "--sandbox", str(sb), *case["shim"], "--", server, str(sb)]  # fmt: skip
    session = RawSession(
        [*relay, "--", *backend], env={"PATH": os.environ["PATH"], "HOME": str(ws)}, cwd=str(ws)
    )
    session.initialize()
    tool, args = workflow.arguments("S3", sb)
    ids = ["T-1"]
    if case.get("colliding"):
        ids = ["stateguard-reconcile:1", "T-1"]
        session.send({"jsonrpc": "2.0", "id": ids[0], "method": "tools/call",
                      "params": {"name": "read_text_file",
                                 "arguments": {"path": str(sb / fixture.ORDER)}}})  # fmt: skip
    session.send({"jsonrpc": "2.0", "id": "T-1", "method": "tools/call",
                  "params": {"name": tool, "arguments": args}})  # fmt: skip
    time.sleep(2.0)  # let anything late arrive before the client closes its side
    if session.proc.stdin is not None:
        session.proc.stdin.close()
    counts = {i: 0 for i in ids}
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            item = session._lines.get(timeout=0.5)
        except Exception:  # noqa: BLE001 -- queue.Empty
            if session.proc.poll() is not None and session._lines.empty():
                break
            continue
        if item is None:
            break  # the relay's stdout closed: everything it wrote has been read
        if item.get("id") in counts and "method" not in item:
            counts[item["id"]] += 1
    session.close()
    return {"case": case["id"], "rep": rep, "answers": counts}


def main() -> None:
    server, ws, out = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3])
    reps = int(sys.argv[5]) if sys.argv[4:5] == ["--reps"] else 5
    ws.mkdir(parents=True, exist_ok=True)
    results = [trial(server, ws, c, r) for c in CASES for r in range(reps)]
    fixture.reset(ws / "sandbox")
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    for r in results:
        print(r["case"], r["rep"], r["answers"], flush=True)


if __name__ == "__main__":
    main()
