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

Check a tool call's arguments against an MCP tool definition (or a bare
JSON Schema), and see what a proxy would do with it::

    stateguard check --tool tool.json --payload arguments.json

What changes if this tool definition changes? (a tool, a bare schema, or a
whole ``tools/list`` on each side)::

    stateguard diff tools-v1.json tools-v2.json

Replay recorded calls against a tool list -- what would StateGuard do?::

    stateguard replay --tools tools.json --calls calls.jsonl [--shadow]

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
3   AMBIGUOUS — a repair was found but the evidence did not justify applying
    it unsupervised. Distinct from 2 because it is actionable: the candidates
    are printed, so a caller can choose one or re-prompt for better output.

``stateguard diff`` exits 0 when no change can break a call that worked
before, 1 when one can (including a tool removed, or a schema that became
unreadable), and 2 on a usage error. ``stateguard replay`` exits 0 when the
replay ran, 2 on a usage error -- its findings are in the output.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

from stateguard import __version__

__all__ = ["main"]

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

_EXIT_OK = 0  # SUCCESS or ALREADY_VALID
_EXIT_PARTIAL = 1  # PARTIAL
_EXIT_FAILED = 2  # FAILED  (argparse also uses 2 for bad args, per convention)
_EXIT_AMBIGUOUS = 3  # AMBIGUOUS -- a repair was found but withheld
_EXIT_BROKEN_PIPE = 141  # stdout closed early (128 + SIGPIPE), as a shell reports it


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path_str: str) -> Any:
    """Load and parse a JSON file; raise SystemExit with a clear message on error."""
    path = Path(path_str)
    if not path.exists():
        _die(f"File not found: {path}")
    try:
        # utf-8-sig tolerates (and strips) a leading BOM -- e.g. files written
        # by PowerShell's `Out-File -Encoding utf8` or Notepad's "UTF-8" save.
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
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


def _candidate_discriminator(candidate: Any) -> str:
    """
    Render whatever tells one ambiguous candidate apart from its rivals.

    A ``RENAME`` is identified by the key it reads from; a ``SET_VALUE`` or
    ``SET_DEFAULT`` by the value it would write.  Both are schema- or
    payload-key data of the kind this human output already prints (violation
    messages quote received values); the structured repair *log* is what
    ``RepairConfig.include_values_in_log`` governs, and it is unaffected.
    """
    from stateguard.core.errors.operations import FieldOpType  # noqa: PLC0415

    if candidate.source_path:
        return f"  ← {candidate.source_path}"
    if candidate.op_type in (FieldOpType.SET_VALUE, FieldOpType.SET_DEFAULT):
        return f"  → {candidate.value!r}"
    return ""


