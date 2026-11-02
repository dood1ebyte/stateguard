"""
Cross-version regression demonstrations for the Week 3 reconciliation defects.

These tests use only interfaces that exist in every version of the relay since
the Week 3 implementation, so the same file can be run against

1. the pre-audit-fix code (reconstructed byte-exactly; defects A, B, C present),
2. the post-audit-fix baseline (defect D -- prefix routing of relay-owned and
   retry ids -- still present),
3. the current code (all fixed),

which is how ``docs/runtime/WEEK3_RECONCILIATION_CONTRACT_V2.md`` shows that
each test fails on the code it targets. Ordering uses events and thread joins,
never sleeps; answers are counted at end of stream.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from tests.examples.test_relay_reconciliation import (
    TOOLS,
    First,
    FsServer,
    World,
    _data,
    _move,
    _policies,
)

# On sys.path via the module above.
from runtime.harness import META, RelayMode, Rig, ScriptedServer, ServerAction  # noqa: E402

pytestmark = pytest.mark.integration

CLAIMS = ("verified", "observed_satisfied")  # the satisfied outcome, under either contract


def _rig(factory: Any, fs: dict[str, str], action: ServerAction) -> Rig:
    effects, policy = _policies()
    return Rig(
        FsServer(TOOLS, faults=First("move", action), fs=fs),
        RelayMode.AUTO,
        server_factory=factory,
        max_restarts=2,
        restart_policy="after-unknown",
        effects=effects,
        reconcile=policy,
        retry="off",
    )


def _answers(rig: Rig, identifier: Any) -> int:
    rig.close()  # end of stream: every line the relay wrote has been read
    return sum(
        1
        for m in rig.client_received
        if isinstance(m, dict) and m.get("id") == identifier and "method" not in m
    )


def test_defect_a_a_running_old_executor_never_yields_a_satisfied_claim() -> None:
    fs = {"a.txt": "order 001"}
    undone = threading.Event()

    def careless_factory() -> ScriptedServer:
        def old_executor_finishes() -> None:
            fs["a.txt"] = fs.pop("b.txt")
            undone.set()

        threading.Timer(0.5, old_executor_finishes).start()  # the injected fault
        return FsServer(TOOLS, fs=fs)

    rig = _rig(careless_factory, fs, ServerAction.CLOSE_AFTER_EXECUTE)
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        data = _data(_move(World(rig, fs)))
        assert undone.wait(5)
        assert fs == {"a.txt": "order 001"}  # ground truth: the move did not last
        assert data["io.stateguard/postcondition"] not in CLAIMS
    finally:
        rig.close()


def test_defect_b_an_answer_being_written_survives_the_relay_stopping() -> None:
    fs = {"a.txt": "order 001"}

    def broken() -> ScriptedServer:
        raise OSError("cannot start the server")

    rig = _rig(broken, fs, ServerAction.CLOSE_AFTER_EXECUTE)
    relay = rig.relay
    pump_thread = rig._threads[3]
    record = relay._record_verdict
    name = (
        "_settle_reconciliations"
        if hasattr(relay, "_settle_reconciliations")
        else "_abandon_reconciliations"
    )
    settle = getattr(relay, name)
    claimed = threading.Event()

    def record_after_claim(*args: Any, **kwargs: Any) -> None:
        if threading.current_thread() is not pump_thread:
            claimed.set()
            pump_thread.join(timeout=1.0)  # a fixed relay cannot finish stopping meanwhile
        record(*args, **kwargs)

    def settle_after_claim(reason: str) -> None:
        claimed.wait(5)
        settle(reason)

    relay._record_verdict = record_after_claim
    setattr(relay, name, settle_after_claim)
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        _move(World(rig, fs), timeout=10.0)
        assert _answers(rig, rig.sent_ids[-1]) == 1, "the client must get exactly one answer"
    finally:
        rig.close()


def _ask(rig: Rig, identifier: str, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    rig.send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
    return rig.wait_for(identifier, timeout=3.0)


@pytest.mark.parametrize(
    ("identifier", "method", "params"),
    [
        pytest.param(
            "stateguard-reconcile:1",
            "tools/call",
            {"name": "read", "arguments": {"path": "a.txt"}, "_meta": META},
            id="defect_c_probe_prefix",
        ),
        pytest.param(
            "stateguard-relay:999", "tools/list", {"_meta": META}, id="defect_d_own_prefix"
        ),
        pytest.param(
            "stateguard-retry:1",
            "tools/call",
            {"name": "read", "arguments": {"path": "a.txt"}, "_meta": META},
            id="defect_d_retry_prefix",
        ),
    ],
)
def test_a_client_id_resembling_an_internal_id_gets_exactly_one_answer(
    identifier: str, method: str, params: dict[str, Any]
) -> None:
    fs = {"a.txt": "order 001"}
    rig = _rig(lambda: FsServer(TOOLS, fs=fs), fs, ServerAction.NORMAL)
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        response = _ask(rig, identifier, method, params)
        # The server's own answer, while the session is open -- not the relay's
        # "outcome unknown" for a request it never answered, sent at close.
        assert response is not None and "result" in response
        assert _answers(rig, identifier) == 1
    finally:
        rig.close()
