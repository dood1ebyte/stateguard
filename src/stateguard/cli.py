"""
StateGuard command-line interface.

Entry point: ``stateguard``
Sub-command: ``stateguard check``

Usage examples
--------------
Check a payload against a Pydantic model::

    stateguard check --model mypackage.models:Weather --payload payload.json

Check a payload against a JSON schema file::

    stateguard check --schema contract.json --payload payload.json

JSON output (for piping into other tools)::

    stateguard check --schema contract.json --payload payload.json --json

Exit codes
----------
0   SUCCESS or ALREADY_VALID — data conforms to the contract (after repair
    if repair was attempted).
1   PARTIAL — the data was partially repaired but violations remain.
2   FAILED — the data could not be repaired to satisfy the contract.
    ``argparse`` also uses exit code 2 for usage errors (argument parsing
    failures), which is a standard Unix convention.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, NoReturn, cast

__all__ = ["main"]

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

_EXIT_OK = 0  # SUCCESS or ALREADY_VALID
_EXIT_PARTIAL = 1  # PARTIAL
_EXIT_FAILED = 2  # FAILED  (argparse also uses 2 for bad args, per convention)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path_str: str) -> dict[str, Any]:
    """Load and parse a JSON file; raise SystemExit with a clear message on error."""
    path = Path(path_str)
    if not path.exists():
        _die(f"File not found: {path}")
    try:
        # utf-8-sig tolerates (and strips) a leading BOM -- e.g. files written
        # by PowerShell's `Out-File -Encoding utf8` or Notepad's "UTF-8" save.
        with open(path, encoding="utf-8-sig") as f:
            return cast(dict[str, Any], json.load(f))
    except json.JSONDecodeError as exc:
        _die(f"Could not parse JSON in {path}: {exc}")


def _load_model(model_ref: str) -> Any:
    """
    Dynamically import a class from a ``module.path:ClassName`` reference.

    Raises ``SystemExit`` with a clear message if the module can't be
    imported or the attribute doesn't exist.
    """
    if ":" not in model_ref:
        _die(
            f"--model value must be in the form 'module.path:ClassName' "
            f"(e.g. 'mypackage.models:Weather'), got: {model_ref!r}"
        )
    module_path, class_name = model_ref.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        _die(f"Could not import module '{module_path}': {exc}")
    try:
        return getattr(module, class_name)
    except AttributeError:
        _die(f"Module '{module_path}' has no attribute '{class_name}'.")


def _die(message: str) -> NoReturn:
    print(f"stateguard error: {message}", file=sys.stderr)
    sys.exit(_EXIT_FAILED)


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


def _print_human(result: Any, args: argparse.Namespace) -> None:
    """Print a human-readable repair summary to stdout."""
    from stateguard.core.errors.results import RepairStatus  # local import keeps startup fast

    status = result.status
    status_icons = {
        RepairStatus.SUCCESS: "✓",
        RepairStatus.ALREADY_VALID: "✓",
        RepairStatus.PARTIAL: "⚠",
        RepairStatus.FAILED: "✗",
    }
    icon = status_icons.get(status, "?")
    print(f"\n{icon} Status: {status.value.upper()}\n")

    if result.initial_violations:
        print("Violations detected:")
        for v in result.initial_violations:
            sev = v.severity.value.upper()
            print(f"  [{sev}] {v.field_path}: {v.violation_type.value}  — {v.message}")
        print()

    if result.attempts:
        print(f"Repair attempts: {len(result.attempts)}")
        for attempt in result.attempts:
            op_count = len(attempt.applied_operations)
            rej_count = len(attempt.rejected_operations)
            tick = "✓" if attempt.succeeded else "✗"
            print(f"  {tick} Attempt {attempt.attempt_number}: {attempt.strategy_name}")
            print(f"      Applied: {op_count} operation(s)  Rejected: {rej_count}")
            for op in attempt.applied_operations:
                src = f"  ← {op.source_path}" if op.source_path else ""
                print(
                    f"      • {op.op_type.value}  {op.target_path}{src}  "
                    f"(confidence {op.confidence:.2f})"
                )
        print()

    if result.remaining_violations:
        print("Remaining violations:")
        for v in result.remaining_violations:
            print(f"  [{v.severity.value.upper()}] {v.field_path}: {v.violation_type.value}")
        print()

    if result.repaired_output is not None:
        print("Repaired payload:")
        _dump_output(result.repaired_output)
    else:
        print("Repaired payload: (none — repair failed)")
    print()


def _dump_output(obj: Any) -> None:
    """Print *obj* as indented JSON, handling Pydantic models via model_dump()."""
    try:
        from pydantic import BaseModel  # noqa: PLC0415

        if isinstance(obj, BaseModel):
            obj = obj.model_dump()
    except ImportError:
        pass
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def _print_json(result: Any) -> None:
    """Print a machine-readable JSON summary to stdout."""
    try:
        from pydantic import BaseModel  # noqa: PLC0415

        repaired = result.repaired_output
        if isinstance(repaired, BaseModel):
            repaired = repaired.model_dump()
    except ImportError:
        repaired = result.repaired_output

    output = {
        "status": result.status.value,
        "contract_id": result.contract_id,
        "violations": [
            {
                "field_path": v.field_path,
                "violation_type": v.violation_type.value,
                "severity": v.severity.value,
                "message": v.message,
            }
            for v in result.initial_violations
        ],
        "remaining_violations": [
            {
                "field_path": v.field_path,
                "violation_type": v.violation_type.value,
                "severity": v.severity.value,
            }
            for v in result.remaining_violations
        ],
        "attempts": [
            {
                "attempt_number": a.attempt_number,
                "strategy": a.strategy_name,
                "succeeded": a.succeeded,
                "applied_operations": [
                    {
                        "op_type": op.op_type.value,
                        "target_path": op.target_path,
                        "source_path": op.source_path,
                        "confidence": op.confidence,
                    }
                    for op in a.applied_operations
                ],
                "rejected_count": len(a.rejected_operations),
            }
            for a in result.attempts
        ],
        "repaired_output": repaired,
    }
    print(json.dumps(output, indent=2, default=str))


# ---------------------------------------------------------------------------
# check sub-command
# ---------------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> int:
    from stateguard.core.models.config import GuardConfig, RepairConfig  # noqa: PLC0415
    from stateguard.guard import ContractGuard  # noqa: PLC0415

    # Build config from CLI flags
    repair_config = RepairConfig(
        max_attempts=args.max_attempts,
        min_confidence_threshold=args.confidence_threshold,
    )
    config = GuardConfig(
        strict_mode=args.strict,
        repair=repair_config,
    )

    # Load payload
    payload: dict[str, Any] = _load_json(args.payload)

    # Build guard from --model or --schema
    if args.model:
        model_class = _load_model(args.model)
        guard = ContractGuard.with_pydantic(config=config)
        schema: Any = model_class
    elif args.schema:
        schema = _load_json(args.schema)
        guard = ContractGuard.with_dict_schema(config=config)
    else:
        # Unreachable: argparse's mutually_exclusive_group(required=True)
        # on --model/--schema already guarantees one is set before this
        # function is ever called. Kept as a defensive guard, not a tested
        # code path.
        _die("One of --model or --schema is required.")  # pragma: no cover
        return _EXIT_FAILED  # pragma: no cover

    # Run repair
    result = guard.repair(schema, payload)

    # Emit output
    if args.json:
        _print_json(result)
    else:
        _print_human(result, args)

    # Exit code
    from stateguard.core.errors.results import RepairStatus  # noqa: PLC0415

    if result.status in (RepairStatus.SUCCESS, RepairStatus.ALREADY_VALID):
        return _EXIT_OK
    if result.status is RepairStatus.PARTIAL:
        return _EXIT_PARTIAL
    return _EXIT_FAILED


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stateguard",
        description="StateGuard — runtime contract repair for AI tool outputs.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # -- check ---------------------------------------------------------------
    check = sub.add_parser(
        "check",
        help="Validate and repair a payload against a contract.",
        description=(
            "Validate a JSON payload against a contract (Pydantic model or "
            "JSON schema file). Attempts to repair any violations found and "
            "reports the result."
        ),
    )

    schema_group = check.add_mutually_exclusive_group(required=True)
    schema_group.add_argument(
        "--model",
        metavar="MODULE:CLASS",
        help=(
            "Pydantic BaseModel to validate against, as a 'module.path:ClassName' "
            "reference (e.g. 'mypackage.models:Weather'). Requires pydantic."
        ),
    )
    schema_group.add_argument(
        "--schema",
        metavar="FILE",
        help=(
            "Path to a JSON contract schema file in StateGuard's simple format. "
            "See 'stateguard/adapters/dict_adapter.py' for the format spec."
        ),
    )

    check.add_argument(
        "--payload",
        metavar="FILE",
        required=True,
        help="Path to the JSON payload file to validate.",
    )
    check.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit machine-readable JSON output instead of human-readable text.",
    )
    check.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help=(
            "Enable strict mode: extra fields in the payload that are not "
            "declared in the contract are treated as errors rather than warnings."
        ),
    )
    check.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        metavar="N",
        dest="max_attempts",
        help="Maximum number of repair iterations (default: 5).",
    )
    check.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.7,
        metavar="FLOAT",
        dest="confidence_threshold",
        help=(
            "Minimum confidence score [0.0, 1.0] required before an operation "
            "is applied (default: 0.7)."
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """
    Main entry point for the ``stateguard`` CLI.

    Parameters
    ----------
    argv:
        Argument list, defaults to ``sys.argv[1:]``.  Separated out to
        make the function testable without subprocess overhead.
    """
    # Windows consoles often default to a legacy codepage (e.g. cp1252) that
    # can't encode the Unicode icons/bullets used in human-readable output.
    # Force UTF-8 on stdout/stderr where possible rather than crashing.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass  # pragma: no cover

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        sys.exit(_cmd_check(args))
    else:
        # Unreachable: sub.required = True forces argparse to error out
        # before main() is reached if no subcommand is given. Kept as a
        # defensive guard for future subcommands, not a tested code path.
        parser.print_help()  # pragma: no cover
        sys.exit(_EXIT_FAILED)  # pragma: no cover
