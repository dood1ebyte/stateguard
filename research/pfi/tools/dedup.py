"""
Track B deduplication (PHASE2_PLAN.md section 10): reports of the same root issue become one
record with every URL.

    python -m research.pfi.tools.dedup CORPUS.jsonl [--write]

Two records are the same root issue when any of:
- they share a URL (after normalising: no fragment, query or trailing slash);
- one is already marked ``duplicate_of`` the other;
- they name the same project and version and the same error signature (``signature``: an
  optional lower-cased error string we record, e.g. "connection closed").

The kept record is the earliest id; it gains every URL, and the others are marked
``duplicate_of`` it (they stay in the corpus, so the merge is auditable, and are never counted
as independent reports). Recurrence is the number of records in a group, a count, never a rate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def normalise(url: str) -> str:
    url = url.split("#", 1)[0].split("?", 1)[0] if "news.ycombinator.com" not in url else url
    return url.rstrip("/").lower()


def groups(records: list[dict[str, Any]]) -> list[list[str]]:
    """The ids of each root issue, earliest first (union-find over the three rules)."""
    parent = {r["id"]: r["id"] for r in records}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_url: dict[str, str] = {}
    by_signature: dict[tuple[str, str, str], str] = {}
    for record in records:
        ident = record["id"]
        for url in record.get("urls") or []:
            key = normalise(url)
            if key in by_url:
                union(by_url[key], ident)
            else:
                by_url[key] = ident
        target = record.get("duplicate_of")
        if isinstance(target, str) and target in parent:
            union(target, ident)
        signature = record.get("signature")
        if isinstance(signature, str) and signature.strip():
            key3 = (
                str(record.get("project")).lower(),
                str(record.get("version")).lower(),
                signature.strip().lower(),
            )
            if key3 in by_signature:
                union(by_signature[key3], ident)
            else:
                by_signature[key3] = ident
    out: dict[str, list[str]] = {}
    for record in records:
        out.setdefault(find(record["id"]), []).append(record["id"])
    return [sorted(ids) for ids in sorted(out.values(), key=lambda ids: min(ids))]


def merge(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The corpus with each group merged into its earliest record (others marked duplicate)."""
    by_id = {r["id"]: dict(r) for r in records}
    for ids in groups(records):
        keep, rest = ids[0], ids[1:]
        urls = list(dict.fromkeys(u for i in ids for u in by_id[i].get("urls") or []))
        by_id[keep]["urls"] = urls
        by_id[keep]["duplicate_of"] = None
        for other in rest:
            by_id[other]["duplicate_of"] = keep
    return [by_id[r["id"]] for r in records]


def main(argv: list[str]) -> int:
    path = Path(argv[0])
    records = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    found = groups(records)
    for ids in found:
        if len(ids) > 1:
            print(f"one root issue: {', '.join(ids)}")
    print(f"{len(records)} records, {len(found)} root issues")
    if "--write" in argv:
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in merge(records)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
