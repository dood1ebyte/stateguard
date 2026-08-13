"""
CLI reporting for the AMBIGUOUS outcome.

An abstained repair only helps if the caller can see it, so this covers the
surface that carries it out of the process: exit code 3, the human-readable
block, and the JSON payload. All of it shipped untested in the first Phase 3
pass, which is what dropped `cli.py` from 99% to 89% coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stateguard.cli import main

# One key, two plausible destinations -- withheld rather than guessed.
CONTESTED_SCHEMA: dict[str, Any] = {
    "fields": [
        {"path": "user_id", "type": "string"},
        {"path": "user_name", "type": "string"},
    ]
}
CONTESTED_PAYLOAD: dict[str, Any] = {"user_email": "a@b.com"}

CLEAN_SCHEMA: dict[str, Any] = {"fields": [{"path": "x", "type": "string"}]}


@pytest.fixture
def files(tmp_path: Path) -> Any:
    def _write(schema: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str]:
        schema_path = tmp_path / "schema.json"
        payload_path = tmp_path / "payload.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        return str(schema_path), str(payload_path)

    return _write


def _run(schema_path: str, payload_path: str, *extra: str) -> int:
    try:
        main(["check", "--schema", schema_path, "--payload", payload_path, *extra])
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


# ===========================================================================
# Exit codes
# ===========================================================================


class TestExitCode:
    def test_ambiguous_exits_three(self, files: Any) -> None:
        """
        Distinct from 2 (FAILED) on purpose: a caller can branch on "a repair
        exists but needs a decision" versus "there is nothing to apply".
        """
        assert _run(*files(CONTESTED_SCHEMA, CONTESTED_PAYLOAD)) == 3

    def test_success_still_exits_zero(self, files: Any) -> None:
        assert _run(*files(CLEAN_SCHEMA, {"x": "hi"})) == 0


# ===========================================================================
# Human-readable output
# ===========================================================================


class TestHumanOutput:
    def test_reports_the_ambiguous_status(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(*files(CONTESTED_SCHEMA, CONTESTED_PAYLOAD))
        assert "AMBIGUOUS" in capsys.readouterr().out

    def test_prints_the_withheld_repair_block(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(*files(CONTESTED_SCHEMA, CONTESTED_PAYLOAD))
        out = capsys.readouterr().out

        assert "Ambiguous repairs (found, not applied):" in out
        assert "user_email" in out
        assert "rename" in out

    def test_names_the_bar_that_was_missed(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without the risk tier and its threshold, a trust score is unreadable."""
        _run(*files(CONTESTED_SCHEMA, CONTESTED_PAYLOAD))
        out = capsys.readouterr().out

        assert "trust" in out
        assert "INFERRED" in out

    def test_shows_the_measured_evidence(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(*files(CONTESTED_SCHEMA, CONTESTED_PAYLOAD))
        assert "jaro-winkler" in capsys.readouterr().out

    def test_applied_operations_report_trust_and_risk_not_confidence(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """
        The human and JSON surfaces disagreed: one said "confidence", the
        other "trust"/"risk", for the same operation.
        """
        schema = {"fields": [{"path": "temperature", "type": "float"}]}
        _run(*files(schema, {"temp_celsius": 31.5}))
        out = capsys.readouterr().out

        assert "trust" in out
        assert "risk" in out
        assert "confidence" not in out


# ===========================================================================
# JSON output
# ===========================================================================


class TestJsonOutput:
    @staticmethod
    def _json(files: Any, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
        _run(*files(CONTESTED_SCHEMA, CONTESTED_PAYLOAD), "--json")
        return json.loads(capsys.readouterr().out)  # type: ignore[no-any-return]

    def test_status_is_ambiguous(self, files: Any, capsys: pytest.CaptureFixture[str]) -> None:
        assert self._json(files, capsys)["status"] == "ambiguous"

    def test_carries_the_candidates_machine_readably(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The point of the outcome: a caller can act on it without parsing prose."""
        entry = self._json(files, capsys)["ambiguous"][0]

        assert entry["target_path"] in {"user_id", "user_name"}
        assert entry["candidates"][0]["source_path"] == "user_email"
        assert entry["candidates"][0]["risk"] == "INFERRED"
        assert 0.0 < entry["candidates"][0]["trust"] < 1.0

    def test_explains_why_it_was_withheld(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert "INFERRED" in self._json(files, capsys)["ambiguous"][0]["reason"]

    def test_no_output_is_invented(self, files: Any, capsys: pytest.CaptureFixture[str]) -> None:
        assert self._json(files, capsys)["repaired_output"] is None

    def test_a_clean_run_reports_an_empty_ambiguous_list(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(*files(CLEAN_SCHEMA, {"x": "hi"}), "--json")
        assert json.loads(capsys.readouterr().out)["ambiguous"] == []


# ===========================================================================
# Telling competing candidates apart
# ===========================================================================


ENUM_COLLISION_SCHEMA: dict[str, Any] = {
    "fields": [
        {
            "path": "status",
            "type": "string",
            "constraints": [{"type": "enum_values", "value": ["in_progress", "in progress"]}],
        }
    ]
}
ENUM_COLLISION_PAYLOAD: dict[str, Any] = {"status": "IN PROGRESS"}


class TestCandidatesAreDistinguishable:
    """
    The ambiguous block exists so a caller can *choose*. Rendering the
    competing candidates identically makes that impossible: two enum
    candidates both printed as a bare ``set_value`` line, with no indication
    of what either would write.
    """

    def test_human_output_names_the_value_each_candidate_would_write(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(*files(ENUM_COLLISION_SCHEMA, ENUM_COLLISION_PAYLOAD))
        out = capsys.readouterr().out
        assert "→ 'in_progress'" in out
        assert "→ 'in progress'" in out

    def test_human_output_names_the_key_a_rename_would_read_from(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A rename's discriminator is its source, not a value."""
        _run(*files(CONTESTED_SCHEMA, CONTESTED_PAYLOAD))
        out = capsys.readouterr().out
        assert "← user_email" in out

    def test_json_carries_the_value_for_each_candidate(
        self, files: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """
        An agent re-prompting on an ambiguous result reads this. Without
        ``value`` the two candidates are byte-identical JSON objects.
        """
        _run(*files(ENUM_COLLISION_SCHEMA, ENUM_COLLISION_PAYLOAD), "--json")
        payload = json.loads(capsys.readouterr().out)
        candidates = payload["ambiguous"][0]["candidates"]
        assert {c["value"] for c in candidates} == {"in_progress", "in progress"}
        assert len({json.dumps(c, sort_keys=True) for c in candidates}) == 2
