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

Shadow never blocks
-------------------
Shadow mode exists to measure what StateGuard *would* do before it is
trusted to do it, so it must not change what happens to a single call. It
used to: a call auto mode would refuse or escalate was mapped to
``REFUSE`` / ``ESCALATE`` in shadow too, so a shadow deployment blocked real
traffic -- including any call StateGuard refused wrongly, which is precisely
the error shadow exists to surface. Every outcome now carries
``auto_action``, what auto mode would do, computed once and independently of
the mode; in shadow, ``action`` is only ever ``FORWARD`` (auto would send
exactly what the model sent) or ``HOLD`` (it would not -- it would repair,
escalate or refuse), and either way the model's arguments are forwarded.

``arguments`` is always what to send
------------------------------------
Whenever ``should_forward`` is true, ``ToolCallOutcome.arguments`` holds the
payload to put on the wire: the repaired one under ``FORWARD``, the one the
model sent under ``HOLD``. It used to be ``None`` under ``HOLD`` -- to keep a
caller from forwarding the preview by accident -- which left the obvious
``if outcome.should_forward: send(outcome.arguments)`` sending *nothing* in
shadow, the one mode whose promise is that the server sees exactly what the
model wrote. The preview stays on ``preview``; it is never on ``arguments``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stateguard.adapters.mcp.errors import ForwardingInvariantError
from stateguard.core.errors.results import RepairResult, RepairStatus
from stateguard.core.explain import root_normalisation

if TYPE_CHECKING:
    from stateguard.guard import ContractGuard

