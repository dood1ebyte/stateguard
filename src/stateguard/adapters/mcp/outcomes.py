"""
``RepairResult`` -> a decision a tool-calling proxy can act on.

A proxy sitting in front of ``tools/call`` has exactly one question after
StateGuard runs: *do I forward this call, and with what arguments?*
``RepairResult`` answers that, but it answers it across five statuses, two
payload fields and an ambiguity list, and every call site that re-derives the
answer from those parts is a place to get it subtly wrong -- forwarding a
shadow-mode preview, or collapsing an ambiguous result into a plain failure.

So the mapping lives here, once.

Why ``ESCALATE`` is not ``REFUSE``
----------------------------------
``AMBIGUOUS`` means a repair *was* found and the evidence did not justify
applying it unsupervised -- as opposed to ``FAILED``, where nothing was
found at all. Folding the two together would throw away the distinction the
hardening phase existed to create, and it is the actionable one: an
ambiguous result carries candidates, so an agent can re-prompt with them, a
reviewer can pick, or a UI can ask. A failure carries nothing to act on.

Why ``HOLD`` is not ``FORWARD``
-------------------------------
Under ``RepairMode.SHADOW`` the engine deliberately withholds the payload:
``repaired_output`` is ``None`` and the value is on ``proposed_output``
instead, so that a caller piping the usual field into production gets
nothing rather than silently getting mutated data. A proxy in shadow should
forward the arguments **as the model sent them** and log the difference --
that is the entire point of shadow -- so this maps to its own action and
carries the preview separately from what to send.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from stateguard.core.errors.results import RepairResult, RepairStatus

__all__ = ["MCPAction", "ToolCallOutcome", "outcome_for"]


class MCPAction(StrEnum):
    """
    What the proxy should do with the tool call.

    Members
    -------
    FORWARD:
        Send the call. ``ToolCallOutcome.arguments`` holds what to send --
        either the original payload (it was already valid) or the repaired
        one.
    HOLD:
        Shadow mode. Send the call with the arguments **as received**, and
        log ``ToolCallOutcome.preview`` as what auto mode would have sent.
        Nothing is withheld from the server; what is withheld is the repair.
    ESCALATE:
        A repair was found but not trusted enough to apply unsupervised.
        ``ToolCallOutcome.result.ambiguous`` carries the candidates. Do not
        forward; re-prompt, ask a reviewer, or surface the choice.
    REFUSE:
        No repair was found. Do not forward -- the server would reject the
        call anyway, and forwarding it spends a round trip to learn that.
    """

    FORWARD = "forward"
    HOLD = "hold"
    ESCALATE = "escalate"
    REFUSE = "refuse"


@dataclass(frozen=True)
class ToolCallOutcome:
    """
    The decision, the payload it applies to, and why.

    Attributes
    ----------
    action:
        What to do. Branch on this, not on ``result.status``.
    arguments:
        What to send, when *action* is ``FORWARD``. ``None`` for every other
        action -- deliberately, so that a caller who forwards without
        checking sends nothing rather than something unintended.
    preview:
        Under ``HOLD``, what auto mode *would* have sent. ``None``
        otherwise. This is the shadow-mode diff a team watches before
        turning auto on.
    reason:
        One line, safe to log or hand back as an error message. Says what
        happened, not what to do about it.
    result:
        The full ``RepairResult``: every attempt, operation, trust score and
        ambiguity. The audit trail, unabridged.
    """

    action: MCPAction
    arguments: dict[str, Any] | None
    preview: dict[str, Any] | None
    reason: str
    result: RepairResult

    @property
    def should_forward(self) -> bool:
        """Whether the call proceeds at all (``FORWARD`` or ``HOLD``)."""
        return self.action in (MCPAction.FORWARD, MCPAction.HOLD)


def outcome_for(result: RepairResult, original_arguments: dict[str, Any]) -> ToolCallOutcome:
    """
    Map a ``RepairResult`` onto what the proxy should do.

    *original_arguments* is what the model sent, needed because ``HOLD``
    forwards exactly that -- shadow mode observes without changing what the
    server receives.
    """
    status = result.status

    if status is RepairStatus.ALREADY_VALID:
        # ``repaired_output`` is the *wrapped* payload, which for this
        # adapter may carry defaults the model omitted. Prefer it over the
        # raw arguments so the server sees the same thing whether or not a
        # repair happened -- but say so, because "already valid" and "what
        # I am sending you differs from what you sent me" are both true
        # here and a log line claiming otherwise would be wrong.
        arguments = result.repaired_output
        if arguments is None:
            arguments = dict(original_arguments)
        filled = sorted(set(arguments) - set(original_arguments))
        reason = "Arguments matched the tool's schema; forwarded unchanged."
        if filled:
            reason = (
                f"Arguments matched the tool's schema. Forwarded with "
                f"{_join(filled)} filled from the schema's declared default(s)."
            )
        return ToolCallOutcome(
            action=MCPAction.FORWARD,
            arguments=arguments,
            preview=None,
            reason=reason,
            result=result,
        )

    if status is RepairStatus.AMBIGUOUS:
        return ToolCallOutcome(
            action=MCPAction.ESCALATE,
            arguments=None,
            preview=None,
            reason=(
                f"A repair was found but not trusted enough to apply "
                f"unsupervised ({len(result.ambiguous)} ambiguous field(s): "
                f"{_ambiguous_fields(result)}). Not forwarded."
            ),
            result=result,
        )

    if status is RepairStatus.SUCCESS:
        # Shadow withholds the payload: ``repaired_output`` is None and the
        # value is on ``proposed_output``. Forward what the model sent.
        if result.proposed_output is not None:
            return ToolCallOutcome(
                action=MCPAction.HOLD,
                arguments=None,
                preview=result.proposed_output,
                reason=(
                    f"Shadow mode: {_operation_count(result)} repair(s) determined "
                    f"and withheld. Forward the original arguments and review the "
                    f"preview."
                ),
                result=result,
            )
        return ToolCallOutcome(
            action=MCPAction.FORWARD,
            arguments=result.repaired_output,
            preview=None,
            reason=f"Repaired {_operation_count(result)} field(s) before forwarding.",
            result=result,
        )

    # PARTIAL and FAILED both mean the payload still violates the schema.
    # PARTIAL can carry a payload when ``allow_partial_repair`` is on, but
    # it is one the server will still reject, so it is not forwarded.
    remaining = len(result.remaining_violations)
    return ToolCallOutcome(
        action=MCPAction.REFUSE,
        arguments=None,
        preview=None,
        reason=(
            f"Could not bring the arguments up to the tool's schema "
            f"({remaining} violation(s) remain: {_remaining_fields(result)}). "
            f"Not forwarded."
        ),
        result=result,
    )


def _operation_count(result: RepairResult) -> int:
    return sum(len(attempt.applied_operations) for attempt in result.attempts)


def _ambiguous_fields(result: RepairResult) -> str:
    return _join(sorted({a.target_path for a in result.ambiguous}))


def _remaining_fields(result: RepairResult) -> str:
    return _join(sorted({v.field_path for v in result.remaining_violations}))


def _join(names: list[str]) -> str:
    """Names as a short readable list -- these go into log lines."""
    if not names:
        return "none named"
    if len(names) <= 3:
        return ", ".join(repr(n) for n in names)
    shown = ", ".join(repr(n) for n in names[:3])
    return f"{shown} and {len(names) - 3} more"
