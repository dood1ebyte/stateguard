"""
InvocationLedger: execution state per relayed request.

Unit tests pin each transition; the property test drives random interleavings
of every operation over a few ids and checks the invariants that make the
ledger safe to act on:

* a request's client gets at most one answer: at most one DELIVER per
  invocation, and none after it was answered locally or resolved UNKNOWN;
* UNKNOWN is never rounded: it never becomes COMPLETED or ANSWERED_LOCALLY;
* ANSWERED_LOCALLY is never followed by SENT;
* nothing raises, whatever the order.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stateguard.adapters.mcp import (
    ExecutionState,
    InvocationLedger,
    ResponseDisposition,
)

S, D = ExecutionState, ResponseDisposition


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ledger() -> InvocationLedger:
    return InvocationLedger()


class TestTransitions:
    def test_the_normal_path(self, ledger: InvocationLedger) -> None:
        ledger.received(1, "tools/call", "search")
        ledger.sent(1)
        assert ledger.response(1, is_error=False) is D.DELIVER
        inv = ledger.get(1)
        assert inv is not None and inv.state is S.COMPLETED and inv.is_error is False
        assert inv.history == [S.PENDING, S.SENT, S.COMPLETED]

    def test_a_second_response_is_a_duplicate(self, ledger: InvocationLedger) -> None:
        ledger.received(1, "tools/call")
        ledger.sent(1)
        ledger.response(1, is_error=False)
        assert ledger.response(1, is_error=False) is D.DUPLICATE
        assert ledger.get(1).duplicate_responses == 1  # type: ignore[union-attr]

    def test_an_unknown_id_is_unsolicited(self, ledger: InvocationLedger) -> None:
        assert ledger.response("nope", is_error=False) is D.UNSOLICITED

    def test_ids_of_different_types_are_different(self, ledger: InvocationLedger) -> None:
        ledger.received(1, "tools/call")
        ledger.sent(1)
        assert ledger.response("1", is_error=False) is D.UNSOLICITED
        assert ledger.response(1, is_error=False) is D.DELIVER

    def test_answered_locally_is_final_and_never_sent(self, ledger: InvocationLedger) -> None:
        ledger.received(1, "tools/call")
        ledger.answered_locally(1)
        ledger.sent(1)
        inv = ledger.get(1)
        assert inv is not None and inv.state is S.ANSWERED_LOCALLY
        assert ledger.response(1, is_error=False) is D.DUPLICATE

    def test_transport_close_makes_sent_requests_unknown(self, ledger: InvocationLedger) -> None:
        ledger.received(1, "tools/call")
        ledger.sent(1)
        ledger.received(2, "tools/call")  # pending: never written
        (closed,) = ledger.transport_closed()
        assert closed.request_id == 1 and closed.state is S.UNKNOWN
        assert closed.unknown_reason == "transport_closed"
        assert ledger.get(2).state is S.PENDING  # type: ignore[union-attr]

    def test_a_failed_send_is_unknown_not_unexecuted(self, ledger: InvocationLedger) -> None:
        ledger.received(1, "tools/call")
        ledger.sent(1)
        inv = ledger.send_failed(1)
        assert inv is not None and inv.state is S.UNKNOWN and inv.unknown_reason == "send_failed"

    def test_a_late_answer_is_recorded_not_delivered(self, ledger: InvocationLedger) -> None:
        ledger.received(1, "tools/call")
        ledger.sent(1)
        ledger.transport_closed()
        assert ledger.response(1, is_error=True) is D.LATE
        inv = ledger.get(1)
        assert inv is not None and inv.state is S.UNKNOWN
        assert inv.late_response and inv.late_is_error is True

    def test_deadline_expiry(self) -> None:
        clock = Clock()
        ledger = InvocationLedger(clock=clock)
        ledger.received(1, "tools/call")
        ledger.sent(1)
        clock.now = 0.9
        assert ledger.expire(1.0) == []
        clock.now = 1.0
        (expired,) = ledger.expire(1.0)
        assert expired.unknown_reason == "deadline"
        assert ledger.response(1, is_error=False) is D.LATE

    def test_reusing_a_finished_id_starts_a_new_invocation(self, ledger: InvocationLedger) -> None:
        ledger.received(1, "tools/call")
        ledger.sent(1)
        ledger.response(1, is_error=False)
        ledger.received(1, "tools/call")
        ledger.sent(1)
        assert ledger.response(1, is_error=False) is D.DELIVER  # as without a ledger

    def test_reusing_an_id_in_flight_is_an_anomaly(self, ledger: InvocationLedger) -> None:
        ledger.received(1, "tools/call")
        ledger.received(1, "tools/call")
        assert ledger.get(1).anomalies  # type: ignore[union-attr]

    def test_retention_is_bounded(self) -> None:
        ledger = InvocationLedger(retain=2)
        for i in range(5):
            ledger.received(i, "tools/call")
            ledger.sent(i)
            ledger.response(i, is_error=False)
        assert len(ledger.finished()) == 2
        assert ledger.response(0, is_error=False) is D.UNSOLICITED  # forgotten: pass through

    def test_retain_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            InvocationLedger(retain=0)


# ---------------------------------------------------------------------------
# Property: arbitrary interleavings
# ---------------------------------------------------------------------------

IDS = st.sampled_from([1, 2, "a"])
OPS = st.one_of(
    st.tuples(st.just("received"), IDS),
    st.tuples(st.just("sent"), IDS),
    st.tuples(st.just("answered_locally"), IDS),
    st.tuples(st.just("send_failed"), IDS),
    st.tuples(st.just("response"), IDS, st.booleans()),
    st.tuples(st.just("transport_closed")),
    st.tuples(st.just("expire"), st.floats(0, 2)),
    st.tuples(st.just("tick"), st.floats(0, 1)),
)


@settings(max_examples=600, deadline=None)
@given(ops=st.lists(OPS, max_size=40))
def test_property_one_answer_per_invocation_and_unknown_is_never_rounded(
    ops: list[tuple[Any, ...]],
) -> None:
    clock = Clock()
    ledger = InvocationLedger(clock=clock)
    # Per live invocation (identified by object identity): client answers.
    answers: dict[int, int] = {}
    tracked: dict[int, Any] = {}

    def answer(inv: Any) -> None:
        answers[id(inv)] = answers.get(id(inv), 0) + 1
        tracked[id(inv)] = inv

    for op in ops:
        name = op[0]
        if name == "received":
            inv = ledger.received(op[1], "tools/call")
            tracked[id(inv)] = inv
        elif name == "sent":
            ledger.sent(op[1])
        elif name == "answered_locally":
            before = ledger.get(op[1])
            was_pending = before is not None and before.state is S.PENDING
            ledger.answered_locally(op[1])
            if was_pending:
                answer(before)
        elif name == "send_failed":
            inv = ledger.send_failed(op[1])
            if inv is not None:
                answer(inv)  # the proxy tells the client "unknown"
        elif name == "response":
            before = ledger.get(op[1])
            if ledger.response(op[1], is_error=op[2]) is D.DELIVER:
                assert before is not None
                answer(before)
        elif name == "transport_closed":
            for inv in ledger.transport_closed():
                answer(inv)
        elif name == "expire":
            for inv in ledger.expire(op[1]):
                answer(inv)
        else:
            clock.now += op[1]

    for key, count in answers.items():
        assert count <= 1, tracked[key].to_dict()
    for inv in tracked.values():
        history = inv.history
        if S.UNKNOWN in history:
            after = history[history.index(S.UNKNOWN) + 1 :]
            assert after == [], history
        if S.ANSWERED_LOCALLY in history:
            assert S.SENT not in history[history.index(S.ANSWERED_LOCALLY) :], history
        assert history[0] is S.PENDING
        assert len(set(history)) == len(history), history  # never revisits a state
