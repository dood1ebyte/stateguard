"""
Offline characterization of mcp-server-filesystem (no StateGuard in the path).

usage: probe.py SERVER_BIN WORKSPACE OUT_JSON

WORKSPACE must be a private scratch directory; the probe creates
WORKSPACE/sandbox (the server's only allowed directory) and WORKSPACE/other
(used only by the roots probe). Every mutating probe runs on a freshly reset
sandbox and records the on-disk effect by hashing files directly.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402
from rawclient import RawSession, outcome  # noqa: E402

SERVER, WS, OUT = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
SANDBOX, OTHER = WS / "sandbox", WS / "other"
ENV = {"PATH": os.environ["PATH"], "HOME": str(WS)}


def session(extra_dirs: list[Path] | None = None) -> RawSession:
    dirs = [str(SANDBOX)] + [str(d) for d in (extra_dirs or [])]
    return RawSession([SERVER, *dirs], env=ENV, cwd=str(WS))


def summarize(resp: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"wire": outcome(resp)}
    if resp and "error" in resp:
        out["code"] = resp["error"].get("code")
        out["message"] = str(resp["error"].get("message"))[:160]
    elif resp and "result" in resp:
        r = resp["result"]
        texts = [c.get("text", "") for c in r.get("content", []) if isinstance(c, dict)]
        out["text"] = (texts[0] if texts else "")[:160]
        sc = r.get("structuredContent")
        out["structured"] = None if sc is None else {k: type(v).__name__ for k, v in sc.items()}
        out["structured_equals_text"] = bool(sc) and texts[:1] == [sc.get("content")]
        out["result_keys"] = sorted(r)
    return out


def effect(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {
        "created": sorted(set(after) - set(before)),
        "deleted": sorted(set(before) - set(after)),
        "modified": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
    }


results: list[dict[str, Any]] = []


def record(pid: str, category: str, hypothesis: str, **data: Any) -> None:
    results.append({"id": pid, "category": category, "hypothesis": hypothesis, **data})
    print(f"{pid:5} {category:10} {json.dumps(data, default=str)[:260]}")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
fixture.reset(SANDBOX)

s = session()
record(
    "L1",
    "lifecycle",
    "tools/list before initialize is rejected",
    **summarize(s.request("tools/list")),
)
s.close()

s = session()
init = s.request(
    "initialize",
    {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
)
record(
    "L2",
    "lifecycle",
    "initialize negotiates 2025-11-25",
    wire=outcome(init),
    protocol=(init or {}).get("result", {}).get("protocolVersion"),
    server=(init or {}).get("result", {}).get("serverInfo"),
    capabilities=(init or {}).get("result", {}).get("capabilities"),
)
record(
    "L3",
    "lifecycle",
    "tools/list before notifications/initialized",
    **summarize(s.request("tools/list")),
)
s.notify("notifications/initialized")
record(
    "L4",
    "lifecycle",
    "SDK 2.x server/discover probe on a TS SDK 1.32 server",
    **summarize(s.request("server/discover")),
)
record("L5", "lifecycle", "unknown method", **summarize(s.request("foo/bar")))
s.send("{not json")
time.sleep(0.3)
record(
    "L6", "lifecycle", "malformed JSON line", answered=s._lines.qsize() > 0, unparsed=s.unparsed[:1]
)
record("L7", "lifecycle", "ping", **summarize(s.request("ping")))
t0 = time.monotonic()
rc = s.close()
record(
    "L8",
    "lifecycle",
    "stdin close ends the server",
    exit_code=rc,
    seconds=round(time.monotonic() - t0, 2),
)

s = session()
old = s.request(
    "initialize",
    {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
)
record(
    "L9",
    "lifecycle",
    "older protocol version is negotiated, not refused",
    wire=outcome(old),
    protocol=(old or {}).get("result", {}).get("protocolVersion"),
)
s.close()

# Roots: a client that declares roots replaces the command-line allowed directory?
OTHER.mkdir(exist_ok=True)
(OTHER / "other.txt").write_text("other root\n", encoding="utf-8")


class RootsSession(RawSession):
    def wait_for(self, rid: Any, timeout: float = 15.0) -> dict[str, Any] | None:
        while True:
            item = self._lines.get(timeout=timeout)
            if item is None:
                return None
            if item.get("method") == "roots/list" and "id" in item:
                self.roots_requested = True
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": item["id"],
                        "result": {"roots": [{"uri": OTHER.as_uri(), "name": "other"}]},
                    }
                )
                continue
            if item.get("id") == rid and "method" not in item:
                return item


rs = RootsSession([SERVER, str(SANDBOX)], env=ENV, cwd=str(WS))
rs.roots_requested = False
rs.request(
    "initialize",
    {
        "protocolVersion": "2025-11-25",
        "capabilities": {"roots": {"listChanged": True}},
        "clientInfo": {"name": "probe", "version": "0"},
    },
)
rs.notify("notifications/initialized")
time.sleep(0.5)
allowed = summarize(rs.call("list_allowed_directories", {}))
in_sandbox = summarize(rs.call("read_text_file", {"path": str(SANDBOX / fixture.ORDER)}))
in_other = summarize(rs.call("read_text_file", {"path": str(OTHER / "other.txt")}))
rs.close()
record(
    "L10",
    "lifecycle",
    "client roots replace the command-line sandbox",
    roots_requested=rs.roots_requested,
    allowed=allowed.get("text"),
    read_sandbox=in_sandbox["wire"],
    read_other=in_other["wire"],
)

# ---------------------------------------------------------------------------
# Contract / validation and results (read-only unless noted)
# ---------------------------------------------------------------------------
P = str(SANDBOX / fixture.ORDER)
READ_ONLY = [
    ("C1", "valid read", "read_text_file", {"path": P}),
    ("C2", "missing required path", "read_text_file", {}),
    ("C3", "integer for string path", "read_text_file", {"path": 123}),
    ("C4", "undeclared extra key", "read_text_file", {"path": P, "verbose": True}),
    ("C5", "numeric string for number head", "read_text_file", {"path": P, "head": "2"}),
    ("C6", "fractional head", "read_text_file", {"path": P, "head": 1.5}),
    ("C7", "integral float head", "read_text_file", {"path": P, "head": 2.0}),
    (
        "C8",
        "invalid enum sortBy",
        "list_directory_with_sizes",
        {"path": str(SANDBOX), "sortBy": "SIZE"},
    ),
    (
        "C9",
        "valid enum sortBy",
        "list_directory_with_sizes",
        {"path": str(SANDBOX), "sortBy": "size"},
    ),
    ("C10", "path outside the sandbox", "read_text_file", {"path": str(WS / fixture.SENTINEL)}),
    ("C11", "nonexistent path in sandbox", "read_text_file", {"path": str(SANDBOX / "nope.txt")}),
    ("C12", "unknown tool", "no_such_tool", {"path": P}),
    ("C13", "scalar for array paths", "read_multiple_files", {"paths": P}),
    (
        "C14",
        "one bad path among good ones",
        "read_multiple_files",
        {"paths": [P, str(SANDBOX / "nope.txt")]},
    ),
    ("C15", "relative path", "read_text_file", {"path": fixture.ORDER}),
    ("C16", "path traversal", "read_text_file", {"path": str(SANDBOX / ".." / fixture.SENTINEL)}),
    ("C17", "arguments omitted", "list_allowed_directories", ...),
    ("C18", "head and tail together", "read_text_file", {"path": P, "head": 1, "tail": 1}),
]
s = session()
s.initialize()
for pid, label, tool, args in READ_ONLY:
    before = fixture.snapshot(SANDBOX)
    res = summarize(s.call(tool, args))
    record(
        pid, "contract", label, tool=tool, **res, effect=effect(before, fixture.snapshot(SANDBOX))
    )
s.close()

# ---------------------------------------------------------------------------
# Effects: each mutation, then the identical request replayed (fresh sandbox each)
# ---------------------------------------------------------------------------
LOGP, ORDP, DONEP = (
    str(SANDBOX / fixture.LOG),
    str(SANDBOX / fixture.ORDER),
    str(SANDBOX / fixture.DONE),
)
MUTATIONS = [
    (
        "E1",
        "write_file new file, replayed",
        "write_file",
        {"path": str(SANDBOX / "new.txt"), "content": "x\n"},
    ),
    ("E2", "write_file overwrite, replayed", "write_file", {"path": ORDP, "content": "replaced\n"}),
    (
        "E3",
        "edit_file replace once, replayed",
        "edit_file",
        {"path": ORDP, "edits": [{"oldText": "status: pending", "newText": "status: approved"}]},
    ),
    (
        "E4",
        "edit_file append-before-marker, replayed",
        "edit_file",
        {"path": LOGP, "edits": [{"oldText": "END", "newText": "approved order 001\nEND"}]},
    ),
    (
        "E5",
        "edit_file dryRun, replayed",
        "edit_file",
        {
            "path": ORDP,
            "edits": [{"oldText": "status: pending", "newText": "status: approved"}],
            "dryRun": True,
        },
    ),
    ("E6", "move_file, replayed", "move_file", {"source": ORDP, "destination": DONEP}),
    ("E7", "create_directory, replayed", "create_directory", {"path": str(SANDBOX / "a" / "b")}),
    (
        "E8",
        "edit_file with an ambiguous whitespace match",
        "edit_file",
        {
            "path": str(SANDBOX / "config/settings.ini"),
            "edits": [{"oldText": "mode = draft", "newText": "mode = live"}],
        },
    ),
    (
        "E9",
        "edit_file with one bad edit among two",
        "edit_file",
        {
            "path": ORDP,
            "edits": [
                {"oldText": "status: pending", "newText": "status: approved"},
                {"oldText": "no such text", "newText": "x"},
            ],
        },
    ),
    (
        "E10",
        "write_file outside the sandbox",
        "write_file",
        {"path": str(WS / "escape.txt"), "content": "x"},
    ),
    (
        "E11",
        "edit_file edits item missing newText",
        "edit_file",
        {"path": ORDP, "edits": [{"oldText": "status: pending"}]},
    ),
]
for pid, label, tool, args in MUTATIONS:
    fixture.reset(SANDBOX)
    s = session()
    s.initialize()
    s0 = fixture.snapshot(SANDBOX)
    first = summarize(s.call(tool, args))
    s1 = fixture.snapshot(SANDBOX)
    second = summarize(s.call(tool, args))
    s2 = fixture.snapshot(SANDBOX)
    s.close()
    escaped = (WS / "escape.txt").exists()
    record(
        pid,
        "effect",
        label,
        tool=tool,
        first=first,
        first_effect=effect(s0, s1),
        replay=second,
        replay_effect=effect(s1, s2),
        state_after_replay_equals_after_first=(s1 == s2),
        escaped=escaped,
    )
fixture.reset(SANDBOX)
Path(OUT).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
print("probes:", len(results))
