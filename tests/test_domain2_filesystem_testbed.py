"""
Tests for the Week 2 filesystem testbed (benchmarks/domains/mcp_server_filesystem).

The testbed drives a Node server that CI does not install, so these tests cover
only the parts that decide what a run *means* -- the fixture oracle, the step
and workflow classification, and the fault shim -- against a tiny stdio
server written here. The live runs are documented in
docs/runtime/WEEK2_DOMAIN2_FILESYSTEM.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

_DOMAIN = (
    Path(__file__).resolve().parent.parent / "benchmarks" / "domains" / "mcp_server_filesystem"
)
sys.path.insert(0, str(_DOMAIN))

import fixture  # noqa: E402
import workflow  # noqa: E402
from rawclient import RawSession  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture oracle
# ---------------------------------------------------------------------------


def test_reset_builds_exactly_the_initial_checkpoint(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    fixture.reset(sandbox)
    assert fixture.snapshot(sandbox) == fixture.initial()
    assert (tmp_path / fixture.SENTINEL).is_file()  # outside the sandbox, watched


def test_build_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    fixture.reset(sandbox)
    with pytest.raises(FileExistsError):
        fixture.build(sandbox)


def test_checkpoints_follow_the_steps_by_plain_file_operations(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    fixture.reset(sandbox)
    cps = fixture.checkpoints()
    assert cps[0] == cps[1]  # inspect changes nothing
    order = sandbox / fixture.ORDER
    order.write_text(order.read_text().replace("status: pending", "status: approved"))
    assert fixture.snapshot(sandbox) == cps[2]
    order.rename(sandbox / fixture.DONE)
    assert fixture.snapshot(sandbox) == cps[3]
    log = sandbox / fixture.LOG
    log.write_text(log.read_text().replace("END", f"{fixture.LOG_LINE}\nEND"))
    assert fixture.snapshot(sandbox) == cps[4] == fixture.expected_after()


def test_a_duplicate_effect_matches_no_checkpoint(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    fixture.reset(sandbox)
    for _ in range(2):  # the naive-retry duplicate: the line twice
        log = sandbox / fixture.LOG
        log.write_text(log.read_text().replace("END", f"{fixture.LOG_LINE}\nEND"))
    assert fixture.matching_checkpoint(fixture.snapshot(sandbox)) is None


def test_snapshot_sees_directories_and_the_sentinel(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    fixture.reset(sandbox)
    (sandbox / "a").mkdir()
    (tmp_path / fixture.SENTINEL).write_text("changed\n")
    snap = fixture.snapshot(sandbox)
    assert snap["a/"] == fixture.DIR
    assert snap["<sentinel>"] != fixture.digest(fixture.SENTINEL_TEXT)


# ---------------------------------------------------------------------------
# What the client was told
# ---------------------------------------------------------------------------


def _err(state: str | None) -> dict[str, Any]:
    data = {"io.stateguard/executionState": state} if state else None
    return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "m", "data": data}}


def _result(text: str, *, is_error: bool = False, structured: Any = ...) -> dict[str, Any]:
    r: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        r["isError"] = True
    if structured is not ...:
        r["structuredContent"] = structured
    return {"jsonrpc": "2.0", "id": 1, "result": r}


@pytest.mark.parametrize(
    ("response", "told"),
    [
        (None, "execution_unknown"),
        ({"_timeout": True}, "execution_unknown"),
        ({"_send_failed": True}, "known_not_executed"),
        (_err("not_sent"), "known_not_executed"),
        (_err("unknown"), "execution_unknown"),
        (_err(None), "failed_reported"),
        (_result("The arguments were not sent: ...", is_error=True), "known_not_executed"),
        (_result("MCP error -32602: Input validation error", is_error=True), "failed_reported"),
        (_result("ok", structured={"content": "ok"}), "success"),
    ],
)
def test_wire_class(response: dict[str, Any] | None, told: str) -> None:
    assert workflow.wire_class(response) == told


def test_result_problem_checks_the_declared_output_shape() -> None:
    assert workflow.result_problem(_result("ok", structured={"content": "ok"})) is None
    assert workflow.result_problem(_result("ok")) == "structuredContent absent"
    assert workflow.result_problem(_result("ok", structured={"content": 42})) is not None


# ---------------------------------------------------------------------------
# Workflow classification
# ---------------------------------------------------------------------------


def _rec(step: str, state: str) -> workflow.StepRecord:
    r = workflow.StepRecord(step, 1, "t", {}, True)
    r.state = state
    return r


CPS = fixture.checkpoints()


@pytest.mark.parametrize(
    ("states", "believed", "disk", "expected"),
    [
        (["completed"] * 4, 4, CPS[4], "completed"),
        (["completed", "recovered", "completed", "completed"], 4, CPS[4], "recovered"),
        (["completed", "failed_reported"], 1, CPS[1], "partially_completed_stopped"),
        (["known_not_executed"], 0, CPS[0], "not_started_stopped"),
        (["completed", "completed", "execution_unknown"], 2, CPS[2], "stopped_execution_unknown"),
        (["completed", "completed", "execution_unknown"], 2, CPS[3], "stopped_execution_unknown"),
        (["completed", "completed", "failed_reported"], 2, CPS[3], "failed_unrecovered"),
        (
            ["completed", "completed", "postcondition_violated"],
            2,
            CPS[2],
            "stopped_postcondition_violated",
        ),
        (["completed", "result_invalid"], 1, CPS[2], "stopped_result_invalid"),
        (["completed"] * 3 + ["postcondition_violated"], 3, {"x": "y"}, "failed_unrecovered"),
    ],
)
def test_workflow_classification(
    states: list[str], believed: int, disk: dict[str, str], expected: str
) -> None:
    records = [_rec(f"S{i + 1}", s) for i, s in enumerate(states)]
    assert workflow.classify(records, believed, disk) == expected


# ---------------------------------------------------------------------------
# Fault shim, against a tiny stdio server
# ---------------------------------------------------------------------------

_TINY_SERVER = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    marker = Path(sys.argv[1])
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg.get("method") == "tools/call":
            marker.write_text(marker.read_text() + "x" if marker.exists() else "x")
            text = "status: pending"
            result = {"content": [{"type": "text", "text": text}], "structuredContent": {"content": text}}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
    """
)


