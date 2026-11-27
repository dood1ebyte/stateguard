"""
Tests for the Week 3 reconciliation experiment (benchmarks/domains/mcp_server_filesystem).

``reconcile.py`` is pure; these tests pin its safety rules exhaustively. The
fault shim is exercised against a tiny stdio server. No Node, no relay. The
live experiment and its results are in docs/runtime/WEEK3_RECONCILIATION_EXPERIMENT.md.
"""

from __future__ import annotations

import itertools
import json
import sys
import textwrap
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

_DOMAIN = (
    Path(__file__).resolve().parent.parent / "benchmarks" / "domains" / "mcp_server_filesystem"
)
sys.path.insert(0, str(_DOMAIN))

import reconcile as rc  # noqa: E402
from rawclient import RawSession  # noqa: E402

SRC, DST = "inbox/o.txt", "done/o.txt"
PRE_TEXT = "status: approved\n"
PRE, POST, PATHS = rc.move_spec(SRC, DST, PRE_TEXT)
D = rc.digest(PRE_TEXT)
OPEN = rc.Declaration(exclusive=True, resend_if_not_executed=True)


def reads(
    state: Mapping[str, str | None], *, at: float = 10.0, gen: int | None = 2
) -> list[rc.Read]:
    """Reads of PATHS for a state {path: digest or None (absent)}."""
    out = []
    for p in PATHS:
        v = state[p]
        out.append(rc.Read(p, "enoent" if v is None else "ok", v, at, gen))
    return out


PRE_STATE: dict[str, str | None] = {SRC: D, DST: None}
POST_STATE: dict[str, str | None] = {SRC: None, DST: D}
OTHER_STATE: dict[str, str | None] = {SRC: D, DST: rc.digest("forged\n")}