def _print_human(result: Any, args: argparse.Namespace) -> None:
    """Print a human-readable repair summary to stdout."""
    from stateguard.core.errors.results import RepairStatus  # local import keeps startup fast

    status = result.status
    status_icons = {
        RepairStatus.SUCCESS: "✓",
        RepairStatus.ALREADY_VALID: "✓",
        RepairStatus.PARTIAL: "⚠",
        RepairStatus.FAILED: "✗",
        RepairStatus.AMBIGUOUS: "?",
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
                    f"(trust {op.trust:.2f}, risk {op.risk.name})"
                )
        print()

    if result.ambiguous:
        print("Ambiguous repairs (found, not applied):")
        for item in result.ambiguous:
            best = item.best
            print(f"  ? {item.target_path}: {item.reason}")
            for candidate in item.candidates:
                # Name what distinguishes this candidate from its rivals: a
                # rename is identified by where it reads from, a set_value by
                # what it would write. Printing neither made two competing
                # enum candidates render as identical lines -- an unusable
                # list for the one decision this section exists to support.
                print(
                    f"      • {candidate.op_type.value}{_candidate_discriminator(candidate)}  "
                    f"(trust {candidate.trust:.2f}, risk {candidate.risk.name})"
                )
            if best is not None:
                for note in best.evidence.notes:
                    print(f"        - {note}")
        print()

    if result.remaining_violations:
        print("Remaining violations:")
        for v in result.remaining_violations:
            print(f"  [{v.severity.value.upper()}] {v.field_path}: {v.violation_type.value}")
        print()

    # Shadow never prints "Repaired payload" -- the whole point is that
    # nothing was committed, and a heading that says otherwise is exactly the
    # misreading the mode exists to prevent.
    if result.is_shadow:
        if result.proposed_output is not None:
            print("Proposed payload (SHADOW — not applied):")
            _dump_output(result.proposed_output)
        else:
            print("Proposed payload: (none — no repair found)")
    elif result.repaired_output is not None:
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
    repaired = _jsonable_output(result.repaired_output)
    proposed = _jsonable_output(result.proposed_output)

    output = {
        "status": result.status.value,
        # Both fields are always present so a consumer can read one shape
        # regardless of mode; exactly one of them is ever non-null.
        "mode": result.mode.value,
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
                        "trust": op.trust,
                        "risk": op.risk.name,
                    }
                    for op in a.applied_operations
                ],
                "rejected_count": len(a.rejected_operations),
            }
            for a in result.attempts
        ],
        "ambiguous": [
            {
                "target_path": item.target_path,
                "reason": item.reason,
                "candidates": [
                    {
                        "op_type": candidate.op_type.value,
                        "source_path": candidate.source_path,
                        # Without this two competing set_value candidates are
                        # byte-identical in the JSON, so an agent re-prompting
                        # on an ambiguous result cannot tell them apart --
                        # which is the entire purpose of this block.
                        "value": candidate.value,
                        "trust": candidate.trust,
                        "risk": candidate.risk.name,
                        "notes": list(candidate.evidence.notes),
                    }
                    for candidate in item.candidates
                ],
            }
            for item in result.ambiguous
        ],
        "repaired_output": repaired,
        "proposed_output": proposed,
    }
    # Added later, so appended rather than merged into the keys above:
    # existing consumers keep the shape they parse.
    record = result.to_dict()
    output["original_input"] = record["original_input"]
    output["decisions"] = record["decisions"]
    print(json.dumps(output, indent=2, default=str))


def _jsonable_output(payload: Any) -> Any:
    """Render a repaired/proposed payload as JSON-serialisable data."""
    try:
        from pydantic import BaseModel  # noqa: PLC0415
    except ImportError:
        return payload
    return payload.model_dump() if isinstance(payload, BaseModel) else payload


# ---------------------------------------------------------------------------
# check sub-command
# ---------------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> int:
    from stateguard.core.models.config import (  # noqa: PLC0415
        GuardConfig,
        RepairConfig,
        RepairMode,
    )
    from stateguard.guard import ContractGuard  # noqa: PLC0415

    # Build config from CLI flags
    repair_config = RepairConfig(
        max_attempts=args.max_attempts,
        min_confidence_threshold=args.confidence_threshold,
    )
    config = GuardConfig(
        strict_mode=args.strict,
        repair=repair_config,
        mode=RepairMode.SHADOW if args.shadow else RepairMode.AUTO,
    )

    # Load payload
    payload: Any = _load_json(args.payload)

    if args.tool:
        return _cmd_check_tool(args, config, payload)

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

    return _exit_code(result.status)


def _exit_code(status: Any) -> int:
    from stateguard.core.errors.results import RepairStatus  # noqa: PLC0415

    if status in (RepairStatus.SUCCESS, RepairStatus.ALREADY_VALID):
        return _EXIT_OK
    if status is RepairStatus.PARTIAL:
        return _EXIT_PARTIAL
    if status is RepairStatus.AMBIGUOUS:
        return _EXIT_AMBIGUOUS
    return _EXIT_FAILED


