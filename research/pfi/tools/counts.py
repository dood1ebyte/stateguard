"""
Track B counts (PHASE2_PLAN.md sections 10-11): report counts, never rates.

    python -m research.pfi.tools.counts CORPUS.jsonl

Counts independent records (``duplicate_of`` null) by classification, category, severity and
StateGuard capability. A count is "N independent public reports found by the documented
queries between dates D1 and D2"; it is not a failure rate, a denominator, or evidence of any
percentage about StateGuard.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path
from typing import Any


def counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    independent = [r for r in records if r.get("duplicate_of") is None]
    out: dict[str, Any] = {"records": len(records), "independent": len(independent)}
    for field in ("classification", "category", "severity", "capability", "status"):
        out[field] = dict(collections.Counter(str(r.get(field)) for r in independent))
    return out


def main(argv: list[str]) -> int:
    path = Path(argv[0])
    records = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    print(json.dumps(counts(records), indent=1, sort_keys=True))
    print("These are report counts, not rates (PHASE2_PLAN.md section 11).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
