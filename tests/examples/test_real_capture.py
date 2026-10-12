"""
Real capture through the relay, end to end -- **in a test harness**.

THIS IS NOT PRODUCTION EVIDENCE. The "real capture" sessions below run the
real relay against the in-process ScriptedServer (and, for the CLI, the
local fake_mcp_server.py) and only *label* the recording ``real_capture`` to
exercise that path. No public MCP server or external service is contacted.
They show what a real capture would hold and that capturing changes
nothing -- not how any real server behaves.

What must hold:

* real capture is shadow-only: the relay refuses it in auto or passthrough,
  and the CLI refuses it (before starting the server) without --shadow,
  --record, or mask redaction;
* every client tools/call reaches the server byte-identical -- including
  one StateGuard would have repaired; no repaired payload is forwarded;
* nothing is retried (an auth failure, a transient tool error, an unknown
  outcome), nothing is restarted, no answer is dropped or invented;
* auth failures are still classified (T11), an unknown outcome stays
  unknown, and a restart is only reported as one StateGuard would attempt;
* the recording says real_capture, carries no fault or label, holds none
  of the session's secrets, and still replays.
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

from runtime import replay  # noqa: E402
from runtime.harness import (  # noqa: E402
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)

from stateguard.adapters.mcp import EffectPolicy, summarize_evidence  # noqa: E402
from stateguard.adapters.mcp.recording import (  # noqa: E402
    Provenance,
    Recorder,
    parse_recording,
    read_recording,
)
from tests.adapters.mcp.test_recording import SECRETS  # noqa: E402

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "examples" / "mcp" / "stdio_proxy.py"
REPORT = ROOT / "examples" / "mcp" / "evidence_report.py"
SERVER = Path(__file__).parent / "fake_mcp_server.py"

SEARCH = ServerTool(
    {
        "name": "search",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
)

#: (arguments, what the server does). Index = the server's tools/call count.
SESSION: list[tuple[dict[str, Any], ServerAction]] = [
    ({"query": "plain"}, ServerAction.NORMAL),
    ({"query": "typed", "limit": "5"}, ServerAction.NORMAL),  # auto would repair
    ({"query": "flaky"}, ServerAction.TRANSIENT_ERROR),  # auto would retry (read_only)
    ({"query": "expired"}, ServerAction.AUTH_EXPIRED),  # T11: auth, never retried
    (
        {"query": SECRETS["bearer"], "api_key": SECRETS["sk"], "note": SECRETS["aws"]},
        ServerAction.NORMAL,  # auto would refuse (unknown properties)
    ),
    ({"query": "lost"}, ServerAction.DROP_RESPONSE),  # unknown: never answered
    ({"query": "late"}, ServerAction.DELAYED_RESPONSE),  # unknown at the deadline, then answered
]


def _shadow_capture() -> tuple[Rig, list[bytes], list[Any], str]:
    actions = [action for _, action in SESSION]

    def faults(call: ServerCall) -> ServerAction:
        return actions[call.index] if call.index < len(actions) else ServerAction.NORMAL

    sink = io.StringIO()
    recorder = Recorder(
        sink,
        provenance=Provenance.REAL_CAPTURE,
        session="harness-not-production",
        deterministic=False,  # as the CLI records
        background=True,
        config={"mode": "shadow", "retry": "auto", "restart": 1},
    )
    rig = Rig(
        ScriptedServer([SEARCH], faults=faults),
        RelayMode.SHADOW,
        call_deadline=0.3,
        retry="auto",  # asks for retries: shadow must still never send one
        effects=EffectPolicy({"search": "read_only"}),
        server_factory=lambda: ScriptedServer([SEARCH]),
        max_restarts=1,  # asks for restarts: shadow must still never restart
        recorder=recorder,
    )
    sent: list[bytes] = []
    send = rig.send

    def capture(message: dict[str, Any]) -> None:
        sent.append(json.dumps(message).encode())
        send(message)

    rig.send = capture  # type: ignore[method-assign]
    rig.request("server/discover")
    assert rig.wait_synced()
    answers = [rig.call("search", arguments, timeout=1.5) for arguments, _ in SESSION]
    rig.kill_server()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(
        e.get("event") == "restart_would_attempt" for e in rig.audit
    ):
        time.sleep(0.01)
    rig.close()
    recorder.close()
    return rig, sent, answers, sink.getvalue()


@pytest.fixture(scope="module")
def capture() -> tuple[Rig, list[bytes], list[Any], str]:
    return _shadow_capture()


def _events(rig: Rig, name: str) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("event") == name]


def test_every_call_reaches_the_server_byte_identical(capture: Any) -> None:
    rig, sent, _, _ = capture
    client_calls = [line for line in sent if b'"tools/call"' in line]
    server_calls = [line.rstrip(b"\n") for line in rig.all_received if b'"tools/call"' in line]
    assert server_calls == client_calls  # same bytes, same order, nothing extra
    # The repairable call went as sent ("5", a string), not as auto would send it.
    assert json.loads(server_calls[1])["params"]["arguments"] == {"query": "typed", "limit": "5"}
    (decided,) = [e for e in _events(rig, "call") if e["id"] == 3]
    assert decided["would_modify"] is True


def test_nothing_is_retried_restarted_dropped_or_invented(capture: Any) -> None:
    rig, _, answers, _ = capture
    assert len(rig.servers) == 1  # never restarted
    assert _events(rig, "restart_would_attempt") and not _events(rig, "server_restarted")
    assert all(not e["acted"] for e in _events(rig, "retry_decision"))
    assert not _events(rig, "response_dropped") and not _events(rig, "retry_not_sent")
    executed = [call.arguments for call in rig.all_executed]
    assert executed.count({"query": "flaky"}) == 0  # transient: refused once, never re-sent
    assert sum(1 for line in rig.all_received if b'"tools/call"' in line) == len(SESSION)
    # Every answer the client got is the server's own.
    assert answers[5] is None  # never answered: the relay says nothing in shadow
    assert all(
        a is not None and "io.stateguard/executionState" not in json.dumps(a)
        for i, a in enumerate(answers)
        if i != 5
    )


def test_auth_and_unknown_semantics_are_intact(capture: Any) -> None:
    rig, _, answers, _ = capture
    (auth,) = _events(rig, "auth_failure")
    assert auth["id"] == 5 and auth["category"] == "K"
    (decision,) = [e for e in _events(rig, "retry_decision") if e["id"] == 5]
    assert decision["observed"] == "auth_error" and decision["decision"] == "reject"
    unknown = _events(rig, "outcome_unknown")
    assert {e["id"] for e in unknown} == {7, 8}
    assert all(e["surfaced"] is False for e in unknown)
    retried_unknown = [
        e for e in _events(rig, "retry_decision") if e["id"] in (7, 8) and e["acted"]
    ]
    assert retried_unknown == []
    assert answers[6] is not None and "result" in answers[6]  # the late answer, as sent


def test_the_recording_is_a_real_capture_and_only_that(capture: Any) -> None:
    _, _, _, text = capture
    recording = parse_recording(text.splitlines())
    assert recording.complete and recording.provenance is Provenance.REAL_CAPTURE
    assert recording.header["redaction"]["mode"] == "mask"
    assert recording.header["started_at"].endswith("Z")
    assert recording.of_type("fault") == () and recording.of_type("label") == ()
    for value in (SECRETS["bearer"], SECRETS["sk"], SECRETS["aws"]):
        assert value not in text
    assert "abcDEF123456ghiJKL" not in text and "plain" not in text


def test_the_evidence_summary_separates_fact_from_proposal(capture: Any) -> None:
    _, _, _, text = capture
    summary = summarize_evidence(parse_recording(text.splitlines()))
    wire = summary["wire"]
    assert wire["tool_calls"] == len(SESSION) == wire["tool_calls_forwarded_unchanged"]
    assert wire["tool_calls_forwarded_changed"] == wire["tool_calls_not_forwarded"] == 0
    assert wire["tool_calls_compared_by"] == {"digest": len(SESSION)}  # the bytes, not text
    assert wire["client_answers_not_from_server"] == 0
    observed = summary["observed"]
    # The server validates: the two calls auto would have repaired or refused
    # failed as sent -- observed tool errors, next to the transient one. That
    # auto mode *would* have changed them is a proposal, counted separately.
    assert observed["by_failure_class"] == {"authentication": 1, "none": 2, "tool": 3}
    assert observed["tool_calls_never_answered"] == 1 and observed["server_lost"] == 1
    proposed = summary["proposed"]
    assert proposed["hypothetical"] is True
    assert proposed["would_modify_call"] == 2
    assert proposed["would_modify_by_kind"] == {"refuse": 1, "repair": 1}
    assert summary["stateguard_observed"]["calls_with_schema_violations"] == 2
    # Each tool error on a read_only tool would have been retried in auto.
    assert proposed["would_retry"] == 3 and proposed["would_restart"] == 1
    assert proposed["acted"] == {"retries_sent": 0, "restarts": 0, "responses_dropped": 0}
    assert summary["unknown"]["outcome_unknown"] == 2
    assert summary["unknown"]["answer_seen_later"] == 1
    assert summary["unknown"]["never_answered"] == 1
    assert summary["integrity"]["ground_truth_records"] == 0


def test_a_real_capture_still_replays(capture: Any) -> None:
    _, _, _, text = capture
    env = replay.derive(parse_recording(text.splitlines()))
    assert env.labels == {}  # nothing to judge against: no ground truth
    report = replay.measure([env], {}, repeat_for_determinism=False, overhead_runs=1)
    assert report["corpus"]["evidence"] == Provenance.REAL_CAPTURE.evidence
    assert report["corpus"]["labelled_categories"] == {}
    assert report["limitations"][0].startswith("The corpus is observed traffic (real_capture)")
    assert not any("record_corpus.py" in line for line in report["limitations"])
    for arm, block in report["arms"].items():
        assert block.get("prevented", 0) == 0, arm  # nothing is claimed as prevented
        assert block.get("recovered", 0) == 0, arm


# ---------------------------------------------------------------------------
# Refusals: real capture cannot run outside shadow mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [RelayMode.AUTO, RelayMode.PASSTHROUGH])
def test_the_relay_refuses_real_capture_outside_shadow(mode: RelayMode) -> None:
    recorder = Recorder(io.StringIO(), provenance=Provenance.REAL_CAPTURE, session="s")
    with pytest.raises(ValueError, match="shadow mode"):
        Rig(ScriptedServer([SEARCH]), mode, recorder=recorder)


@pytest.mark.parametrize("provenance", [p for p in Provenance if p is not Provenance.REAL_CAPTURE])
def test_other_provenances_still_record_in_auto(provenance: Provenance) -> None:
    rig = Rig(
        ScriptedServer([SEARCH]),
        RelayMode.AUTO,
        recorder=Recorder(io.StringIO(), provenance=provenance, session="s"),
    )
    rig.close()


def _relay(tmp_path: Path, *flags: str) -> tuple[subprocess.CompletedProcess[bytes], Path]:
    marker = tmp_path / "server-started"
    result = subprocess.run(
        [
            sys.executable,
            str(RELAY),
            *flags,
            "--",
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').close()",
        ],
        input=b"",
        capture_output=True,
        cwd=ROOT,
        timeout=30,
    )
    return result, marker


@pytest.mark.parametrize(
    "flags,complaint",
    [
        (["--record-provenance", "real_capture", "--shadow"], "needs --record"),
        (["--record-provenance", "real_capture", "--record", "{tape}"], "shadow-only"),
        (
            [
                "--record-provenance",
                "real_capture",
                "--record",
                "{tape}",
                "--shadow",
                "--passthrough",
            ],
            "shadow-only",
        ),
        (
            [
                "--record-provenance",
                "real_capture",
                "--record",
                "{tape}",
                "--shadow",
                "--record-redaction",
                "secrets_only",
            ],
            "requires --record-redaction mask",
        ),
        (["--record-provenance", "replay", "--record", "{tape}"], "invalid choice"),
    ],
)
def test_the_cli_refuses_unsafe_real_capture_before_starting_anything(
    tmp_path: Path, flags: list[str], complaint: str
) -> None:
    tape = tmp_path / "tape.jsonl"
    result, marker = _relay(tmp_path, *(f.format(tape=tape) for f in flags))
    assert result.returncode == 2
    assert complaint in result.stderr.decode()
    assert not marker.exists()  # the server never started
    assert not tape.exists()  # nothing recorded


def test_shadow_and_auto_defaults_never_claim_real_capture(tmp_path: Path) -> None:
    for flags in (["--shadow"], []):
        tape = tmp_path / f"tape{len(flags)}.jsonl"
        result, _ = _relay(tmp_path, *flags, "--record", str(tape))
        assert result.returncode == 0, result.stderr
        assert read_recording(tape).provenance is Provenance.LOCAL_DEVELOPMENT


def test_cli_real_capture_against_the_local_fake_server(tmp_path: Path) -> None:
    """Harness, not production evidence: the local fake_mcp_server.py, no network."""
    tape = tmp_path / "capture.jsonl"
    audit = tmp_path / "audit.jsonl"
    secret_arg = SECRETS["github"]
    proc = subprocess.Popen(
        [
            sys.executable,
            str(RELAY),
            "--shadow",
            "--record",
            str(tape),
            "--record-provenance",
            "real_capture",
            "--audit",
            str(audit),
            "--",
            sys.executable,
            str(SERVER),
            str(tmp_path / "server.jsonl"),
            f"--token={secret_arg}",  # a server command line holding a credential
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        cwd=ROOT,
    )
    assert proc.stdin and proc.stdout
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    sent = []
    for n, (method, params) in enumerate(
        [
            ("server/discover", {}),
            ("tools/call", {"name": "search", "arguments": {"query": "x", "limit": "3"}}),
        ],
        start=1,
    ):
        message = {"jsonrpc": "2.0", "id": n, "method": method, "params": {**params, "_meta": meta}}
        sent.append(message)
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()
        while json.loads(proc.stdout.readline()).get("id") != n:
            pass
    proc.stdin.close()
    proc.wait(timeout=10)
    received = [json.loads(line) for line in (tmp_path / "server.jsonl").read_text().splitlines()]
    assert sent[1] in received  # the repairable call reached the server as sent
    recording = read_recording(tape)
    assert recording.complete and recording.provenance is Provenance.REAL_CAPTURE
    assert recording.header["config"]["mode"] == "shadow"
    assert "started_at" in recording.header
    for path in (tape, audit):
        assert secret_arg not in path.read_text(), path.name
    (started,) = [
        json.loads(line)
        for line in audit.read_text().splitlines()
        if json.loads(line).get("event") == "relay_started"
    ]
    assert "server" not in started and started["server_args"] == 3

    report = subprocess.run(
        [sys.executable, str(REPORT), str(tape), "--json"], capture_output=True, cwd=ROOT
    )
    assert report.returncode == 0, report.stderr
    summary = json.loads(report.stdout)
    assert summary["provenance"] == "real_capture" and summary["proposed"]["hypothetical"]
    assert summary["wire"]["tool_calls_forwarded_unchanged"] == 1
    assert summary["wire"]["tool_calls_compared_by"] == {"digest": 1}


def test_the_report_refuses_a_malformed_recording(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type": "header"}\n')
    report = subprocess.run([sys.executable, str(REPORT), str(bad)], capture_output=True, cwd=ROOT)
    assert report.returncode == 2 and b"error:" in report.stderr
