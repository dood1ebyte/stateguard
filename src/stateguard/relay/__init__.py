"""
StateGuard's MCP relays, as package commands.

* :mod:`stateguard.relay.stdio` -- the stdio relay (``stateguard relay stdio [opts] -- <server>``).
* :mod:`stateguard.relay.http` -- the Streamable HTTP relay
  (``stateguard relay http --upstream URL [opts]``; needs ``sguard[mcp]``).

Nothing is imported here: the HTTP relay's dependencies are optional, and importing
``stateguard`` (or this package) must never pull them in.
"""
