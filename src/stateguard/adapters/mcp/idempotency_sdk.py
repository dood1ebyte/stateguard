"""
Idempotency keys for a server built on the official Python MCP SDK (``mcp`` 2.x).

The contract and its crash windows are ``stateguard.adapters.mcp.idempotency``.
Configure the keyed tools once, then attach them one of two ways:

    store = IdempotencyStore("orders.db", scope="orders")
    keyed = KeyedTools(store)
    keyed.two_phase("append_ledger")                 # any side effect
    keyed.transactional("create_order", insert_order)  # effect in the store's database
    # or, with a recovery check (needs IdempotencyStore(..., exclusive=True)):
    keyed.two_phase("append_ledger", recover=ledger_has_key)

    # (a) any mcp 2.x server -- MCPServer (the former FastMCP) or a low-level
    #     Server -- on any transport, as middleware:
    app = MCPServer("orders", middleware=[keyed.middleware()])

    # (b) or by wrapping a low-level Server's own handlers:
    server = Server(
        "orders",
        on_list_tools=keyed.wrap_list(list_tools),
        on_call_tool=keyed.wrap_call(call_tool),
    )

Either way:

* ``tools/list`` gains the ``io.stateguard/idempotency`` advertisement in the
  ``_meta`` of each keyed tool's definition. Other tools are listed unchanged.
* A ``tools/call`` to a keyed tool **that carries a key**:
  - *two-phase* tools run the server's own handler at most once per key;
    with ``recover=check``, an abandoned attempt (see the contract's
    "Recovery of abandoned attempts") is settled by ``check(key,
    arguments) -> Recovery`` instead of staying in doubt -- ``found``
    carries a ``CallToolResult``;
  - *transactional* tools run ``effect(db, arguments)`` inside the store's
    transaction instead.

  A call without a key, or to a tool that is not keyed, goes to the server's
  handler unchanged. Clients that know nothing of the contract see no
  difference.
* The outcome travels with the answer:
  - results carry ``_meta["io.stateguard/idempotencyOutcome"]``
    (``executed``, ``executed_unrecorded``, ``replayed``, ``recovered``, or
    ``rolled_back`` for a transactional tool's own failure);
  - an outcome that executed nothing is a JSON-RPC error (``MCPError``) whose
    ``data`` names it.

**What a handler's answer means for its key.**
- A ``CallToolResult`` -- success or ``isError`` -- is the call's outcome: it
  is recorded, and a resend of the key gets it again. (``MCPServer`` turns
  a tool's exceptions into ``isError`` results, so those are recorded too.)
- ``NotExecuted`` (raised before acting) frees the key.
- An ``InputRequiredResult`` (2026-07-28: the client must supply input and
  retry) is passed through unrecorded and frees the key. The request did not
  complete, and a keyed tool must not act before it has its input.
- Anything else raised -- an exception, an ``MCPError``, a cancellation --
  leaves the key in doubt: it may have acted.

The middleware validates a keyed call's params itself before claiming the
key, so the SDK's own rejection of malformed params never claims one.

``current_key()`` is the key of the keyed call a handler is running for (or
None). A handler that writes the key into its own effect makes a recovery
check exact; see the contract's second condition.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import anyio
import mcp_types as types
from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from stateguard.adapters.mcp.idempotency import (
    IDEMPOTENCY_KEY,
    IDEMPOTENCY_OUTCOME,
    Execution,
    IdempotencyStore,
    NotExecuted,
    Outcome,
    Recovery,
    advertisement,
    error_code,
    error_data,
    error_message,
    fingerprint,
)
from stateguard.adapters.mcp.idempotency_async import (
    PointHook,
    Unrecorded,
    current_key,
    execute_two_phase,
)

__all__ = ["KeyedTools", "NotExecuted", "Recovery", "current_key"]

CallHandler = Callable[[Any, types.CallToolRequestParams], Awaitable[Any]]
ListHandler = Callable[[Any, Any], Awaitable[types.ListToolsResult]]
Effect = Callable[[sqlite3.Connection, dict[str, Any]], types.CallToolResult]
#: ``check(key, arguments)``: did this key's own effect happen? Runs in a worker thread.
Recover = Callable[[str, dict[str, Any]], Recovery]


class KeyedTools:
    """Idempotency keys for chosen tools of an ``mcp`` 2.x server."""

    def __init__(
        self,
        store: IdempotencyStore,
        *,
        wait_seconds: float = 5.0,
        on_point: PointHook | None = None,
    ) -> None:
        self.store = store
        self.wait_seconds = wait_seconds
        #: TEST-ONLY: called at each point of a keyed call (fault injection).
        self.on_point = on_point
        self._two_phase: set[str] = set()
        self._recover: dict[str, Recover] = {}
        self._transactional: dict[str, Effect] = {}

    def two_phase(self, *names: str, recover: Recover | None = None) -> KeyedTools:
        """
        Key these tools; their handler runs at most once per key (two-phase).
        *recover* settles abandoned attempts; it is only sound under the
        contract's two conditions (the store must be exclusive for another
        process's attempts to count as abandoned).
        """
        self._two_phase.update(names)
        if recover is not None:
            self._recover.update(dict.fromkeys(names, recover))
        return self

    def transactional(self, name: str, effect: Effect) -> KeyedTools:
        """
        Key *name*; with a key, *effect(db, arguments)* runs instead of the
        handler, in one transaction with the key's record (``db`` is a
        connection to the store's database -- write the effect there).
        """
        self._transactional[name] = effect
        return self

    @property
    def keyed(self) -> frozenset[str]:
        return frozenset(self._two_phase | set(self._transactional))

    # -- listing -----------------------------------------------------------------

    def advertise(self, tools: Iterable[types.Tool]) -> list[types.Tool]:
        out = []
        for tool in tools:
            if tool.name in self.keyed:
                meta = {**(tool.meta or {}), **advertisement()}
                tool = tool.model_copy(update={"meta": meta})
            out.append(tool)
        return out

    def wrap_list(self, handler: ListHandler) -> ListHandler:
        async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
            result = await handler(ctx, params)
            return result.model_copy(update={"tools": self.advertise(result.tools)})

        return on_list_tools

    def _advertised(self, result: HandlerResult) -> HandlerResult:
        """A ``tools/list`` result (a model or its dict form) with keyed tools advertised."""
        if isinstance(result, types.ListToolsResult):
            return result.model_copy(update={"tools": self.advertise(result.tools)})
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            tools = []
            for tool in result["tools"]:
                if isinstance(tool, dict) and tool.get("name") in self.keyed:
                    tool = {**tool, "_meta": {**(tool.get("_meta") or {}), **advertisement()}}
                tools.append(tool)
            return {**result, "tools": tools}
        return result

    # -- calls ---------------------------------------------------------------------

    def wrap_call(self, handler: CallHandler) -> CallHandler:
        async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> Any:
            meta = params.meta if isinstance(params.meta, dict) else {}
            key = meta.get(IDEMPOTENCY_KEY)
            if params.name not in self.keyed or not isinstance(key, str) or not key:
                return await handler(ctx, params)
            return await self._keyed_call(
                key, params.name, dict(params.arguments or {}), lambda: handler(ctx, params)
            )

        return on_call_tool

    def middleware(self) -> ServerMiddleware[Any]:
        """
        The keyed tools as ``mcp`` 2.x server middleware: pass it in
        ``MCPServer(..., middleware=[...])`` or append it to a low-level
        ``Server.middleware``. Transport-independent (stdio, Streamable HTTP).
        """

        async def keyed_middleware(
            ctx: ServerRequestContext[Any, Any], call_next: CallNext
        ) -> HandlerResult:
            if ctx.request_id is None:
                return await call_next(ctx)  # a notification
            if ctx.method == "tools/list":
                return self._advertised(await call_next(ctx))
            if ctx.method != "tools/call":
                return await call_next(ctx)
            raw: dict[str, Any] = ctx.params if isinstance(ctx.params, dict) else {}
            meta_value = raw.get("_meta")
            meta: dict[str, Any] = meta_value if isinstance(meta_value, dict) else {}
            key = meta.get(IDEMPOTENCY_KEY)
            if raw.get("name") not in self.keyed or not isinstance(key, str) or not key:
                return await call_next(ctx)
            try:
                params = types.CallToolRequestParams.model_validate(raw)
            except ValidationError:
                return await call_next(ctx)  # the SDK rejects it; no key is claimed
            return await self._keyed_call(
                key, params.name, dict(params.arguments or {}), lambda: call_next(ctx)
            )

        return keyed_middleware

    async def _keyed_call(
        self,
        key: str,
        name: str,
        arguments: dict[str, Any],
        run: Callable[[], Awaitable[Any]],
    ) -> types.CallToolResult | types.InputRequiredResult:
        """The answer to a keyed call: a result, or an ``MCPError`` raised."""
        if name in self._transactional:
            effect = self._transactional[name]
            if self.on_point is not None:
                await self.on_point("before_begin", name)
            execution = await anyio.to_thread.run_sync(
                lambda: self.store.transact(
                    key,
                    fingerprint(name, arguments),
                    lambda db: _dump(effect(db, arguments)),
                    tool=name,
                )
            )
            if execution.outcome is Outcome.EXECUTED and self.on_point is not None:
                await self.on_point("after_record", name)
            return _answer(execution, key)
        check = self._recover.get(name)
        answered = await execute_two_phase(
            self.store,
            key,
            name,
            arguments,
            run,
            settle=_settle,
            recover=None if check is None else lambda k, a: _recorded(check(k, a)),
            wait_seconds=self.wait_seconds,
            on_point=self.on_point,
        )
        if isinstance(answered, Unrecorded):
            return answered.answer  # type: ignore[no-any-return]
        return _answer(answered, key)


class _NotAResultError(Exception):
    """A keyed handler answered something that is not a tools/call result."""


def _as_result(value: Any) -> types.CallToolResult | types.InputRequiredResult:
    """A handler's answer (a model or its dict form) as a tools/call result."""
    if isinstance(value, types.CallToolResult | types.InputRequiredResult):
        return value
    if isinstance(value, dict):
        kind = value.get("resultType", value.get("result_type"))
        model = types.InputRequiredResult if kind == "input_required" else types.CallToolResult
        try:
            return model.model_validate(value)
        except ValidationError as exc:
            raise _NotAResultError(str(exc)) from exc
    raise _NotAResultError(f"a {type(value).__name__}")


