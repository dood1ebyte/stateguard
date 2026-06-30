"""
Import isolation tests.

These tests enforce the core architectural law:

    importing stateguard.core must never trigger loading of any
    external (non-stdlib) package.

Each test spawns a fresh subprocess so that sys.modules starts empty.
This catches both direct imports ("import pydantic") and transitive
imports hidden inside helper functions or class bodies.

The tests intentionally run in two CI environments:
  - isolation job:  pydantic NOT installed  → catches accidental imports
    (the subprocess itself would exit non-zero if core tried to import pydantic)
  - test job:       pydantic IS installed   → checks sys.modules membership
    after import, confirming core never requested it
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(code: str) -> tuple[int, str, str]:
    """
    Execute *code* in a fresh subprocess using the current interpreter.

    Returns (returncode, stdout.strip(), stderr.strip()).
    The 30-second timeout prevents a hung import from blocking CI.
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.isolation
class TestCoreImportIsolation:
    """Verify stateguard.core has zero external runtime dependencies."""

    def test_core_imports_without_error(self) -> None:
        """Baseline: stateguard.core can be imported at all."""
        rc, _, stderr = _run("import stateguard.core")
        assert rc == 0, f"stateguard.core failed to import:\n{stderr}"

    def test_core_does_not_load_pydantic(self) -> None:
        """
        Importing stateguard.core must not trigger loading pydantic.

        This is the primary architectural invariant of the system.
        If this test fails the isolation CI job aborts; no other tests run.
        """
        rc, stdout, stderr = _run("""
            import sys, json
            import stateguard.core
            found = [m for m in sys.modules
                     if m == "pydantic" or m.startswith("pydantic.")]
            print(json.dumps(found))
        """)
        assert rc == 0, f"Import-check subprocess failed:\n{stderr}"
        loaded = json.loads(stdout)
        assert loaded == [], (
            "stateguard.core unexpectedly loaded pydantic modules: "
            f"{loaded}\n"
            "The core engine must have zero external dependencies."
        )

    def test_core_loads_only_stdlib_and_self(self) -> None:
        """
        Importing stateguard.core must only introduce stdlib + stateguard
        modules into sys.modules.

        Uses sys.stdlib_module_names (Python 3.10+) as the authoritative
        set of stdlib module roots.
        """
        rc, stdout, stderr = _run("""
            import sys, json

            baseline = set(sys.modules)
            import stateguard.core
            new_mods = set(sys.modules) - baseline

            third_party = []
            for name in new_mods:
                root = name.split(".")[0]
                if root == "stateguard":
                    continue
                if root.startswith("_"):
                    continue
                if root in sys.stdlib_module_names:
                    continue
                third_party.append(root)

            print(json.dumps(sorted(set(third_party))))
        """)
        assert rc == 0, f"Import-check subprocess failed:\n{stderr}"
        third_party = json.loads(stdout)
        assert third_party == [], (
            "stateguard.core loaded third-party packages: "
            f"{third_party}\n"
            "Only stdlib is permitted inside stateguard.core."
        )

    def test_core_models_does_not_load_pydantic(self) -> None:
        """Sub-package stateguard.core.models must also be clean."""
        rc, stdout, stderr = _run("""
            import sys, json
            import stateguard.core.models
            found = [m for m in sys.modules
                     if m == "pydantic" or m.startswith("pydantic.")]
            print(json.dumps(found))
        """)
        assert rc == 0, f"subprocess failed:\n{stderr}"
        assert json.loads(stdout) == []

    def test_core_errors_does_not_load_pydantic(self) -> None:
        """Sub-package stateguard.core.errors must also be clean."""
        rc, stdout, stderr = _run("""
            import sys, json
            import stateguard.core.errors
            found = [m for m in sys.modules
                     if m == "pydantic" or m.startswith("pydantic.")]
            print(json.dumps(found))
        """)
        assert rc == 0, f"subprocess failed:\n{stderr}"
        assert json.loads(stdout) == []

    def test_pydantic_adapter_stub_imports_without_error(self) -> None:
        """The pydantic adapter stub must be importable (even while empty)."""
        rc, _, stderr = _run("import stateguard.adapters.pydantic")
        assert rc == 0, (
            "stateguard.adapters.pydantic stub failed to import:\n"
            f"{stderr}"
        )

    def test_importing_core_does_not_import_adapter(self) -> None:
        """
        Importing stateguard.core must not drag in stateguard.adapters.
        The adapter boundary must be explicit — adapters are never
        auto-imported by the engine.
        """
        rc, stdout, stderr = _run("""
            import sys, json
            import stateguard.core
            adapter_mods = [m for m in sys.modules
                            if m.startswith("stateguard.adapters")]
            print(json.dumps(adapter_mods))
        """)
        assert rc == 0, f"subprocess failed:\n{stderr}"
        assert json.loads(stdout) == [], (
            "Importing stateguard.core should never load stateguard.adapters."
        )
