"""
Recording in the relay (T13): it observes, and nothing else.

* the same session with and without a recorder produces the same bytes for
  the client and the same lines for the server, in every mode;
* a recorder that fails -- its sink, its redaction, or the recorder itself
  -- stops recording, is reported once, and leaves the traffic exactly as
  it would have been without it (no category Z, no exception);
* what is recorded is what happened: every line in each direction, in
  order, with the ids the wire carried and the server generation; an
  unknown outcome stays unknown in the recording, never a success;
* secrets in the traffic never reach the file.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import (  # noqa: E402
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)

from stateguard.adapters.mcp.recording import (  # noqa: E402
    Direction,
    Provenance,
    Recorder,
    parse_recording,
    read_recording,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "examples" / "mcp" / "stdio_proxy.py"
SERVER = Path(__file__).parent / "fake_mcp_server.py"

SEARCH = ServerTool(
    {
        "name": "search",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "maximum": 10},
                "state": {"type": "string", "enum": ["open", "closed"]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
)

CALLS: list[dict[str, Any]] = [
    {"query": "plain"},
    {"query": "typed", "limit": "5"},  # repairable: string for integer
    {"query": "drift", "state": "Open"},  # repairable: enum case
    {"q": "renamed"},  # repairable or refused, depending on trust
    {"query": "Bearer abcdefghijklmnop", "api_key": "zzz-secret-1"},  # rejected by the server
]


def _recorder(sink: io.StringIO | None = None) -> Recorder:
    return Recorder(
        sink if sink is not None else io.StringIO(),
        provenance=Provenance.FAULT_INJECTION,
        session="relay-test",
    )


def _run(
    mode: RelayMode, recorder: Any = None, plan: dict[int, ServerAction] | None = None
) -> tuple[list[Any], list[Any], list[Any], Rig]:
    def faults(call: ServerCall) -> ServerAction:
        return (plan or {}).get(call.index, ServerAction.NORMAL)

    rig = Rig(ScriptedServer([SEARCH], faults=faults), mode, recorder=recorder)
    rig.request("server/discover")
    if mode is not RelayMode.PASSTHROUGH:
        assert rig.wait_synced()
    for arguments in CALLS:
        rig.call("search", arguments, timeout=2.0)
    rig.close()
    client = list(rig.client_received)
    server = [json.loads(line) for line in rig.all_received]
    executed = [call.arguments for call in rig.all_executed]
    return client, server, executed, rig


@pytest.mark.parametrize("mode", list(RelayMode))
def test_recording_does_not_change_traffic(mode: RelayMode) -> None:
    baseline = _run(mode)
    recorder = _recorder()
    recorded = _run(mode, recorder)
    assert recorded[:3] == baseline[:3]
    assert recorder.healthy and recorder.records > 0


class _BrokenSink(io.StringIO):
    def __init__(self, fail_after: int) -> None:
        super().__init__()
        self.writes = 0
        self.fail_after = fail_after

    def write(self, text: str) -> int:
        self.writes += 1
        if self.writes > self.fail_after:
            raise OSError("no space left on device")
        return super().write(text)


class _ExplodingRecorder(Recorder):
    """A recorder that breaks its own contract and raises."""

    def message(self, *args: Any, **kwargs: Any) -> None:
        if self.records > 4:
            raise RuntimeError("recorder bug")
        super().message(*args, **kwargs)


def _redaction_fails(recorder: Recorder) -> Recorder:
    original = recorder.redactor.message
    seen = {"n": 0}

    def flaky(message: Any) -> Any:
        seen["n"] += 1
        if seen["n"] > 6:
            raise ValueError("cannot redact")
        return original(message)

    recorder.redactor.message = flaky  # type: ignore[method-assign]
    return recorder


@pytest.mark.parametrize("mode", [RelayMode.AUTO, RelayMode.SHADOW, RelayMode.PASSTHROUGH])
@pytest.mark.parametrize(
    "make",
    [
        lambda: _recorder(_BrokenSink(fail_after=5)),
        lambda: _ExplodingRecorder(
            io.StringIO(), provenance=Provenance.FAULT_INJECTION, session="x"
        ),
        lambda: _redaction_fails(_recorder()),
    ],
    ids=["sink_fails", "recorder_raises", "redaction_fails"],
)
def test_a_failing_recorder_leaves_traffic_unchanged(mode: RelayMode, make: Any) -> None:
    baseline = _run(mode)
    recorder = make()
    client, server, executed, rig = _run(mode, recorder)
    assert (client, server, executed) == baseline[:3]
    stopped = [e for e in rig.audit if e.get("event") == "recording_stopped"]
    assert len(stopped) == 1
    assert stopped[0]["impact"].startswith("recording only")
    assert not [e for e in rig.audit if e.get("category") == "Z"]


def _ids(records: list[dict[str, Any]]) -> list[Any]:
    return [r.get("id") for r in records]


def test_the_recording_is_what_crossed_the_wire_in_order() -> None:
    sink = io.StringIO()
    recorder = _recorder(sink)
    client, server, _, rig = _run(RelayMode.AUTO, recorder)
    recorder.close()
    recording = parse_recording(sink.getvalue().splitlines())
    assert recording.complete

    from_client = recording.messages(Direction.CLIENT_TO_RELAY)
    assert [r["method"] for r in from_client] == ["server/discover"] + ["tools/call"] * len(CALLS)
    assert _ids(from_client) == [1, 2, 3, 4, 5, 6]
    assert _ids(recording.messages(Direction.RELAY_TO_CLIENT)) == [m["id"] for m in client]
    assert _ids(recording.messages(Direction.RELAY_TO_SERVER)) == [m.get("id") for m in server]
    to_server = recording.messages(Direction.RELAY_TO_SERVER)
    assert {r["gen"] for r in to_server} == {0}
    # StateGuard's decisions are in the same recording, in sequence.
    decisions = [r["event"] for r in recording.of_type("audit") if r["event"]["event"] == "call"]
    assert [d["id"] for d in decisions] == [2, 3, 4, 5, 6]
    seqs = [r["seq"] for r in recording.records]
    assert seqs == sorted(seqs)


def test_no_secret_reaches_the_recording() -> None:
    sink = io.StringIO()
    recorder = _recorder(sink)
    _run(RelayMode.AUTO, recorder)
    recorder.close()
    assert "abcdefghijklmnop" not in sink.getvalue()
    assert "zzz-secret-1" not in sink.getvalue()


def test_an_unknown_outcome_stays_unknown_in_the_recording() -> None:
    sink = io.StringIO()
    recorder = _recorder(sink)

    def faults(call: ServerCall) -> ServerAction:
        return ServerAction.DROP_RESPONSE if call.index == 0 else ServerAction.NORMAL

    rig = Rig(
        ScriptedServer([SEARCH], faults=faults),
        RelayMode.AUTO,
        call_deadline=0.3,
        recorder=recorder,
    )
    rig.request("server/discover")
    rig.wait_synced()
    answer = rig.call("search", {"query": "x"}, timeout=3.0)
    rig.close()
    recorder.close()
    assert answer is not None and answer["error"]["data"]["io.stateguard/executionState"] == (
        "unknown"
    )
    recording = parse_recording(sink.getvalue().splitlines())
    call_id = answer["id"]
    from_server = [
        r for r in recording.messages(Direction.SERVER_TO_RELAY) if r.get("id") == call_id
    ]
    assert from_server == []  # the server never answered: nothing fabricated
    to_client = [r for r in recording.messages(Direction.RELAY_TO_CLIENT) if r.get("id") == call_id]
    assert len(to_client) == 1 and to_client[0]["kind"] == "error"
    assert to_client[0]["message"]["error"]["data"]["io.stateguard/executionState"] == "unknown"
    unknown = [
        r["event"] for r in recording.of_type("audit") if r["event"]["event"] == "outcome_unknown"
    ]
    assert [e["id"] for e in unknown] == [call_id]


def test_generations_are_recorded_across_a_restart() -> None:
    sink = io.StringIO()
    recorder = _recorder(sink)
    rig = Rig(
        ScriptedServer([SEARCH]),
        RelayMode.AUTO,
        server_factory=lambda: ScriptedServer([SEARCH]),
        max_restarts=1,
        recorder=recorder,
    )
    rig.request("server/discover")
    rig.wait_synced()
    rig.kill_server()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and rig.relay.generation == 0:
        time.sleep(0.01)
    answer = rig.call("search", {"query": "after"}, timeout=3.0)
    rig.close()
    recorder.close()
    assert answer is not None and "result" in answer
    recording = parse_recording(sink.getvalue().splitlines())
    gens = {r["gen"] for r in recording.messages(Direction.RELAY_TO_SERVER)}
    assert gens == {0, 1}
    events = [r["event"]["event"] for r in recording.of_type("audit")]
    assert events.index("server_lost") < events.index("server_restarted")


def test_relay_cli_records(tmp_path: Path) -> None:
    tape = tmp_path / "tape.jsonl"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(RELAY),
            "--audit",
            str(tmp_path / "audit.jsonl"),
            "--record",
            str(tape),
            "--",
            sys.executable,
            str(SERVER),
            str(tmp_path / "server.jsonl"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        cwd=ROOT,
    )
    assert proc.stdin and proc.stdout
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    for n, (method, params) in enumerate(
        [("server/discover", {}), ("tools/call", {"name": "search", "arguments": {"query": "x"}})],
        start=1,
    ):
        message = {"jsonrpc": "2.0", "id": n, "method": method, "params": {**params, "_meta": meta}}
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()
        while json.loads(proc.stdout.readline()).get("id") != n:
            pass
    proc.stdin.close()
    proc.wait(timeout=10)
    recording = read_recording(tape)
    assert recording.complete
    assert recording.provenance is Provenance.LOCAL_DEVELOPMENT
    assert {r["dir"] for r in recording.messages()} == {d.value for d in Direction}
    assert recording.header["deterministic"] is False
    assert all("t_ms" in r["diag"] for r in recording.messages())
    assert "server" not in recording.header["config"]  # the command line is never written
