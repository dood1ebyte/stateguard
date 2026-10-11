"""
Result integrity: does a ``tools/call`` result keep the tool's own contract?

Observation only. StateGuard never changes a result: a result "repaired"
into shape is a fabricated answer, and an error or empty result rewritten
into a success is a phantom one. This module *checks* and reports; what to
do with a finding is the host's decision.

What is checked
---------------
* **Shape** -- a ``CallToolResult`` is an object whose ``content`` is a list
  (required by the schema of every protocol revision). Anything else is
  ``MALFORMED_RESULT``.
* **Structured content** -- when the tool declares an ``outputSchema`` and
  the result is not an error, ``structuredContent`` must conform to it
  (2026-07-28: "conforms to the tool's outputSchema if one is defined").
  A non-conforming one is ``STRUCTURED_CONTENT_INVALID``, reported with the
  failing paths and violation types -- never the values.

Reported as advisories, never as integrity failures:

* ``STRUCTURED_CONTENT_ABSENT`` -- an ``outputSchema`` is declared and no
  ``structuredContent`` came back. 2025-06-18 required it; the 2026-07-28
  text calls the field optional. Flagging it as a failure would be a claim
  the current specification does not make.
* ``OUTPUT_SCHEMA_UNREADABLE`` -- the ``outputSchema`` uses something the
  JSON Schema adapter refuses, or its root is not an object (2026-07-28
  allows any root). Not checked, and said so.

Error results (``isError: true``) are not checked against ``outputSchema``:
the schema describes success.

Zero external dependencies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from stateguard.adapters.jsonschema.errors import JSONSchemaError
from stateguard.guard import ContractGuard

__all__ = ["ResultCheck", "ResultIssue", "ResultIssueKind", "check_tool_result"]


class ResultIssueKind(StrEnum):
    MALFORMED_RESULT = "malformed_result"
    STRUCTURED_CONTENT_INVALID = "structured_content_invalid"
    STRUCTURED_CONTENT_ABSENT = "structured_content_absent"
    OUTPUT_SCHEMA_UNREADABLE = "output_schema_unreadable"

    @property
    def advisory(self) -> bool:
        return self in (
            ResultIssueKind.STRUCTURED_CONTENT_ABSENT,
            ResultIssueKind.OUTPUT_SCHEMA_UNREADABLE,
        )


@dataclass(frozen=True)
class ResultIssue:
    kind: ResultIssueKind
    detail: str
    #: Paths inside ``structuredContent`` that failed; no values.
    paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "detail": self.detail, "paths": list(self.paths)}


@dataclass(frozen=True)
class ResultCheck:
    issues: tuple[ResultIssue, ...] = field(default_factory=tuple)
    #: True when ``structuredContent`` was validated against an ``outputSchema``.
    structured_checked: bool = False

    @property
    def ok(self) -> bool:
        """No integrity failure. Advisories do not count."""
        return not any(not issue.kind.advisory for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "structured_checked": self.structured_checked,
            "issues": [issue.to_dict() for issue in self.issues],
        }


_GUARD: ContractGuard | None = None


def _guard() -> ContractGuard:
    global _GUARD
    if _GUARD is None:
        _GUARD = ContractGuard.with_json_schema()
    return _GUARD


def check_tool_result(
    tool: Mapping[str, Any] | None, result: Any, *, guard: ContractGuard | None = None
) -> ResultCheck:
    """
    Check *result* (the ``result`` member of a ``tools/call`` response) against
    *tool* (its definition, or ``None`` if unknown). Never modifies either.
    """
    if not isinstance(result, Mapping) or not isinstance(result.get("content"), list):
        return ResultCheck(
            (
                ResultIssue(
                    ResultIssueKind.MALFORMED_RESULT,
                    "a CallToolResult must be an object with a 'content' list",
                ),
            )
        )
    if result.get("isError") is True or tool is None:
        return ResultCheck()
    output_schema = tool.get("outputSchema")
    if output_schema is None:
        return ResultCheck()
    if "structuredContent" not in result:
        return ResultCheck(
            (
                ResultIssue(
                    ResultIssueKind.STRUCTURED_CONTENT_ABSENT,
                    "the tool declares an outputSchema; no structuredContent was returned",
                ),
            )
        )
    if not isinstance(output_schema, Mapping) or output_schema.get("type") != "object":
        return ResultCheck(
            (
                ResultIssue(
                    ResultIssueKind.OUTPUT_SCHEMA_UNREADABLE,
                    "outputSchema root is not an object schema; structuredContent not checked",
                ),
            )
        )
    try:
        validation = (guard or _guard()).validate(dict(output_schema), result["structuredContent"])
    except JSONSchemaError as exc:
        return ResultCheck(
            (
                ResultIssue(
                    ResultIssueKind.OUTPUT_SCHEMA_UNREADABLE,
                    f"outputSchema not checked: {type(exc).__name__}",
                ),
            )
        )
    if validation.is_valid:
        return ResultCheck(structured_checked=True)
    paths = tuple(sorted({v.field_path or "<root>" for v in validation.violations}))
    kinds = sorted({v.violation_type.value for v in validation.violations})
    return ResultCheck(
        (
            ResultIssue(
                ResultIssueKind.STRUCTURED_CONTENT_INVALID,
                f"structuredContent does not conform to outputSchema ({', '.join(kinds)})",
                paths,
            ),
        ),
        structured_checked=True,
    )
