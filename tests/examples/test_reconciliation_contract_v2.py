"""
Reconciliation contract v2: what each outcome may -- and may not -- be taken to mean.

Semantics, not internals: every test drives the relay through a client, reads
ground truth from the scripted servers' shared filesystem dict directly, and
checks the client-visible answer. See docs/runtime/RECONCILIATION_CONTRACT_V2.md.

Covered here:

* termination evidence bound to the executor the call was sent to (wrong
  executor, evidence reused from an earlier restart, a reused identity);
* an external side effect that no executor-termination evidence can cover;
* predicate semantics: satisfied by the original, already satisfied, wrong
  content (presence vs content checks), state changed after the observation,
  a misleading probe server;
* that no outcome is ever worded or keyed as proof of execution, causation,
  durability or success;
* the overall time bound, independent of how many restarts are allowed.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import IO, Any

import pytest

from tests.examples.test_relay_reconciliation import (
    PEEK,
    READ,
    TOOLS,
    First,
    FsServer,
    World,
    _data,
    _events,
    _move,
    _move_entry,
    _never_resent,
    _policies,
    _result,
    _world,
)

# On sys.path via the module above.
from runtime.harness import (  # noqa: E402
    RelayMode,
    Rig,
    ServerAction,
    ServerCall,
    load_relay_module,
)

from stateguard.adapters.mcp import TerminationEvidence  # noqa: E402

pytestmark = pytest.mark.integration

CONTENT = "order 001"
CONTENT_SHA = hashlib.sha256(CONTENT.encode()).hexdigest()

# Probes for "a.txt moved to b.txt with its content intact".
CONTENT_PROBES = [
    {
        "tool": "read",
        "arguments": {"path": "$source"},
        "expect": "absent",
        "absent_marker": "ENOENT",
    },
    {
        "tool": "read",
        "arguments": {"path": "$destination"},
        "expect": {"text_sha256_equals": CONTENT_SHA},
        "absent_marker": "ENOENT",
    },
]


def _restart_with(world: World, evidence: Callable[[Any], TerminationEvidence]) -> None:
    """Replace what the rig's restart reports, keeping the real restart."""
    relay_module = load_relay_module()
    original = world.rig.relay._restart

    def restart() -> Any:
        result = original()
        return relay_module.Restarted(
            result.stdin, result.stdout, evidence(result), result.executor
        )

    world.rig.relay._restart = restart


def _message(response: dict[str, Any] | None) -> str:
    assert response is not None, "no answer"
    return str(response["error"]["message"])


def _assert_no_claim_beyond_observation(data: dict[str, Any], message: str) -> None:
    """What every reconciled answer must say, whatever the postcondition."""
    assert data["io.stateguard/executionState"] == "unknown"
    assert data["io.stateguard/attribution"] == "unknown"
    assert data["io.stateguard/durability"] == "not_established"
    assert data["io.stateguard/operationOutcome"] == "unknown"
    assert data["io.stateguard/reconciliationContract"] == "2"
    not_established = data["io.stateguard/reconciliation"]["does_not_establish"]
    for claim in (
        "that this request executed",
        "that this request caused the observed state",
        "that the observed state still holds",
        "that the operation succeeded",
    ):
        assert claim in not_established
    lowered = message.lower()
    for claim in ("verified", "was successful", "completed successfully", "executed successfully"):
        assert claim not in lowered
    if data["io.stateguard/postcondition"] == "observed_satisfied":
        assert "does not show this request executed" in lowered


# ---------------------------------------------------------------------------
# Termination evidence must apply to the executor the call was sent to
# ---------------------------------------------------------------------------


def test_evidence_naming_another_executor_never_allows_a_probe() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, attest_termination=False)
    _restart_with(
        world, lambda _r: TerminationEvidence.scope_ended("joined", executor="rig-executor-7")
    )
    try:
        response = _move(world)
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "undetermined"
        (reason,) = data["io.stateguard/reconciliation"]["reasons"]
        assert "names a different executor" in reason
        assert not _events(world.rig, "reconcile_probe")
        (evidence,) = _events(world.rig, "executor_termination")
        assert evidence["kind"] == "scope_ended" and evidence["applies"] is False
        _assert_no_claim_beyond_observation(data, _message(response))
        _never_resent(world)
    finally:
        world.rig.close()


