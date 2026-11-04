"""
Idempotency keys for a server built on the official Python MCP SDK 1.x
(``mcp`` < 2): its low-level ``Server``, and ``FastMCP``, which runs one.
The contract and its crash windows are ``stateguard.adapters.mcp.idempotency``;
``idempotency_sdk`` is the same adapter for ``mcp`` 2.x.

    store = IdempotencyStore("orders.db", scope="orders")
    keyed = KeyedTools(store).two_phase("append_ledger")
    # or, with a recovery check (needs IdempotencyStore(..., exclusive=True)):
    keyed.two_phase("append_ledger", recover=ledger_has_key)

    keyed.install(server)          # a low-level Server, once its handlers are registered
    keyed.install(app._mcp_server)  # FastMCP

``install`` wraps the server's registered ``tools/list`` and ``tools/call``
handlers (``server.request_handlers``), which every 1.x server dispatches
through, so it is transport-independent. For a server whose code you cannot
change, ``python -m stateguard.adapters.mcp.inject`` does it at process start.

* ``tools/list`` gains the ``io.stateguard/idempotency`` advertisement in the
  ``_meta`` of each keyed tool's definition. Other tools are listed unchanged.
* A ``tools/call`` to a keyed tool **that carries a key** runs the server's own
  handler at most once per key (two-phase). With ``recover=check``, an
  abandoned attempt is settled by ``check(key, arguments) -> Recovery``
  instead of staying in doubt; ``found`` carries a ``CallToolResult``.
  A call without a key, or to a tool that is not keyed, goes to the handler
  unchanged.
* The outcome travels with the answer: results carry
  ``_meta["io.stateguard/idempotencyOutcome"]`` (``executed``,
  ``executed_unrecorded``, ``replayed`` or ``recovered``); an outcome that
  executed nothing is a JSON-RPC error (``McpError``) whose ``data`` names it.

**What a handler's answer means for its key.** The SDK's ``call_tool``
decorator turns a tool's exceptions into ``isError`` results, so those are
recorded and replayed like any result. ``NotExecuted`` (raised before acting)
frees the key. A handler that raises anyway (a hand-written one raising
``McpError``, or a cancellation) leaves the key in doubt: it may have acted.
SDK 1.x has no "input required" answer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from mcp import types
from mcp.shared.exceptions import McpError

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
)
from stateguard.adapters.mcp.idempotency_async import (
    PointHook,
    Unrecorded,
    current_key,
    execute_two_phase,
)

__all__ = ["KeyedTools", "NotExecuted", "Recovery", "current_key"]

Handler = Callable[[Any], Awaitable[Any]]
#: ``check(key, arguments)``: did this key's own effect happen? Runs in a worker thread.
Recover = Callable[[str, dict[str, Any]], Recovery]

_INSTALLED = "_stateguard_keyed_tools"


class KeyedTools:
    """Idempotency keys for chosen tools of an ``mcp`` 1.x server."""

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

    @property
    def keyed(self) -> frozenset[str]:
        return frozenset(self._two_phase)

    def install(self, server: Any) -> None:
        """
        Key *server*'s registered tools (a low-level ``Server``). Install it
        after the server's ``list_tools``/``call_tool`` handlers are
        registered: a handler registered later replaces the wrapped one.
        Installing twice is a no-op.
        """
        if getattr(server, _INSTALLED, None) is not None:
            return
        handlers: dict[Any, Handler] = server.request_handlers
        call = handlers.get(types.CallToolRequest)
        if call is not None:
            handlers[types.CallToolRequest] = self.wrap_call(call)
        listed = handlers.get(types.ListToolsRequest)
        if listed is not None:
            handlers[types.ListToolsRequest] = self.wrap_list(listed)
        setattr(server, _INSTALLED, self)

    # -- listing -----------------------------------------------------------------

    def advertise(self, tools: Iterable[types.Tool]) -> list[types.Tool]:
        out = []
        for tool in tools:
            if tool.name in self.keyed:
                # SDK 1.x's Tool has no `meta` field; `_meta` travels as an extra field.
                definition = tool.model_dump(by_alias=True, exclude_none=True)
                meta = definition.get("_meta")
                own = meta if isinstance(meta, dict) else {}
                definition["_meta"] = {**own, **advertisement()}
                tool = types.Tool.model_validate(definition)
            out.append(tool)
        return out

    def wrap_list(self, handler: Handler) -> Handler:
        async def on_list_tools(request: Any) -> Any:
            answer = await handler(request)
            result = answer.root if isinstance(answer, types.ServerResult) else answer
            if not isinstance(result, types.ListToolsResult):
                return answer
            listed = result.model_copy(update={"tools": self.advertise(result.tools)})
            return types.ServerResult(listed)

        return on_list_tools

    # -- calls ---------------------------------------------------------------------

    def wrap_call(self, handler: Handler) -> Handler:
        async def on_call_tool(request: types.CallToolRequest) -> Any:
            params = request.params
            meta = params.meta.model_extra if params.meta is not None else None
            key = (meta or {}).get(IDEMPOTENCY_KEY)
            if params.name not in self.keyed or not isinstance(key, str) or not key:
                return await handler(request)
            check = self._recover.get(params.name)
            answered = await execute_two_phase(
                self.store,
                key,
                params.name,
                dict(params.arguments or {}),
                lambda: handler(request),
                settle=_settle,
                recover=None if check is None else lambda k, a: _recorded(check(k, a)),
                wait_seconds=self.wait_seconds,
                on_point=self.on_point,
            )
            assert not isinstance(answered, Unrecorded)  # _settle never passes an answer through
            return _answer(answered, key)

        return on_call_tool


class _NotAResultError(Exception):
    """A keyed handler answered something that is not a tools/call result."""


def _settle(answer: Any) -> dict[str, Any]:
    """A handler's answer (``ServerResult`` or ``CallToolResult``) as the JSON to record."""
    result = answer.root if isinstance(answer, types.ServerResult) else answer
    if not isinstance(result, types.CallToolResult):
        raise _NotAResultError(f"a {type(result).__name__}")
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


def _answer(execution: Execution, key: str) -> types.ServerResult:
    outcome = execution.outcome
    if outcome in (
        Outcome.EXECUTED,
        Outcome.EXECUTED_UNRECORDED,
        Outcome.REPLAYED,
        Outcome.RECOVERED,
    ):
        result = types.CallToolResult.model_validate(execution.result)
        meta = {**(result.meta or {}), IDEMPOTENCY_OUTCOME: outcome.value, IDEMPOTENCY_KEY: key}
        return types.ServerResult(result.model_copy(update={"meta": meta}))
    message = error_message(outcome)
    if execution.error is not None and outcome is Outcome.NOT_EXECUTED:
        message = f"{message} ({execution.error})"
    raise McpError(
        types.ErrorData(code=error_code(outcome), message=message, data=error_data(outcome, key))
    )
