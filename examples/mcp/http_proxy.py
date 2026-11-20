"""
StateGuard's Streamable HTTP relay -- now :mod:`stateguard.relay.http`, run as
``stateguard relay http --upstream URL [opts]`` (needs ``sguard[mcp]``).

This file is a thin wrapper kept so existing scripts, drivers and tests keep working:
run as a script it is the relay (the upstream URL positional, as before); imported, it
*is* the package module (the same object).
"""

import sys

from stateguard.relay import http as _relay

if __name__ == "__main__":
    sys.exit(_relay.main())

sys.modules[__name__] = _relay