def _two_losses() -> Callable[[ServerCall], ServerAction]:
    seen = {"moves": 0}

    def faults(call: ServerCall) -> ServerAction:
        if call.name == "move":
            seen["moves"] += 1
            if seen["moves"] <= 2:
                return ServerAction.CLOSE_AFTER_EXECUTE
        return ServerAction.NORMAL

    return faults


def test_evidence_from_an_earlier_restart_does_not_carry_over() -> None:
    """A valid attestation for generation 0, returned again for generation 1, is stale."""
    fs = {"a.txt": CONTENT, "c.txt": "order 002"}
    faults = _two_losses()
    effects, policy = _policies()
    rig = Rig(
        FsServer(TOOLS, faults=faults, fs=fs),
        RelayMode.AUTO,
        server_factory=lambda: FsServer(TOOLS, faults=faults, fs=fs),
        max_restarts=3,
        restart_policy="after-unknown",
        effects=effects,
        reconcile=policy,
        retry="off",
    )
    world = World(rig, fs)
    first = TerminationEvidence.scope_ended("joined", executor=Rig.executor_name(0))
    _restart_with(world, lambda _r: first)  # always the same evidence object
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        one = _data(rig.call("move", {"source": "a.txt", "destination": "b.txt"}, timeout=8))
        assert one["io.stateguard/postcondition"] == "observed_satisfied"  # it applied once
        two = _data(rig.call("move", {"source": "c.txt", "destination": "d.txt"}, timeout=8))
        assert two["io.stateguard/postcondition"] == "undetermined"
        assert "names a different executor" in two["io.stateguard/reconciliation"]["reasons"][0]
        assert fs == {"b.txt": CONTENT, "d.txt": "order 002"}  # ground truth: both ran
        moves = [
            m
            for line in rig.all_received
            if (m := json.loads(line)).get("method") == "tools/call"
            and m["params"]["name"] == "move"
        ]
        assert sorted(m["id"] for m in moves) == sorted(set(m["id"] for m in moves))  # once each
        assert len(moves) == 2
    finally:
        rig.close()


def test_a_reused_executor_identity_is_rejected_and_never_attests() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, attest_termination=False)
    relay_module = load_relay_module()
    original = world.rig.relay._restart

    def restart() -> Any:
        result = original()
        # Gives the new server the old server's identity, then attests that one.
        return relay_module.Restarted(
            result.stdin,
            result.stdout,
            TerminationEvidence.scope_ended("joined", executor=Rig.executor_name(0)),
            Rig.executor_name(0),
        )

    world.rig.relay._restart = restart
    try:
        data = _data(_move(world))
        # The attestation names generation 0's real identity, so this call is
        # still reconciled; the reused identity only disqualifies generation 1.
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        (rejected,) = _events(world.rig, "executor_identity_rejected")
        assert rejected["generation"] == 1
        assert world.rig.relay._executors[1] is None
    finally:
        world.rig.close()


class _ExternalWorkerServer(FsServer):
    """
    Hands every move to an external worker (a thread outside the server's
    scope) and returns. The worker moves the file only when ``release`` is set.
    """

    release = threading.Event()
    worker_done = threading.Event()

    def _execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != "move":
            return super()._execute(name, arguments)
        fs, src, dst = self.fs, arguments["source"], arguments["destination"]

        def worker() -> None:
            if self.release.wait(10):
                fs[dst] = fs.pop(src)
            self.worker_done.set()

        threading.Thread(target=worker, daemon=True).start()
        return _result("queued")


def test_an_external_side_effect_is_outside_any_termination_evidence() -> None:
    """
    The scope truly ended (serve thread joined) -- and the work still happened
    afterwards, elsewhere. The answer must not suggest "did not execute".
    """
    _ExternalWorkerServer.release = threading.Event()
    _ExternalWorkerServer.worker_done = threading.Event()
    world = _world(
        action=ServerAction.CLOSE_AFTER_EXECUTE,
        server_cls=_ExternalWorkerServer,
    )
    try:
        response = _move(world)
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "observed_not_satisfied"
        not_established = data["io.stateguard/reconciliation"]["does_not_establish"]
        assert "that this request did not execute" in not_established
        assert "does not show the request did not execute" in _message(response)
        _ExternalWorkerServer.release.set()
        assert _ExternalWorkerServer.worker_done.wait(5)
        assert world.fs == {"b.txt": CONTENT}  # ground truth: it executed after the answer
        _never_resent(world)
    finally:
        _ExternalWorkerServer.release.set()
        world.rig.close()


