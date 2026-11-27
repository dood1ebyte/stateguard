#!/bin/bash
# Time a clean install, from nothing to the first call through StateGuard (Phase 2 S5.2).
# usage: scripts/clean_machine.sh WHEEL  -> one JSON line of timings on stdout
# A fresh HOME, a new venv, empty pip and npm caches; the protected server is downloaded too.
set -euo pipefail
WHEEL=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
WORK=$(mktemp -d)
export HOME=$WORK/home PIP_NO_CACHE_DIR=1 npm_config_cache=$WORK/npm-cache
mkdir -p "$HOME" "$WORK/notes"
now() { python3 -c 'import time; print(time.monotonic())'; }
t0=$(now)
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q "$WHEEL"
t1=$(now)
mkdir "$WORK/server" && (cd "$WORK/server" && npm init -y >/dev/null && \
  npm install --no-audit --no-fund -q @modelcontextprotocol/server-filesystem@2026.8.31 >/dev/null)
SERVER=(node "$WORK/server/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js" "$WORK/notes")
t2=$(now)
"$WORK/venv/bin/stateguard" doctor -- "${SERVER[@]}" > "$WORK/doctor.txt"
t3=$(now)
cat > "$WORK/effects.json" <<JSON
{"tools": [{"name": "write_file", "effect": "non_idempotent"}, {"name": "read_text_file", "effect": "read_only"}]}
JSON
"$WORK/venv/bin/python" - "$WORK" "${SERVER[@]}" > "$WORK/call.json" <<'PY'
import json, subprocess, sys
work, server = sys.argv[1], sys.argv[2:]
relay = [f"{work}/venv/bin/stateguard", "relay", "stdio", "--retry", "auto", "--effects",
         f"{work}/effects.json", "--audit", f"{work}/audit.jsonl", "--", *server]
p = subprocess.Popen(relay, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def ask(i, method, params):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
    p.stdin.flush()
    while True:
        m = json.loads(p.stdout.readline())
        if m.get("id") == i:
            return m
ask(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "first-call", "version": "1"}})
p.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); p.stdin.flush()
path = f"{work}/notes/hello.md"
w = ask(2, "tools/call", {"name": "write_file", "arguments": {"path": path, "content": "first protected call\n"}})
r = ask(3, "tools/call", {"name": "read_text_file", "arguments": {"path": path}})
p.stdin.close(); p.wait(timeout=30)
ok = (not w["result"].get("isError")) and "first protected call" in r["result"]["content"][0]["text"]
print(json.dumps({"write_ok": not w["result"].get("isError"), "read_ok": ok}))
PY
t4=$(now)
python3 - "$t0" "$t1" "$t2" "$t3" "$t4" "$WORK/call.json" "$WORK/doctor.txt" <<'PY'
import json, sys
t = [float(x) for x in sys.argv[1:6]]
call = json.load(open(sys.argv[6]))
doctor = open(sys.argv[7]).read().strip().splitlines()[-1]
print(json.dumps({"install_stateguard_s": round(t[1] - t[0], 1), "install_server_s": round(t[2] - t[1], 1),
                  "doctor_s": round(t[3] - t[2], 1), "first_call_s": round(t[4] - t[3], 1),
                  "total_s": round(t[4] - t[0], 1), "doctor": doctor, **call}))
PY
rm -rf "$WORK"
