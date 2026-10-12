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
display and audit, and they never change it.

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
2. An authorization signal: never retried -- it is the operator's (or the
   user's) to resolve, and repeating it cannot help.
3. A JSON-RPC protocol error: never retried -- the server rejected the
   request itself; sending it again gets the same answer.
4. The attempt budget is spent: escalate if the outcome is unknown,
   otherwise deliver the error.
5. The effect is not declared READ_ONLY or IDEMPOTENT by the operator:
   escalate if the outcome is unknown (it may have executed), otherwise
   deliver the error.
6. Declared safe to repeat: retry -- unless there is no connection to send it
   on, in which case escalate.

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
    #: True only when the operator declared it. A default is never a declaration.
    declared: bool
    #: The server's annotations, as sent. Untrusted; recorded, never used.
    hints: Mapping[str, Any] = field(default_factory=dict)

    @property
    def repeatable(self) -> bool:
        return self.declared and self.effect.repeatable

    def to_dict(self) -> dict[str, Any]:
        return {"effect": self.effect.value, "declared": self.declared, "hints": dict(self.hints)}


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
    ) -> EffectDeclaration:
        """The tool's declaration. *annotations* are recorded as hints only."""
        hints = {k: annotations[k] for k in _HINT_KEYS if annotations and k in annotations}
        effect = self._declared.get((name, server))
        if effect is None:
            return EffectDeclaration(Effect.UNKNOWN, declared=False, hints=hints)
        return EffectDeclaration(effect, declared=True, hints=hints)

    def __len__(self) -> int:
        return len(self._declared)


class Observed(StrEnum):
    """What happened to one attempt, as the proxy saw it."""

    SUCCESS = "success"
    ANSWERED_LOCALLY = "answered_locally"  # StateGuard refused / escalated; never sent
    TOOL_ERROR = "tool_error"  # the server answered with isError: true
    PROTOCOL_ERROR = "protocol_error"  # the server answered with a JSON-RPC error
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
) -> RetryVerdict:
    """Whether to send an attempt again. See the module docstring for the rules."""
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
    if attempts_made >= max_attempts:
        if observed.outcome_unknown:
            return RetryVerdict(
                ambiguous,
                f"outcome unknown after {attempts_made} attempt(s), the most allowed; escalated",
            )
        return RetryVerdict(reject, f"{attempts_made} attempt(s) made, the most allowed")
    if not effect.repeatable:
        why = (
            "declared non-idempotent by the operator"
            if effect.declared and effect.effect is Effect.NON_IDEMPOTENT
            else "not declared safe to repeat by the operator"
        )
        if observed.outcome_unknown:
            return RetryVerdict(ambiguous, f"may have executed and is {why}; escalated")
        return RetryVerdict(reject, f"the tool is {why}; the error is delivered as-is")
    if observed is Observed.UNKNOWN_TRANSPORT and not connection_available:
        return RetryVerdict(
            ambiguous, "declared safe to repeat, but there is no connection to retry on"
        )
    return RetryVerdict(
        apply, f"declared {effect.effect.value} by the operator; repeating it is safe"
    )