def _session(tmp_path: Path, fault: str | None) -> tuple[RawSession, Path]:
    server = tmp_path / "server.py"
    server.write_text(_TINY_SERVER)
    marker = tmp_path / "executed"
    flags = ["--fault", fault] if fault else []
    argv = [
        sys.executable,
        str(_DOMAIN / "shim.py"),
        *flags,
        "--tee",
        str(tmp_path / "tee.jsonl"),
        "--",
    ]
    return RawSession([*argv, sys.executable, str(server), str(marker)]), marker


def _executions(marker: Path) -> int:
    return len(marker.read_text()) if marker.exists() else 0


def test_shim_without_a_fault_relays_byte_identically(tmp_path: Path) -> None:
    s, marker = _session(tmp_path, None)
    assert s.request("ping") == {"jsonrpc": "2.0", "id": 1, "result": {}}
    s.call("t", {}, rid="a")
    s.close()
    tee = [json.loads(line) for line in (tmp_path / "tee.jsonl").read_text().splitlines()]
    assert [(e["dir"], e["sha256"]) for e in tee] == s.wire
    assert _executions(marker) == 1


def test_shim_swallow_request_never_delivers(tmp_path: Path) -> None:
    s, marker = _session(tmp_path, "swallow_request:t")
    assert s.call("t", {}, rid="a") is None  # the pipe closed: no answer
    s.close()
    assert _executions(marker) == 0


def test_shim_drop_response_executes_then_loses_the_answer(tmp_path: Path) -> None:
    s, marker = _session(tmp_path, "drop_response:t")
    assert s.call("t", {}, rid="a") is None
    s.close()
    assert _executions(marker) == 1


def test_shim_fake_success_answers_without_executing(tmp_path: Path) -> None:
    s, marker = _session(tmp_path, "fake_success:t")
    resp = s.call("t", {}, rid="a")
    s.close()
    assert workflow.wire_class(resp) == "success"
    assert _executions(marker) == 0


def test_shim_corrupt_text_stays_schema_valid(tmp_path: Path) -> None:
    s, _ = _session(tmp_path, "corrupt_text:t")
    resp = s.call("t", {}, rid="a")
    s.close()
    assert workflow.result_problem(resp) is None  # only an independent read-back can catch it
    assert resp is not None and resp["result"]["structuredContent"]["content"] == "status: approved"


def test_shim_fault_fires_on_the_nth_call_only(tmp_path: Path) -> None:
    s, marker = _session(tmp_path, "drop_response:t:2")
    assert s.call("t", {}, rid="a") is not None
    assert s.call("t", {}, rid="b") is None
    s.close()
    assert _executions(marker) == 2
