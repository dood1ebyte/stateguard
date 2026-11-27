"""
What real MCP clients see under reconciliation contract v2 (isolated tool).

usage: relay_client_semantics_v2.py SERVER_BIN TS_SDK_DIR WORKSPACE OUT_JSON

``relay_client_semantics.py`` (the Week 3 audit's, unchanged) drives the CLI,
which now refuses ``reconcile`` blocks. This reuses its three clients -- the
raw JSON-RPC line, the Python SDK and the TypeScript SDK -- against the
reference embedder with a PID-namespace executor scope, for a lost answer
after execution (``observed_satisfied``) and a request that never arrived
(``observed_not_satisfied``).
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

EMBEDDED = HERE / "embedded_relay.py"


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
        relay_audit.PY, str(EMBEDDED), "--scope", "pid-namespace",
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
                r["postcondition"] = r.get("postcondition")
            else:
                r = v1.ts_client(cmd, sb, sdk, trial)
            r["final_checkpoint"] = fixture.matching_checkpoint(fixture.snapshot(sb))
            results[f"{fault}/{client}"] = r
            print(fault, client, json.dumps(r)[:400], flush=True)
    fixture.reset(ws / "sandbox")
    Path(out).write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
