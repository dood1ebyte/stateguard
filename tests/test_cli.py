"""Tests for stateguard.cli."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from stateguard.cli import _build_parser, _load_json, _load_model, main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def schema_file(tmp_path: Path) -> Path:
    schema = {
        "fields": [
            {"path": "temperature", "type": "float"},
            {"path": "humidity", "type": "integer"},
        ]
    }
    p = tmp_path / "schema.json"
    p.write_text(json.dumps(schema))
    return p


@pytest.fixture
def schema_with_default_file(tmp_path: Path) -> Path:
    schema = {
        "fields": [
            {"path": "temperature", "type": "float"},
            {"path": "humidity", "type": "integer", "required": False, "default": 60},
        ]
    }
    p = tmp_path / "schema_defaults.json"
    p.write_text(json.dumps(schema))
    return p


@pytest.fixture
def payload_valid(tmp_path: Path) -> Path:
    p = tmp_path / "valid.json"
    p.write_text(json.dumps({"temperature": 31.5, "humidity": 80}))
    return p


@pytest.fixture
def payload_drifted(tmp_path: Path) -> Path:
    p = tmp_path / "drifted.json"
    p.write_text(json.dumps({"temp_celsius": 31.5, "humidity": 80}))
    return p


@pytest.fixture
def payload_broken(tmp_path: Path) -> Path:
    p = tmp_path / "broken.json"
    p.write_text(json.dumps({"xyz": 31.5, "abc": 80}))
    return p


@pytest.fixture
def payload_coercible(tmp_path: Path) -> Path:
    p = tmp_path / "coercible.json"
    p.write_text(json.dumps({"temperature": "31.5", "humidity": 80}))
    return p


def _run(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    """Run main(argv) and return (exit_code, stdout, stderr)."""
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    code = int(exc_info.value.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ===========================================================================
# Argument parsing — --help / --version
# ===========================================================================


class TestArgParsing:
    def test_help_exits_zero(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0

    def test_version_output(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit):
            main(["--version"])
        out, _ = capsys.readouterr()
        assert "0.1.0" in out

    def test_check_help_exits_zero(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["check", "--help"])
        assert exc_info.value.code == 0

    def test_no_subcommand_exits_nonzero(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code != 0

    def test_check_requires_payload(self, schema_file: Path, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["check", "--schema", str(schema_file)])
        assert exc_info.value.code != 0

    def test_check_requires_model_or_schema(
        self, payload_valid: Path, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["check", "--payload", str(payload_valid)])
        assert exc_info.value.code != 0

    def test_model_and_schema_are_mutually_exclusive(
        self, schema_file: Path, payload_valid: Path, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "check",
                    "--schema",
                    str(schema_file),
                    "--model",
                    "pydantic:BaseModel",
                    "--payload",
                    str(payload_valid),
                ]
            )
        assert exc_info.value.code != 0


# ===========================================================================
# Exit codes
# ===========================================================================


class TestExitCodes:
    def test_exit_0_on_already_valid(
        self, schema_file: Path, payload_valid: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_valid)],
            capsys,
        )
        assert code == 0

    def test_exit_0_on_success(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_drifted)],
            capsys,
        )
        assert code == 0

    def test_exit_2_on_failed(
        self, schema_file: Path, payload_broken: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_broken)],
            capsys,
        )
        assert code == 2

    def test_exit_1_on_partial(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """One field is fuzzy-fixable, the other has no plausible
        candidate at all -- guaranteed PARTIAL, never FAILED or SUCCESS."""
        schema = {
            "fields": [
                {"path": "temperature", "type": "float"},
                {"path": "humidity", "type": "integer"},
                {"path": "country_code", "type": "string"},
            ]
        }
        schema_file = tmp_path / "partial_schema.json"
        schema_file.write_text(json.dumps(schema))
        payload_file = tmp_path / "partial_payload.json"
        payload_file.write_text(json.dumps({"temp_celsius": 31.5, "humidity": 80}))

        code, out, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_file)],
            capsys,
        )
        assert code == 1
        assert "PARTIAL" in out.upper()


# ===========================================================================
# Human-readable output
# ===========================================================================


class TestHumanOutput:
    def test_already_valid_shows_status(
        self, schema_file: Path, payload_valid: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _, out, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_valid)],
            capsys,
        )
        assert "ALREADY_VALID" in out.upper()

    def test_success_shows_strategy(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _, out, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_drifted)],
            capsys,
        )
        assert "FuzzyFieldMatchStrategy" in out

    def test_success_shows_repaired_payload(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _, out, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_drifted)],
            capsys,
        )
        assert "temperature" in out
        assert "31.5" in out

    def test_failed_shows_remaining_violations(
        self, schema_file: Path, payload_broken: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _, out, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_broken)],
            capsys,
        )
        assert "Remaining violations" in out

    def test_violations_shown_before_repair(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _, out, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_drifted)],
            capsys,
        )
        assert "missing_required_field" in out

    def test_coercion_repair_shown(
        self, schema_file: Path, payload_coercible: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _, out, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_coercible)],
            capsys,
        )
        assert "SUCCESS" in out.upper()

    def test_human_output_without_pydantic_installed(
        self,
        schema_file: Path,
        payload_drifted: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_dump_output's BaseModel-aware unwrapping also has an
        ImportError fallback for pydantic-less environments."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "pydantic":
                raise ImportError("simulated: pydantic not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        _, out, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_drifted)],
            capsys,
        )
        assert "SUCCESS" in out.upper()
        assert "temperature" in out


# ===========================================================================
# JSON output mode
# ===========================================================================


class TestJsonOutput:
    def _get_json(
        self, schema_file: Path, payload_file: Path, capsys: pytest.CaptureFixture
    ) -> dict[str, Any]:
        _, out, _ = _run(
            [
                "check",
                "--schema",
                str(schema_file),
                "--payload",
                str(payload_file),
                "--json",
            ],
            capsys,
        )
        return json.loads(out)

    def test_json_output_is_valid_json(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        data = self._get_json(schema_file, payload_drifted, capsys)
        assert isinstance(data, dict)

    def test_json_output_has_status_field(
        self, schema_file: Path, payload_valid: Path, capsys: pytest.CaptureFixture
    ) -> None:
        data = self._get_json(schema_file, payload_valid, capsys)
        assert data["status"] == "already_valid"

    def test_json_output_has_violations(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        data = self._get_json(schema_file, payload_drifted, capsys)
        assert isinstance(data["violations"], list)
        assert any(v["violation_type"] == "missing_required_field" for v in data["violations"])

    def test_json_output_has_attempts(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        data = self._get_json(schema_file, payload_drifted, capsys)
        assert len(data["attempts"]) == 1
        assert data["attempts"][0]["strategy"] == "FuzzyFieldMatchStrategy"

    def test_json_output_has_repaired_payload(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        data = self._get_json(schema_file, payload_drifted, capsys)
        assert data["repaired_output"]["temperature"] == 31.5
        assert data["repaired_output"]["humidity"] == 80

    def test_json_failed_repaired_output_is_null(
        self, schema_file: Path, payload_broken: Path, capsys: pytest.CaptureFixture
    ) -> None:
        data = self._get_json(schema_file, payload_broken, capsys)
        assert data["status"] == "failed"
        assert data["repaired_output"] is None

    def test_json_has_remaining_violations(
        self, schema_file: Path, payload_broken: Path, capsys: pytest.CaptureFixture
    ) -> None:
        data = self._get_json(schema_file, payload_broken, capsys)
        assert isinstance(data["remaining_violations"], list)
        assert len(data["remaining_violations"]) > 0

    def test_json_has_contract_id(
        self, schema_file: Path, payload_valid: Path, capsys: pytest.CaptureFixture
    ) -> None:
        data = self._get_json(schema_file, payload_valid, capsys)
        assert "contract_id" in data
        assert isinstance(data["contract_id"], str)

    def test_json_output_without_pydantic_installed(
        self,
        schema_file: Path,
        payload_drifted: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        _print_json's BaseModel-aware unwrapping is wrapped in a
        try/except ImportError so that --schema (DictContractAdapter)
        mode works even in environments without pydantic installed at
        all. Simulate that by forcing the local `from pydantic import
        BaseModel` to raise ImportError.
        """
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "pydantic":
                raise ImportError("simulated: pydantic not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        data = self._get_json(schema_file, payload_drifted, capsys)
        assert data["status"] == "success"
        assert data["repaired_output"]["temperature"] == 31.5


# ===========================================================================
# --strict flag
# ===========================================================================


class TestStrictFlag:
    def test_non_strict_extra_field_is_already_valid(
        self, schema_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        payload = tmp_path / "extra.json"
        payload.write_text(json.dumps({"temperature": 1.0, "humidity": 2, "extra": "hi"}))
        code, _, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload)],
            capsys,
        )
        assert code == 0

    def test_strict_extra_field_fails(
        self, schema_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        payload = tmp_path / "extra_strict.json"
        payload.write_text(
            json.dumps({"temperature": 1.0, "humidity": 2, "extra_unrelated_field_xyz": "hi"})
        )
        code, _, _ = _run(
            [
                "check",
                "--schema",
                str(schema_file),
                "--payload",
                str(payload),
                "--strict",
            ],
            capsys,
        )
        assert code != 0


# ===========================================================================
# Error handling — bad paths, bad JSON, bad model ref
# ===========================================================================


class TestErrorHandling:
    def test_missing_payload_file(self, schema_file: Path, capsys: pytest.CaptureFixture) -> None:
        code, _, err = _run(
            [
                "check",
                "--schema",
                str(schema_file),
                "--payload",
                "/no/such/file.json",
            ],
            capsys,
        )
        assert code == 2
        assert "error" in err.lower() or "not found" in err.lower()

    def test_missing_schema_file(self, payload_valid: Path, capsys: pytest.CaptureFixture) -> None:
        code, _, err = _run(
            [
                "check",
                "--schema",
                "/no/such/schema.json",
                "--payload",
                str(payload_valid),
            ],
            capsys,
        )
        assert code == 2
        assert "error" in err.lower()

    def test_invalid_json_payload(
        self, schema_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        code, _, err = _run(
            ["check", "--schema", str(schema_file), "--payload", str(bad)],
            capsys,
        )
        assert code == 2
        assert "error" in err.lower()

    def test_bad_model_ref_missing_colon(
        self, payload_valid: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, err = _run(
            [
                "check",
                "--model",
                "nocoalonthis",
                "--payload",
                str(payload_valid),
            ],
            capsys,
        )
        assert code == 2
        assert "error" in err.lower()

    def test_bad_model_ref_nonexistent_module(
        self, payload_valid: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, err = _run(
            [
                "check",
                "--model",
                "no.such.module:WeatherModel",
                "--payload",
                str(payload_valid),
            ],
            capsys,
        )
        assert code == 2
        assert "error" in err.lower()

    def test_bad_model_ref_nonexistent_class(
        self, payload_valid: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, err = _run(
            [
                "check",
                "--model",
                "pydantic:NonExistentClass9999",
                "--payload",
                str(payload_valid),
            ],
            capsys,
        )
        assert code == 2
        assert "error" in err.lower()

    def test_invalid_schema_json(
        self, payload_valid: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        bad_schema = tmp_path / "bad_schema.json"
        bad_schema.write_text("{not valid json at all}")
        code, _, err = _run(
            [
                "check",
                "--schema",
                str(bad_schema),
                "--payload",
                str(payload_valid),
            ],
            capsys,
        )
        assert code == 2
        assert "error" in err.lower()


# ===========================================================================
# --model flag (Pydantic)
# ===========================================================================


class TestModelFlag:
    def test_pydantic_model_repair(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Test --model with a real module that ships with stateguard's
        test suite; we can import from tests.conftest in test context."""
        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps({"temp_celsius": 31.5, "humidity": 80}))

        code, out, _ = _run(
            [
                "check",
                "--model",
                "tests.adapters.pydantic.conftest:Weather",
                "--payload",
                str(payload),
            ],
            capsys,
        )
        assert code == 0
        assert "SUCCESS" in out.upper() or "ALREADY_VALID" in out.upper()

    def test_pydantic_model_json_output_dumps_basemodel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """With --model, repaired_output is a validated BaseModel instance
        -- _print_json must dump it via model_dump(), not raise on a
        non-JSON-serializable object."""
        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps({"temp_celsius": 31.5, "humidity": 80}))

        _, out, _ = _run(
            [
                "check",
                "--model",
                "tests.adapters.pydantic.conftest:Weather",
                "--payload",
                str(payload),
                "--json",
            ],
            capsys,
        )
        data = json.loads(out)
        assert data["repaired_output"]["temperature"] == 31.5
        assert data["repaired_output"]["humidity"] == 80


# ===========================================================================
# --max-attempts and --confidence-threshold flags
# ===========================================================================


class TestPowerUserFlags:
    def test_max_attempts_flag_accepted(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, _ = _run(
            [
                "check",
                "--schema",
                str(schema_file),
                "--payload",
                str(payload_drifted),
                "--max-attempts",
                "3",
            ],
            capsys,
        )
        assert code == 0

    def test_confidence_threshold_zero_still_repairs(
        self, schema_file: Path, payload_drifted: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, _ = _run(
            [
                "check",
                "--schema",
                str(schema_file),
                "--payload",
                str(payload_drifted),
                "--confidence-threshold",
                "0.1",
            ],
            capsys,
        )
        assert code == 0


# ===========================================================================
# Internal helper unit tests
# ===========================================================================


class TestLoadJson:
    def test_valid_json_file(self, tmp_path: Path) -> None:
        p = tmp_path / "test.json"
        p.write_text('{"key": "value"}')
        result = _load_json(str(p))
        assert result == {"key": "value"}

    def test_missing_file_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit):
            _load_json("/no/such/file_9999.json")

    def test_invalid_json_raises_system_exit(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{invalid}")
        with pytest.raises(SystemExit):
            _load_json(str(p))


class TestLoadModel:
    def test_valid_reference(self) -> None:
        model = _load_model("pydantic:BaseModel")
        from pydantic import BaseModel

        assert model is BaseModel

    def test_missing_colon_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit):
            _load_model("pydanticBaseModel")

    def test_nonexistent_module_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit):
            _load_model("no.such.module:Class")

    def test_nonexistent_class_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit):
            _load_model("pydantic:NonExistentClass9999")


# ---------------------------------------------------------------------------
# --tool: replaying an MCP tool call
# ---------------------------------------------------------------------------

FORECAST_TOOL: dict[str, Any] = {
    "name": "get_forecast",
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string"},
            "days": {"type": "integer", "maximum": 14},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"], "default": "celsius"},
        },
        "required": ["location", "days"],
    },
}


def _write(tmp_path: Path, name: str, content: Any) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(content))
    return str(p)


class TestToolFlag:
    """
    ``check --tool`` is the replay harness: a captured tool definition and a
    captured call in, what a repairing proxy would do out.
    """

    def test_a_repairable_call_forwards(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tool = _write(tmp_path, "tool.json", FORECAST_TOOL)
        args = _write(tmp_path, "args.json", {"loc": "Mumbai", "days": "5"})
        code, out, _ = _run(["check", "--tool", tool, "--payload", args], capsys)
        assert code == 0
        assert out.lstrip().startswith("FORWARD: Repaired 2 field(s)")
        assert "repaired    location <- loc" in out
        assert '"location": "Mumbai"' in out

    def test_a_bare_input_schema_works_too(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tool = _write(tmp_path, "schema.json", FORECAST_TOOL["inputSchema"])
        args = _write(tmp_path, "args.json", {"location": "Mumbai", "days": 3})
        code, out, _ = _run(["check", "--tool", tool, "--payload", args], capsys)
        assert code == 0
        assert "FORWARD" in out

    def test_shadow_sends_the_received_arguments_and_shows_the_preview(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tool = _write(tmp_path, "tool.json", FORECAST_TOOL)
        args = _write(tmp_path, "args.json", {"loc": "Mumbai", "days": "5"})
        code, out, _ = _run(["check", "--tool", tool, "--payload", args, "--shadow"], capsys)
        assert code == 0
        assert out.lstrip().startswith("HOLD:")
        assert "Arguments to send (as received" in out
        assert "Shadow preview" in out

    def test_an_out_of_range_value_is_refused_with_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tool = _write(tmp_path, "tool.json", FORECAST_TOOL)
        args = _write(tmp_path, "args.json", {"location": "x", "days": 99})
        code, out, _ = _run(["check", "--tool", tool, "--payload", args], capsys)
        assert code == 2
        assert "REFUSE" in out
        assert "not forwarded" in out

    def test_an_ambiguous_call_escalates_with_exit_3(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tool = _write(
            tmp_path,
            "tool.json",
            {
                "name": "lookup",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string"},
                        "account_name": {"type": "string"},
                    },
                    "required": ["account_id", "account_name"],
                },
            },
        )
        args = _write(tmp_path, "args.json", {"account": "x"})
        code, out, _ = _run(["check", "--tool", tool, "--payload", args], capsys)
        assert code == 3
        assert "ESCALATE" in out
        assert "held        account_id <- account" in out

    def test_null_arguments_are_no_arguments(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tool = _write(tmp_path, "tool.json", {"name": "ping", "inputSchema": {"type": "object"}})
        args = _write(tmp_path, "args.json", None)
        code, out, _ = _run(["check", "--tool", tool, "--payload", args], capsys)
        assert code == 0
        assert "FORWARD" in out

    def test_an_unsupported_schema_is_an_error_message_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tool = _write(
            tmp_path, "tool.json", {"name": "t", "inputSchema": {"type": "object", "allOf": [{}]}}
        )
        args = _write(tmp_path, "args.json", {})
        code, out, err = _run(["check", "--tool", tool, "--payload", args], capsys)
        assert code == 2
        assert out == ""
        assert "cannot check calls against this tool's schema" in err
        assert "allOf" in err

    def test_json_output_is_the_outcome_record(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tool = _write(tmp_path, "tool.json", FORECAST_TOOL)
        sent = {"loc": "Mumbai", "days": "5"}
        args = _write(tmp_path, "args.json", sent)
        code, out, _ = _run(["check", "--tool", tool, "--payload", args, "--json"], capsys)
        assert code == 0
        record = json.loads(out)
        assert record["action"] == "forward"
        assert record["arguments"] == {"location": "Mumbai", "days": 5, "unit": "celsius"}
        assert record["result"]["original_input"] == sent
        assert [d["decision"] for d in record["result"]["decisions"]] == ["repaired", "repaired"]

    def test_tool_is_exclusive_with_schema(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tool = _write(tmp_path, "tool.json", FORECAST_TOOL)
        args = _write(tmp_path, "args.json", {})
        with pytest.raises(SystemExit) as exc_info:
            main(["check", "--tool", tool, "--schema", tool, "--payload", args])
        assert exc_info.value.code == 2


class TestJsonOutputAdditions:
    def test_schema_json_output_gains_original_input_and_decisions(
        self, schema_file: Path, payload_coercible: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, out, _ = _run(
            ["check", "--schema", str(schema_file), "--payload", str(payload_coercible), "--json"],
            capsys,
        )
        assert code == 0
        record = json.loads(out)
        assert record["original_input"] == {"temperature": "31.5", "humidity": 80}
        assert [(d["path"], d["decision"]) for d in record["decisions"]] == [
            ("temperature", "repaired")
        ]
        # The pre-existing keys are still there.
        for key in ("status", "mode", "violations", "attempts", "ambiguous", "repaired_output"):
            assert key in record


# ---------------------------------------------------------------------------
# diff / replay
# ---------------------------------------------------------------------------


def _git_tools() -> list[dict[str, Any]]:
    path = Path(__file__).parent / "adapters/jsonschema/corpus/schemas/mcp-server-git.json"
    data = json.loads(path.read_text())
    return list(data["tools"] if isinstance(data, dict) else data)


class TestDiffCommand:
    def test_identical_lists_exit_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        tools = _write(tmp_path, "t.json", {"tools": _git_tools()})
        code, out, _ = _run(["diff", tools, tools], capsys)
        assert code == 0
        assert "0 breaking" in out

    def test_a_breaking_list_change_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        import copy

        v1 = _git_tools()
        v2 = copy.deepcopy(v1)
        commit = next(t for t in v2 if t["name"] == "git_commit")
        commit["inputSchema"]["properties"]["author"] = {"type": "string"}
        commit["inputSchema"]["required"].append("author")
        v2 = [t for t in v2 if t["name"] != "git_reset"]
        code, out, _ = _run(
            ["diff", _write(tmp_path, "a.json", v1), _write(tmp_path, "b.json", {"tools": v2})],
            capsys,
        )
        assert code == 1
        assert "git_commit  BREAKING" in out and "REMOVED   git_reset" in out

    def test_json_report(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        v1 = {"tools": _git_tools()}
        v2 = {"jsonrpc": "2.0", "id": 1, "result": {"tools": _git_tools()[:-1]}}
        code, out, _ = _run(
            ["diff", _write(tmp_path, "a.json", v1), _write(tmp_path, "b.json", v2), "--json"],
            capsys,
        )
        report = json.loads(out)
        assert code == 1
        assert report["summary"]["removed"] == 1

    def test_a_single_tool(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        before = FORECAST_TOOL
        after = json.loads(json.dumps(FORECAST_TOOL))
        after["inputSchema"]["properties"]["days"]["maximum"] = 30
        code, out, _ = _run(
            ["diff", _write(tmp_path, "a.json", before), _write(tmp_path, "b.json", after)], capsys
        )
        assert code == 0
        assert "'maximum' loosened: 14 -> 30" in out

    def test_bare_schemas(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        before = FORECAST_TOOL["inputSchema"]
        after = {**before, "allOf": [{}]}
        code, out, _ = _run(
            [
                "diff",
                _write(tmp_path, "a.json", before),
                _write(tmp_path, "b.json", after),
                "--json",
            ],
            capsys,
        )
        assert code == 1
        assert json.loads(out)["status"] == "unsupported"

    def test_a_list_against_a_tool_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, err = _run(
            [
                "diff",
                _write(tmp_path, "a.json", {"tools": []}),
                _write(tmp_path, "b.json", FORECAST_TOOL),
            ],
            capsys,
        )
        assert code == 2 and "tool list" in err


class TestReplayCommand:
    def _calls(self, tmp_path: Path) -> str:
        calls = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_forecast", "arguments": {"loc": "Mumbai", "days": "5"}},
            },
            {"name": "get_forecast", "arguments": {"location": "x", "days": 99}},
            {"name": "nope", "arguments": {}},
            "garbage",
        ]
        path = tmp_path / "calls.jsonl"
        path.write_text("\n".join(json.dumps(c) for c in calls))
        return str(path)

    def test_human_summary(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        tools = _write(tmp_path, "t.json", {"tools": [FORECAST_TOOL]})
        code, out, _ = _run(["replay", "--tools", tools, "--calls", self._calls(tmp_path)], capsys)
        assert code == 0
        assert "Summary: 1 forward, 1 malformed_call, 1 refuse, 1 unknown_tool" in out

    def test_shadow_forwards_everything_it_can_read(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tools = _write(tmp_path, "t.json", [FORECAST_TOOL])
        code, out, _ = _run(
            ["replay", "--tools", tools, "--calls", self._calls(tmp_path), "--shadow", "--json"],
            capsys,
        )
        record = json.loads(out)
        assert record["mode"] == "shadow"
        assert record["summary"] == {"hold": 2, "malformed_call": 1, "unknown_tool": 1}
        assert [c.get("auto_action") for c in record["calls"][:2]] == ["forward", "refuse"]
        assert record["calls"][0]["id"] == 1
        assert record["auto_summary"] == {
            "forward": 1,
            "malformed_call": 1,
            "refuse": 1,
            "unknown_tool": 1,
        }

    def test_shadow_summary_says_what_auto_would_change(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        tools = _write(tmp_path, "t.json", [FORECAST_TOOL])
        code, out, _ = _run(
            ["replay", "--tools", tools, "--calls", self._calls(tmp_path), "--shadow"], capsys
        )
        assert code == 0
        assert "Summary: 2 hold, 1 malformed_call, 1 unknown_tool" in out
        assert "Auto mode would: 1 forward, 1 malformed_call, 1 refuse, 1 unknown_tool" in out

    def test_unreadable_tools_are_named(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        bad = {"name": "get_forecast", "inputSchema": {"type": "object", "allOf": [{}]}}
        code, out, _ = _run(
            [
                "replay",
                "--tools",
                _write(tmp_path, "t.json", [bad]),
                "--calls",
                self._calls(tmp_path),
            ],
            capsys,
        )
        assert "cannot read: get_forecast" in out
        assert "UNUSABLE_SCHEMA" in out

    def test_a_non_list_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code, _, err = _run(
            [
                "replay",
                "--tools",
                _write(tmp_path, "t.json", FORECAST_TOOL),
                "--calls",
                self._calls(tmp_path),
            ],
            capsys,
        )
        assert code == 2 and "not a tool list" in err
