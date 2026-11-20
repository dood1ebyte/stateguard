"""
What real MCP clients see when the relay answers a lost ``move_file`` (Week 3 audit).

usage: relay_client_semantics.py SERVER_BIN TS_SDK_DIR WORKSPACE OUT_JSON

For two faults -- executed with the answer lost (``drop_response``: the relay
answers ``postcondition: verified``) and never delivered (``swallow_request``:
``not_verified``) -- the same production relay configuration is driven by:

``raw``     ``rawclient.RawSession``: the exact JSON-RPC response line
``python``  the Python SDK (``mcp`` ``Client.call_tool``)
``ts``      the TypeScript SDK (``Client.callTool`` over ``StdioClientTransport``)

Recorded per client: whether the call raised, the exception type, the code,
the message, whether the StateGuard ``data`` fields are reachable, and whether
the session still serves a follow-up read. Absolute paths are redacted; no
file contents are recorded.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixture  # noqa: E402
import relay_audit  # noqa: E402
from rawclient import RawSession  # noqa: E402

TS_CLIENT = r"""
const [sdk, cmdJson, src, dst, readPath] = process.argv.slice(2);
const { Client } = await import(`${sdk}/dist/esm/client/index.js`);
const { StdioClientTransport } = await import(`${sdk}/dist/esm/client/stdio.js`);
const cmd = JSON.parse(cmdJson);
const transport = new StdioClientTransport({ command: cmd[0], args: cmd.slice(1),
  env: { PATH: process.env.PATH, HOME: process.env.HOME } });
const client = new Client({ name: "sg-week3-audit", version: "0" });
await client.connect(transport);
const out = {};
try {
  const r = await client.callTool({
    name: "move_file", arguments: { source: src, destination: dst } });
  out.raised = false; out.isError = r.isError ?? null;
} catch (e) {
  out.raised = true; out.type = e?.constructor?.name; out.code = e?.code ?? null;
  out.message = String(e?.message ?? "");
  out.data_keys = e?.data ? Object.keys(e.data).sort() : null;
  out.postcondition = e?.data?.["io.stateguard/postcondition"] ?? null;
  out.execution_state = e?.data?.["io.stateguard/executionState"] ?? null;
}
try {
  const r2 = await client.callTool({ name: "read_text_file", arguments: { path: readPath } });
  out.followup = r2.isError ? "tool-error" : "success";
} catch (e) { out.followup = "raised " + (e?.constructor?.name ?? "?"); }
await client.close();
console.log(JSON.stringify(out));
"""


def _redact(text: str, sandbox: Path) -> str:
    return text.replace(str(sandbox), "<sandbox>")


def setup(ws: Path, fault: str) -> tuple[list[str], Path, Path]:
    trial = ws / f"trial-{fault}"
    trial.mkdir(parents=True, exist_ok=True)
    for f in trial.iterdir():
        f.unlink()
    sb = ws / "sandbox"
    fixture.reset(sb)
    (sb / fixture.ORDER).write_text(fixture.APPROVED_ORDER, encoding="utf-8")
    effects = trial / "effects.json"
    effects.write_text(json.dumps(relay_audit.effects("reconcile")), encoding="utf-8")
    cmd = [
        relay_audit.PY, str(relay_audit.RELAY), "--audit", str(trial / "audit.jsonl"),
        "--effects", str(effects), "--retry", "off", "--restart", "2",
        "--restart-policy", "after-unknown", "--",
        relay_audit.PY, str(relay_audit.SHIM), "--state", str(trial), "--sandbox", str(sb),
        "--fault", fault, "--", SERVER, str(sb),
    ]  # fmt: skip
    return cmd, sb, trial


def raw_client(cmd: list[str], sb: Path) -> dict[str, Any]:
    session = RawSession(cmd, env={"PATH": os.environ["PATH"], "HOME": str(sb.parent)})
    session.initialize()
    args = {"source": str(sb / fixture.ORDER), "destination": str(sb / fixture.DONE)}
    response = session.call("move_file", args, rid="T-1", timeout=30.0)
    follow = session.call("read_text_file", {"path": str(sb / fixture.LOG)}, rid="T-2")
    session.close()
    return {
        "response": json.loads(_redact(json.dumps(response), sb)),
        "followup": "success" if follow and "result" in follow else str(follow)[:80],
    }


async def python_client(cmd: list[str], sb: Path) -> dict[str, Any]:
    from mcp import Client, StdioServerParameters
    from mcp.shared.exceptions import MCPError

    params = StdioServerParameters(
        command=cmd[0], args=cmd[1:], env={"PATH": os.environ["PATH"], "HOME": str(sb.parent)}
    )
    out: dict[str, Any] = {}
    async with Client(params, read_timeout_seconds=30) as client:
        try:
            result = await client.call_tool(
                "move_file",
                {"source": str(sb / fixture.ORDER), "destination": str(sb / fixture.DONE)},
            )
            out.update(raised=False, isError=result.is_error)
        except MCPError as exc:
            data = exc.data if isinstance(exc.data, dict) else None
            out.update(
                raised=True,
                type=f"{type(exc).__module__}.{type(exc).__name__}",
                code=exc.code,
                message=_redact(exc.message, sb),
                str=_redact(str(exc), sb)[:300],
                data_keys=sorted(data) if data else None,
                postcondition=(data or {}).get("io.stateguard/postcondition"),
                execution_state=(data or {}).get("io.stateguard/executionState"),
            )
        except Exception as exc:  # noqa: BLE001 -- record what a client would see
            out.update(raised=True, type=f"{type(exc).__module__}.{type(exc).__name__}")
        try:
            r2 = await client.call_tool("read_text_file", {"path": str(sb / fixture.LOG)})
            out["followup"] = "tool-error" if r2.is_error else "success"
        except Exception as exc:  # noqa: BLE001
            out["followup"] = f"raised {type(exc).__name__}"
    return out


def ts_client(cmd: list[str], sb: Path, sdk: str, trial: Path) -> dict[str, Any]:
    script = trial / "ts_client.mjs"
    script.write_text(TS_CLIENT, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script), sdk, json.dumps(cmd), str(sb / fixture.ORDER),
         str(sb / fixture.DONE), str(sb / fixture.LOG)],
        capture_output=True, timeout=90, env={"PATH": os.environ["PATH"], "HOME": str(sb.parent)},
    )  # fmt: skip
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.startswith("{")]
    if not lines:
        return {"error": _redact(proc.stderr.decode()[-400:], sb), "exit": proc.returncode}
    out: dict[str, Any] = json.loads(_redact(lines[-1], sb))
    return out


SERVER = ""


def main() -> None:
    global SERVER
    SERVER, sdk, ws, out = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve(), sys.argv[4]
    ws.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for fault in ("drop_response", "swallow_request"):
        for client in ("raw", "python", "ts"):
            cmd, sb, trial = setup(ws, fault)
            if client == "raw":
                r = raw_client(cmd, sb)
            elif client == "python":
                r = asyncio.run(python_client(cmd, sb))
            else:
                r = ts_client(cmd, sb, sdk, trial)
            r["final_checkpoint"] = fixture.matching_checkpoint(fixture.snapshot(sb))
            results[f"{fault}/{client}"] = r
            print(fault, client, json.dumps(r)[:600], flush=True)
    fixture.reset(ws / "sandbox")
    Path(out).write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
