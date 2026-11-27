"""
``stateguard trial`` (Phase 2 S5.1): the same scripted calls in shadow, then auto, with a
before/after report and two redacted bundles kept locally.

End to end on the repository's cooperating notes server (``--no-keys``: an ordinary server),
whose database is the trial's sandbox. Synthetic fixtures and scripted calls, not traffic.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from stateguard import trial

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "examples" / "mcp" / "cooperating_notes_server.py"


def _calls(path: Path, calls: list[dict[str, Any]]) -> Path:
    path.write_text("# scripted\n" + "".join(json.dumps(c) + "\n" for c in calls) + "\n")
    return path


def _sandbox(tmp: Path) -> Path:
    box = tmp / "data"
    box.mkdir()
    with sqlite3.connect(box / "notes.db") as conn:
        conn.execute("CREATE TABLE notes (seq INTEGER PRIMARY KEY, text TEXT NOT NULL)")
        conn.execute("INSERT INTO notes (text) VALUES ('seed')")
    return box


def _server(box: Path) -> list[str]:
    return [sys.executable, str(SERVER), str(box / "notes.db"), "--no-keys"]


def test_calls_are_read_in_order_and_bad_lines_are_refused(tmp_path: Path) -> None:
    path = _calls(tmp_path / "c.jsonl", [{"tool": "a", "arguments": {"x": 1}}, {"tool": "b"}])
    assert trial.read_calls(path) == [{"tool": "a", "arguments": {"x": 1}},
                                      {"tool": "b", "arguments": {}}]  # fmt: skip
    for text, message in [("{", "not JSON"), ('{"arguments": {}}', "needs a"),
                          ('{"tool": "a", "arguments": []}', "must be an object"),
                          ("# nothing\n", "no calls")]:  # fmt: skip
        bad = tmp_path / "bad.jsonl"
        bad.write_text(text + "\n")
        with pytest.raises(trial.TrialError, match=message):
            trial.read_calls(bad)


def test_what_the_client_saw_is_one_word_never_the_content() -> None:
    assert trial._status(None) == "no_answer"
    assert trial._status({"result": {"isError": False, "content": []}}) == "ok"
    assert trial._status({"result": {"isError": True}}) == "tool_error"
    assert trial._status({"error": {"code": -1}}) == "error"
    unknown = {"error": {"data": {"io.stateguard/executionState": "unknown"}}}
    assert trial._status(unknown) == "error:unknown"


def test_the_sandbox_path_is_replaced_in_the_command_and_the_arguments() -> None:
    assert trial._substitute(["srv", "/a/db", "--x=/a/db"], "/a", "/b") == ["srv", "/b/db",
                                                                            "--x=/b/db"]  # fmt: skip
    value = {"path": "/a/f", "list": ["/a/g", 3], "n": 1}
    assert trial._in_sandbox(value, "/a", "/b") == {"path": "/b/f", "list": ["/b/g", 3], "n": 1}


def test_a_trial_that_cannot_run_as_asked_is_refused(tmp_path: Path) -> None:
    calls = [{"tool": "list_notes", "arguments": {}}]
    with pytest.raises(trial.TrialError, match="no server command"):
        trial.run_trial([], calls, tmp_path / "a", label="x")
    with pytest.raises(trial.TrialError, match="needs --sandbox"):
        trial.run_trial(["srv"], calls, tmp_path / "b", label="x", fault="kill_before",
                        fault_tool="t")  # fmt: skip
    with pytest.raises(trial.TrialError, match="needs --fault-tool"):
        trial.run_trial(["srv"], calls, tmp_path / "c", label="x", sandbox=str(tmp_path),
                        fault="kill_before")  # fmt: skip
    with pytest.raises(trial.TrialError, match="does not exist"):
        trial.run_trial(["srv"], calls, tmp_path / "d", label="x", sandbox=str(tmp_path / "no"))
    used = tmp_path / "used"
    used.mkdir()
    (used / "f").write_text("x")
    with pytest.raises(trial.TrialError, match="never overwritten"):
        trial.run_trial(["srv"], calls, used, label="x")


@pytest.mark.integration
def test_a_trial_runs_shadow_then_auto_and_keeps_both_bundles(tmp_path: Path) -> None:
    pytest.importorskip("mcp.server.mcpserver")
    box = _sandbox(tmp_path)
    calls = _calls(tmp_path / "calls.jsonl", [
        {"tool": "list_notes", "arguments": {}},
        {"tool": "add_note", "arguments": {"text": "from the trial"}},
        {"tool": "add_note", "arguments": {"text": ""}},  # the server refuses it
    ])  # fmt: skip
    out = tmp_path / "out"
    code = trial.main(["--dir", str(out), "--calls", str(calls), "--sandbox", str(box),
                       "--label", "test trial", "--", *_server(box)])  # fmt: skip
    assert code == 0
    report = json.loads((out / "trial_report.json").read_text())
    assert report["schema"] == trial.TRIAL_SCHEMA and report["calls"] == 3
    for mode in ("shadow", "auto"):
        side = report[mode]
        assert side["calls"] == 3 and side["denominators_hold"] and side["bundle_rebuilds"]
        assert side["client"] == {"ok": 2, "tool_error": 1}
        assert (out / mode / "capture" / "bundle" / "report.json").exists()
        copy = out / mode / "sandbox" / "data" / "notes.db"
        with sqlite3.connect(copy) as conn:  # each run on its own copy
            rows = [r[0] for r in conn.execute("SELECT text FROM notes ORDER BY seq")]
        assert rows == ["seed", "from the trial"]
    assert report["changed_calls"] == []
    with sqlite3.connect(box / "notes.db") as conn:  # the original is never touched
        assert [r[0] for r in conn.execute("SELECT text FROM notes")] == ["seed"]
    text = (out / "trial_report.txt").read_text()
    assert "shadow (before)" in text and "calls answered differently: none" in text


@pytest.mark.integration
def test_a_lost_answer_on_a_declared_read_is_recovered_in_auto_only(tmp_path: Path) -> None:
    pytest.importorskip("mcp.server.mcpserver")
    box = _sandbox(tmp_path)
    effects = tmp_path / "effects.json"
    effects.write_text(json.dumps({"tools": [{"name": "list_notes", "effect": "read_only"}]}))
    calls = _calls(tmp_path / "calls.jsonl", [{"tool": "list_notes", "arguments": {}}] * 2)
    out = tmp_path / "out"
    result = trial.run_trial(
        _server(box), trial.read_calls(calls), out, label="fault", effects=str(effects),
        sandbox=str(box), fault="lost_answer", fault_tool="list_notes",
    )  # fmt: skip
    shadow, auto = result["shadow"], result["auto"]
    assert shadow["fault_fired"] and auto["fault_fired"]
    # Shadow changes nothing: the server died with the answer, and the session with it.
    assert shadow["client"] == {"no_answer": 2} and shadow["unknown"] == 1
    # Auto resent the declared read to the restarted server.
    assert auto["client"] == {"ok": 2} and auto["calls_resent"] >= 1 and auto["unknown"] == 0
    assert [c["call"] for c in result["changed_calls"]] == [0, 1]
    assert "shadow no_answer -> auto ok" in trial.render(result)
