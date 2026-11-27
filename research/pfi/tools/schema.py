"""
Track B record schema and validator (PHASE2_PLAN.md section 10). One record per signal.

    python -m research.pfi.tools.schema CORPUS.jsonl   -> exit 1 and the reasons if any record fails

Every field is required. A field we could not establish holds the explicit string
"not available" (never a guess), except the fields that identify the record and say how it was
found (id, source_platform, urls, description, classification, collected, query), which must
be real values. The repository is public: records hold URLs, our own short paraphrases and
classifications, and no personal data.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

NA = "not available"
PLATFORMS = ("github", "reddit", "hackernews", "indiehackers", "other")
CATEGORIES = tuple("ABCDEFGHIJK") + ("Z", "outside the taxonomy")
REPRODUCTION = ("yes", "no", "partial")
STATUS = ("open", "resolved", "recurring", "unclear")
SEVERITY = ("critical", "high", "medium", "low")
CAPABILITY = ("prevents", "detects", "recovers", "cannot handle", "unclear")
CONTROLLED = ("yes", "no", "unknown")
CLASSIFICATION = (
    "confirmed technical failure",
    "likely technical failure",
    "feature request",
    "speculation",
    "opinion",
    "unrelated",
)
CONFIDENCE = ("high", "medium", "low")
#: field -> allowed values (None: free text). "not available" is allowed unless NOT_NA.
FIELDS: dict[str, tuple[str, ...] | None] = {
    "id": None,
    "source_platform": PLATFORMS,
    "urls": None,
    "report_date": None,
    "last_activity_date": None,
    "project": None,
    "version": None,
    "description": None,
    "reproduction": REPRODUCTION,
    "category": CATEGORIES,
    "status": STATUS,
    "severity": SEVERITY,
    "developer_impact": None,
    "capability": CAPABILITY,
    "capability_basis": None,
    "controlled_reproduction": CONTROLLED,
    "controlled_reproduction_note": None,
    "classification": CLASSIFICATION,
    "confidence": CONFIDENCE,
    "confidence_reason": None,
    "duplicate_of": None,
    "collected": None,
    "query": None,
}
NOT_NA = {"id", "source_platform", "urls", "description", "classification", "collected", "query"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID = re.compile(r"^PFI-\d{4}$")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
#: A URL to a signal (an issue, a pull request, a discussion, a post), never to a profile.
SIGNAL_URL = re.compile(
    r"^https://(github\.com/[\w.-]+/[\w.-]+/(issues|pull|discussions)/\d+"
    r"|(www\.|old\.)?reddit\.com/r/[\w-]+/comments/\w+"
    r"|news\.ycombinator\.com/item\?id=\d+"
    r"|www\.indiehackers\.com/post/[\w-]+"
    r"|[\w.-]+/.+)$"
)
PROFILE_URL = re.compile(
    r"^https://(github\.com/[\w-]+/?$|(www\.)?reddit\.com/u(ser)?/|"
    r"news\.ycombinator\.com/user\?)"
)
MAX_DESCRIPTION = 400


def problems(record: Any) -> list[str]:
    """Why *record* is not a valid Track B record (empty: it is)."""
    if not isinstance(record, dict):
        return ["not an object"]
    out = []
    missing = [name for name in FIELDS if name not in record]
    out += [f"missing field {name!r}" for name in missing]
    out += [f"unknown field {name!r}" for name in record if name not in FIELDS]
    for name, allowed in FIELDS.items():
        if name not in record:
            continue
        value = record[name]
        if value == NA:
            if name in NOT_NA:
                out.append(f"{name}: must be known, not {NA!r}")
            continue
        if name == "urls":
            if not isinstance(value, list) or not value:
                out.append("urls: a non-empty list")
                continue
            for url in value:
                if not isinstance(url, str) or not SIGNAL_URL.match(url) or PROFILE_URL.match(url):
                    out.append(f"urls: not a signal URL: {url!r}")
            continue
        if name == "duplicate_of":
            if value is not None and not (isinstance(value, str) and ID.match(value)):
                out.append("duplicate_of: null or a record id")
            continue
        if not isinstance(value, str) or not value.strip():
            out.append(f"{name}: a non-empty string")
            continue
        if allowed is not None and value not in allowed:
            out.append(f"{name}: {value!r} is not one of {list(allowed)}")
        if name == "id" and not ID.match(value):
            out.append("id: PFI-NNNN")
        if name in ("report_date", "last_activity_date", "collected") and not DATE.match(value):
            out.append(f"{name}: YYYY-MM-DD or {NA!r}")
        if name == "description" and len(value) > MAX_DESCRIPTION:
            out.append(f"description: our own short paraphrase, at most {MAX_DESCRIPTION} chars")
        if EMAIL.search(value):
            out.append(f"{name}: looks like it holds an email address")
    return out


def validate_file(path: str | Path) -> dict[str, list[str]]:
    """{record id or line number: problems} for every failing record of a JSONL corpus."""
    failing: dict[str, list[str]] = {}
    seen: set[str] = set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            failing[f"line {number}"] = [f"not JSON: {exc.msg}"]
            continue
        found = problems(record)
        ident = record.get("id") if isinstance(record, dict) else None
        if isinstance(ident, str):
            if ident in seen:
                found.append("duplicate id")
            seen.add(ident)
        if found:
            failing[str(ident or f"line {number}")] = found
    return failing


def main(argv: list[str]) -> int:
    failing = validate_file(argv[0])
    for ident, reasons in failing.items():
        print(f"{ident}: " + "; ".join(reasons))
    print(f"{'FAIL' if failing else 'OK'}: {len(failing)} failing record(s)")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
