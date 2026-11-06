"""
Tool effects and the retry decision.

Whether a failed or unanswered ``tools/call`` may be sent again depends on one
fact StateGuard cannot observe: **what the tool does when it runs twice.** A
search can run twice; a payment cannot. Nothing on the wire says which is
which reliably.

``Effect`` -- declared by the operator, never inferred
-----------------------------------------------------
=====================  ======================================================
``UNKNOWN``            The default for every tool. Never retried after it
                       may have executed.
``READ_ONLY``          No side effects. Safe to repeat.
``IDEMPOTENT``         Running it twice has the effect of running it once.
                       Safe to repeat.
``NON_IDEMPOTENT``     Must not run twice. Never retried after it may have
                       executed.
=====================  ======================================================

An ``EffectPolicy`` holds the operator's declarations, keyed exactly like the
registry: ``(tool name, server)``, with no guessing across servers. MCP tool
annotations (``readOnlyHint``, ``idempotentHint``, ``destructiveHint``) are
**hints from the server, not facts**: the specification itself calls them
untrusted unless the server is. They are recorded on the declaration for
display and audit, and by default they never change it.

The one opt-in exception: ``of(..., trust_read_only_hints=True)`` -- the
operator saying "I trust this server's annotations" -- reads
``readOnlyHint: true`` on a tool the operator did not declare as READ_ONLY,
with ``source="server_hint"`` on the declaration so the audit shows where it
came from. No other hint is ever trusted (``idempotentHint`` and
``destructiveHint`` included), and an operator declaration always wins.

``decide_retry`` -- a rule, not a score
---------------------------------------
Given what happened to an attempt (``Observed``), the declared effect and the
attempts already made, it returns a ``RetryVerdict`` in ``TrustPolicy``'s
vocabulary:

* ``APPLY`` -- retry;
* ``AMBIGUOUS`` -- the outcome is unknown and repeating is not known to be
  safe: escalate, and never call it a failure or a success;
* ``REJECT`` -- do not retry; deliver what the server said.

The rules, in order:

1. Success, or a call StateGuard itself answered: nothing to retry.
2. An authentication or authorization failure: never retried -- it is the
   operator's (or the user's) to resolve, and repeating it cannot help.
3. A JSON-RPC protocol error: never retried -- the server rejected the
   request itself; sending it again gets the same answer. A JSON-RPC
   *internal* error (-32603, ``SERVER_ERROR``) is not a rejection: the server
   failed, perhaps transiently. It goes on to rule 5, like a tool error.
4. An unknown outcome (no answer by the deadline, or the connection lost
   with the request in flight): **never retried** -- escalated. It may have
   executed; replaying an uncertain side effect is not StateGuard's call,
   even for a tool declared IDEMPOTENT (idempotent is not the same as safe
   to run twice *concurrently*, and a deadline-expired call may still be
   running).

   Two exceptions, both opt-in:

   * ``reread_unknown=True``: a tool the operator declared **READ_ONLY** is
     requested once more. A read has no side effect to repeat, so asking
     again is a new read, not a replay of an effect -- and an original still
     running beside it changes nothing either. The declaration is the
     operator's; annotations never count.
   * ``keyed=True``: the call carries an idempotency key its server says it
     honours. The resend is the same request with the same key; the server
     answers it from its own durable record (the recorded result, or "in
     doubt") and never executes it twice. See ``idempotency``.
5. From here the attempt failed *knowingly* (a tool error, or a server's
   internal error, after which the call may or may not have taken effect:
   so, as for a tool error, only what is safe to repeat is repeated). The attempt
   budget is spent: deliver the error. A keyed call (see below) is resent
   once: the server answers a recorded error from its record, and executes
   only a key it never claimed.
6. The effect is not declared READ_ONLY or IDEMPOTENT by the operator:
   deliver the error.
7. Declared safe to repeat: retry.

``TrustPolicy`` itself scores *evidence* for field repairs. Retry safety is
not a matter of evidence strength, so it is not scored; it reuses the
decision vocabulary and the Shadow/Auto adoption path, and nothing else.

Zero dependencies.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from stateguard.core.trust import TrustDecision

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "Effect",
    "EffectDeclaration",
    "EffectPolicy",
    "Observed",
    "RetryVerdict",
    "decide_retry",
]

#: Attempts per call, the first included. One retry.
DEFAULT_MAX_ATTEMPTS = 2

#: Annotation keys recorded as hints.
_HINT_KEYS = ("readOnlyHint", "idempotentHint", "destructiveHint", "openWorldHint")


class Effect(StrEnum):
    UNKNOWN = "unknown"
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"

    @property
    def repeatable(self) -> bool:
        """Running it twice is harmless."""
        return self in (Effect.READ_ONLY, Effect.IDEMPOTENT)


@dataclass(frozen=True)
class EffectDeclaration:
    """A tool's effect, and where that came from."""

    effect: Effect
    #: True only when the operator declared it (or, opted in, trusted the
    #: server's readOnlyHint for it). A default is never a declaration.
    declared: bool
    #: The server's annotations, as sent. Untrusted; recorded, never used
    #: unless the operator opted in to trusting readOnlyHint.
    hints: Mapping[str, Any] = field(default_factory=dict)
    #: Where a declaration came from: "operator", "server_hint", or "" (none).
    source: str = ""

    @property
    def repeatable(self) -> bool:
        return self.declared and self.effect.repeatable

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "effect": self.effect.value,
            "declared": self.declared,
            "hints": dict(self.hints),
        }
        # Only a trusted hint says where it came from: every other declaration
        # is the operator's (or none), and its record is unchanged.
        if self.source == "server_hint":
            out["source"] = self.source
        return out


