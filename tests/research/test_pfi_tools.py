"""Track B tools: the record validator, deduplication and counts (research/pfi/tools)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from research.pfi.tools import counts, dedup, schema

ROOT = Path(__file__).resolve().parents[2]


def _record(**changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": "PFI-0001", "source_platform": "github",
        "urls": ["https://github.com/example/project/issues/1"],
        "report_date": "2026-11-01", "last_activity_date": "not available",
        "project": "example/project", "version": "not available",
        "description": "A call hangs when the server closes its output.",
        "reproduction": "not available", "category": "I", "status": "open", "severity": "high",
        "developer_impact": "a call hangs", "capability": "detects",
        "capability_basis": "a deadline", "controlled_reproduction": "yes",
        "controlled_reproduction_note": "not available",
        "classification": "confirmed technical failure", "confidence": "medium",
        "confidence_reason": "labelled a bug", "duplicate_of": None, "collected": "2026-11-27",
        "query": "GH-1",
    }  # fmt: skip
    record.update(changes)
    return record


def test_a_complete_record_passes() -> None:
    assert schema.problems(_record()) == []


@pytest.mark.parametrize("field", sorted(schema.FIELDS))
def test_a_record_missing_any_field_is_rejected(field: str) -> None:
    record = _record()
    del record[field]
    assert f"missing field {field!r}" in schema.problems(record)


def test_not_available_is_accepted_where_a_value_may_be_unknown() -> None:
    record = _record(report_date="not available", version="not available",
                     severity="not available", capability="not available")  # fmt: skip
    assert schema.problems(record) == []


@pytest.mark.parametrize("field", sorted(schema.NOT_NA))
def test_not_available_is_refused_where_the_record_must_say_how_it_was_found(field: str) -> None:
    assert any(field in p for p in schema.problems(_record(**{field: "not available"})))


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"category": "Q"}, "category"),
        ({"classification": "bug"}, "classification"),
        ({"report_date": "Nov 1"}, "report_date"),
        ({"id": "X-1"}, "id"),
        ({"urls": []}, "urls"),
        ({"urls": ["https://github.com/someone"]}, "not a signal URL"),
        ({"urls": ["https://www.reddit.com/user/someone"]}, "not a signal URL"),
        ({"description": "contact me at a.person@example.com"}, "email"),
        ({"description": "x" * 401}, "paraphrase"),
        ({"duplicate_of": "nope"}, "duplicate_of"),
        ({"extra": 1}, "unknown field"),
    ],
)
def test_bad_values_are_rejected(changes: dict[str, Any], fragment: str) -> None:
    assert any(fragment in p for p in schema.problems(_record(**changes))), changes


def test_validate_file_reports_bad_lines_and_repeated_ids(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text(json.dumps(_record()) + "\n" + json.dumps(_record()) + "\n{\n")
    failing = schema.validate_file(path)
    assert failing["PFI-0001"] == ["duplicate id"] and "line 3" in failing
    assert schema.main([str(path)]) == 1


def test_the_committed_corpus_passes_the_validator() -> None:
    assert schema.validate_file(ROOT / "research" / "pfi" / "corpus.jsonl") == {}


def test_dedup_merges_by_url_duplicate_mark_and_signature() -> None:
    a = _record(id="PFI-0001", urls=["https://github.com/e/p/issues/1"])
    b = _record(id="PFI-0002", urls=["https://github.com/e/p/issues/1/"])  # same URL
    c = _record(id="PFI-0003", urls=["https://github.com/e/p/issues/3"], duplicate_of="PFI-0002")
    d = _record(id="PFI-0004", urls=["https://github.com/e/p/issues/4"], signature="Conn closed")
    e = _record(id="PFI-0005", urls=["https://github.com/e/p/issues/5"], signature="conn closed ")
    f = _record(id="PFI-0006", urls=["https://github.com/e/p/issues/6"], signature="conn closed",
                project="other/project")  # fmt: skip
    found = dedup.groups([a, b, c, d, e, f])
    assert found == [["PFI-0001", "PFI-0002", "PFI-0003"], ["PFI-0004", "PFI-0005"], ["PFI-0006"]]
    merged = {r["id"]: r for r in dedup.merge(copy.deepcopy([a, b, c, d, e, f]))}
    assert merged["PFI-0001"]["duplicate_of"] is None
    assert set(merged["PFI-0001"]["urls"]) == {"https://github.com/e/p/issues/1",
                                               "https://github.com/e/p/issues/1/",
                                               "https://github.com/e/p/issues/3"}  # fmt: skip
    assert merged["PFI-0002"]["duplicate_of"] == "PFI-0001"
    assert merged["PFI-0003"]["duplicate_of"] == "PFI-0001"
    assert merged["PFI-0005"]["duplicate_of"] == "PFI-0004"
    totals = counts.counts(list(merged.values()))
    assert totals["records"] == 6 and totals["independent"] == 3  # counts, never rates


def test_dedup_writes_the_merged_corpus(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    a = _record(id="PFI-0001")
    b = _record(id="PFI-0002")
    path.write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
    assert dedup.main([str(path), "--write"]) == 0
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["duplicate_of"] for r in rows] == [None, "PFI-0001"]
