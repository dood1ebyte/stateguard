"""
stateguard.core — The framework-agnostic repair engine.

Architectural invariant
-----------------------
This package and every sub-package beneath it must have **zero external
runtime dependencies**.  Only Python stdlib modules may be imported.

This invariant is enforced by ``tests/isolation/test_import_isolation.py``,
which runs as the first job in CI and must never be bypassed.
"""
