"""
A StateGuard-repairing proxy in front of ``tools/call``.

What it does, in order:

1. Fetches ``tools/list`` once and keeps the tool definitions.
2. On each call, repairs the arguments against the tool's own declared
   schema.
3. Forwards, holds, escalates, or refuses -- whichever
   ``stateguard.adapters.mcp.outcome_for`` says.

The server is unmodified and unaware. So is the agent. That is the shape the
whole thing is meant to have: drift gets corrected at the boundary, not by
asking every server to be more forgiving or every model to be more careful.

Two caches, doing different jobs
--------------------------------
This class caches the *tool definitions* from ``tools/list``, so a call does
not re-fetch them. ``MCPToolAdapter`` separately caches the *extracted
contract* per ``(tool name, schema content)``, so a call does not re-walk the
schema. Neither substitutes for the other, and both are keyed on content so
a server that changes a signature is picked up rather than papered over.

Refusing costs less than failing
--------------------------------
When no repair brings the arguments up to the schema, the call is not sent.
The server would reject it anyway -- forwarding spends a round trip to learn
what StateGuard already knows, and in an agent loop that reply is another
turn of context spent on a failure.

Requires the MCP SDK: ``pip install 'sguard[mcp]'``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp import Client
from mcp.types import CallToolResult

from stateguard import ContractGuard
from stateguard.adapters.mcp import MCPAction, ToolCallOutcome, outcome_for

__all__ = ["ProxiedCall", "RepairingClient"]


@dataclass
class ProxiedCall:
    """
    What happened to one tool call.

    Attributes
    ----------
    outcome:
        StateGuard's decision, plus the full repair audit trail on
        ``outcome.result``.
    sent:
        The arguments actually put on the wire, or ``None`` if the call was
        not made.
    response:
        The server's reply, or ``None`` if the call was not made.
    """

    outcome: ToolCallOutcome
    sent: dict[str, Any] | None
    response: CallToolResult | None

    @property
    def called(self) -> bool:
        return self.response is not None


class RepairingClient:
    """
    Wraps an MCP ``Client`` and repairs arguments on the way through.

    Parameters
    ----------
    client:
        A connected MCP ``Client``.
    guard:
        The guard to repair with.  Defaults to
        ``ContractGuard.with_mcp()`` (auto mode).  Pass one built with
        ``GuardConfig(mode=RepairMode.SHADOW)`` to observe without
        correcting -- the proxy then forwards the model's arguments
        untouched and reports what it *would* have changed.
    on_event:
        Optional callback taking one line of human-readable narration, for
        a demo or a log.  Defaults to silence.
    """

    def __init__(
        self,
        client: Client,
        guard: ContractGuard | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._guard = guard if guard is not None else ContractGuard.with_mcp()
        self._say = on_event if on_event is not None else (lambda _line: None)
        self._tools: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Schemas
    # ------------------------------------------------------------------

    async def load_tools(self) -> dict[str, dict[str, Any]]:
        """
        Fetch ``tools/list`` and keep each definition as a plain dict.

        ``by_alias=True`` is deliberate: on the SDK's ``Tool`` model the
        camelCase wire names are Pydantic aliases, so a plain
        ``model_dump()`` would emit ``input_schema``. Both spellings are
        accepted downstream, but dumping the wire form keeps what this proxy
        holds identical to what came over the connection.
        """
        listing = await self._client.list_tools()
        self._tools = {
            tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools
        }
        self._say(f"Loaded {len(self._tools)} tool definition(s): {', '.join(self._tools)}")
        return self._tools

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ProxiedCall:
        """Repair *arguments* against the tool's schema, then act on the verdict."""
        if not self._tools:
            await self.load_tools()

        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(
                f"Unknown tool {name!r}. Known: {sorted(self._tools)}. "
                f"Call load_tools() again if the server's tool list changed."
            )

        outcome = outcome_for(self._guard.repair(tool, arguments), arguments)
        self._say(f"  StateGuard: {outcome.action.upper()} -- {outcome.reason}")

        if outcome.action is MCPAction.FORWARD:
            # Asserted rather than defaulted. FORWARD promises repaired
            # arguments; ``or {}`` would turn a broken promise into a silent
            # call with no arguments at all, which the server would answer
            # with a confusing validation error instead of the real fault.
            if outcome.arguments is None:
                raise AssertionError(
                    f"FORWARD for {name!r} carried no arguments. This is a bug in "
                    f"outcome_for, not in the payload -- forwarding an empty dict "
                    f"here would send the server something the model never wrote."
                )
            sent = outcome.arguments
        elif outcome.action is MCPAction.HOLD:
            # Shadow: the server sees exactly what the model sent. The
            # repair is reported, not applied -- that is the whole contract
            # of shadow mode, and applying it here would quietly break it.
            sent = arguments
            self._say(f"  would have sent: {outcome.preview}")
        else:
            # ESCALATE or REFUSE. Not sent.
            return ProxiedCall(outcome=outcome, sent=None, response=None)

        response = await self._client.call_tool(name, sent)
        return ProxiedCall(outcome=outcome, sent=sent, response=response)
