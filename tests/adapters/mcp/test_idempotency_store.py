"""
The server-side idempotency contract (``stateguard.adapters.mcp.idempotency``).

Crashes are real: a child process claims a key and dies (``os._exit``) at a
chosen point. Ground truth is an effect file (two-phase) or an effect table in
the store's database (transactional), read directly -- never the store's own
record of what happened.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from stateguard.adapters.mcp.idempotency import (
    IDEMPOTENCY_OUTCOME,
    NOT_EXECUTED_OUTCOMES,
    Begin,
    BeginStatus,
    IdempotencyStore,
    KeyedExecutor,
    NotExecuted,
    Outcome,
    Recovery,
    StoreUnavailable,
    execution_for,
    fingerprint,
    not_executed_outcome,
    outcome_error,
)

FP = fingerprint("pay", {"amount": 5})

# A child that runs one two-phase or transactional call and dies at POINT.
# "marked": an exclusive store, and the effect line carries its key (recoverable).
# "recover_crash": an exclusive store that adopts the key and dies inside the check.
CHILD = r"""
import json, os, sys
from stateguard.adapters.mcp.idempotency import IdempotencyStore, KeyedExecutor, fingerprint
db, effects, mode, point, key = sys.argv[1:6]
store = IdempotencyStore(db, exclusive=mode in ("marked", "recover_crash"))

def crash(here):
    if here == point:
        os._exit(137)

if mode == "recover_crash":
    KeyedExecutor(store).execute(key, "pay", {"amount": 5}, lambda: 1, recover=lambda: os._exit(137))