# ---------------------------------------------------------------------------
# Predicate semantics
# ---------------------------------------------------------------------------


def test_satisfied_by_the_original_is_observation_not_proof() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE)
    try:
        response = _move(world)
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        assert world.fs == {"b.txt": CONTENT}  # it did run -- the answer still does not say so
        _assert_no_claim_beyond_observation(data, _message(response))
        observation = data["io.stateguard/observation"]
        assert observation["observed_from"] <= observation["observed_to"]
        assert [p["strength"] for p in observation["predicates"]] == ["presence", "presence"]
        assert observation["content_checked"] is False
        assert "presence-only" in _message(response)
        _never_resent(world)
    finally:
        world.rig.close()


def test_a_predicate_that_already_held_is_satisfied_without_attribution() -> None:
    """The original failed (ENOENT) on a state that already matched the predicate."""

    def already(fs: dict[str, str]) -> None:
        pass

    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, actor=already)
    world.fs.clear()
    world.fs["b.txt"] = CONTENT  # before the call: already "moved"
    try:
        response = _move(world)
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        assert [c.name for c in world.rig.all_executed] == ["move"]  # it ran, and failed
        _assert_no_claim_beyond_observation(data, _message(response))
    finally:
        world.rig.close()


def _forge(fs: dict[str, str]) -> None:
    """Another actor: the source is gone, the destination holds other content."""
    fs.pop("b.txt", None)
    fs["b.txt"] = "order: forged"


def test_wrong_content_passes_a_presence_check_and_says_so() -> None:
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, actor=_forge)
    try:
        response = _move(world)
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        assert world.fs == {"b.txt": "order: forged"}  # the content is wrong
        assert data["io.stateguard/observation"]["content_checked"] is False
        not_established = data["io.stateguard/reconciliation"]["does_not_establish"]
        assert "that any content is correct (presence checks only)" in not_established
        assert "content was not checked" in _message(response)
    finally:
        world.rig.close()


def test_wrong_content_fails_a_content_check() -> None:
    world = _world(
        action=ServerAction.CLOSE_AFTER_EXECUTE,
        actor=_forge,
        entries=(READ, PEEK, _move_entry(CONTENT_PROBES)),
    )
    try:
        data = _data(_move(world))
        assert data["io.stateguard/postcondition"] == "observed_not_satisfied"
        statuses = [p["status"] for p in _events(world.rig, "reconcile_probe")]
        assert statuses == ["satisfied", "unsatisfied"]
    finally:
        world.rig.close()


def test_the_right_content_passes_a_content_check_and_is_still_only_an_observation() -> None:
    world = _world(
        action=ServerAction.CLOSE_AFTER_EXECUTE,
        entries=(READ, PEEK, _move_entry(CONTENT_PROBES)),
    )
    try:
        response = _move(world)
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        observation = data["io.stateguard/observation"]
        assert observation["content_checked"] is True
        assert observation["predicates"][1]["check"] == {
            "expect": "text_sha256_equals",
            "sha256": CONTENT_SHA,
        }
        assert "content was not checked" not in _message(response)
        _assert_no_claim_beyond_observation(data, _message(response))
    finally:
        world.rig.close()


class _ChangesAfterLastRead(FsServer):
    """Another actor restores the source right after the last probe read was answered."""

    changed = threading.Event()

    def _read(self, identifier: Any, arguments: dict[str, Any], writer: IO[bytes]) -> bool:
        answered = super()._read(identifier, arguments, writer)
        if self.reads == 2:
            self.fs["a.txt"] = self.fs.get("b.txt", "")
            self.changed.set()
        return answered


