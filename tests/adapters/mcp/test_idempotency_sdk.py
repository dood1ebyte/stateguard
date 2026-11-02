"""
The Python MCP SDK adapter (``stateguard.adapters.mcp.idempotency_sdk``) in
process: the middleware's routing, what a handler's answer means for its key,
and ``current_key()``. End to end, against real processes, is
``tests/examples/test_keyed_mcpserver.py`` and ``test_keyed_reference_server.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

import anyio  # noqa: E402
import mcp_types as types  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402

from stateguard.adapters.mcp.idempotency import (  # noqa: E402
    IDEMPOTENCY_CAPABILITY,
    IDEMPOTENCY_KEY,
    IDEMPOTENCY_OUTCOME,
    IdempotencyStore,
)
from stateguard.adapters.mcp.idempotency_sdk import (  # noqa: E402
    KeyedTools,
    NotExecuted,
    current_key,
)


@dataclass
class Ctx:
    method: str
    params: Any = None
    request_id: Any = 1
    seen: list[Any] = field(default_factory=list)


def _call(name: str, arguments: Any, key: str | None = None) -> Ctx:
    params: dict[str, Any] = {"name": name, "arguments": arguments}
    if key is not None:
        params["_meta"] = {IDEMPOTENCY_KEY: key}
    return Ctx("tools/call", params)


def _text(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


class Handler:
    """A ``call_next`` that counts its runs and answers what it is told."""

    def __init__(self, answer: Any = None) -> None:
        self.runs = 0
        self.keys: list[str | None] = []
        self.answer = answer if answer is not None else _text("done")

    async def __call__(self, ctx: Any) -> Any:
        self.runs += 1
        self.keys.append(current_key())
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _run(middleware: Any, ctx: Ctx, handler: Any) -> Any:
    return anyio.run(middleware, ctx, handler)


@pytest.fixture
def keyed(tmp_path: Path) -> KeyedTools:
    return KeyedTools(IdempotencyStore(tmp_path / "keys.db")).two_phase("pay")


def test_a_keyed_call_runs_once_and_replays(keyed: KeyedTools) -> None:
    handler = Handler()
    first = _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), handler)
    again = _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), handler)
    assert handler.runs == 1
    assert first.meta[IDEMPOTENCY_OUTCOME] == "executed"
    assert again.meta[IDEMPOTENCY_OUTCOME] == "replayed"
    assert again.content == first.content


def test_unkeyed_calls_other_tools_other_methods_and_notifications_pass_through(
    keyed: KeyedTools,
) -> None:
    middleware = keyed.middleware()
    for ctx in (
        _call("pay", {"n": 1}),  # no key
        _call("refund", {"n": 1}, "k"),  # not a keyed tool
        Ctx("resources/read", {"uri": "x"}),
        Ctx("notifications/cancelled", {}, request_id=None),
    ):
        handler = Handler(answer={"passed": True})
        assert _run(middleware, ctx, handler) == {"passed": True}
        assert handler.runs == 1 and handler.keys == [None]


def test_malformed_params_claim_no_key(keyed: KeyedTools) -> None:
    ctx = Ctx("tools/call", {"name": "pay", "arguments": "not an object", "_meta": {}})
    ctx.params["_meta"][IDEMPOTENCY_KEY] = "k"
    handler = Handler(answer={"sdk": "rejects it"})
    assert _run(keyed.middleware(), ctx, handler) == {"sdk": "rejects it"}
    assert keyed.store.state_of("k") is None


def test_tools_list_is_advertised_in_model_and_dict_form(keyed: KeyedTools) -> None:
    tools = [
        types.Tool(name="pay", input_schema={"type": "object"}),
        types.Tool(name="read", input_schema={"type": "object"}),
    ]
    model = _run(keyed.middleware(), Ctx("tools/list"), Handler(types.ListToolsResult(tools=tools)))
    assert model.tools[0].meta == {IDEMPOTENCY_CAPABILITY: {"version": 1}}
    assert model.tools[1].meta is None
    as_dict = {"tools": [t.model_dump(by_alias=True, exclude_none=True) for t in tools]}
    out = _run(keyed.middleware(), Ctx("tools/list"), Handler(as_dict))
    assert out["tools"][0]["_meta"] == {IDEMPOTENCY_CAPABILITY: {"version": 1}}
    assert "_meta" not in out["tools"][1]


def test_current_key_is_the_calls_key_only_inside_its_handler(keyed: KeyedTools) -> None:
    handler = Handler()
    _run(keyed.middleware(), _call("pay", {"n": 1}, "k-inside"), handler)
    _run(keyed.middleware(), _call("pay", {"n": 1}), handler)
    assert handler.keys == ["k-inside", None]
    assert current_key() is None


def test_a_recorded_tool_error_is_replayed_not_rerun(keyed: KeyedTools) -> None:
    error = types.CallToolResult(
        content=[types.TextContent(type="text", text="declined")], is_error=True
    )
    handler = Handler(error)
    first = _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), handler)
    again = _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), handler)
    assert handler.runs == 1 and first.is_error and again.is_error
    assert again.meta[IDEMPOTENCY_OUTCOME] == "replayed"


def test_input_required_passes_through_unrecorded_and_frees_the_key(keyed: KeyedTools) -> None:
    ask = types.InputRequiredResult(request_state="need-confirmation")
    asked = _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), Handler(ask))
    assert isinstance(asked, types.InputRequiredResult)
    assert keyed.store.state_of("k") is None  # not "done": a retry must not replay it
    retried = _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), Handler())
    assert retried.meta[IDEMPOTENCY_OUTCOME] == "executed"


def test_dict_answers_are_results_too(keyed: KeyedTools) -> None:
    as_dict = _text("from a dict").model_dump(by_alias=True, exclude_none=True)
    first = _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), Handler(as_dict))
    assert first.meta[IDEMPOTENCY_OUTCOME] == "executed"
    ask = {"resultType": "input_required", "requestState": "s"}
    asked = _run(keyed.middleware(), _call("pay", {"n": 2}, "k2"), Handler(ask))
    assert isinstance(asked, types.InputRequiredResult)


@pytest.mark.parametrize(
    "raised, outcome",
    [
        (NotExecuted("rejected before acting"), "not_executed"),
        (RuntimeError("gateway timed out"), "in_doubt"),
        (MCPError(-32603, "tool failed"), "in_doubt"),
    ],
)
def test_what_a_raised_handler_means_for_its_key(
    keyed: KeyedTools, raised: BaseException, outcome: str
) -> None:
    with pytest.raises(MCPError) as answer:
        _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), Handler(raised))
    assert answer.value.error.data[IDEMPOTENCY_OUTCOME] == outcome
    expected_state = None if outcome == "not_executed" else "in_doubt"
    assert keyed.store.state_of("k") == expected_state


def test_an_answer_that_is_not_a_result_leaves_the_key_in_doubt(keyed: KeyedTools) -> None:
    with pytest.raises(MCPError) as answer:
        _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), Handler(answer=42))
    assert answer.value.error.data[IDEMPOTENCY_OUTCOME] == "in_doubt"
    assert keyed.store.state_of("k") == "in_doubt"


def test_wrap_call_and_middleware_share_one_record(keyed: KeyedTools) -> None:
    handler = Handler()

    async def low_level(ctx: Any, params: types.CallToolRequestParams) -> Any:
        return await handler(ctx)

    params = types.CallToolRequestParams(
        name="pay", arguments={"n": 1}, _meta={IDEMPOTENCY_KEY: "k"}
    )
    first = anyio.run(keyed.wrap_call(low_level), None, params)
    again = _run(keyed.middleware(), _call("pay", {"n": 1}, "k"), handler)
    assert handler.runs == 1
    assert (first.meta[IDEMPOTENCY_OUTCOME], again.meta[IDEMPOTENCY_OUTCOME]) == (
        "executed",
        "replayed",
    )
