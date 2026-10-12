"""
Summarize a relay recording as evidence: observed facts, StateGuard's
observations, its (hypothetical) proposals, and unknown outcomes -- apart.

    python examples/mcp/evidence_report.py capture.jsonl [--json]

Reads the file only; contacts nothing. Exit status 2 when the recording is
malformed, 1 when it is incomplete (the recorder stopped early or was never
closed), else 0. See docs/runtime/REAL_CAPTURE.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from stateguard.adapters.mcp import RecordingError, read_recording, summarize_evidence


def _lines(summary: dict[str, Any]) -> list[str]:
    out = [
        f"recording   {summary['session']}  ({summary['provenance']}, mode "
        f"{summary['mode']}, redaction {summary['redaction']})",
        f"evidence    {summary['evidence']}",
    ]
    if summary["started_at"]:
        out.append(f"started     {summary['started_at']}")
    for section, title in (
        ("integrity", "integrity"),
        ("wire", "on the wire (happened)"),
        ("observed", "observed failures (happened)"),
        ("stateguard_observed", "StateGuard observed"),
        (
            "proposed",
            "proposed by StateGuard (hypothetical: did NOT happen in shadow mode)"
            if summary["proposed"]["hypothetical"]
            else f"StateGuard decisions (mode {summary['mode']}: NOT shadow; these were applied)",
        ),
        ("unknown", "unknown outcomes (neither success nor failure)"),
    ):
        out.append("")
        out.append(title)
        for key, value in summary[section].items():
            out.append(f"  {key:34} {json.dumps(value, sort_keys=True)}")
    out.append("")
    out.append("not claimed")
    out.extend(f"  - {line}" for line in summary["not_claimed"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument("recording", help="A stateguard.mcp.recording file")
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    args = parser.parse_args(argv)
    try:
        recording = read_recording(args.recording)
    except RecordingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    summary = summarize_evidence(recording)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print("\n".join(_lines(summary)))
    if not recording.complete:
        print("warning: recording is incomplete; counts may be missing records", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
