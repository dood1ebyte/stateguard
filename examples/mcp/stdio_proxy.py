"""
StateGuard's stdio relay -- now :mod:`stateguard.relay.stdio`, run as
``stateguard relay stdio [opts] -- <server command>``.

This file is a thin wrapper kept so existing scripts, drivers and tests keep working:
run as a script it is the relay; imported, it *is* the package module (the same object,
so attributes patched on it are the relay's own).
"""

import sys

from stateguard.relay import stdio as _relay

if __name__ == "__main__":
    _relay.run()

sys.modules[__name__] = _relay