def _settle(value: Any) -> Any:
    """A handler's answer as the JSON to record, or ``Unrecorded`` for "input required"."""
    result = _as_result(value)
    if isinstance(result, types.InputRequiredResult):
        return Unrecorded(result)
    return _dump(result)


def _dump(result: types.CallToolResult) -> dict[str, Any]:
    return result.model_dump(by_alias=True, mode="json", exclude_none=True)


def _recorded(found: Recovery) -> Recovery:
    """A found ``CallToolResult``, as the JSON the store records (anything else: unknown)."""
    if isinstance(found, Recovery) and found.applied is True:
        if isinstance(found.result, types.CallToolResult):
            return Recovery.found(_dump(found.result))
        return Recovery.unknown(f"found a {type(found.result).__name__}, not a CallToolResult")
    return found


def _answer(execution: Execution, key: str) -> types.CallToolResult:
    outcome = execution.outcome
    if outcome in (
        Outcome.EXECUTED,
        Outcome.EXECUTED_UNRECORDED,
        Outcome.REPLAYED,
        Outcome.RECOVERED,
    ):
        result = types.CallToolResult.model_validate(execution.result)
        meta = {**(result.meta or {}), IDEMPOTENCY_OUTCOME: outcome.value, IDEMPOTENCY_KEY: key}
        return result.model_copy(update={"meta": meta})
    if outcome is Outcome.ROLLED_BACK:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Tool failed: {execution.error}")],
            is_error=True,
            _meta={IDEMPOTENCY_OUTCOME: outcome.value, IDEMPOTENCY_KEY: key},
        )
    message = error_message(outcome)
    if execution.error is not None and outcome is Outcome.NOT_EXECUTED:
        message = f"{message} ({execution.error})"
    raise MCPError(error_code(outcome), message, error_data(outcome, key))