def _cmd_check_tool(args: argparse.Namespace, config: Any, payload: Any) -> int:
    """
    ``check --tool``: what a repairing MCP proxy would do with this call.

    The payload is the call's ``arguments`` (``null`` meaning none, as MCP
    allows). Output is the ``ToolCallOutcome`` -- its explanation, or with
    ``--json`` its ``to_dict()`` record -- so a captured tool definition and
    a captured call can be replayed and inspected without writing code.
    """
    from stateguard.adapters.jsonschema.errors import JSONSchemaError  # noqa: PLC0415
    from stateguard.adapters.mcp import repair_tool_call  # noqa: PLC0415
    from stateguard.guard import ContractGuard  # noqa: PLC0415

    tool = _load_json(args.tool)
    guard = ContractGuard.with_mcp(config=config)
    try:
        outcome = repair_tool_call(guard, tool, payload)
    except JSONSchemaError as exc:
        # Not a usage error and not a failed repair: StateGuard cannot read
        # this schema, so it cannot vouch for any call to the tool. Said
        # plainly rather than as a traceback.
        _die(f"cannot check calls against this tool's schema: {exc}")

    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, default=str))
    else:
        print()
        print(outcome.explain())
        print()
        if outcome.arguments is not None:
            shadow = " (as received — shadow mode)" if outcome.preview is not None else ""
            print(f"Arguments to send{shadow}:")
            _dump_output(outcome.arguments)
        else:
            print("Arguments to send: (none — not forwarded)")
        if outcome.preview is not None:
            print("Shadow preview (what auto mode would send):")
            _dump_output(outcome.preview)
        print()

    return _exit_code(outcome.result.status)


# ---------------------------------------------------------------------------
# diff / replay -- tool lists
# ---------------------------------------------------------------------------


def _tool_list(document: Any) -> list[Any] | None:
    """
    The tools in *document* if it is a tool list, else ``None``.

    Accepts the shapes a captured listing comes in: a ``tools/list`` result
    (``{"tools": [...]}``), a whole JSON-RPC response carrying one, or a bare
    array of tool definitions.
    """
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        if isinstance(document.get("tools"), list):
            return list(document["tools"])
        result = document.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            return list(result["tools"])
    return None


def _cmd_diff(args: argparse.Namespace) -> int:
    from stateguard.adapters.jsonschema.diff import diff_schemas  # noqa: PLC0415
    from stateguard.adapters.mcp import (  # noqa: PLC0415
        InvalidToolDefinitionError,
        ToolRegistry,
        split_tool_definition,
    )

    before, after = _load_json(args.before), _load_json(args.after)
    before_list, after_list = _tool_list(before), _tool_list(after)

    if (before_list is None) != (after_list is None):
        _die("compare a tool list with a tool list, or a tool/schema with a tool/schema.")

    if before_list is not None and after_list is not None:
        registry = ToolRegistry()
        try:
            registry.sync(before_list)
            report = registry.sync(after_list)
        except InvalidToolDefinitionError as exc:
            _die(str(exc))
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, default=str))
        else:
            print(report.explain())
        return _EXIT_PARTIAL if report.breaking else _EXIT_OK

    both_tools = all(isinstance(doc, dict) and "name" in doc for doc in (before, after))
    if both_tools:
        registry = ToolRegistry()
        try:
            registry.register(before)
            change = registry.register(after) if after["name"] == before["name"] else None
        except InvalidToolDefinitionError as exc:
            _die(str(exc))
        if change is not None:
            if args.json:
                print(json.dumps(change.to_dict(), indent=2, default=str))
            else:
                print(change.explain())
            return _EXIT_PARTIAL if change.breaking else _EXIT_OK

    # Bare schemas, or two tools with different names: compare the schemas.
    def schema_of(document: Any) -> Any:
        try:
            return split_tool_definition(document)[1]
        except ValueError:
            return document  # let diff_schemas classify it

    diff = diff_schemas(schema_of(before), schema_of(after))
    if args.json:
        print(json.dumps(diff.to_dict(), indent=2, default=str))
    else:
        print(diff.explain())
    return _EXIT_OK if diff.is_compatible else _EXIT_PARTIAL