elif mode in ("two_phase", "marked"):
    line = "paid\t" + key + "\n" if mode == "marked" else "paid\n"
    crash("before_begin")
    executor = KeyedExecutor(store)
    def run():
        crash("after_begin")
        with open(effects, "a") as out:
            if point == "mid_effect":
                out.write(line[: len(line) // 2]); out.flush(); os.fsync(out.fileno())
                os._exit(137)
            out.write(line); out.flush(); os.fsync(out.fileno())
        crash("after_effect")
        return {"paid": 5}
    # complete() runs inside execute(); "after_record" is checked after it returns
    execution = executor.execute(key, "pay", {"amount": 5}, run)
    crash("after_record")
    print(json.dumps({"outcome": execution.outcome.value}))
else:
    import sqlite3
    with store.connect() as db_:
        db_.execute("CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, amount INT)")
    def effect(conn):
        crash("after_begin")
        conn.execute("INSERT INTO payments (amount) VALUES (5)")
        crash("mid_effect")
        crash("after_effect")
        return {"paid": 5}
    crash("before_begin")
    execution = store.transact(key, fingerprint("pay", {"amount": 5}), effect, tool="pay")
    crash("after_record")
    print(json.dumps({"outcome": execution.outcome.value}))
"""


def _child(tmp: Path, mode: str, point: str, key: str = "k1") -> int:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            CHILD,
            str(tmp / "keys.db"),
            str(tmp / "effects.txt"),
            mode,
            point,
            key,
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return proc.returncode


def _ledger_lines(tmp: Path) -> list[str]:
    path = tmp / "effects.txt"
    return path.read_text().split("\n") if path.exists() else []


def _payments(tmp: Path) -> int:
    with sqlite3.connect(tmp / "keys.db") as db:
        try:
            return int(db.execute("SELECT COUNT(*) FROM payments").fetchone()[0])
        except sqlite3.OperationalError:
            return 0


def _resend(tmp: Path, *, transactional: bool = False) -> tuple[Outcome, list[int]]:
    """A restarted server (new process, new instance) receives the resend."""
    store = IdempotencyStore(tmp / "keys.db")
    ran: list[int] = []
    if transactional:
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, amount INT)")

        def effect(conn: sqlite3.Connection) -> Any:
            ran.append(1)
            conn.execute("INSERT INTO payments (amount) VALUES (5)")
            return {"paid": 5}

        return store.transact("k1", FP, effect, tool="pay").outcome, ran

    def run() -> Any:
        ran.append(1)
        with open(tmp / "effects.txt", "a") as out:
            out.write("paid\n")
        return {"paid": 5}

    return KeyedExecutor(store).execute("k1", "pay", {"amount": 5}, run).outcome, ran


# -- crash windows: two-phase -------------------------------------------------------

#: point -> (resend outcome, resend executes?, complete "paid" lines on disk after)
TWO_PHASE = {
    "before_begin": (Outcome.EXECUTED, True, 1),  # nothing claimed: the resend is the execution
    "after_begin": (Outcome.IN_DOUBT, False, 0),  # claimed, never acted: conservatively unknown
    "mid_effect": (Outcome.IN_DOUBT, False, 0),  # half an effect: unknown, never re-run
    "after_effect": (Outcome.IN_DOUBT, False, 1),  # the documented crash window: acted, unknown
    "after_record": (Outcome.REPLAYED, False, 1),  # recorded: replayed, not re-run
}


@pytest.mark.parametrize("point", list(TWO_PHASE))
def test_two_phase_crash_windows(tmp_path: Path, point: str) -> None:
    assert _child(tmp_path, "two_phase", point) == 137
    outcome, ran = _resend(tmp_path)
    expected, executes, lines = TWO_PHASE[point]
    assert outcome is expected
    assert bool(ran) is executes
    assert _ledger_lines(tmp_path).count("paid") == lines  # ground truth: never two
    if point == "mid_effect":
        assert _ledger_lines(tmp_path)[-1] == "pa"  # half of "paid\n": still there


# -- crash windows: two-phase with recovery ---------------------------------------------


def _recovering_resend(tmp: Path, checks: list[str] | None = None) -> tuple[Outcome, list[int]]:
    """
    A restarted server that recovers: it holds the store exclusively, cuts a
    torn last line (an interrupted append), and checks for the key's own line.
    """
    store = IdempotencyStore(tmp / "keys.db", exclusive=True)
    path = tmp / "effects.txt"
    if path.exists():
        data = path.read_bytes()
        path.write_bytes(data[: data.rfind(b"\n") + 1])
    ran: list[int] = []

    def check() -> Recovery:
        if checks is not None:
            checks.append("checked")
        lines = path.read_text().split("\n")[:-1] if path.exists() else []
        return Recovery.found({"paid": 5}) if "paid\tk1" in lines else Recovery.absent()

    def run() -> Any:
        ran.append(1)
        with open(path, "a") as out:
            out.write("paid\tk1\n")
        return {"paid": 5}

    try:
        execution = KeyedExecutor(store).execute("k1", "pay", {"amount": 5}, run, recover=check)
    finally:
        store.close()
    return execution.outcome, ran


#: point -> (resend outcome, resend executes?)
RECOVERED = {
    "before_begin": (Outcome.EXECUTED, True),
    "after_begin": (Outcome.EXECUTED, True),  # abandoned, the key's line absent: executed once
    "mid_effect": (Outcome.EXECUTED, True),  # the torn line cut, then absent
    "after_effect": (Outcome.RECOVERED, False),  # the key's own line: recorded from it
    "after_record": (Outcome.REPLAYED, False),
}


@pytest.mark.parametrize("point", list(RECOVERED))
def test_two_phase_crash_windows_close_with_recovery(tmp_path: Path, point: str) -> None:
    assert _child(tmp_path, "marked", point) == 137
    outcome, ran = _recovering_resend(tmp_path)
    expected, executes = RECOVERED[point]
    assert outcome is expected and bool(ran) is executes
    assert (tmp_path / "effects.txt").read_text() == "paid\tk1\n"  # exactly once, nothing torn
    # And settled for good: the next resend replays without a check.
    checks: list[str] = []
    assert _recovering_resend(tmp_path, checks)[0] is Outcome.REPLAYED and not checks


def test_a_crash_during_recovery_leaves_the_key_abandoned_again(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "keys.db")
    assert store.begin("k1", FP).status is BeginStatus.EXECUTE
    store.mark_in_doubt("k1", "cut off")
    assert _child(tmp_path, "recover_crash", "none") == 137  # adopted, died in the check
    assert store.state_of("k1") == "started"
    outcome, ran = _recovering_resend(tmp_path)
    assert outcome is Outcome.EXECUTED and ran == [1]


def test_a_dead_non_exclusive_owner_is_never_presumed_dead(tmp_path: Path) -> None:
    # Its process died, but nothing proves that: it held no lock.
    assert _child(tmp_path, "two_phase", "after_begin") == 137
    checks: list[str] = []
    outcome, ran = _recovering_resend(tmp_path, checks)
    assert outcome is Outcome.IN_DOUBT and not ran and not checks


def test_an_exclusive_store_has_one_live_instance(tmp_path: Path) -> None:
    first = IdempotencyStore(tmp_path / "keys.db", exclusive=True)
    with pytest.raises(StoreUnavailable, match="exclusively"):
        IdempotencyStore(tmp_path / "keys.db", exclusive=True, timeout_seconds=0.2)
    # Another scope is another lock.
    IdempotencyStore(tmp_path / "keys.db", scope="other", exclusive=True).close()
    assert first.begin("k", FP).status is BeginStatus.EXECUTE
    with pytest.raises(RuntimeError):
        first.close()  # a key is still executing here
    first.complete("k", 1)
    first.close()
    IdempotencyStore(tmp_path / "keys.db", exclusive=True, timeout_seconds=0.2).close()


def test_an_exclusive_store_that_fails_to_open_holds_no_lock(tmp_path: Path) -> None:
    (tmp_path / "keys.db").mkdir()  # not a database: opening it fails
    for _ in range(2):
        with pytest.raises(StoreUnavailable) as failure:
            IdempotencyStore(tmp_path / "keys.db", exclusive=True, timeout_seconds=0.2)
        assert "exclusively" not in str(failure.value)


def test_adopt_has_one_taker_and_never_takes_a_live_attempt(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "keys.db", exclusive=True)
    assert store.adopt("k", FP).status is BeginStatus.EXECUTE  # unknown key: claimed
    assert store.adopt("k", FP).status is BeginStatus.IN_PROGRESS  # live here: not abandoned
    store.mark_in_doubt("k", "handler raised")
    assert store.adopt("k", fingerprint("pay", {"amount": 6})).status is BeginStatus.MISMATCH
    assert store.adopt("k", FP).status is BeginStatus.RECOVER
    assert store.adopt("k", FP).status is BeginStatus.IN_PROGRESS  # one taker
    # A non-exclusive instance cannot prove the exclusive taker gone: in doubt.
    assert IdempotencyStore(tmp_path / "keys.db").adopt("k", FP).status is BeginStatus.IN_DOUBT
    store.complete("k", {"paid": 5})
    assert store.adopt("k", FP).status is BeginStatus.REPLAY


@pytest.mark.parametrize(
    "verdict, outcome, runs, state",
    [
        (lambda: Recovery.found({"receipt": 9}), Outcome.RECOVERED, 0, "done"),
        (Recovery.absent, Outcome.EXECUTED, 1, "done"),
        (lambda: Recovery.unknown("cannot tell"), Outcome.IN_DOUBT, 0, "in_doubt"),
        (lambda: 1 / 0, Outcome.IN_DOUBT, 0, "in_doubt"),
        (lambda: "yes", Outcome.IN_DOUBT, 0, "in_doubt"),  # not a Recovery: fail closed
    ],
)
def test_recovery_verdicts(
    tmp_path: Path, verdict: Any, outcome: Outcome, runs: int, state: str
) -> None:
    store = IdempotencyStore(tmp_path / "keys.db")
    executor = KeyedExecutor(store)

    def boom() -> Any:
        raise RuntimeError("cut off")

    assert executor.execute("k", "pay", {"amount": 5}, boom).outcome is Outcome.IN_DOUBT
    ran: list[int] = []
    run = lambda: ran.append(1) or {"paid": 5}  # noqa: E731
    settled = executor.execute("k", "pay", {"amount": 5}, run, recover=verdict)
    assert settled.outcome is outcome and len(ran) == runs
    assert store.state_of("k") == state
    if outcome is Outcome.RECOVERED:
        assert settled.result == {"receipt": 9}
        replay = executor.execute("k", "pay", {"amount": 5}, run, recover=verdict)
        assert replay.outcome is Outcome.REPLAYED and replay.result == {"receipt": 9}
    if state == "in_doubt":  # repeated: checked again each time, never run
        again = executor.execute("k", "pay", {"amount": 5}, run, recover=verdict)
        assert again.outcome is Outcome.IN_DOUBT and not ran


def test_concurrent_resends_of_an_abandoned_key_check_and_run_once(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "keys.db")
    executor = KeyedExecutor(store, wait_seconds=5)
    assert store.begin("k", FP).status is BeginStatus.EXECUTE
    store.mark_in_doubt("k", "cut off")
    checks: list[int] = []
    ran: list[int] = []
    barrier = threading.Barrier(4)
    outcomes: list[Outcome] = []

    def check() -> Recovery:
        checks.append(1)
        time.sleep(0.3)
        return Recovery.absent()

    def run() -> Any:
        ran.append(1)
        return {"paid": 5}

    def worker() -> None:
        barrier.wait()
        outcomes.append(executor.execute("k", "pay", {"amount": 5}, run, recover=check).outcome)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(checks) == 1 and len(ran) == 1
    assert sorted(outcomes) == sorted([Outcome.EXECUTED] + [Outcome.REPLAYED] * 3)


def test_a_claimed_key_is_not_an_outcome() -> None:
    for status in (BeginStatus.EXECUTE, BeginStatus.RECOVER):
        with pytest.raises(ValueError):
            execution_for(Begin(status))


# -- crash windows: transactional -----------------------------------------------------

TRANSACTIONAL = {
    "before_begin": (Outcome.EXECUTED, True),
    "after_begin": (Outcome.EXECUTED, True),  # inside the transaction: rolled back
    "mid_effect": (Outcome.EXECUTED, True),  # the insert was never committed
    "after_effect": (Outcome.EXECUTED, True),
    "after_record": (Outcome.REPLAYED, False),  # committed with its record
}


@pytest.mark.parametrize("point", list(TRANSACTIONAL))
def test_transactional_crash_windows_are_exactly_once(tmp_path: Path, point: str) -> None:
    assert _child(tmp_path, "transactional", point) == 137
    outcome, ran = _resend(tmp_path, transactional=True)
    expected, executes = TRANSACTIONAL[point]
    assert outcome is expected and bool(ran) is executes
    assert _payments(tmp_path) == 1  # exactly one payment, whatever the crash point


# -- ownership and concurrency -----------------------------------------------------------


def test_in_progress_here_in_doubt_elsewhere(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "keys.db")
    assert store.begin("k", FP).status is BeginStatus.EXECUTE
    assert store.begin("k", FP).status is BeginStatus.IN_PROGRESS
    assert IdempotencyStore(tmp_path / "keys.db").begin("k", FP).status is BeginStatus.IN_DOUBT


def test_concurrent_duplicates_in_one_process_run_once_and_replay(tmp_path: Path) -> None:
    executor = KeyedExecutor(IdempotencyStore(tmp_path / "keys.db"), wait_seconds=5)
    calls: list[int] = []
    start = threading.Barrier(4)
    outcomes: list[Outcome] = []

    def run() -> Any:
        calls.append(1)
        time.sleep(0.3)
        return {"paid": 5}

    def worker() -> None:
        start.wait()
        outcomes.append(executor.execute("k", "pay", {"amount": 5}, run).outcome)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert sorted(outcomes) == sorted([Outcome.EXECUTED] + [Outcome.REPLAYED] * 3)


def test_concurrent_duplicates_across_processes_never_run_twice(tmp_path: Path) -> None:
    # Two "server processes" (store instances) race for one key.
    stores = [IdempotencyStore(tmp_path / "keys.db") for _ in range(2)]
    results: list[BeginStatus] = []
    barrier = threading.Barrier(2)

    def race(store: IdempotencyStore) -> None:
        barrier.wait()
        results.append(store.begin("k", FP).status)

    threads = [threading.Thread(target=race, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == sorted([BeginStatus.EXECUTE, BeginStatus.IN_DOUBT])


def test_transactional_concurrent_duplicates_are_serialised(tmp_path: Path) -> None:
    stores = [IdempotencyStore(tmp_path / "keys.db", timeout_seconds=10) for _ in range(3)]
    with stores[0].connect() as db:
        db.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, amount INT)")
    outcomes: list[Outcome] = []
    barrier = threading.Barrier(3)

    def effect(conn: sqlite3.Connection) -> Any:
        conn.execute("INSERT INTO payments (amount) VALUES (5)")
        time.sleep(0.2)
        return {"paid": 5}

    def race(store: IdempotencyStore) -> None:
        barrier.wait()
        outcomes.append(store.transact("k", FP, effect, tool="pay").outcome)

    threads = [threading.Thread(target=race, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == sorted([Outcome.EXECUTED, Outcome.REPLAYED, Outcome.REPLAYED])
    assert _payments(tmp_path) == 1


# -- misuse, persistence, failures ---------------------------------------------------------


def test_same_key_other_arguments_or_tool_is_rejected(tmp_path: Path) -> None:
    executor = KeyedExecutor(IdempotencyStore(tmp_path / "keys.db"))
    ran: list[str] = []
    assert executor.execute("k", "pay", {"amount": 5}, lambda: ran.append("a") or 1).outcome is (
        Outcome.EXECUTED
    )
    other_args = executor.execute("k", "pay", {"amount": 6}, lambda: ran.append("b") or 2)
    other_tool = executor.execute("k", "refund", {"amount": 5}, lambda: ran.append("c") or 3)
    assert other_args.outcome is other_tool.outcome is Outcome.REJECTED_MISMATCH
    assert ran == ["a"]


def test_a_restarted_server_replays_a_persisted_result(tmp_path: Path) -> None:
    first = KeyedExecutor(IdempotencyStore(tmp_path / "keys.db"))
    assert first.execute("k", "pay", {"amount": 5}, lambda: {"receipt": 42}).outcome is (
        Outcome.EXECUTED
    )
    restarted = KeyedExecutor(IdempotencyStore(tmp_path / "keys.db"))
    replay = restarted.execute("k", "pay", {"amount": 5}, lambda: pytest.fail("ran twice"))
    assert replay.outcome is Outcome.REPLAYED and replay.result == {"receipt": 42}


def test_a_corrupted_store_fails_closed(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "keys.db")
    (tmp_path / "keys.db").write_bytes(b"this is not a database" * 100)
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"keys.db{suffix}").unlink(missing_ok=True)
    ran: list[int] = []
    execution = KeyedExecutor(store).execute("k", "pay", {"amount": 5}, lambda: ran.append(1))
    assert execution.outcome is Outcome.REJECTED_STORE and not ran


def test_an_unreachable_store_cannot_be_opened(tmp_path: Path) -> None:
    with pytest.raises(StoreUnavailable):
        IdempotencyStore(tmp_path / "missing" / "dir" / "keys.db")


def test_a_locked_store_fails_closed(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "keys.db", timeout_seconds=0.2)
    holder = sqlite3.connect(tmp_path / "keys.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        ran: list[int] = []
        execution = KeyedExecutor(store).execute("k", "pay", {"amount": 5}, lambda: ran.append(1))
        assert execution.outcome is Outcome.REJECTED_STORE and not ran
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_result_that_cannot_be_recorded_leaves_the_key_in_doubt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = IdempotencyStore(tmp_path / "keys.db")
    real_complete = store.complete

    def failing_complete(key: str, result: Any) -> None:
        store._active.discard(key)  # as complete() does, whatever happens
        raise StoreUnavailable("disk full")

    monkeypatch.setattr(store, "complete", failing_complete)
    execution = KeyedExecutor(store).execute("k", "pay", {"amount": 5}, lambda: {"ok": 1})
    assert execution.outcome is Outcome.EXECUTED_UNRECORDED and execution.result == {"ok": 1}
    monkeypatch.setattr(store, "complete", real_complete)
    again = KeyedExecutor(IdempotencyStore(tmp_path / "keys.db"))
    assert again.execute("k", "pay", {"amount": 5}, lambda: pytest.fail("re-run")).outcome is (
        Outcome.IN_DOUBT
    )


def test_a_handler_that_proves_it_did_nothing_frees_the_key(tmp_path: Path) -> None:
    executor = KeyedExecutor(IdempotencyStore(tmp_path / "keys.db"))

    def reject() -> Any:
        raise NotExecuted("invalid amount")

    assert executor.execute("k", "pay", {"amount": 5}, reject).outcome is Outcome.NOT_EXECUTED
    assert executor.execute("k", "pay", {"amount": 5}, lambda: 1).outcome is Outcome.EXECUTED


def test_a_handler_that_fails_otherwise_leaves_the_key_in_doubt(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "keys.db")
    executor = KeyedExecutor(store)

    def boom() -> Any:
        raise RuntimeError("payment gateway timed out")

    failed = executor.execute("k", "pay", {"amount": 5}, boom)
    assert failed.outcome is Outcome.IN_DOUBT and isinstance(failed.error, RuntimeError)
    assert store.state_of("k") == "in_doubt"
    # Repeated attempts stay in doubt and never run.
    for _ in range(3):
        assert executor.execute("k", "pay", {"amount": 5}, lambda: pytest.fail("ran")).outcome is (
            Outcome.IN_DOUBT
        )


def test_operator_release_and_resolve_are_the_only_ways_out_of_doubt(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "keys.db")
    executor = KeyedExecutor(store)
    executor.execute("a", "pay", {"amount": 5}, lambda: (_ for _ in ()).throw(RuntimeError()))
    executor.execute("b", "pay", {"amount": 5}, lambda: (_ for _ in ()).throw(RuntimeError()))
    assert store.purge() == {"expired": 0, "deleted": 0}  # doubt never expires
    assert store.release("a")  # the operator checked: it did not run
    assert executor.execute("a", "pay", {"amount": 5}, lambda: 1).outcome is Outcome.EXECUTED
    assert store.resolve("b", {"receipt": 7})  # the operator checked: it ran
    replay = executor.execute("b", "pay", {"amount": 5}, lambda: pytest.fail("ran"))
    assert replay.outcome is Outcome.REPLAYED and replay.result == {"receipt": 7}


# -- retention, expiry, key reuse ---------------------------------------------------------


def test_expiry_tombstones_and_the_reuse_horizon(tmp_path: Path) -> None:
    now = [1000.0]
    store = IdempotencyStore(
        tmp_path / "keys.db", retention_seconds=10, tombstone_seconds=100, clock=lambda: now[0]
    )
    executor = KeyedExecutor(store)
    assert executor.execute("k", "pay", {"amount": 5}, lambda: 1).outcome is Outcome.EXECUTED
    now[0] += 11  # past retention: executed earlier, result gone
    assert executor.execute("k", "pay", {"amount": 5}, lambda: pytest.fail("ran")).outcome is (
        Outcome.EXPIRED
    )
    assert store.purge() == {"expired": 1, "deleted": 0}
    assert store.state_of("k") == "expired"
    assert executor.execute("k", "pay", {"amount": 5}, lambda: pytest.fail("ran")).outcome is (
        Outcome.EXPIRED
    )
    now[0] += 100  # past the tombstone horizon: the key is forgotten
    assert store.purge() == {"expired": 0, "deleted": 1}
    # Documented hazard: a key reused after the horizon executes as new.
    assert executor.execute("k", "pay", {"amount": 5}, lambda: 2).outcome is Outcome.EXECUTED


def test_scopes_are_separate(tmp_path: Path) -> None:
    a = KeyedExecutor(IdempotencyStore(tmp_path / "keys.db", scope="tenant-a"))
    b = KeyedExecutor(IdempotencyStore(tmp_path / "keys.db", scope="tenant-b"))
    assert a.execute("k", "pay", {"amount": 5}, lambda: 1).outcome is Outcome.EXECUTED
    assert b.execute("k", "pay", {"amount": 5}, lambda: 2).outcome is Outcome.EXECUTED


def test_retention_must_not_exceed_the_tombstone_horizon(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        IdempotencyStore(tmp_path / "keys.db", retention_seconds=10, tombstone_seconds=5)


# -- wire helpers ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", sorted(NOT_EXECUTED_OUTCOMES))
def test_every_not_executed_outcome_round_trips(outcome: Outcome) -> None:
    error = outcome_error(7, outcome, "k")
    assert error["error"]["data"][IDEMPOTENCY_OUTCOME] == outcome.value
    assert not_executed_outcome(error) is outcome
    json.dumps(error)


def test_executed_outcomes_are_never_errors() -> None:
    for outcome in (
        Outcome.EXECUTED,
        Outcome.REPLAYED,
        Outcome.RECOVERED,
        Outcome.EXECUTED_UNRECORDED,
    ):
        with pytest.raises(ValueError):
            outcome_error(1, outcome, "k")
    assert not_executed_outcome({"error": {"data": {IDEMPOTENCY_OUTCOME: "executed"}}}) is None
    assert not_executed_outcome({"result": {}}) is None