def ev(
    rs: list[rc.Read], executor: rc.Executor = rc.Executor.TERMINATED, **kw: object
) -> rc.Evidence:
    return rc.Evidence(rs, 1.0, executor, 1, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The safety rules
# ---------------------------------------------------------------------------


def test_strict_never_says_not_executed_without_verified_exclusivity() -> None:
    for executor in rc.Executor:
        a = rc.assess(PRE, POST, ev(reads(PRE_STATE), executor), OPEN, rc.Policy.STRICT)
        assert a.verdict is not rc.Verdict.NOT_EXECUTED


def test_declared_exclusivity_is_what_makes_declared_say_not_executed() -> None:
    a = rc.assess(PRE, POST, ev(reads(PRE_STATE)), OPEN, rc.Policy.DECLARED)
    assert a.verdict is rc.Verdict.NOT_EXECUTED
    closed = rc.Declaration(exclusive=False, resend_if_not_executed=True)
    assert rc.assess(PRE, POST, ev(reads(PRE_STATE)), closed, rc.Policy.DECLARED).verdict is (
        rc.Verdict.INCONCLUSIVE
    )


def test_verified_exclusivity_is_the_only_strict_path_to_not_executed() -> None:
    a = rc.assess(
        PRE, POST, ev(reads(PRE_STATE), exclusivity_verified=True), OPEN, rc.Policy.STRICT
    )
    assert a.verdict is rc.Verdict.NOT_EXECUTED


@pytest.mark.parametrize("policy", list(rc.Policy))
def test_a_live_executor_is_never_not_executed(policy: rc.Policy) -> None:
    for executor in (rc.Executor.ALIVE, rc.Executor.UNKNOWN):
        a = rc.assess(
            PRE, POST, ev(reads(PRE_STATE), executor, exclusivity_verified=True), OPEN, policy
        )
        assert a.verdict is rc.Verdict.UNSATISFIED_OPEN
        ok, _ = rc.resend_eligible(a, OPEN, resends_made=0, executor=executor)
        assert not ok


@pytest.mark.parametrize("policy", list(rc.Policy))
def test_satisfied_never_attributes_or_relabels_execution(policy: rc.Policy) -> None:
    for executor in rc.Executor:
        a = rc.assess(PRE, POST, ev(reads(POST_STATE), executor), OPEN, policy)
        assert a.verdict is rc.Verdict.SATISFIED
        assert a.execution_state == "unknown" and a.attribution == "unknown"
        assert a.late_execution_possible is (executor is not rc.Executor.TERMINATED)
        assert not rc.resend_eligible(a, OPEN, resends_made=0, executor=executor)[0]


@pytest.mark.parametrize("policy", list(rc.Policy))
def test_contradictory_state_is_inconclusive(policy: rc.Policy) -> None:
    a = rc.assess(PRE, POST, ev(reads(OTHER_STATE)), OPEN, policy)
    assert a.verdict is rc.Verdict.INCONCLUSIVE and a.postcondition == "contradictory"


@pytest.mark.parametrize("policy", list(rc.Policy))
@pytest.mark.parametrize(
    "bad",
    [
        "stale_time",
        "stale_generation",
        "inconsistent",
        "error",
        "missing",
        "unavailable",
    ],
)
def test_unusable_evidence_is_inconclusive_even_when_it_shows_the_post_state(
    policy: rc.Policy, bad: str
) -> None:
    rs = reads(POST_STATE)
    if bad == "stale_time":
        rs = reads(POST_STATE, at=0.5)  # before loss_at = 1.0
    elif bad == "stale_generation":
        rs = reads(POST_STATE, gen=1)  # the process whose outcome is unknown
    elif bad == "inconsistent":
        rs[-1] = rc.Read(SRC, "ok", D, 10.0, 2)  # src absent, then present
    elif bad == "error":
        rs[1] = rc.Read(DST, "error", None, 10.0, 2)
    elif bad == "missing":
        rs[2] = rc.Read(SRC, "missing", None, 10.0, None)
    else:
        rs = [rc.Read(p, "missing", None, 10.0, None) for p in PATHS]
    a = rc.assess(PRE, POST, ev(rs), OPEN, policy)
    assert a.verdict is rc.Verdict.INCONCLUSIVE and a.postcondition == "unknown"


def test_resend_requires_every_condition() -> None:
    a = rc.assess(PRE, POST, ev(reads(PRE_STATE)), OPEN, rc.Policy.DECLARED)
    assert rc.resend_eligible(a, OPEN, resends_made=0, executor=rc.Executor.TERMINATED)[0]
    assert not rc.resend_eligible(a, OPEN, resends_made=1, executor=rc.Executor.TERMINATED)[0]
    assert not rc.resend_eligible(a, OPEN, resends_made=0, executor=rc.Executor.ALIVE)[0]
    no = rc.Declaration(exclusive=True, resend_if_not_executed=False)
    assert not rc.resend_eligible(a, no, resends_made=0, executor=rc.Executor.TERMINATED)[0]


def test_no_combination_lets_strict_resend() -> None:
    """Exhaustive over states, executors, declarations and evidence defects."""
    states = [PRE_STATE, POST_STATE, OTHER_STATE]
    for state, executor, excl, again in itertools.product(
        states, rc.Executor, (True, False), (True, False)
    ):
        decl = rc.Declaration(exclusive=excl, resend_if_not_executed=again)
        a = rc.assess(PRE, POST, ev(reads(state), executor), decl, rc.Policy.STRICT)
        assert not rc.resend_eligible(a, decl, resends_made=0, executor=executor)[0]


def test_assess_is_deterministic() -> None:
    e = ev(reads(PRE_STATE))
    outcomes = {repr(rc.assess(PRE, POST, e, OPEN, rc.Policy.DECLARED).to_dict()) for _ in range(5)}
    assert len(outcomes) == 1


def test_append_spec_counts_exactly_one_insertion() -> None:
    pre, post, paths = rc.append_spec("log", "# log\nEND\n", "END", "line")
    once, twice = "# log\nline\nEND\n", "# log\nline\nline\nEND\n"
    assert post.matches({"log": rc.digest(once)})
    assert not post.matches({"log": rc.digest(twice)}) and not pre.matches(
        {"log": rc.digest(twice)}
    )
    assert paths == ["log", "log"]


# ---------------------------------------------------------------------------
# reconcile_shim, against a tiny stdio server
# ---------------------------------------------------------------------------

_TINY = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    marker = Path(sys.argv[1])
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg.get("method") == "tools/call":
            marker.write_text((marker.read_text() if marker.exists() else "") + "x")
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": []}}), flush=True)
    """
)


def _session(tmp_path: Path, fault: str | None) -> tuple[RawSession, Path, Path]:
    (tmp_path / "server.py").write_text(_TINY)
    marker, log = tmp_path / "executed", tmp_path / "deliveries.jsonl"
    flags = ["--fault", fault] if fault else []
    cmd = [
        sys.executable,
        str(_DOMAIN / "reconcile_shim.py"),
        "--gen",
        "7",
        "--deliveries",
        str(log),
        *flags,
        "--",
        sys.executable,
        str(tmp_path / "server.py"),
        str(marker),
    ]
    return RawSession(cmd), marker, log


def _events(log: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in log.read_text().splitlines()]


def _runs(marker: Path) -> int:
    return len(marker.read_text()) if marker.exists() else 0


def test_shim_ledger_records_delivery_and_answer(tmp_path: Path) -> None:
    s, marker, log = _session(tmp_path, None)
    assert s.call("t", {}, rid="a") is not None
    s.close()
    kinds = [e["event"] for e in _events(log)]
    assert kinds[0] == "started" and "delivered" in kinds and "answered" in kinds
    assert _runs(marker) == 1 and all(e["gen"] == 7 for e in _events(log))


def test_shim_drop_response_logs_the_answer_it_drops(tmp_path: Path) -> None:
    s, marker, log = _session(tmp_path, "drop_response:t")
    assert s.call("t", {}, rid="a") is None
    s.close()
    answered = [e for e in _events(log) if e["event"] == "answered"]
    assert answered and answered[0]["dropped"] is True and _runs(marker) == 1
    assert any(e["event"] == "server_reaped" for e in _events(log))


def test_shim_swallow_never_delivers(tmp_path: Path) -> None:
    s, marker, log = _session(tmp_path, "swallow_request:t")
    assert s.call("t", {}, rid="a") is None
    s.close()
    assert _runs(marker) == 0 and not any(e["event"] == "delivered" for e in _events(log))


def test_shim_delay_delivers_late_and_hang_never(tmp_path: Path) -> None:
    s, marker, _ = _session(tmp_path, "delay_request:t:1:0.6")
    assert s.call("t", {}, rid="a", timeout=0.2) == {"_timeout": True}
    assert _runs(marker) == 0  # not yet: the client gave up first
    time.sleep(1.0)
    s.close()
    assert _runs(marker) == 1  # executed after the client gave up
    (tmp_path / "h").mkdir()
    h, hmarker, _ = _session(tmp_path / "h", "hang_request:t")
    assert h.call("t", {}, rid="a", timeout=0.2) == {"_timeout": True}
    h.close()
    assert _runs(hmarker) == 0
