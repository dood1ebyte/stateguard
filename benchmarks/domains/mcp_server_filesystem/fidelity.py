"""
Fidelity and privacy checks for a capture workspace written by ``workload.py``.

usage: fidelity.py WORKSPACE OUT_JSON

Expects three passes over the same tests in the same WORKSPACE (identical paths,
so server output is byte-comparable):

``results-raw.json``     client -> server, no relay
``results-shadow.json``  client -> relay --shadow (recorded, WORKSPACE/rec/)
``results-tee.json``     client -> relay --shadow -> tee shim -> server (WORKSPACE/tee/)

Checks, all on digests (no content is read except to search the private files
for leaked values):

1. client view: the lines the client sent and received are byte-identical with
   and without the relay;
2. both directions through the relay (tee pass): every client line reached the
   server byte-identical and in order, every server line reached the client
   byte-identical and in order; the only extra lines are the relay's own
   ``tools/list`` requests and their responses, which the client never sees;
3. the recording's own integrity and digest-based comparison (evidence report);
4. privacy: no fixture value, sandbox path or file content appears in any
   recording or audit log.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fixture  # noqa: E402

REPO = HERE.parents[2]


def by_tag(path: Path) -> dict[str, dict[str, Any]]:
    return {f"{r['test']}-{r['rep']}": r for r in json.loads(path.read_text(encoding="utf-8"))}


def directions(wire: list[list[list[str]]], d: str) -> list[str]:
    return [h for session in wire for (dd, h) in session if dd == d]


def tee_check(client: dict[str, Any], tee_file: Path) -> dict[str, Any]:
    entries = [json.loads(line) for line in tee_file.read_text(encoding="utf-8").splitlines()]
    c2s_client = directions(client["client_wire"], "c2s")
    s2c_client = directions(client["client_wire"], "s2c")
    client_hashes = set(c2s_client)
    own_ids = {
        e["id"]
        for e in entries
        if e["dir"] == "c2s" and e["sha256"] not in client_hashes and e.get("id") is not None
    }
    own_methods = sorted(
        {e.get("method") for e in entries if e["dir"] == "c2s" and e.get("id") in own_ids}
    )
    c2s_server = [e["sha256"] for e in entries if e["dir"] == "c2s" and e.get("id") not in own_ids]
    s2c_server = [
        e["sha256"]
        for e in entries
        if e["dir"] == "s2c" and not (e.get("id") in own_ids and e.get("method") is None)
    ]
    return {
        "client_to_server_identical_in_order": c2s_server == c2s_client,
        "server_to_client_identical_in_order": s2c_server == s2c_client,
        "client_lines": len(c2s_client),
        "server_lines_to_client": len(s2c_client),
        "relay_own_requests": len(own_ids),
        "relay_own_methods": own_methods,
    }


def evidence(rec: Path) -> dict[str, Any]:
    out = subprocess.run(
        [sys.executable, str(REPO / "examples" / "mcp" / "evidence_report.py"), str(rec), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "exit": out.returncode,
        "summary": json.loads(out.stdout) if out.stdout.strip() else None,
    }


def needles(ws: Path) -> list[str]:
    values = [str(ws), str(ws / "sandbox"), fixture.LOG_LINE, fixture.SENTINEL_TEXT.strip()]
    for text in fixture.FILES.values():
        values += [line.strip() for line in text.splitlines() if line.strip()]
    values += ["status: approved", "mode = live", "Successfully moved", "Access denied"]
    return sorted(set(values))


def shape(text: str) -> str:
    """The recorder's shape-preserving mask: letters -> a / A, digits -> 1."""
    return "".join(
        "A" if c.isupper() else "a" if c.isalpha() else "1" if c.isdigit() else c for c in text
    )


def leaks(path: Path, values: list[str]) -> dict[str, Any]:
    """Which values occur, and -- for an audit log -- in which events (never the content)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    hits: dict[str, Any] = {v: text.count(v) for v in values if v in text}
    if hits and path.parent.name == "audit":
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        hits["_events"] = sorted(
            {e.get("event") for e in events if any(v in json.dumps(e) for v in values)}
        )
    return hits


def main() -> None:
    ws, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    raw, shadow, tee = (by_tag(ws / f"results-{n}.json") for n in ("raw", "shadow", "tee"))
    report: dict[str, Any] = {"tests": {}, "privacy": {}}
    for tag in sorted(raw):
        r, s, t = raw[tag], shadow[tag], tee[tag]
        tee_file = ws / "tee" / f"{r['test']}-shadow-{r['rep']}.jsonl"
        rec = ws / "rec" / f"{r['test']}-shadow-{r['rep']}.jsonl"
        report["tests"][tag] = {
            "client_view_raw_eq_shadow": r["client_wire"] == s["client_wire"],
            "client_view_raw_eq_tee": r["client_wire"] == t["client_wire"],
            "same_ground_truth": r["workflow_state"] == s["workflow_state"] == t["workflow_state"]
            and r["disk_checkpoint"] == s["disk_checkpoint"] == t["disk_checkpoint"],
            "tee": tee_check(t, tee_file),
            "evidence": evidence(rec),
        }
    values = needles(ws)
    # A value the mask maps to itself (e.g. "[a]") cannot tell a leak from masked output.
    uncheckable = sorted(v for v in values if shape(v) == v)
    values = [v for v in values if shape(v) != v]
    for f in sorted((ws / "rec").glob("*.jsonl")) + sorted((ws / "audit").glob("*shadow*.jsonl")):
        hit = leaks(f, values)
        report["privacy"][f"{f.parent.name}/{f.name}"] = hit
    report["privacy_values_checked"] = len(values)
    report["privacy_values_uncheckable"] = uncheckable
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    ok = all(
        v["client_view_raw_eq_shadow"]
        and v["client_view_raw_eq_tee"]
        and v["same_ground_truth"]
        and v["tee"]["client_to_server_identical_in_order"]
        and v["tee"]["server_to_client_identical_in_order"]
        for v in report["tests"].values()
    )
    leaked = {k: v for k, v in report["privacy"].items() if v}
    print(
        f"fidelity ok: {ok}; tests: {len(report['tests'])}; "
        f"files scanned: {len(report['privacy'])}; files with a leaked value: {len(leaked)}"
    )
    for tag, v in report["tests"].items():
        ev, tv = v["evidence"], v["tee"]
        print(
            f"  {tag:6} raw==shadow {v['client_view_raw_eq_shadow']!s:5} "
            f"raw==tee {v['client_view_raw_eq_tee']!s:5} "
            f"c2s {tv['client_to_server_identical_in_order']!s:5} "
            f"s2c {tv['server_to_client_identical_in_order']!s:5} "
            f"own={tv['relay_own_requests']}{tv['relay_own_methods']} evidence-exit={ev['exit']}"
        )
    for k, v in leaked.items():
        print(
            "  HIT", k, sorted(x for x in v if x != "_events"), v.get("_events", "")
        )  # which value, never content
    print("  values the mask maps to themselves (not checkable):", uncheckable)


if __name__ == "__main__":
    main()
