"""
``examples/mcp/b1_lost_response_cases.py``: the #3394 B1 lost-response
scenario on the real ``mcp`` SDK, with and without the keyed adapter. Every
case must meet its stated effect count and client-visible outcome.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "mcp" / "b1_lost_response_cases.py"


def test_every_b1_case_meets_its_effect_count_and_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("b1_lost_response_cases", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # dataclasses resolve it there
    spec.loader.exec_module(module)
    out = tmp_path / "results.json"

    assert module.main(["--json", str(out), "--read-timeout", "0.5"]) == 0

    cases = {c["case"]: c for c in json.loads(out.read_text())["cases"]}
    assert len(cases) == 9
    assert cases["baseline"]["effects"] == 2  # the duplicate the adapter exists for
    assert cases["record_lost_unresolved"]["fault_fired"] is True
    assert cases["record_lost_recovered"]["fault_fired"] is True
    assert all(c["passed"] for c in cases.values())
