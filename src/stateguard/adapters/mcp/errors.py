"""
Errors raised by the MCP adapter -- one class per thing that can go wrong.

A proxy in front of ``tools/call`` has to react differently to each of
these, so they are not collapsed into one exception:

=================================  ============================================
What happened                      Raised as
=================================  ============================================
The tool definition is malformed   ``InvalidToolDefinitionError``
  (not an object, no name, no
  ``inputSchema``, not JSON)
The schema is outside the          ``JSONSchemaError`` subclasses, from
  supported subset                   ``stateguard.adapters.jsonschema`` --
                                     unchanged, and still ``ValueError``
No tool by that name is            ``ToolNotFoundError``
  registered
A tool is registered but its       ``ToolSchemaUnusableError`` -- carries the
  schema could not be read           original error on ``cause``
A JSON-RPC message is not a        ``MalformedMessageError``
  well-formed ``tools/call``
StateGuard's own forwarding        ``ForwardingInvariantError`` -- a bug in
  invariants failed                  StateGuard, never a property of the input
=================================  ============================================

Invalid *arguments* are not an exception at all: they are an outcome
(``REFUSE``, or ``ESCALATE`` when ambiguous), because the call was understood
and a decision was made about it.

``MCPAdapterError`` is the common base for everything raised by the MCP
layer itself. ``InvalidToolDefinitionError`` and ``ToolSchemaUnusableError``
also subclass ``ValueError`` and ``ToolNotFoundError`` subclasses
``LookupError``, matching what a caller catching the builtin would expect.

Zero external dependencies.
"""

from __future__ import annotations

__all__ = [
    "ForwardingInvariantError",
    "InvalidToolDefinitionError",
    "MCPAdapterError",
    "MalformedMessageError",
    "ToolNotFoundError",
    "ToolSchemaUnusableError",
]


class MCPAdapterError(Exception):
    """Base for every error raised by ``stateguard.adapters.mcp`` itself."""


class InvalidToolDefinitionError(MCPAdapterError, ValueError):
    """
    A tool definition StateGuard cannot even identify.

    Not an object, no usable ``name`` where one is required, an
    ``inputSchema`` that is not an object, or content that is not JSON
    (and so has no stable identity). Distinct from a schema that is
    well-formed but uses an unsupported construct, which raises
    ``UnsupportedSchemaError``.
    """


class ToolNotFoundError(MCPAdapterError, LookupError):
    """No tool is registered under this (server, name)."""

    def __init__(self, name: str, server: str | None, known: list[str]) -> None:
        self.name = name
        self.server = server
        self.known = known
        where = f" on server {server!r}" if server is not None else ""
        super().__init__(
            f"No tool {name!r} is registered{where}. Known: {known}. If the "
            f"server's tool list changed, register or sync it again."
        )


class ToolSchemaUnusableError(MCPAdapterError, ValueError):
    """
    The tool is registered, but its current schema could not be read.

    Raised instead of validating against a *previous* schema: the server has
    moved on, so the old contract is no longer the truth, and validating
    against it would be exactly the stale-contract failure this adapter
    exists to catch. ``cause`` is the original error -- usually an
    ``UnsupportedSchemaError`` or ``SchemaReferenceError`` naming the
    construct and where it is.
    """

    def __init__(self, name: str, server: str | None, cause: Exception) -> None:
        self.name = name
        self.server = server
        self.cause = cause
        where = f" on server {server!r}" if server is not None else ""
        super().__init__(
            f"Tool {name!r}{where} is registered, but StateGuard cannot read its "
            f"current schema, so no call to it can be validated: {cause}"
        )


class MalformedMessageError(MCPAdapterError, ValueError):
    """A JSON-RPC message that is not a well-formed request of the expected kind."""


class ForwardingInvariantError(MCPAdapterError, RuntimeError):
    """
    StateGuard was about to forward something its own rules forbid.

    For example a shadow-mode outcome carrying a payload other than the one
    received, or an auto-mode payload that does not validate. Every such
    rule is guaranteed by construction; this is raised only if a change
    breaks one, so that the failure is loud and nothing is forwarded.
    Report it as a bug.
    """
