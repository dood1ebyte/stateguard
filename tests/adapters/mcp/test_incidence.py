"""
``stateguard.adapters.mcp.incidence``: how an invocation ended, from the answer its client was
sent, and per-invocation fault incidence from a relay's ``invocation`` records -- nothing
dropped from the denominator, a tool's own error not counted as a failed invocation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stateguard.adapters.mcp.incidence import (
    INVOCATION_EVENT,
    INVOCATION_OPEN_EVENT,
    failed,
    read_records,
    render,
    resolution,
    summarize,
    unresolved,
    wilson,
    with_faults,
)
from stateguard.cli import main


def _error(code: int, data: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": "x"}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": 1, "error": error}


@pytest.mark.parametrize(
    ("final", "status", "expected"),
    [
        ({"jsonrpc": "2.0", "id": 1, "result": {"content": []}}, None, ("answered", None)),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"_meta": {"io.stateguard/idempotencyOutcome": "replayed"}},
            },
            None,
            ("answered", "replayed"),
        ),
        (_error(-32000, {"io.stateguard/executionState": "unknown"}), None, ("unknown", None)),
        (_error(-32000, {"io.stateguard/executionState": "not_sent"}), None, ("not_sent", None)),
        (_error(-32010), None, ("in_doubt", None)),
        (_error(-32011), None, ("in_progress", None)),
        (_error(-32602), None, ("answered_error", None)),
        (None, None, ("client_gone", None)),
        (None, 502, ("http_502", None)),
    ],
)
def test_how_an_invocation_ended(final: Any, status: Any, expected: tuple[str, Any]) -> None:
    assert resolution(final, status=status) == expected


def test_an_answer_after_a_fault_is_a_recovery_and_nothing_else_is() -> None:
    assert with_faults("answered", ["transport_lost"]) == "recovered"
    assert with_faults("answered_error", ["session_lost"]) == "recovered_error"
    assert with_faults("answered", []) == "answered"
    assert with_faults("unknown", ["transport_lost"]) == "unknown"
    assert with_faults("not_sent", ["not_sent"]) == "not_sent"


@pytest.mark.parametrize(
    ("kind", "is_unresolved", "is_failed"),
    [
        ("answered", False, False),
        ("answered_error", False, False),  # the tool's own error: an answer
        ("recovered", False, False),
        ("unknown", True, True),
        ("in_doubt", True, True),
        ("in_progress", True, True),
        ("client_gone", True, True),
        ("unfinished", True, True),  # the relay stopped before it answered
        ("http_503", True, True),
        ("not_sent", False, True),  # it did not run: resolved, but failed
        ("refused", False, True),
        ("http_400", False, True),
    ],
)
def test_unresolved_and_failed(kind: str, is_unresolved: bool, is_failed: bool) -> None:
    assert unresolved(kind) is is_unresolved
    assert failed(kind) is is_failed


def _record(
    resolution: str, faults: list[str], *, tool: str = "add_note", **extra: Any
) -> dict[str, Any]:
    return {
        "event": INVOCATION_EVENT,
        "method": "tools/call",
        "tool": tool,
        "faults": faults,
        "resolution": resolution,
        **extra,
    }


def test_rates_are_per_invocation_and_keep_every_invocation() -> None:
    records = [
        _record("answered", []),
        _record("answered", []),
        _record("recovered", ["transport_lost"], server_outcome="replayed"),
        _record("unknown", ["transport_lost"]),
        _record("not_sent", ["not_sent"]),
        _record("answered_error", [], tool="list_notes"),
        {
            "event": INVOCATION_EVENT,
            "method": "tools/list",
            "faults": ["session_lost"],
            "resolution": "recovered",
        },
    ]
    summary = summarize(records)
    calls = summary["tools/call"]
    assert calls["invocations"] == 6
    assert calls["faulted"]["k"] == 3 and calls["faulted"]["n"] == 6
    assert calls["invocations_with_fault"]["transport_lost"]["k"] == 2
    assert calls["unresolved"]["k"] == 1  # unknown
    assert calls["failed"]["k"] == 2  # unknown + not sent; the tool's own error is not one
    assert calls["recovered_of_faulted"] == {**calls["recovered_of_faulted"], "k": 1, "n": 3}
    assert calls["server_outcomes"] == {"replayed": 1}
    assert summary["all"]["invocations"] == 7
    assert summary["by_tool"]["list_notes"]["invocations"] == 1
    lo, hi = calls["failed"]["ci95"]
    assert (lo, hi) == pytest.approx(wilson(2, 6))
    assert "Only the relay-to-server hop is seen" in render(summary)


def test_no_invocations_is_no_rate() -> None:
    summary = summarize([])
    assert summary["all"]["invocations"] == 0
    assert summary["all"]["failed"]["rate"] is None
    assert summary["all"]["failed"]["ci95"] == [0.0, 1.0]


def test_the_cli_reads_only_invocation_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"event": "session_opened", "session": "s"},
                _record("answered", []),
                _record("unknown", ["transport_lost"]),
                {"event": "retry_decision", "retry": False},
            ]
        )
        + "\n"
    )
    assert len(read_records([audit])) == 2
    with pytest.raises(SystemExit) as done:
        main(["incidence", str(audit), "--json"])
    assert done.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tools/call"]["invocations"] == 2
    assert out["tools/call"]["unresolved"]["k"] == 1
    with pytest.raises(SystemExit) as done:
        main(["incidence", str(audit)])
    assert done.value.code == 0
    assert "tools/call: 2 invocations" in capsys.readouterr().out


def test_the_cli_refuses_an_unreadable_log(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as done:
        main(["incidence", str(tmp_path / "missing.jsonl")])
    assert done.value.code != 0


# -- a request a relay never finished is still counted --------------------------------------------


def _open(inv: str, **extra: Any) -> dict[str, Any]:
    return {
        "event": INVOCATION_OPEN_EVENT,
        "inv": inv,
        "method": "tools/call",
        "tool": "add_note",
        "id": 1,
        **extra,
    }


def _write(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_an_open_request_with_no_record_is_unfinished_and_unresolved(tmp_path: Path) -> None:
    audit = _write(
        tmp_path / "audit.jsonl",
        [
            _open("a"),
            _record("answered", [], inv="a"),
            _open("b", session="s"),  # the relay was killed before it answered
        ],
    )
    records = read_records([audit])
    assert [r["resolution"] for r in records] == ["answered", "unfinished"]
    (unfinished,) = [r for r in records if r["resolution"] == "unfinished"]
    assert unfinished["inv"] == "b" and unfinished["session"] == "s"
    assert unfinished["tool"] == "add_note" and unfinished["faults"] == []
    calls = summarize(records)["tools/call"]
    assert calls["invocations"] == 2
    assert calls["unresolved"]["k"] == 1 and calls["failed"]["k"] == 1


def test_an_open_line_and_its_record_pair_across_logs_in_any_order(tmp_path: Path) -> None:
    first = _write(tmp_path / "1.jsonl", [_record("unknown", ["transport_lost"], inv="a")])
    second = _write(tmp_path / "2.jsonl", [_open("a")])
    records = read_records([first, second])
    assert [r["resolution"] for r in records] == ["unknown"]


def test_records_without_open_lines_are_read_as_before(tmp_path: Path) -> None:
    audit = _write(tmp_path / "audit.jsonl", [_record("answered", []), _record("unknown", [])])
    assert [r["resolution"] for r in read_records([audit])] == ["answered", "unknown"]