__all__ = ["MCPAction", "ToolCallOutcome", "outcome_for", "repair_tool_call"]


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
        Shadow mode only. Send the call with the arguments **as received**
        -- ``ToolCallOutcome.arguments`` holds them -- and record what auto
        mode would have done instead: ``auto_action`` says what, and
        ``preview`` holds the payload when that was a repair. Nothing is
        withheld from the server; what is withheld is StateGuard's decision.
    ESCALATE:
        Auto mode only. A repair was found but not trusted enough to apply
        unsupervised. ``ToolCallOutcome.result.ambiguous`` carries the
        candidates. Do not forward; re-prompt, ask a reviewer, or surface the
        choice.
    REFUSE:
        Auto mode only. No repair was found. Do not forward -- the server
        would reject the call anyway, and forwarding it spends a round trip
        to learn that.

    In shadow mode ``action`` is always ``FORWARD`` or ``HOLD``: shadow
    never blocks a call. ``ToolCallOutcome.auto_action`` is always one of
    ``FORWARD``, ``ESCALATE`` or ``REFUSE`` -- what auto mode does.
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
        What to send. Under ``FORWARD``, the original payload (it was already
        valid) or the repaired one; under ``HOLD``, the payload exactly as
        the model sent it -- never the preview. ``None`` under ``ESCALATE``
        and ``REFUSE``, so a caller who forwards without checking sends
        nothing rather than something unintended. Non-``None`` exactly when
        ``should_forward`` is true.
    preview:
        Under ``HOLD``, the payload auto mode *would* have sent, when auto
        mode would have repaired the call; ``None`` when it would have
        escalated or refused it, and ``None`` for every other action. This is
        the shadow-mode diff a team watches before turning auto on.
    reason:
        One line, safe to log or hand back as an error message. Says what
        happened, not what to do about it.
    result:
        The full ``RepairResult``: every attempt, operation, trust score and
        ambiguity. The audit trail, unabridged.
    auto_action:
        What auto mode does with this call: ``FORWARD``, ``ESCALATE`` or
        ``REFUSE``. Equal to ``action`` in auto mode; in shadow mode it is
        the decision that was observed and not enforced. Computed from the
        ``RepairResult`` alone, so it is the same in both modes for the same
        call.
    """

    action: MCPAction
    arguments: dict[str, Any] | None
    preview: dict[str, Any] | None
    reason: str
    result: RepairResult
    auto_action: MCPAction | None = None

    def __post_init__(self) -> None:
        # Defaulted only so the dataclass stays constructible positionally;
        # ``outcome_for`` always sets it. Without a value it means "the same
        # as ``action``", which is the auto-mode truth.
        if self.auto_action is None:
            object.__setattr__(self, "auto_action", self.action)

    @property
    def should_forward(self) -> bool:
        """Whether the call proceeds at all (``FORWARD`` or ``HOLD``)."""
        return self.action in (MCPAction.FORWARD, MCPAction.HOLD)

    @property
    def original_arguments(self) -> Any:
        """
        The arguments as received (after MCP's ``None`` -> ``{}``), never mutated.

        Normally a ``dict``; a JSON-encoded argument string is kept as the
        string that arrived.
        """
        return self.result.original_input

    @property
    def proposed_arguments(self) -> dict[str, Any] | None:
        """
        The payload StateGuard decided on, in either mode; ``None`` if none.

        In auto mode, what is forwarded. In shadow mode, what auto mode would
        forward -- ``preview`` under ``HOLD``, the arguments themselves under
        ``FORWARD`` -- so comparing it with ``original_arguments`` is the same
        question in both modes. ``None`` whenever ``auto_action`` is
        ``ESCALATE`` or ``REFUSE``.
        """
        if self.auto_action is not MCPAction.FORWARD:
            return None
        if self.action is MCPAction.HOLD:
            return self.preview
        return self.arguments

    def explain(self) -> str:
        """
        The decision and why, field by field -- value-free, safe to log.

        The first line is the action and its reason; the rest is
        ``RepairResult.explain()``.
        """
        return f"{self.action.value.upper()}: {self.reason}\n{self.result.explain()}"

    def to_dict(self) -> dict[str, Any]:
        """
        The decision plus the full ``RepairResult.to_dict()`` record.

        Includes payloads -- ``arguments``, ``preview`` and the result's own
        -- so treat it like the data it describes.
        """
        auto_action = self.auto_action if self.auto_action is not None else self.action
        return {
            "action": self.action.value,
            "auto_action": auto_action.value,
            "mode": self.result.mode.value,
            "should_forward": self.should_forward,
            "reason": self.reason,
            "arguments": self.arguments,
            "preview": self.preview,
            "proposed_arguments": self.proposed_arguments,
            "result": self.result.to_dict(),
        }


def outcome_for(result: RepairResult, original_arguments: Any) -> ToolCallOutcome:
    """
    Map a ``RepairResult`` onto what the proxy should do.

    *original_arguments* is what the model sent, needed because ``HOLD``
    forwards exactly that -- shadow mode observes without changing what the
    server receives.

    The decision is made once, as auto mode would make it (``_verdict``), and
    shadow mode is a *view* of that decision rather than a second mapping:
    there is no path by which a shadow result can be enforced. Every outcome
    passes ``_check_forwarding_invariants`` before it is returned.
    """
    auto_action, payload, auto_reason = _verdict(result, original_arguments)

    if not result.is_shadow:
        outcome = ToolCallOutcome(
            action=auto_action,
            arguments=payload if auto_action is MCPAction.FORWARD else None,
            preview=None,
            reason=auto_reason,
            result=result,
            auto_action=auto_action,
        )
    elif auto_action is MCPAction.FORWARD and payload == original_arguments:
        # Nothing to observe: auto mode would send exactly what the model
        # sent. There is no diff to review, so there is nothing to hold.
        outcome = ToolCallOutcome(
            action=MCPAction.FORWARD,
            arguments=_as_received(original_arguments),
            preview=None,
            reason="Arguments matched the tool's schema; forwarded unchanged.",
            result=result,
            auto_action=auto_action,
        )
    else:
        outcome = ToolCallOutcome(
            action=MCPAction.HOLD,
            arguments=_as_received(original_arguments),
            preview=payload if auto_action is MCPAction.FORWARD else None,
            reason=_shadow_reason(result, auto_action, payload, original_arguments),
            result=result,
            auto_action=auto_action,
        )

    _check_forwarding_invariants(outcome, original_arguments)
    return outcome


def repair_tool_call(guard: ContractGuard, tool: Any, arguments: Any) -> ToolCallOutcome:
    """
    Repair one ``tools/call`` and decide what to do with it, in one step.

    Equivalent to ``outcome_for(guard.repair(tool, arguments), arguments)``,
    with MCP's rule for absent arguments applied first. Prefer it over the
    two-step form: that form takes the arguments twice, and if the two ever
    differ, ``HOLD`` forwards something other than what the model sent.

    Parameters
    ----------
    guard:
        A guard built with ``ContractGuard.with_mcp()`` (auto or shadow).
    tool:
        The tool definition from ``tools/list``, or its bare ``inputSchema``.
    arguments:
        The call's ``arguments`` as received. ``None`` is read as ``{}``:
        MCP makes ``arguments`` optional, the Python SDK's
        ``call_tool(name, arguments=None)`` sends ``None`` for a tool that
        takes no parameters, and treating it as a payload refused every such
        call. Anything else is passed through unchanged -- a JSON-encoded
        object string is normalised by the engine, other shapes are refused.

    Raises
    ------
    JSONSchemaError
        (a ``ValueError``) when the tool's schema uses a construct StateGuard
        does not support, or is not a usable schema at all. No call to that
        tool can be validated, so no outcome is returned: whether such a tool
        is blocked or passed through unvalidated is the caller's policy, not
        something to decide per call. Catch it where the tool list is loaded
        or where the call is made. See ``docs/jsonschema-adapter.md``.
    ForwardingInvariantError
        if the payload auto mode is about to forward does not validate
        against the tool's schema. Guaranteed not to happen by the engine;
        checked here because this is the last point before the payload
        leaves StateGuard, and a bug there must not reach a server.
    """
    received = {} if arguments is None else arguments
    outcome = outcome_for(guard.repair(tool, received), received)
    _check_forwarded_payload_validates(guard, tool, outcome)
    return outcome


def _check_forwarded_payload_validates(
    guard: ContractGuard, tool: Any, outcome: ToolCallOutcome
) -> None:
    """
    Re-validate a payload StateGuard produced before it is forwarded.

    Only auto mode's ``FORWARD`` qualifies: shadow forwards what it received
    and makes no claim about it. One extra validation pass per forwarded
    call, against an already-cached contract -- the price of "a forwarded
    payload is valid" being checked at the boundary rather than assumed
    from the stages before it (``JSONSchemaAdapter.wrap`` fills defaults
    *after* the engine's own revalidation, for instance).
    """
    if outcome.result.is_shadow or outcome.action is not MCPAction.FORWARD:
        return
    validation = guard.validate(tool, outcome.arguments)
    if not validation.is_valid:
        fields = sorted({v.field_path for v in validation.violations})
        raise ForwardingInvariantError(
            f"The payload about to be forwarded does not validate against the tool's "
            f"schema (fields: {fields}). Nothing was forwarded."
        )


def _verdict(
    result: RepairResult, original_arguments: Any
) -> tuple[MCPAction, dict[str, Any] | None, str]:
    """
    What auto mode does with this call: ``(action, payload, reason)``.

    Mode-independent by construction -- it reads the payload from whichever
    of ``repaired_output`` / ``proposed_output`` the run populated, and never
    asks which mode produced it. *payload* is ``None`` unless the action is
    ``FORWARD``.
    """
    status = result.status

    if status is RepairStatus.AMBIGUOUS:
        return (
            MCPAction.ESCALATE,
            None,
            f"A repair was found but not trusted enough to apply unsupervised "
            f"({len(result.ambiguous)} ambiguous field(s): {_ambiguous_fields(result)}). "
            f"Not forwarded.",
        )

    if status in (RepairStatus.ALREADY_VALID, RepairStatus.SUCCESS):
        payload = (
            result.proposed_output if result.proposed_output is not None else result.repaired_output
        )
        if payload is None:  # pragma: no cover -- the engine sets one for both statuses
            raise ForwardingInvariantError(
                f"{status.value} result carries no payload; refusing to guess one."
            )
        if status is RepairStatus.SUCCESS:
            phrase = _repairs_phrase(result, as_nouns=False)
            return MCPAction.FORWARD, payload, f"{phrase[0].upper()}{phrase[1:]} before forwarding."
        # ALREADY_VALID. The payload is the *wrapped* one, which for this
        # adapter may carry defaults the model omitted. Prefer it over the raw
        # arguments so the server sees the same thing whether or not a repair
        # happened -- but say so, because "already valid" and "what I am
        # sending you differs from what you sent me" can both be true.
        return (
            MCPAction.FORWARD,
            payload,
            _fill_reason(
                payload,
                original_arguments,
                unchanged="Arguments matched the tool's schema; forwarded unchanged.",
                filled="Arguments matched the tool's schema. Forwarded with {names} "
                "filled from the schema's declared default(s).",
                changed="Arguments matched the tool's schema, but the payload forwarded "
                "is not the one that was sent.",
            ),
        )

    # PARTIAL and FAILED both mean the payload still violates the schema.
    # PARTIAL can carry a payload when ``allow_partial_repair`` is on, but
    # it is one the server will still reject, so it is not forwarded.
    return (
        MCPAction.REFUSE,
        None,
        f"Could not bring the arguments up to the tool's schema "
        f"({len(result.remaining_violations)} violation(s) remain: "
        f"{_remaining_fields(result)}). Not forwarded.",
    )


def _shadow_reason(
    result: RepairResult,
    auto_action: MCPAction,
    payload: dict[str, Any] | None,
    original_arguments: Any,
) -> str:
    """Why a shadow call was held: what auto mode would have done instead."""
    if auto_action is MCPAction.ESCALATE:
        return (
            f"Shadow mode: auto mode would escalate this call -- a repair was found "
            f"but not trusted enough to apply ({_ambiguous_fields(result)}). "
            f"Forwarded as received."
        )
    if auto_action is MCPAction.REFUSE:
        return (
            f"Shadow mode: auto mode would refuse this call "
            f"({len(result.remaining_violations)} violation(s) remain: "
            f"{_remaining_fields(result)}). Forwarded as received."
        )
    assert payload is not None  # FORWARD always carries one (see _verdict)
    if _operation_count(result) or root_normalisation(result) is not None:
        return (
            f"Shadow mode: {_repairs_phrase(result, as_nouns=True)} determined and "
            f"withheld. Forward the original arguments and review the preview."
        )
    # ALREADY_VALID with declared defaults filled: no repair was needed,
    # but the payload auto mode would send is still not this one.
    return _fill_reason(
        payload,
        original_arguments,
        # Unreachable -- the caller already established that the two differ --
        # but the wording has to be right if it ever is.
        unchanged="Shadow mode: arguments matched the tool's schema and nothing was withheld.",
        filled="Shadow mode: arguments matched the tool's schema, but auto "
        "mode would also have filled {names} from the schema's declared "
        "default(s). Forward the original arguments and review the preview.",
        changed="Shadow mode: arguments matched the tool's schema, but the "
        "payload auto mode would send is not the one that was sent. "
        "Forward the original arguments and review the preview.",
    )


def _check_forwarding_invariants(outcome: ToolCallOutcome, original_arguments: Any) -> None:
    """
    The last gate before an outcome leaves this module.

    Every rule here is guaranteed by construction above; this exists so that
    a future change which breaks one fails loudly, here, instead of
    forwarding the wrong payload. A violation is a StateGuard bug, never a
    property of the input, so it raises rather than mapping to an action.
    """
    result = outcome.result

    if (outcome.arguments is not None) is not outcome.should_forward:
        raise ForwardingInvariantError(
            f"{outcome.action.value}: 'arguments' must be set exactly when the call is forwarded."
        )
    if result.is_shadow:
        if outcome.action not in (MCPAction.FORWARD, MCPAction.HOLD):
            raise ForwardingInvariantError(
                f"Shadow mode produced {outcome.action.value}; shadow never blocks a call."
            )
        if outcome.arguments != original_arguments:
            raise ForwardingInvariantError(
                "Shadow mode would forward a payload other than the one received."
            )
        return

    if outcome.action is MCPAction.HOLD:
        raise ForwardingInvariantError("HOLD outside shadow mode.")
    if outcome.action is not MCPAction.FORWARD:
        return
    if result.status not in (RepairStatus.SUCCESS, RepairStatus.ALREADY_VALID):
        raise ForwardingInvariantError(
            f"Auto mode would forward a {result.status.value} result; only a payload "
            f"that passed revalidation may be forwarded."
        )
    if outcome.arguments is not result.repaired_output:
        raise ForwardingInvariantError(
            "Auto mode would forward a payload other than the one the engine committed."
        )
    # A source key the engine declined to place must not have been placed
    # anyway by an operation that reached the payload.
    held_sources = {
        candidate.source_path
        for item in result.ambiguous
        for candidate in item.candidates
        if candidate.source_path is not None
    }
    consumed = {
        op.source_path
        for attempt in result.attempts
        if attempt.kept
        for op in attempt.applied_operations
        if op.source_path is not None
    }
    if held_sources & consumed:
        raise ForwardingInvariantError(
            f"Key(s) {sorted(held_sources & consumed)} were held as ambiguous but "
            f"moved by an applied repair."
        )


def _fill_reason(
    payload: dict[str, Any],
    original: dict[str, Any],
    *,
    unchanged: str,
    filled: str,
    changed: str,
) -> str:
    """
    Describe how *payload* differs from what the model sent.

    Three outcomes, because there are three truths and only one of them used
    to be told. *filled* is formatted with ``{names}``.

    The *changed* case is the one this exists for. The reason string used to
    be chosen by whether any **top-level** key had been added, while
    ``JSONSchemaAdapter.wrap`` fills defaults at every depth -- so a schema
    declaring a default on a nested property produced a payload that differed
    from the model's, under a line reading "forwarded unchanged", and the
    shadow wording rendered as "would also have filled none named". Comparing
    the payloads decides *whether* anything changed; walking them decides
    what to call it.
    """
    if payload == original:
        return unchanged
    names = _added_paths(payload, original)
    if names:
        return filled.format(names=_join(names))
    return changed


def _added_paths(
    payload: dict[str, Any],
    original: dict[str, Any],
    prefix: str = "",
) -> list[str]:
    """
    Dotted paths present in *payload* but not in *original*, at any depth.

    Recursing matters because ``wrap`` does: a default declared on a nested
    property is added without any top-level key appearing, which a set
    difference over the top level cannot see.
    """
    added: list[str] = []
    for key, value in payload.items():
        path = f"{prefix}{key}"
        if key not in original:
            added.append(path)
        elif isinstance(value, Mapping) and isinstance(original[key], Mapping):
            added.extend(_added_paths(dict(value), dict(original[key]), f"{path}."))
    return sorted(added)


def _operation_count(result: RepairResult) -> int:
    """Operations that reached the payload -- a discarded attempt's did not."""
    return sum(len(attempt.applied_operations) for attempt in result.attempts if attempt.kept)


def _repairs_phrase(result: RepairResult, *, as_nouns: bool) -> str:
    """
    What was done to the payload, as a lower-case clause.

    As a verb phrase: ``"repaired 2 field(s)"``, ``"normalised the payload
    root (parsed a JSON object from the string payload)"``, or both joined.
    As nouns -- for shadow mode, where nothing was done: ``"2 repair(s)"``.
    """
    count = _operation_count(result)
    root = root_normalisation(result)
    if as_nouns:
        fields = f"{count} repair(s)"
        root_part = f"a payload-root normalisation ({root})"
    else:
        fields = f"repaired {count} field(s)"
        root_part = f"normalised the payload root ({root})"
    if root is None:
        return fields
    return root_part if not count else f"{root_part} and {fields}"


def _as_received(arguments: Any) -> Any:
    """A copy of the model's payload, for ``HOLD`` to send unchanged."""
    return dict(arguments) if isinstance(arguments, Mapping) else arguments


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