def test_a_state_that_changes_after_the_observation_is_not_reported_durable() -> None:
    _ChangesAfterLastRead.changed = threading.Event()
    world = _world(action=ServerAction.CLOSE_AFTER_EXECUTE, server_cls=_ChangesAfterLastRead)
    try:
        response = _move(world)
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "observed_satisfied"
        assert _ChangesAfterLastRead.changed.wait(5)
        assert "a.txt" in world.fs  # ground truth now: the predicate no longer holds
        assert data["io.stateguard/durability"] == "not_established"
        assert data["io.stateguard/observation"]["observed_to"]  # when it was true
        assert "still holds" in _message(response)
    finally:
        world.rig.close()


class _LyingServer(FsServer):
    """A probe server whose reads report the post-state whatever the truth is."""

    def _read(self, identifier: Any, arguments: dict[str, Any], writer: IO[bytes]) -> bool:
        self.reads += 1
        if arguments.get("path") == "a.txt":
            self._reply(writer, identifier, _result("ENOENT: no such file", error=True))
        else:
            self._reply(writer, identifier, _result(CONTENT))
        return True


def test_a_misleading_probe_server_can_only_mislead_the_observation() -> None:
    """Outside the trust model, and documented: the claim stays an observation."""
    world = _world(action=ServerAction.CLOSE_BEFORE_EXECUTE, server_cls=_LyingServer)
    try:
        response = _move(world)
        data = _data(response)
        assert world.fs == {"a.txt": CONTENT}  # ground truth: never executed
        assert data["io.stateguard/postcondition"] == "observed_satisfied"  # what it was told
        _assert_no_claim_beyond_observation(data, _message(response))
        _never_resent(world)
    finally:
        world.rig.close()


@pytest.mark.parametrize(
    "action",
    [ServerAction.CLOSE_AFTER_EXECUTE, ServerAction.CLOSE_BEFORE_EXECUTE],
)
@pytest.mark.parametrize("probe_mode", ["normal", "rpc_error", "malformed", "hang", "die"])
def test_no_outcome_is_ever_worded_as_proof(action: ServerAction, probe_mode: str) -> None:
    world = _world(
        action=action,
        probe_mode=probe_mode,
        entries=(READ, PEEK, _move_entry(timeout_seconds=0.5)),
    )
    try:
        response = _move(world)
        data = _data(response)
        assert data["io.stateguard/postcondition"] in (
            "observed_satisfied",
            "observed_not_satisfied",
            "undetermined",
        )
        if probe_mode != "normal":
            assert data["io.stateguard/postcondition"] == "undetermined"
        _assert_no_claim_beyond_observation(data, _message(response))
        _never_resent(world)
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# Bounded: the discovery wait does not grow with the restart budget
# ---------------------------------------------------------------------------


class _NeverLists(FsServer):
    def _handle(self, line: bytes, writer: IO[bytes]) -> bool:
        if b'"tools/list"' in line:
            return True  # up, but never usable for probes
        return super()._handle(line, writer)


def test_the_discovery_wait_is_one_budget_however_many_restarts() -> None:
    relay_module = load_relay_module()
    saved = relay_module.DISCOVERY_BUDGET_SECONDS
    relay_module.DISCOVERY_BUDGET_SECONDS = 1.0
    fs = {"a.txt": CONTENT}
    faults = First("move", ServerAction.CLOSE_AFTER_EXECUTE)
    effects, policy = _policies()
    rig = Rig(
        FsServer(TOOLS, faults=faults, fs=fs),
        RelayMode.AUTO,
        server_factory=lambda: _NeverLists(TOOLS, faults=faults, fs=fs),
        max_restarts=8,  # the old loop waited up to (8 + 2) x 10 s here
        restart_policy="after-unknown",
        effects=effects,
        reconcile=policy,
        retry="off",
        attest_termination=True,
    )
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        started = time.monotonic()
        data = _data(_move(World(rig, fs), timeout=30))
        elapsed = time.monotonic() - started
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert (
            "no restarted server became available"
            in (data["io.stateguard/reconciliation"]["reasons"][0])
        )
        assert elapsed < 4.0
    finally:
        relay_module.DISCOVERY_BUDGET_SECONDS = saved
        rig.close()
