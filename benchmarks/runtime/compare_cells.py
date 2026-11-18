"""
Compare two recorded batches cell by cell (TEST-ONLY harness).

    python benchmarks/runtime/compare_cells.py NEW.json REF.json [REF2.json ...] [--json OUT]

Each file is a ``summary*.json`` written by ``realhttp/summarize.py`` (both suites use it).
For every (server, arm, cell) in NEW, the reference is the *last* REF file that has that
server and arm (so ``B5 B6`` compares keyed arms with B6 and the others with B5). A cell is
``[failures, trials]``. Output: one line per difference, then the totals per arm; exit 0.

Baseline arms (raw, naive) never run the relay; a difference there is labelled ``baseline``
so it is reported, never hidden, and never mistaken for a relay change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BASELINE = ("raw", "naive")


def _cells(summary: dict[str, Any]) -> dict[tuple[str, str], dict[str, list[int]]]:
    out: dict[tuple[str, str], dict[str, list[int]]] = {}
    for server, arms in summary.get("servers", {}).items():
        for arm, data in arms.items():
            out[(server, arm)] = {cell: list(v) for cell, v in data.get("cells", {}).items()}
    return out


def _unsafe(summary: dict[str, Any]) -> dict[tuple[str, str], dict[str, int]]:
    return {
        (server, arm): dict(data.get("unsafe", {}))
        for server, arms in summary.get("servers", {}).items()
        for arm, data in arms.items()
    }


def compare(new: Path, refs: list[Path]) -> dict[str, Any]:
    new_summary = json.loads(new.read_text())
    ref_summaries = [(r, json.loads(r.read_text())) for r in refs]
    new_cells = _cells(new_summary)
    report: dict[str, Any] = {"new": str(new), "refs": [str(r) for r in refs], "arms": []}
    for (server, arm), cells in sorted(new_cells.items()):
        ref_name, ref_cells = None, None
        for path, summary in ref_summaries:
            found = _cells(summary).get((server, arm))
            if found is not None:
                ref_name, ref_cells = path, found
        entry: dict[str, Any] = {
            "server": server,
            "arm": arm,
            "kind": "baseline" if arm in BASELINE else "stateguard",
            "ref": str(ref_name) if ref_name else None,
            "cells": len(cells),
            "differences": [],
            "unsafe": _unsafe(new_summary)[(server, arm)],
        }
        if ref_cells is None:
            entry["differences"].append({"cell": "*", "new": None, "ref": "no reference"})
        else:
            for cell in sorted(set(cells) | set(ref_cells)):
                if cells.get(cell) != ref_cells.get(cell):
                    entry["differences"].append(
                        {"cell": cell, "new": cells.get(cell), "ref": ref_cells.get(cell)}
                    )
        entry["new_total"] = [sum(v[0] for v in cells.values()), sum(v[1] for v in cells.values())]
        if ref_cells is not None:
            entry["ref_total"] = [
                sum(v[0] for v in ref_cells.values()),
                sum(v[1] for v in ref_cells.values()),
            ]
        report["arms"].append(entry)
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("new")
    parser.add_argument("refs", nargs="+")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    report = compare(Path(args.new), [Path(r) for r in args.refs])
    for entry in report["arms"]:
        status = "MATCH" if not entry["differences"] else "DIFF"
        ref = Path(entry["ref"]).parent.name if entry["ref"] else "-"
        unsafe = sum(entry["unsafe"].values())
        print(
            f"{status:5} {entry['server']:11} {entry['arm']:20} {entry['kind']:10} "
            f"new {entry['new_total'][0]}/{entry['new_total'][1]}  "
            f"ref({ref}) {entry.get('ref_total', ['-', '-'])[0]}/"
            f"{entry.get('ref_total', ['-', '-'])[1]}  cells {entry['cells']}  unsafe {unsafe}"
        )
        for d in entry["differences"]:
            print(f"      {d['cell']:28} new {d['new']}  ref {d['ref']}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
