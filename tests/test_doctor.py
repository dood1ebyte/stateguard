"""``stateguard doctor`` (Phase 2 S5.2): this machine, and one server behind a shadow relay."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from stateguard import doctor

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "examples" / "mcp" / "cooperating_notes_server.py"


def test_the_environment_checks_name_each_piece() -> None:
    lines = doctor.check_environment()
    names = [what for _, what, _ in lines]
    assert names[:3] == ["python", "stateguard", "http relay"]
    assert {"node", "preload", "key store"} <= set(names)
    assert all(level in ("ok", "warn", "FAIL") for level, _, _ in lines)
    assert lines[0][0] == "ok"  # these tests run on 3.11 or later


@pytest.mark.integration
def test_a_server_is_listed_through_a_shadow_relay_and_its_keys_plan_shown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("mcp.server.mcpserver")
    db = tmp_path / "notes.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE notes (seq INTEGER PRIMARY KEY, text TEXT NOT NULL)")
    code = doctor.main(["--", sys.executable, str(SERVER), str(db), "--no-keys"])
    out = capsys.readouterr().out
    assert "server       cooperating-notes" in out
    assert "tools/list   2 tools through the relay" in out
    assert "--keys auto  would start it with the python-injector" in out
    assert out.strip().endswith("ready") and code == 0


@pytest.mark.integration
def test_a_server_that_does_not_start_fails(capsys: pytest.CaptureFixture[str]) -> None:
    code = doctor.main(["--", sys.executable, "-c", "import sys; sys.exit(3)"])
    out = capsys.readouterr().out
    assert "FAIL  server" in out and code == 1
