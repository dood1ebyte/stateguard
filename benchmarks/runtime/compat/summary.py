"""
Score compatibility-matrix rows (``matrix.py``), per server and cell (TEST-ONLY).

    summary.py ROWS.jsonl [--json OUT.json] -> a text table on stdout

Per server: whether the listing through the relay (shadow, auto) is identical to the direct one;
per cell, successes out of trials, the outcome kinds, and the unsafe outcomes; for ``keys/auto``,
the relay's launch plan (adapter, or why unkeyed), the tools advertised with keys, and the
server's idempotency outcome.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for server in sorted({r["server"] for r in rows}):
        mine = [r for r in rows if r["server"] == server]
        direct = {json.dumps(r.get("tools")) for r in mine if r["cell"] == "list/direct"}
        cells: dict[str, Any] = {}
        for cell in sorted({r["cell"] for r in mine}):
            these = [r for r in mine if r["cell"] == cell]
            entry: dict[str, Any] = {
                "trials": len(these),
                "successes": sum(1 for r in these if r.get("success")),
                "kinds": dict(collections.Counter(k for r in these for k in r.get("kinds", []))),
                "unsafe": sum(len(r.get("unsafe", [])) for r in these),
                "fault_fired": sum(1 for r in these if r.get("fault_fired")),
                "errors": [r["error"] for r in these if r.get("error")],
            }
            if cell.startswith("list/") and cell != "list/direct":
                entry["listing_identical"] = sum(
                    1 for r in these if json.dumps(r.get("tools")) in direct and len(direct) == 1
                )
            if cell == "keys/auto":
                plans = {json.dumps(r.get("keys_plan"), sort_keys=True) for r in these}
                entry["keys_plan"] = [json.loads(p) for p in sorted(plans)]
                entry["keys_advertised"] = sorted(
                    {t for r in these for t in r.get("keys_advertised") or []}
                )
                entry["keys_not_advertised"] = sorted(
                    {t for r in these for t in r.get("keys_not_advertised") or []}
                )
                entry["server_outcomes"] = dict(
                    collections.Counter(str(r.get("server_outcome")) for r in these)
                )
            cells[cell] = entry
        out[server] = {
            "rows": len(mine),
            "direct_listings": len(direct),
            "tools": json.loads(next(iter(direct))) if len(direct) == 1 else None,
            "unsafe": sum(c["unsafe"] for c in cells.values()),
            "driver_errors": sum(1 for r in mine if "driver_error" in r.get("kinds", [])),
            "cells": cells,
        }
    return out


def render(summary: dict[str, Any]) -> str:
    lines = []
    for server, s in summary.items():
        tools = s["tools"]
        lines.append(
            f"===== {server}  rows {s['rows']}  unsafe {s['unsafe']}  "
            f"driver_errors {s['driver_errors']}  tools {len(tools) if tools else '?'}"
        )
        for cell, c in s["cells"].items():
            extra = ""
            if "listing_identical" in c:
                extra = f"  listing identical {c['listing_identical']}/{c['trials']}"
            if cell == "keys/auto":
                plan = c["keys_plan"]
                adapters = sorted({str(p.get("adapter")) if p else "None" for p in plan})
                reasons = sorted(
                    {p.get("unkeyed_reason") for p in plan if p and p.get("unkeyed_reason")}
                )
                extra = (
                    f"  adapter {adapters} advertised {c['keys_advertised']} "
                    f"not_advertised {c['keys_not_advertised']} outcomes {c['server_outcomes']}"
                    + (f" unkeyed: {reasons}" if reasons else "")
                )
            lines.append(
                f"  {cell:28s} {c['successes']}/{c['trials']}  fired {c['fault_fired']}  "
                f"unsafe {c['unsafe']}  kinds {c['kinds']}{extra}"
            )
            for e in c["errors"][:2]:
                lines.append(f"      error: {e}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("rows")
    p.add_argument("--json")
    a = p.parse_args()
    rows = [json.loads(x) for x in Path(a.rows).read_text().splitlines() if x.strip()]
    summary = summarize(rows)
    if a.json:
        Path(a.json).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(render(summary))


if __name__ == "__main__":
    main()