def _load_calls(path_str: str) -> list[Any]:
    """Calls from a JSON array or a JSON-lines file; each a call or a tools/call message."""
    path = Path(path_str)
    if not path.exists():
        _die(f"File not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        try:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            _die(f"Could not parse {path} as JSON or JSON lines: {exc}")
    return document if isinstance(document, list) else [document]


def _call_parts(call: Any) -> tuple[str, Any, Any] | None:
    """``(name, arguments, id)`` from a plain call or a JSON-RPC tools/call."""
    if not isinstance(call, dict):
        return None
    params = call.get("params") if call.get("method") == "tools/call" else call
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return None
    return params["name"], params.get("arguments"), call.get("id")


def _cmd_replay(args: argparse.Namespace) -> int:
    from collections import Counter  # noqa: PLC0415

    from stateguard.adapters.mcp import (  # noqa: PLC0415
        InvalidToolDefinitionError,
        ToolNotFoundError,
        ToolRegistry,
        ToolSchemaUnusableError,
    )
    from stateguard.core.models.config import GuardConfig, RepairMode  # noqa: PLC0415
    from stateguard.guard import ContractGuard  # noqa: PLC0415

    tools = _tool_list(_load_json(args.tools))
    if tools is None:
        _die(f"{args.tools} is not a tool list (a tools/list result, or an array of tools).")
    mode = RepairMode.SHADOW if args.shadow else RepairMode.AUTO
    registry = ToolRegistry(ContractGuard.with_mcp(config=GuardConfig(mode=mode)))
    try:
        listing = registry.sync(tools)
    except InvalidToolDefinitionError as exc:
        _die(str(exc))

    records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    auto_counts: Counter[str] = Counter()
    for index, call in enumerate(_load_calls(args.calls)):
        parts = _call_parts(call)
        record: dict[str, Any] = {"index": index}
        if parts is None:
            record.update(outcome="malformed_call", detail="not a call or a tools/call message")
        else:
            name, arguments, identifier = parts
            record.update(name=name, id=identifier)
            try:
                outcome = registry.process_call(name, arguments)
            except ToolNotFoundError:
                record.update(outcome="unknown_tool", detail=f"no tool {name!r} in the list")
            except ToolSchemaUnusableError as exc:
                record.update(outcome="unusable_schema", detail=str(exc.cause))
            else:
                record.update(
                    outcome=outcome.action.value,
                    auto_action=(outcome.auto_action or outcome.action).value,
                    reason=outcome.reason,
                    record=outcome.to_dict() if args.json else None,
                    explanation=None if args.json else outcome.explain(),
                )
        counts[record["outcome"]] += 1
        auto_counts[record.get("auto_action", record["outcome"])] += 1
        records.append(record)

    if args.json:
        print(
            json.dumps(
                {
                    "mode": mode.value,
                    "tools": listing.to_dict()["summary"],
                    "summary": dict(sorted(counts.items())),
                    "auto_summary": dict(sorted(auto_counts.items())),
                    "calls": [{k: v for k, v in r.items() if k != "explanation"} for r in records],
                },
                indent=2,
                default=str,
            )
        )
        return _EXIT_OK

    print(f"Replayed {len(records)} call(s) against {len(registry)} tool(s), {mode.value} mode.")
    unusable = [str(c.key) for c in listing.unusable]
    if unusable:
        print(f"Tools whose schema StateGuard cannot read: {', '.join(unusable)}")
    print()
    for record in records:
        label = f"#{record['index']} {record.get('name', '?')}"
        if record.get("explanation"):
            print(label)
            print("  " + record["explanation"].replace("\n", "\n  "))
        else:
            print(f"{label}\n  {record['outcome'].upper()}: {record.get('detail', '')}")
        print()
    summary = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    print(f"Summary: {summary}")
    if mode is RepairMode.SHADOW:
        # The shadow review question: what would turning auto mode on change?
        would = ", ".join(f"{count} {name}" for name, count in sorted(auto_counts.items()))
        print(f"Auto mode would: {would}")
    return _EXIT_OK


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    # Defaults are read from RepairConfig rather than duplicated as literals,
    # so the CLI and the library can never drift apart (they previously
    # disagreed: RepairConfig.max_attempts was 3, the CLI's default was 5).
    # Core-only import -- no pydantic, negligible startup cost.
    from stateguard.core.models.config import RepairConfig  # noqa: PLC0415

    _defaults = RepairConfig()

    parser = argparse.ArgumentParser(
        prog="stateguard",
        description="StateGuard — runtime contract repair for AI tool outputs.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # -- check ---------------------------------------------------------------
    check = sub.add_parser(
        "check",
        help="Validate and repair a payload against a contract.",
        description=(
            "Validate a JSON payload against a contract (Pydantic model, "
            "StateGuard schema file, or MCP tool definition). Attempts to "
            "repair any violations found and reports the result."
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
    schema_group.add_argument(
        "--tool",
        metavar="FILE",
        help=(
            "Path to an MCP tool definition ({'name', 'inputSchema', ...}) or a "
            "bare JSON Schema. The payload is the call's arguments; the output "
            "is what a repairing proxy would do: FORWARD, HOLD, ESCALATE or "
            "REFUSE, and why, field by field. No extra dependencies needed."
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
        "--shadow",
        action="store_true",
        default=False,
        help=(
            "Shadow mode: detect and plan the repair, validate that the plan "
            "works, and report it -- but do not present it as applied. The "
            "payload appears under 'proposed_output' instead of "
            "'repaired_output'. Exit codes are unchanged, so a shadow run "
            "still reports whether a repair was found."
        ),
    )
    check.add_argument(
        "--max-attempts",
        type=int,
        default=_defaults.max_attempts,
        metavar="N",
        dest="max_attempts",
        help=(
            f"Safety bound on repair iterations (default: "
            f"{_defaults.max_attempts}). The loop stops on its own once it "
            f"stops making progress."
        ),
    )
    check.add_argument(
        "--confidence-threshold",
        type=float,
        default=_defaults.min_confidence_threshold,
        metavar="FLOAT",
        dest="confidence_threshold",
        help=(
            f"Minimum confidence score [0.0, 1.0] required before an operation "
            f"is applied (default: {_defaults.min_confidence_threshold})."
        ),
    )

    # -- diff ----------------------------------------------------------------
    diff = sub.add_parser(
        "diff",
        help="What changes for callers when a tool definition changes.",
        description=(
            "Compare two versions of an MCP tool definition, a bare JSON Schema, or a "
            "whole tools/list, and report every structural change and whether it can "
            "break a call that worked before. Exit 0: nothing breaking; 1: something is."
        ),
    )
    diff.add_argument("before", metavar="BEFORE", help="The old tool / schema / tool list.")
    diff.add_argument("after", metavar="AFTER", help="The new tool / schema / tool list.")
    diff.add_argument(
        "--json", action="store_true", default=False, help="Emit the machine-readable report."
    )

    # -- replay --------------------------------------------------------------
    replay = sub.add_parser(
        "replay",
        help="Replay recorded tool calls against a tool list.",
        description=(
            "Run every call in --calls (a JSON array or JSON lines; each a "
            "{'name', 'arguments'} object or a JSON-RPC tools/call message) through "
            "StateGuard against the tools in --tools, and report what it would do."
        ),
    )
    replay.add_argument("--tools", metavar="FILE", required=True, help="A tools/list result.")
    replay.add_argument("--calls", metavar="FILE", required=True, help="Calls to replay.")
    replay.add_argument(
        "--shadow",
        action="store_true",
        default=False,
        help="Shadow mode: record what auto mode would do, forward everything.",
    )
    replay.add_argument(
        "--json", action="store_true", default=False, help="Emit one record per call."
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
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8")

    parser = _build_parser()
    args = parser.parse_args(argv)

    commands = {"check": _cmd_check, "diff": _cmd_diff, "replay": _cmd_replay}
    if args.command in commands:
        try:
            code = commands[args.command](args)
            sys.stdout.flush()
        except BrokenPipeError:
            # The reader went away (``stateguard replay ... | head``). Not
            # worth a traceback. Point stdout at devnull so the interpreter's
            # own flush at exit does not raise again, and exit as a process
            # killed by SIGPIPE would (128 + 13) -- never 0, because the
            # command's real verdict (``diff``'s "breaking") was not reached.
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            sys.exit(_EXIT_BROKEN_PIPE)
        sys.exit(code)
    else:
        # Unreachable: sub.required = True forces argparse to error out
        # before main() is reached if no subcommand is given. Kept as a
        # defensive guard for future subcommands, not a tested code path.
        parser.print_help()  # pragma: no cover
        sys.exit(_EXIT_FAILED)  # pragma: no cover