class EffectPolicy:
    """
    The operator's effect declarations, keyed by ``(tool name, server)``.

    ``EffectPolicy()`` declares nothing: every tool is UNKNOWN, and nothing
    that may have executed is ever retried.
    """

    def __init__(
        self, declarations: Mapping[tuple[str, str | None] | str, Effect | str] | None = None
    ) -> None:
        self._declared: dict[tuple[str, str | None], Effect] = {}
        for key, effect in (declarations or {}).items():
            name, server = (key, None) if isinstance(key, str) else key
            self._declared[(name, server)] = Effect(effect)

    @classmethod
    def from_entries(cls, entries: Iterable[Mapping[str, Any]]) -> EffectPolicy:
        """From ``[{"name": ..., "server": ... (optional), "effect": ...}, ...]``."""
        declarations: dict[tuple[str, str | None] | str, Effect | str] = {}
        for entry in entries:
            name, effect = entry.get("name"), entry.get("effect")
            if not isinstance(name, str) or not name:
                raise ValueError(f"effect entry needs a non-empty 'name': {entry!r}")
            server = entry.get("server")
            if server is not None and not isinstance(server, str):
                raise ValueError(f"effect entry 'server' must be a string: {entry!r}")
            if not isinstance(effect, str):
                raise ValueError(f"effect entry needs a string 'effect': {entry!r}")
            declarations[(name, server)] = Effect(effect)
        return cls(declarations)

    @classmethod
    def from_file(cls, path: str | Path) -> EffectPolicy:
        """A JSON file: ``{"tools": [{"name", "server"?, "effect"}, ...]}``."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ValueError(f"{path}: expected an object with a 'tools' list")
        return cls.from_entries(entries)

    def of(
        self,
        name: str,
        *,
        server: str | None = None,
        annotations: Mapping[str, Any] | None = None,
        trust_read_only_hints: bool = False,
    ) -> EffectDeclaration:
        """
        The tool's declaration. *annotations* are recorded as hints only --
        unless *trust_read_only_hints*, and then only ``readOnlyHint: true``
        on a tool the operator declared nothing for.
        """
        hints = {k: annotations[k] for k in _HINT_KEYS if annotations and k in annotations}
        effect = self._declared.get((name, server))
        if effect is not None:
            return EffectDeclaration(effect, declared=True, hints=hints, source="operator")
        if trust_read_only_hints and hints.get("readOnlyHint") is True:
            return EffectDeclaration(
                Effect.READ_ONLY, declared=True, hints=hints, source="server_hint"
            )
        return EffectDeclaration(Effect.UNKNOWN, declared=False, hints=hints)

    def __len__(self) -> int:
        return len(self._declared)


class Observed(StrEnum):
    """What happened to one attempt, as the proxy saw it."""

    SUCCESS = "success"
    ANSWERED_LOCALLY = "answered_locally"  # StateGuard refused / escalated; never sent
    TOOL_ERROR = "tool_error"  # the server answered with isError: true
    PROTOCOL_ERROR = "protocol_error"  # the server answered with a JSON-RPC error
    # the server answered a JSON-RPC internal error (-32603): it failed, perhaps transiently;
    # retried only where repeating is safe (keyed, or declared read_only / idempotent)
    SERVER_ERROR = "server_error"
    # authentication or authorization failure (stateguard.adapters.mcp.failures)
    AUTH_ERROR = "auth_error"
    UNKNOWN_DEADLINE = "unknown_deadline"  # sent, unanswered in time; may have executed
    UNKNOWN_TRANSPORT = "unknown_transport"  # sent, connection lost; may have executed

    @property
    def outcome_unknown(self) -> bool:
        return self in (Observed.UNKNOWN_DEADLINE, Observed.UNKNOWN_TRANSPORT)


@dataclass(frozen=True)
class RetryVerdict:
    decision: TrustDecision
    reason: str

    @property
    def retry(self) -> bool:
        return self.decision is TrustDecision.APPLY

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision.value, "reason": self.reason}


def decide_retry(
    observed: Observed,
    effect: EffectDeclaration,
    *,
    attempts_made: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    connection_available: bool = True,
    reread_unknown: bool = False,
    keyed: bool = False,
) -> RetryVerdict:
    """
    Whether to send an attempt again. See the module docstring for the rules.

    ``connection_available`` is accepted for compatibility; no rule depends
    on it (where a re-read goes after a lost connection is the caller's).
    ``reread_unknown`` enables rule 4's first exception: an unknown outcome
    of a tool the operator declared READ_ONLY is requested once more.
    ``keyed``: the call carries an idempotency key that its server says it
    honours (``stateguard.adapters.mcp.idempotency``) -- a resend is then
    answered from the server's record and never executed twice, so rule 4's
    second exception resends it once, with the same key.
    """
    reject, ambiguous, apply = TrustDecision.REJECT, TrustDecision.AMBIGUOUS, TrustDecision.APPLY
    if observed is Observed.SUCCESS:
        return RetryVerdict(reject, "succeeded; nothing to retry")
    if observed is Observed.ANSWERED_LOCALLY:
        return RetryVerdict(reject, "answered by StateGuard and never sent; its answer is final")
    if observed is Observed.AUTH_ERROR:
        return RetryVerdict(
            reject, "authorization signal: for the operator to resolve, never retried"
        )
    if observed is Observed.PROTOCOL_ERROR:
        return RetryVerdict(reject, "the server rejected the request itself; it would again")
    if observed.outcome_unknown:
        if keyed:
            if attempts_made >= max_attempts:
                return RetryVerdict(
                    ambiguous,
                    f"outcome unknown after {attempts_made} keyed attempt(s), the most "
                    "allowed; escalated",
                )
            return RetryVerdict(
                apply,
                "outcome unknown, and the server honours this call's idempotency key: a "
                "resend with the same key is answered from its record, never executed twice",
            )
        if reread_unknown and effect.declared and effect.effect is Effect.READ_ONLY:
            if attempts_made >= max_attempts:
                return RetryVerdict(
                    ambiguous,
                    f"outcome unknown after {attempts_made} attempt(s), the most allowed; "
                    "escalated",
                )
            return RetryVerdict(
                apply,
                "outcome unknown, and the tool is declared read_only by the operator: "
                "requesting it again repeats no side effect",
            )
        # Otherwise, whatever the declared effect: an unknown outcome is never replayed.
        return RetryVerdict(
            ambiguous,
            "outcome unknown: it may have executed, and an unknown outcome is never re-sent "
            "automatically, whatever the tool's declared effect; escalated",
        )
    # From here on the attempt failed *knowingly*: a tool error, or a server's internal error
    # (SERVER_ERROR), after which the call may or may not have taken effect -- so only what
    # is safe to repeat is repeated.
    if attempts_made >= max_attempts:
        return RetryVerdict(reject, f"{attempts_made} attempt(s) made, the most allowed")
    if keyed:
        return RetryVerdict(
            apply,
            "the server honours this call's idempotency key: a resend with the same key is "
            "answered from its record if the error was recorded, executed only if it never was",
        )
    if not effect.repeatable:
        why = (
            "declared non-idempotent by the operator"
            if effect.declared and effect.effect is Effect.NON_IDEMPOTENT
            else "not declared safe to repeat by the operator"
        )
        return RetryVerdict(reject, f"the tool is {why}; the error is delivered as-is")
    return RetryVerdict(
        apply, f"declared {effect.effect.value} by the operator; repeating it is safe"
    )
