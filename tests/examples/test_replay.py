"""
Record / replay (T13), end to end on the committed corpus.

* the corpus parses, is complete, says it is fault injection, and holds none
  of the fake credentials its sessions sent;
* recording the corpus again yields the same content in every stream;
* replay under the recorded configuration reproduces every recorded answer
  and StateGuard decision; replaying twice gives the same outcomes;
* scenarios A-H mean what they say on replay: an UNKNOWN stays UNKNOWN and is
  never re-sent, a reconnect never re-executes, a removed tool is never run;
* what an arm sends that the recording never saw is answered as a marked,
  counted counterfactual -- never as if it had been observed;
* replay never starts a process or opens a socket;
* recordings that cannot be replayed faithfully are refused, not guessed at;
* reports never call synthetic traffic real.
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime import record_corpus, replay  # noqa: E402
from runtime.replay import (  # noqa: E402
    ARMS,
    REPLAY_MARK,
    ArmRun,
    ReplayRefusedError,
    TapeEnvironment,
    derive,
    run_arm,
)

from stateguard.adapters.mcp.recording import (  # noqa: E402
    Direction,
    Provenance,
    Recorder,
    RecordingError,
    parse_recording,
    read_recording,
)

pytestmark = pytest.mark.integration

TAPES = replay.TAPES
NAMES = [
    "A_success",
    "B_tool_failure",
    "C_transport_failure",
    "D_server_death_reconnect",
    "E_unknown_after_dispatch",
    "F_schema_change",
    "G_protocol_error",
    "H_stateguard_intervention",
]


@pytest.fixture(scope="module")
def envs() -> dict[str, TapeEnvironment]:
    return {name: derive(read_recording(TAPES / f"{name}.jsonl")) for name in NAMES}


@pytest.fixture(scope="module")
def runs(envs: dict[str, TapeEnvironment]) -> dict[tuple[str, str], ArmRun]:
    return {
        (name, arm): run_arm(env, arm)
        for name, env in envs.items()
        for arm in (*ARMS, "as_recorded")
    }


def _step(run: ArmRun, identifier: Any) -> Any:
    return next(s for s in run.steps if s.id == identifier)


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def test_the_corpus_is_complete_labelled_and_honest_about_provenance() -> None:
    assert sorted(p.stem for p in TAPES.glob("*.jsonl")) == NAMES
    for name in NAMES:
        recording = read_recording(TAPES / f"{name}.jsonl")
        assert recording.complete, name
        assert recording.provenance is Provenance.FAULT_INJECTION
        assert not recording.provenance.is_real_traffic
        assert recording.header["redaction"]["mode"] == "mask"
        assert recording.of_type("label"), name


def test_no_credential_reaches_the_corpus() -> None:
    text = "".join(p.read_text() for p in TAPES.glob("*.jsonl"))
    for secret in record_corpus.FAKE_SECRETS:
        assert secret not in text
        for fragment in secret.split():
            assert len(fragment) < 8 or fragment not in text
    assert "FAKE" not in text


def _projection(path: Path) -> dict[str, Any]:
    recording = read_recording(path)
    out: dict[str, Any] = {
        d.value: [
            (m["kind"], m.get("id"), m["gen"], json.dumps(m.get("message"), sort_keys=True))
            for m in recording.messages(d)
        ]
        for d in Direction
    }
    for kind in ("audit", "fault", "label"):
        out[kind] = sorted(
            json.dumps({k: v for k, v in r.items() if k != "seq"}, sort_keys=True)
            for r in recording.of_type(kind)
        )
    return out


def test_recording_again_gives_the_same_content(tmp_path: Path) -> None:
    """Per stream, the content is deterministic; only the interleaving of truly
    concurrent lines across streams may differ."""
    record_corpus.main(["--out", str(tmp_path)])
    for name in NAMES:
        assert _projection(tmp_path / f"{name}.jsonl") == _projection(TAPES / f"{name}.jsonl")


# ---------------------------------------------------------------------------
# Fidelity and determinism
# ---------------------------------------------------------------------------


def test_replay_as_recorded_reproduces_answers_and_decisions(
    envs: dict[str, TapeEnvironment], runs: dict[tuple[str, str], ArmRun]
) -> None:
    fidelity = replay._fidelity(list(envs.values()), list(runs.values()))
    assert fidelity["diverged"] == []
    assert fidelity["answers_reproduced"] == fidelity["answers_total"] > 0
    assert fidelity["decisions_reproduced"] == fidelity["decisions_total"] > 0
    for name in NAMES:
        assert sum(s.counterfactual for s in runs[(name, "as_recorded")].steps) == 0


@pytest.mark.parametrize(
    "name", ["D_server_death_reconnect", "E_unknown_after_dispatch", "H_stateguard_intervention"]
)
@pytest.mark.parametrize("arm", ["raw", "auto", "auto_ops"])
def test_replaying_twice_gives_the_same_outcomes(
    envs: dict[str, TapeEnvironment],
    runs: dict[tuple[str, str], ArmRun],
    name: str,
    arm: str,
) -> None:
    again = run_arm(envs[name], arm)
    assert replay._signature(again) == replay._signature(runs[(name, arm)])


# ---------------------------------------------------------------------------
# Scenarios A-H
# ---------------------------------------------------------------------------


def test_a_success(runs: dict[tuple[str, str], ArmRun]) -> None:
    for arm in (*ARMS, "as_recorded"):
        steps = runs[("A_success", arm)].steps
        calls = [s for s in steps if s.method == "tools/call"]
        assert all(s.ok and s.executions == 1 for s in calls), arm


def test_b_tool_failure_is_never_hidden_or_repeated(runs: dict[tuple[str, str], ArmRun]) -> None:
    for arm in (*ARMS, "as_recorded"):
        run = runs[("B_tool_failure", arm)]
        failed_after_effect = _step(run, 2)
        assert failed_after_effect.answer == "tool_error"
        assert failed_after_effect.executions == 1  # executed once; never retried
        transient = _step(run, 3)
        if arm != "auto_ops":
            assert transient.answer == "tool_error" and transient.executions == 0


def test_b_a_retry_the_recording_never_saw_is_counterfactual(
    runs: dict[tuple[str, str], ArmRun],
) -> None:
    transient = _step(runs[("B_tool_failure", "auto_ops")], 3)
    assert transient.retries == 1
    assert transient.counterfactual == 1  # the retry's answer was predicted, not recorded


def test_c_transport_failure(runs: dict[tuple[str, str], ArmRun]) -> None:
    raw = runs[("C_transport_failure", "raw")]
    assert _step(raw, 3).answer == "connection_closed"
    assert _step(raw, 4).answer == "unsent"
    auto = runs[("C_transport_failure", "auto")]
    assert _step(auto, 3).answer == "unknown"  # surfaced, not hidden
    for arm in (*ARMS, "as_recorded"):
        assert _step(runs[("C_transport_failure", arm)], 3).executions == 0


def test_d_reconnect_recovers_without_re_executing(runs: dict[tuple[str, str], ArmRun]) -> None:
    raw = runs[("D_server_death_reconnect", "raw")]
    assert [_step(raw, i).answer for i in (3, 4)] == ["unsent", "unsent"]
    for arm in ("auto_ops", "as_recorded"):
        run = runs[("D_server_death_reconnect", arm)]
        assert run.restarts == 1
        assert run.counterfactual_restarts == 0  # the recording restarted too
        for identifier in (3, 4):
            step = _step(run, identifier)
            assert step.ok and step.executions == 1 and step.executed_on == [1]
            assert step.counterfactual == 0
        assert _step(run, 2).executions == 1  # the call before the death ran once


def test_e_unknown_stays_unknown_and_is_never_replayed(
    runs: dict[tuple[str, str], ArmRun],
) -> None:
    run = runs[("E_unknown_after_dispatch", "as_recorded")]
    for identifier in (3, 4):
        step = _step(run, identifier)
        assert step.answer == "unknown"
        assert step.executions == 1  # it did execute -- and was not told "success"
        assert step.executed_on == [0]  # and was never sent to the restarted server
        assert not step.ok
    after = _step(run, 5)
    assert after.ok and after.executed_on == [1]
    for arm in (*ARMS, "as_recorded"):
        steps = [
            s for s in runs[("E_unknown_after_dispatch", arm)].steps if s.method == "tools/call"
        ]
        assert all(s.executions <= 1 for s in steps), arm
        assert not any(s.answer == "success" and s.executions == 0 for s in steps), arm


def test_f_schema_change(runs: dict[tuple[str, str], ArmRun]) -> None:
    for arm in (*ARMS, "as_recorded"):
        run = runs[("F_schema_change", arm)]
        renamed = _step(run, 3)
        assert renamed.ok and renamed.counterfactual == 0, arm
        removed = _step(run, 4)
        assert removed.executions == 0 and removed.answer != "success", arm


def test_g_protocol_errors(runs: dict[tuple[str, str], ArmRun]) -> None:
    for arm in (*ARMS, "as_recorded"):
        run = runs[("G_protocol_error", arm)]
        assert _step(run, 2).answer == "protocol_error"  # unknown method
        # Malformed arguments: the server rejects them, or auto refuses them itself.
        assert _step(run, "raw-1").answer in (
            "tool_error",
            "protocol_error",
            "refused_by_stateguard",
        )
        assert _step(run, "raw-1").executions == 0
        auth = _step(run, 3)
        assert auth.answer == "protocol_error" and auth.executions == 0
        assert _step(run, 4).ok  # the session goes on


def test_h_interventions_and_their_counterfactual_share(
    runs: dict[tuple[str, str], ArmRun],
) -> None:
    raw = runs[("H_stateguard_intervention", "raw")]
    auto = runs[("H_stateguard_intervention", "auto")]
    shadow = runs[("H_stateguard_intervention", "shadow")]
    raw_failures = sum(1 for s in raw.steps if s.ok is False)
    auto_failures = sum(1 for s in auto.steps if s.ok is False)
    assert auto_failures < raw_failures
    # Shadow observes; it changes nothing the server or the client sees.
    assert replay._signature(shadow) == replay._signature(raw)
    # Every repaired call reached the server with arguments the recording never
    # held: those answers are counterfactual, and counted as such.
    assert sum(s.counterfactual for s in auto.steps) > 0
    assert sum(s.counterfactual for s in raw.steps) == 0
    # The result that breaks its outputSchema is found, and never "fixed".
    assert auto.result_findings >= 1
    weather = next(s for s in auto.steps if s.tool == "get_weather")
    assert weather.answer == "success" and weather.ok is False


def test_no_arm_is_stateguard_induced_or_duplicates(
    envs: dict[str, TapeEnvironment], runs: dict[tuple[str, str], ArmRun]
) -> None:
    summary = replay.summarise(
        list(envs.values()), list(runs.values()), determinism={}, overhead={}, parse_ms={}
    )
    for arm, block in summary["arms"].items():
        assert block["duplicate_executions"] == 0, arm
        assert block["internal_errors_z"] == 0, arm
        if arm != "raw":
            assert block["stateguard_induced"] == 0, arm


def test_counterfactual_answers_are_marked() -> None:
    env = derive(read_recording(TAPES / "H_stateguard_intervention.jsonl"))
    seen: list[Any] = []
    original = replay.TapeServer._reply

    def spy(self: Any, writer: Any, identifier: Any, result: dict[str, Any]) -> None:
        seen.append(result)
        original(self, writer, identifier, result)

    replay.TapeServer._reply = spy  # type: ignore[method-assign]
    try:
        run = run_arm(env, "auto")
    finally:
        replay.TapeServer._reply = original  # type: ignore[method-assign]
    marked = [r for r in seen if r.get("_meta", {}).get(REPLAY_MARK) == "counterfactual"]
    assert len(marked) == sum(s.counterfactual for s in run.steps) > 0


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_replay_never_starts_a_process_or_opens_a_socket(
    monkeypatch: pytest.MonkeyPatch, envs: dict[str, TapeEnvironment]
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("replay must not reach outside the process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for arm in ("auto_ops", "as_recorded"):
        run = run_arm(envs["D_server_death_reconnect"], arm)
        assert run.restarts == 1  # even a restart is an in-process fixture


def _lines(name: str) -> list[str]:
    return (TAPES / f"{name}.jsonl").read_text().splitlines()


def test_an_incomplete_recording_is_refused() -> None:
    recording = parse_recording(_lines("A_success")[:-1])
    with pytest.raises(ReplayRefusedError):
        derive(recording)
    assert derive(recording, allow_incomplete=True).steps


def _rewrite(name: str, change: Any) -> list[str]:
    out = []
    for line in _lines(name):
        record = json.loads(line)
        change(record)
        out.append(json.dumps(record))
    return out


@pytest.mark.parametrize("bad_id", ["stateguard-relay:9", "stateguard-retry:1"])
def test_client_ids_in_the_relay_namespace_are_refused(bad_id: str) -> None:
    def change(record: dict[str, Any]) -> None:
        if record.get("dir") == "client_to_relay" and record.get("id") == 2:
            record["id"] = bad_id
            record["message"]["id"] = bad_id

    with pytest.raises(ReplayRefusedError):
        derive(parse_recording(_rewrite("A_success", change)))


def test_a_reused_request_id_is_refused() -> None:
    def change(record: dict[str, Any]) -> None:
        if record.get("dir") == "client_to_relay" and record.get("id") == 3:
            record["id"] = 2
            record["message"]["id"] = 2

    with pytest.raises(ReplayRefusedError):
        derive(parse_recording(_rewrite("A_success", change)))


def test_malformed_and_unsupported_recordings_fail_before_anything_runs(tmp_path: Path) -> None:
    lines = _lines("A_success")
    header = json.loads(lines[0])
    header["version"] = 99
    (tmp_path / "future.jsonl").write_text("\n".join([json.dumps(header), *lines[1:]]))
    (tmp_path / "broken.jsonl").write_text("\n".join([lines[0], "{not json"]))
    for name in ("future", "broken"):
        with pytest.raises(RecordingError):
            replay.load_one(tmp_path / f"{name}.jsonl")
    assert replay.main(["--tapes", str(tmp_path / "missing")]) == 1


def test_without_ground_truth_unanswered_calls_follow_the_stated_convention() -> None:
    """A real capture has no fault records: the request the server died on is
    the latest unanswered one; earlier unanswered ones were dropped."""
    lines = [
        line
        for line in _lines("E_unknown_after_dispatch")
        if json.loads(line)["type"] not in ("fault", "label")
    ]
    env = derive(parse_recording(lines))
    dropped, died_on = env.attempts["3"][0], env.attempts["4"][0]
    assert not dropped.closed and died_on.closed
    # Execution is not observable: assumed, the conservative side for duplicates.
    assert dropped.executes and died_on.executes


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_reports_never_call_synthetic_traffic_real(envs: dict[str, TapeEnvironment]) -> None:
    corpus = replay._corpus(list(envs.values()), {})
    assert corpus["evidence"].startswith("NOT real-world evidence")
    assert corpus["provenance"] == {"fault_injection": 8}
    assert any("not real traffic" in line for line in replay.LIMITATIONS)


def test_a_real_capture_is_reported_as_one() -> None:
    sink = io.StringIO()
    recorder = Recorder(sink, provenance=Provenance.REAL_CAPTURE, session="captured")
    recorder.close()
    env = derive(parse_recording(sink.getvalue().splitlines()))
    evidence = replay._corpus([env], {})["evidence"]
    assert evidence.startswith("captured real traffic")
    assert "not proof of reliability improvement" in evidence
    mixed = derive(read_recording(TAPES / "A_success.jsonl"))
    assert replay._corpus([env, mixed], {})["evidence"].startswith("NOT real-world evidence")


@settings(max_examples=200, deadline=None)
@given(
    name=st.sampled_from(NAMES),
    index=st.integers(min_value=1, max_value=400),
    path=st.lists(
        st.sampled_from(["message", "params", "result", "tools", "arguments", "id"]), max_size=3
    ),
    value=st.one_of(
        st.none(), st.integers(), st.text(max_size=5), st.lists(st.integers(), max_size=2)
    ),
)
def test_property_damaged_recordings_derive_or_refuse(
    name: str, index: int, path: list[str], value: Any
) -> None:
    """Records the parser accepts but whose bodies are odd: derive returns or
    raises RecordingError, never anything else."""
    lines = _lines(name)
    at = 1 + index % (len(lines) - 2)
    record = json.loads(lines[at])
    target: Any = record
    for key in path[:-1]:
        nxt = target.get(key) if isinstance(target, dict) else None
        if not isinstance(nxt, dict):
            break
        target = nxt
    if isinstance(target, dict):
        target[path[-1] if path else "message"] = value
    lines[at] = json.dumps(record)
    try:
        recording = parse_recording(lines)
    except RecordingError:
        return
    with contextlib.suppress(RecordingError):
        derive(recording)


# ---------------------------------------------------------------------------
# T14: the mid-restart race, replayed the way T13 found it
# ---------------------------------------------------------------------------


def test_unsafe_bypass_counter_counts_the_t13_path_only() -> None:
    lost = [{"event": "server_lost"}, {"event": "tools_list_unsent"}]
    unvalidated = {"event": "call", "action": "unvalidated", "listing": "unconfirmed"}
    assert replay.unsafe_bypasses([*lost, unvalidated]) == 1
    # A refresh that failed on a live server is the documented fail-open, not this bug.
    assert replay.unsafe_bypasses([unvalidated]) == 0
    assert replay.unsafe_bypasses([*lost, {"event": "call", "action": "forward"}]) == 0


def test_race_provoking_replay_never_bypasses_stateguard() -> None:
    from runtime import race

    summary = race.measure(runs=5)
    totals = summary["totals"]
    assert totals["unsafe_bypasses"] == 0 and summary["runs_with_a_bypass"] == 0
    assert totals["unvalidated"] == 0
    assert totals["repaired"] == 3 * summary["runs"]  # every repairable call repaired
    assert totals["duplicate_executions"] == 0
    assert totals["unknown"] == 2 * summary["runs"]  # the two calls the server died on
    assert totals["restarts"] == 2 * summary["runs"]
