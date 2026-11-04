"""
The asynchronous two-phase execution of one keyed tool call, independent of
the MCP SDK version. ``idempotency_sdk`` (``mcp`` 2.x) and
``idempotency_sdk1`` (``mcp`` 1.x) answer their keyed calls with it, so both
apply the same claim, wait, recovery and record rules. The contract and its
crash windows are ``stateguard.adapters.mcp.idempotency``.

Store calls block on SQLite, so they run in a worker thread; the server's own
handler runs on its event loop. Needs ``anyio``, which every ``mcp`` release
depends on.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import anyio

from stateguard.adapters.mcp.idempotency import (
    BeginStatus,
    Execution,
    IdempotencyStore,
    NotExecuted,
    Outcome,
    Recovery,
    StoreUnavailable,
    execution_for,
    fingerprint,
    recover_abandoned,
)

__all__ = ["KeyedPoint", "PointHook", "Unrecorded", "current_key", "execute_two_phase"]

#: Where a keyed call stands, for test-only fault injection: before its key is
#: claimed, after the claim (before the handler runs), after the handler
#: answered (before its record), and after the record (before the answer).
KeyedPoint = Literal["before_begin", "after_begin", "after_effect", "after_record"]
PointHook = Callable[[KeyedPoint, str], Awaitable[None]]

_CURRENT_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "stateguard_idempotency_key", default=None
)


def current_key() -> str | None:
    """The idempotency key of the keyed call this handler is executing, or None."""
    return _CURRENT_KEY.get()


@dataclass(frozen=True)
class Unrecorded:
    """
    A handler answer that is not the call's outcome (2026-07-28 "input
    required": the client must supply input and retry). It is passed through
    unrecorded, and the key is freed: the request did not complete.
    """

    answer: Any


async def execute_two_phase(
    store: IdempotencyStore,
    key: str,
    tool: str,
    arguments: dict[str, Any],
    run: Callable[[], Awaitable[Any]],
    *,
    settle: Callable[[Any], Any],
    recover: Callable[[str, dict[str, Any]], Recovery] | None = None,
    wait_seconds: float = 5.0,
    on_point: PointHook | None = None,
) -> Execution | Unrecorded:
    """
    Run the server's handler, *run()*, at most once for (*key*, *tool*,
    *arguments*), and say what happened.

    *settle(answer)* turns the handler's answer into the JSON to record, or
    returns ``Unrecorded(answer)``; it raises for an answer that is not a
    tools/call result, which leaves the key in doubt. *recover(key,
    arguments)* settles an abandoned attempt (see the contract's "Recovery of
    abandoned attempts"); its ``found`` result must already be that JSON. It
    runs in a worker thread.
    """

    async def point(where: KeyedPoint) -> None:
        if on_point is not None:
            await on_point(where, tool)

    fp = fingerprint(tool, arguments)

    def begin() -> Any:
        return store.begin(key, fp, tool=tool)

    async def waited(begun: Any) -> Any:
        # A duplicate of a call still running (or recovering) here: wait for it.
        if begun.status is BeginStatus.IN_PROGRESS and await anyio.to_thread.run_sync(
            store.wait, key, wait_seconds
        ):
            return await anyio.to_thread.run_sync(begin)
        return begun

    await point("before_begin")
    try:
        begun = await waited(await anyio.to_thread.run_sync(begin))
        if begun.status is BeginStatus.IN_DOUBT and recover is not None:
            begun = await waited(
                await anyio.to_thread.run_sync(lambda: store.adopt(key, fp, tool=tool))
            )
    except StoreUnavailable:
        return Execution(Outcome.REJECTED_STORE, None)
    if begun.status is BeginStatus.RECOVER and recover is not None:
        check = recover

        def settle_abandoned() -> Execution | None:
            return recover_abandoned(store, key, lambda: check(key, arguments))

        settled = await anyio.to_thread.run_sync(settle_abandoned)
        if settled is not None:
            return settled
    elif begun.status is not BeginStatus.EXECUTE:
        return execution_for(begun)
    await point("after_begin")
    token = _CURRENT_KEY.set(key)
    try:
        record = settle(await run())
    except NotExecuted as exc:
        _quietly(store.abort, key)
        return Execution(Outcome.NOT_EXECUTED, None, exc)
    except BaseException as exc:
        # Cancelled, or failed: it may have acted. In doubt, never re-run.
        _quietly(store.mark_in_doubt, key, f"{type(exc).__name__}: {exc}")
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            raise
        return Execution(Outcome.IN_DOUBT, None, exc)
    finally:
        _CURRENT_KEY.reset(token)
    if isinstance(record, Unrecorded):
        # Not complete: the client retries with its input. Nothing acted yet.
        _quietly(store.abort, key)
        return record
    await point("after_effect")
    try:
        await anyio.to_thread.run_sync(store.complete, key, record)
    except StoreUnavailable:
        return Execution(Outcome.EXECUTED_UNRECORDED, record)
    await point("after_record")
    return Execution(Outcome.EXECUTED, record)


def _quietly(action: Callable[..., Any], *args: Any) -> None:
    # On a store error the key stays as it was: started, so in doubt -- never free.
    with contextlib.suppress(StoreUnavailable):
        action(*args)
