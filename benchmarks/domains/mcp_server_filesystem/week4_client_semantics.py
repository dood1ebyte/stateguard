"""
What real MCP clients see from the CLI with ``--executor-scope pid-namespace`` (Week 4A).

usage: week4_client_semantics.py SERVER_BIN TS_SDK_DIR WORKSPACE OUT_JSON

Reuses the three clients of ``relay_client_semantics.py`` (unchanged) -- the raw
JSON-RPC line, the Python SDK and the TypeScript SDK -- against the standard
CLI with an executor scope and reconcile blocks, for a lost answer after
execution (``observed_satisfied``) and a request that never arrived
(``observed_not_satisfied``). Each client must see an error (never a result),
with ``io.stateguard/treatAsSuccess: false`` in its data.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixture  # noqa: E402
import relay_audit  # noqa: E402
import relay_client_semantics as v1  # noqa: E402


def setup(ws: Path, fault: str, server: str) -> tuple[list[str], Path, Path]:
    trial = ws / f"trial-{fault}"
    trial.mkdir(parents=True, exist_ok=True)
    for f in trial.iterdir():
        if f.is_file():
            f.unlink()
    sb = ws / "sandbox"
    fixture.reset(sb)
    (sb / fixture.ORDER).write_text(fixture.APPROVED_ORDER, encoding="utf-8")
    effects = trial / "effects.json"
    effects.write_text(json.dumps(relay_audit.effects("reconcile")), encoding="utf-8")
    cmd = [
        relay_audit.PY, str(relay_audit.RELAY), "--executor-scope", "pid-namespace",
        "--restart", "2", "--restart-policy", "after-unknown", "--retry", "off",
        "--audit", str(trial / "audit.jsonl"), "--effects", str(effects), "--",
        relay_audit.PY, str(relay_audit.SHIM), "--state", str(trial), "--sandbox", str(sb),
        "--fault", fault, "--", server, str(sb),
    ]  # fmt: skip
    return cmd, sb, trial


def main() -> None:
    server, sdk, ws, out = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve(), sys.argv[4]
    ws.mkdir(parents=True, exist_ok=True)
    v1.SERVER = server
    results: dict[str, Any] = {}
    for fault in ("drop_response", "swallow_request"):
        for client in ("raw", "python", "ts"):
            cmd, sb, trial = setup(ws, fault, server)
            if client == "raw":
                r = v1.raw_client(cmd, sb)
            elif client == "python":
                r = asyncio.run(v1.python_client(cmd, sb))
            else:
                r = v1.ts_client(cmd, sb, sdk, trial)
            r["final_checkpoint"] = fixture.matching_checkpoint(fixture.snapshot(sb))
            audit = relay_audit._events(trial / "audit.jsonl")
            r["termination"] = [
                (e.get("kind"), e.get("applies"))
                for e in audit
                if e.get("event") == "executor_termination"
            ]
            results[f"{fault}/{client}"] = r
            print(fault, client, json.dumps(r)[:600], flush=True)
    fixture.reset(ws / "sandbox")
    Path(out).write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
