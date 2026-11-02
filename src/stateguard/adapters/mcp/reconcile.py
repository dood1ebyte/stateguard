"""
Reconciling a ``tools/call`` whose outcome is UNKNOWN: observe a declared predicate, or escalate.

A request that was sent and never answered may or may not have executed
(``ExecutionState.UNKNOWN``). This module decides one narrow thing about it:
whether operator-declared, read-only **probes** observed an operator-declared
**predicate** holding at the time they ran. It does no I/O; the relay sends the
probes and feeds the answers back. The contract is
``docs/runtime/RECONCILIATION_CONTRACT_V2.md``.

What a verdict may say -- and what it never says
------------------------------------------------
``observed_satisfied``      every declared check held when it was read.
``observed_not_satisfied``  the checks answered, and the predicate did not hold.
``undetermined``            the evidence cannot decide: a probe failed, timed
                            out, answered something malformed, disagreed with
                            another read, or the server changed while probing --
                            or nothing was probed, because termination of the
                            original executor was not established.

None of these says whether the request executed, whether it *caused* what was
seen (attribution stays ``unknown``: another actor, or a state that already
held, looks the same), whether the state still holds after the observation, or
whether the operation succeeded. A presence check (``present`` / ``absent``)
says nothing about content; ``observation()`` states which kind each check was.

Nothing here can produce "executed", "not executed" or a resend. The
experiment behind this contract (docs/runtime/WEEK3_RECONCILIATION_EXPERIMENT.md)
found that a probe cannot establish non-execution: an execution undone by
another actor looks exactly like one that never happened. An operator's
``exclusive: true`` is not evidence, and a ``reconcile`` block that asks for a
resend is rejected.

Configuration
-------------
In the operator's effects file, on the entry of the tool to reconcile::

    {"name": "move_file", "effect": "non_idempotent",
     "reconcile": {
       "timeout_seconds": 5,
       "probes": [
         {"tool": "read_text_file", "arguments": {"path": "$source"},
          "expect": "absent", "absent_marker": "ENOENT"},
         {"tool": "read_text_file", "arguments": {"path": "$destination"},
          "expect": "present"}]}}

* every probe tool must itself be declared ``read_only`` in the same file --
  explicitly; server annotations never qualify a tool, and a server that
  annotates it ``readOnlyHint: false`` or ``destructiveHint: true`` disqualifies
  it at probe time;
* probe arguments are references (``"$name"``) to the original call's string
  arguments only -- a probe can read nothing the call did not name;
* ``expect`` is one of (strength in brackets):

  - ``"present"`` -- a non-error result [presence];
  - ``"absent"`` -- an error result whose text contains ``absent_marker`` [presence];
  - ``{"text_equals_argument": "$name"}`` -- the first text block equals that
    argument of the original call; ``$name`` may not be one of the probe's own
    arguments (that would compare a path with itself) [content];
  - ``{"text_sha256_equals": "<64 hex>"}`` -- the SHA-256 of the first text
    block (UTF-8) equals a value declared in this file: a *known* expected
    result, for tools whose effect's content is not one of their arguments
    [content].

  ``absent_marker`` may be given on any probe, so that "does not exist" reads
  as *unsatisfied* rather than as an error; any other error result leaves the
  postcondition undetermined;
* at most ``MAX_PROBES`` probes, each answered within ``timeout_seconds``
  (at most ``MAX_TIMEOUT_SECONDS``);
* one overall budget, ``max_seconds`` (default ``DEFAULT_BUDGET_SECONDS``, at
  most ``MAX_BUDGET_SECONDS``), bounds everything a held answer waits for --
  termination evidence, the restart, tool discovery and every probe. Each of
  those waits is capped by what is left of it, so they cannot add up past it;
  ``timeout_seconds`` times the number of probes may not exceed it;
* the file may not repeat a key in any object (JSON parsers keep the last one
  silently; here that is an error).

Probe results are never stored or logged: only each probe's status.

The executor must be gone first
-------------------------------
A probe is only meaningful once nothing can still perform the original
operation. Restarting a server, killing its direct child, emptying its process
group, closing a pipe or waiting out a timeout does not show that: a descendant
the server started may have left the process group (``setsid``) and may still
act. So probing needs ``TerminationEvidence`` whose kind is *sufficient*, and
the only sufficient kind is ``scope_ended``: every process or thread that could
execute the call ran inside one scope (an in-process server, a container, a VM,
a cgroup the integrator controls), and the integrator's restart code confirmed
that scope ended. Everything else -- including what a plain process restart can
see -- is recorded as evidence and leaves the postcondition ``undetermined``.

``scope_ended`` is an **assertion by the embedder**, not proof by StateGuard,
and it names the executor it is about (``executor=``): it only counts for the
server generation the call was sent to, whose identity the embedder gave when
that generation started. It covers the processes and threads inside the scope.
It does not cover external systems (remote APIs, databases, queues, other
actors), which may still change state after the scope ended.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from stateguard.adapters.mcp.effects import Effect, EffectPolicy
from stateguard.adapters.mcp.ledger import Postcondition

__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "DISCOVERY_BUDGET_SECONDS",
    "MAX_BUDGET_SECONDS",
    "MAX_PROBES",
    "MAX_TIMEOUT_SECONDS",
    "TERMINATION_WAIT_SECONDS",
    "Expect",
    "Probe",
    "ProbeObservation",
    "ProbeStatus",
    "ReconcileConfigError",
    "ReconcilePolicy",
    "ReconcileSpec",
    "TerminationEvidence",
    "TerminationKind",
    "Verdict",
    "bind_probes",
    "decide",
    "executor_identity",
    "observe",
    "probe_tool_problem",
]

MAX_PROBES = 4
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_MARKER_LENGTH = 200
#: The whole time a reconciliation may hold a client's answer (configurable per
#: block as ``max_seconds``, never above the maximum). The waits below are caps
#: *within* it: each is cut short by what remains of the budget.
DEFAULT_BUDGET_SECONDS = 30.0
MAX_BUDGET_SECONDS = 60.0
#: Cap on the wait for the restart to report termination evidence (it includes
#: ending the old executor scope and starting the new one).
TERMINATION_WAIT_SECONDS = 12.0
#: Cap on the wait for a restarted server to become usable for probes.
DISCOVERY_BUDGET_SECONDS = 20.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_ALLOWED_KEYS = frozenset({"probes", "timeout_seconds", "max_seconds"})
_PROBE_KEYS = frozenset({"tool", "arguments", "expect", "absent_marker"})
#: Asking for any of these is asking for a resend; this package has none.
_RESEND_KEYS = frozenset(
    {"exclusive", "resend", "resend_if_not_executed", "retry", "replay", "on_not_executed"}
)
_REFERENCE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_\-]*)$")


class ReconcileConfigError(ValueError):
    """A ``reconcile`` block that cannot be used safely. Raised at load time."""


class Expect(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    TEXT_EQUALS_ARGUMENT = "text_equals_argument"
    TEXT_SHA256_EQUALS = "text_sha256_equals"


#: What a check can show: that something exists or not, or what its content is.
PRESENCE, CONTENT = "presence", "content"
_STRENGTH = {
    Expect.PRESENT: PRESENCE,
    Expect.ABSENT: PRESENCE,
    Expect.TEXT_EQUALS_ARGUMENT: CONTENT,
    Expect.TEXT_SHA256_EQUALS: CONTENT,
}


@dataclass(frozen=True)
class Probe:
    """One read-only probe: a tool, arguments bound to the call, and what it must see."""

    tool: str
    #: (probe parameter, name of the original call's argument it takes)
    arguments: tuple[tuple[str, str], ...]
    expect: Expect
    #: For TEXT_EQUALS_ARGUMENT: the call argument the text must equal.
    argument: str | None = None
    #: The text an error result contains when the thing does not exist
    #: (required for ABSENT; optional otherwise, so "absent" reads as unsatisfied).
    absent_marker: str | None = None
    #: For TEXT_SHA256_EQUALS: the declared SHA-256 (hex) of the expected text.
    sha256: str | None = None

    @property
    def strength(self) -> str:
        """``presence`` or ``content``: what a satisfied check of this kind can show."""
        return _STRENGTH[self.expect]

    def predicate(self) -> dict[str, Any]:
        """Exactly what this probe checks -- argument names, never values."""
        check: dict[str, Any] = {"expect": self.expect.value}
        if self.argument:
            check["equals_argument"] = f"${self.argument}"
        if self.sha256:
            check["sha256"] = self.sha256
        return {
            "tool": self.tool,
            "arguments": {param: f"${ref}" for param, ref in self.arguments},
            "check": check,
            "strength": self.strength,
        }

    def to_dict(self) -> dict[str, Any]:
        """Configuration only -- names, never values."""
        return {
            "tool": self.tool,
            "arguments": {param: f"${ref}" for param, ref in self.arguments},
            "expect": self.expect.value,
            **({"argument": f"${self.argument}"} if self.argument else {}),
            **({"sha256": self.sha256} if self.sha256 else {}),
            **({"absent_marker": self.absent_marker} if self.absent_marker else {}),
        }


@dataclass(frozen=True)
class ReconcileSpec:
    tool: str
    server: str | None
    probes: tuple[Probe, ...]
    timeout_seconds: float
    #: The overall budget: the longest a client's answer is held, all waits included.
    max_seconds: float = DEFAULT_BUDGET_SECONDS

    @property
    def worst_case_seconds(self) -> float:
        """The longest a client's answer is held. Every wait is capped by the budget."""
        return self.max_seconds

    @property
    def content_checked(self) -> bool:
        """True only if every check of something that should exist reads its content."""
        positive = [p for p in self.probes if p.expect is not Expect.ABSENT]
        return bool(positive) and all(p.strength == CONTENT for p in positive)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "server": self.server,
            "timeout_seconds": self.timeout_seconds,
            "max_seconds": self.max_seconds,
            "worst_case_seconds": self.worst_case_seconds,
            "probes": [probe.to_dict() for probe in self.probes],
        }


def _reference(value: Any, where: str) -> str:
    match = _REFERENCE.match(value) if isinstance(value, str) else None
    if match is None:
        raise ReconcileConfigError(
            f"{where}: must reference an argument of the call as '$name', got {value!r}"
        )
    return match.group(1)


def _probe(raw: Any, target: str, server: str | None, effects: EffectPolicy, where: str) -> Probe:
    if not isinstance(raw, Mapping):
        raise ReconcileConfigError(f"{where}: a probe must be an object")
    unknown = set(raw) - _PROBE_KEYS
    if unknown:
        raise ReconcileConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ReconcileConfigError(f"{where}: needs a non-empty 'tool'")
    if tool == target:
        raise ReconcileConfigError(f"{where}: a tool cannot probe itself")
    declaration = effects.of(tool, server=server)
    state = f"declared {declaration.effect.value}" if declaration.declared else "undeclared"
    if not (declaration.declared and declaration.effect is Effect.READ_ONLY):
        raise ReconcileConfigError(
            f"{where}: probe tool {tool!r} must be declared read_only in the same file "
            f"(it is {state})"
        )
    arguments = raw.get("arguments")
    if not isinstance(arguments, Mapping) or not arguments:
        raise ReconcileConfigError(
            f"{where}: 'arguments' must bind at least one probe parameter to the call"
        )
    bound: list[tuple[str, str]] = []
    for param, ref in arguments.items():
        if not isinstance(param, str) or not param:
            raise ReconcileConfigError(f"{where}: probe parameter names must be strings")
        bound.append((param, _reference(ref, f"{where}.arguments.{param}")))
    expect_raw = raw.get("expect")
    argument: str | None = None
    digest: str | None = None
    forms = {Expect.TEXT_EQUALS_ARGUMENT.value, Expect.TEXT_SHA256_EQUALS.value}
    if isinstance(expect_raw, Mapping):
        keys = set(expect_raw)
        if len(keys) != 1 or not keys <= forms:
            raise ReconcileConfigError(f"{where}: unknown 'expect' form {sorted(keys)}")
        expect = Expect(next(iter(keys)))
        if expect is Expect.TEXT_EQUALS_ARGUMENT:
            argument = _reference(expect_raw[expect.value], f"{where}.expect")
            if argument in {ref for _, ref in bound}:
                raise ReconcileConfigError(
                    f"{where}.expect: '${argument}' is also an argument of the probe itself; "
                    "comparing a probe's input with its output is ambiguous"
                )
        else:
            digest = expect_raw[expect.value]
            if not isinstance(digest, str) or not _SHA256.match(digest):
                raise ReconcileConfigError(
                    f"{where}.expect: 'text_sha256_equals' must be 64 lowercase hex characters"
                )
    elif expect_raw in (Expect.PRESENT.value, Expect.ABSENT.value):
        expect = Expect(expect_raw)
    else:
        raise ReconcileConfigError(
            f"{where}: 'expect' must be 'present', 'absent', "
            f"{{'text_equals_argument': '$name'}} or {{'text_sha256_equals': '<hex>'}}, "
            f"got {expect_raw!r}"
        )
    marker = raw.get("absent_marker")
    usable = isinstance(marker, str) and 0 < len(marker) <= MAX_MARKER_LENGTH
    if (marker is not None or expect is Expect.ABSENT) and not usable:
        raise ReconcileConfigError(
            f"{where}: 'absent_marker' must be a non-empty string of at most "
            f"{MAX_MARKER_LENGTH} characters (required for expect 'absent') -- the text "
            "an error result carries when the thing does not exist"
        )
    return Probe(tool, tuple(bound), expect, argument, marker if usable else None, digest)


class ReconcilePolicy:
    """Operator-declared reconciliation probes, keyed like ``EffectPolicy``: (tool, server)."""

    def __init__(self, specs: Mapping[tuple[str, str | None], ReconcileSpec] | None = None) -> None:
        self._specs = dict(specs or {})

    @classmethod
    def from_entries(
        cls, entries: Iterable[Mapping[str, Any]], effects: EffectPolicy
    ) -> ReconcilePolicy:
        """The ``reconcile`` blocks of effects-file entries. Raises ``ReconcileConfigError``."""
        specs: dict[tuple[str, str | None], ReconcileSpec] = {}
        for entry in entries:
            if not isinstance(entry, Mapping) or "reconcile" not in entry:
                continue
            name, server = entry.get("name"), entry.get("server")
            where = f"reconcile[{name!r}]"
            if not isinstance(name, str) or not name:
                raise ReconcileConfigError("a reconcile block needs the entry's non-empty 'name'")
            if server is not None and not isinstance(server, str):
                raise ReconcileConfigError(f"{where}: 'server' must be a string")
            if (name, server) in specs:
                raise ReconcileConfigError(f"{where}: declared twice")
            target = effects.of(name, server=server)
            if target.declared and target.effect is Effect.READ_ONLY:
                raise ReconcileConfigError(f"{where}: a read_only tool has nothing to reconcile")
            block = entry["reconcile"]
            if not isinstance(block, Mapping):
                raise ReconcileConfigError(f"{where}: must be an object")
            asked = set(block) & _RESEND_KEYS
            if asked:
                raise ReconcileConfigError(
                    f"{where}: {sorted(asked)} not supported -- reconciliation never resends, "
                    "and a declaration of exclusivity is not evidence"
                )
            unknown = set(block) - _ALLOWED_KEYS
            if unknown:
                raise ReconcileConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
            probes_raw = block.get("probes")
            if not isinstance(probes_raw, list) or not probes_raw:
                raise ReconcileConfigError(f"{where}: needs a non-empty 'probes' list")
            if len(probes_raw) > MAX_PROBES:
                raise ReconcileConfigError(f"{where}: at most {MAX_PROBES} probes")
            timeout = block.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not 0 < timeout <= MAX_TIMEOUT_SECONDS
            ):
                raise ReconcileConfigError(
                    f"{where}: 'timeout_seconds' must be a number in (0, {MAX_TIMEOUT_SECONDS}]"
                )
            budget = block.get("max_seconds", DEFAULT_BUDGET_SECONDS)
            if (
                isinstance(budget, bool)
                or not isinstance(budget, (int, float))
                or not 0 < budget <= MAX_BUDGET_SECONDS
            ):
                raise ReconcileConfigError(
                    f"{where}: 'max_seconds' must be a number in (0, {MAX_BUDGET_SECONDS}]"
                )
            if timeout * len(probes_raw) > budget:
                raise ReconcileConfigError(
                    f"{where}: {len(probes_raw)} probes x 'timeout_seconds' {timeout} exceed "
                    f"'max_seconds' {budget}; the probes alone could not finish in the budget"
                )
            probes = tuple(
                _probe(raw, name, server, effects, f"{where}.probes[{i}]")
                for i, raw in enumerate(probes_raw)
            )
            specs[(name, server)] = ReconcileSpec(
                name, server, probes, float(timeout), float(budget)
            )
        return cls(specs)

    @classmethod
    def from_file(cls, path: str | Path, effects: EffectPolicy) -> ReconcilePolicy:
        repeated: list[str] = []

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            seen: dict[str, Any] = {}
            for key, value in pairs:
                if key in seen:
                    repeated.append(key)
                seen[key] = value
            return seen

        data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)
        entries_raw = data.get("tools") if isinstance(data, dict) else None
        declares = isinstance(entries_raw, list) and any(
            isinstance(e, Mapping) and "reconcile" in e for e in entries_raw
        )
        if repeated and declares:
            # Only where it matters: an effects file without reconcile blocks is
            # read exactly as before.
            raise ReconcileConfigError(
                f"{path}: key {repeated[0]!r} appears twice in one object; with reconcile "
                "blocks every object must be unambiguous"
            )
        entries = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ReconcileConfigError(f"{path}: expected an object with a 'tools' list")
        return cls.from_entries(entries, effects)

    def of(self, name: str, *, server: str | None = None) -> ReconcileSpec | None:
        return self._specs.get((name, server))

    def tools(self) -> list[str]:
        """The tools with declared probes (names only, for the audit log)."""
        return sorted({name for name, _server in self._specs})

    def __len__(self) -> int:
        return len(self._specs)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


class ProbeStatus(StrEnum):
    SATISFIED = "satisfied"  # observed what was expected
    UNSATISFIED = "unsatisfied"  # answered, and not what was expected
    TOOL_ERROR = "tool_error"  # an error result that does not mean "absent"
    PROTOCOL_ERROR = "protocol_error"  # a JSON-RPC error
    MALFORMED = "malformed"  # not a CallToolResult, or no text to compare
    UNAVAILABLE = "unavailable"  # no answer: timeout, server lost or changed, unsent
    INVALID = "invalid"  # the probe could not be used (unlisted tool, bad binding)


#: Statuses that leave the postcondition undetermined.
_UNUSABLE = frozenset(
    {
        ProbeStatus.TOOL_ERROR,
        ProbeStatus.PROTOCOL_ERROR,
        ProbeStatus.MALFORMED,
        ProbeStatus.UNAVAILABLE,
        ProbeStatus.INVALID,
    }
)


@dataclass(frozen=True)
class ProbeObservation:
    """What one probe saw -- a status and a coarse form, never the result itself."""

    index: int
    tool: str
    status: ProbeStatus
    #: "present", "absent", "error" or "none": compared across probes of the
    #: same tool and arguments to detect an inconsistent view.
    form: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "status": self.status.value,
            "form": self.form,
            "detail": self.detail,
        }


def bind_probes(
    spec: ReconcileSpec, call_arguments: Any
) -> tuple[list[dict[str, str]], list[str]] | str:
    """
    The arguments each probe sends, and a key per probe (same tool + same
    arguments -> same key). A reason string if the call's arguments cannot
    bind every probe: a referenced argument missing or not a string.
    """
    if not isinstance(call_arguments, Mapping):
        return "the call's arguments are not an object"
    bound: list[dict[str, str]] = []
    keys: list[str] = []
    for i, probe in enumerate(spec.probes):
        args: dict[str, str] = {}
        refs = [ref for _, ref in probe.arguments] + ([probe.argument] if probe.argument else [])
        for ref in refs:
            if not isinstance(call_arguments.get(ref), str):
                return f"probe {i}: the call has no string argument {ref!r}"
        for param, ref in probe.arguments:
            args[param] = call_arguments[ref]
        bound.append(args)
        keys.append(json.dumps([probe.tool, args], sort_keys=True))
    return bound, keys


def probe_tool_problem(listed: bool, annotations: Mapping[str, Any] | None) -> str | None:
    """Why a declared probe tool cannot be used on this server now, or None."""
    if not listed:
        return "the probe tool is not in the server's current listing"
    hints = annotations or {}
    if hints.get("readOnlyHint") is False or hints.get("destructiveHint") is True:
        return "the server annotates the probe tool as not read-only"
    return None


def _first_text(result: Mapping[str, Any]) -> str | None:
    for block in result.get("content") or []:
        if isinstance(block, Mapping) and block.get("type") == "text":
            text = block.get("text")
            return text if isinstance(text, str) else None
    return None


def observe(
    probe: Probe, index: int, call_arguments: Mapping[str, Any], response: Any
) -> ProbeObservation:
    """Classify one probe's response (``None``: no answer). The response is not kept."""

    def seen(status: ProbeStatus, form: str, detail: str = "") -> ProbeObservation:
        return ProbeObservation(index, probe.tool, status, form, detail)

    if response is None:
        return seen(ProbeStatus.UNAVAILABLE, "none", "no answer")
    if not isinstance(response, Mapping):
        return seen(ProbeStatus.MALFORMED, "none", "not a JSON-RPC response")
    if "error" in response:
        return seen(ProbeStatus.PROTOCOL_ERROR, "error", "JSON-RPC error")
    result = response.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("content"), list):
        return seen(ProbeStatus.MALFORMED, "none", "not a CallToolResult")
    is_error = result.get("isError", False)
    if not isinstance(is_error, bool):
        return seen(ProbeStatus.MALFORMED, "none", "isError is not a boolean")
    text = _first_text(result)
    marker = probe.absent_marker
    if is_error and marker is not None and text is not None and marker in text:
        status = ProbeStatus.SATISFIED if probe.expect is Expect.ABSENT else ProbeStatus.UNSATISFIED
        return seen(status, "absent")
    if is_error:
        return seen(ProbeStatus.TOOL_ERROR, "error", "error result not recognised as 'absent'")
    if probe.expect is Expect.ABSENT:
        return seen(ProbeStatus.UNSATISFIED, "present")
    if probe.expect is Expect.PRESENT:
        return seen(ProbeStatus.SATISFIED, "present")
    if text is None:
        return seen(ProbeStatus.MALFORMED, "present", "no text block to compare")
    if probe.expect is Expect.TEXT_SHA256_EQUALS:
        matches = hashlib.sha256(text.encode("utf-8")).hexdigest() == probe.sha256
    else:
        matches = text == call_arguments.get(probe.argument or "")
    return seen(ProbeStatus.SATISFIED if matches else ProbeStatus.UNSATISFIED, "present")


class TerminationKind(StrEnum):
    """What a restart observed about the replaced executor. Only ``scope_ended`` suffices."""

    #: The restart factory reported nothing (a plain ``(stdin, stdout)`` pair).
    NOT_REPORTED = "not_reported"
    #: The direct child process was reaped. Its descendants were not tracked.
    DIRECT_CHILD_REAPED = "direct_child_reaped"
    #: No process is left in the server's process group. A descendant that
    #: started its own session or group is outside what this can see.
    PROCESS_GROUP_EMPTY = "process_group_empty"
    #: Every executor ran inside one scope, and that scope was confirmed ended.
    SCOPE_ENDED = "scope_ended"
    #: The executor ran inside a scope, but its end could not be confirmed (it
    #: did not empty, or the confirmation did not arrive, within the deadline).
    SCOPE_UNCONFIRMED = "scope_unconfirmed"


_SUFFICIENT = frozenset({TerminationKind.SCOPE_ENDED})
_MAX_EVIDENCE_TEXT = 120

_LIMITS: dict[TerminationKind, str] = {
    TerminationKind.NOT_REPORTED: "the restart reported no evidence that the old executor ended",
    TerminationKind.DIRECT_CHILD_REAPED: (
        "only the direct child process was reaped; descendants it may have started are not tracked"
    ),
    TerminationKind.PROCESS_GROUP_EMPTY: (
        "the process group is empty, but a descendant in another session or group would not be seen"
    ),
    TerminationKind.SCOPE_UNCONFIRMED: (
        "the executor scope could not be confirmed empty within its deadline"
    ),
}


@dataclass(frozen=True)
class TerminationEvidence:
    """
    What the restart that replaced a server established about its executor.

    Built only through the named constructors, so that what was observed is
    explicit; whether it suffices is decided here, not by the caller.
    """

    kind: TerminationKind
    #: A short, non-sensitive description (an exit status, a scope name).
    detail: str = ""
    #: For ``scope_ended``: the identity of the executor whose scope ended, as the
    #: embedder named it when that server generation started.
    executor: str | None = None

    @property
    def established(self) -> bool:
        """
        True only for a ``scope_ended`` assertion. Whether it applies to a given
        call is ``applies_to``: the assertion must name that call's executor.
        """
        return self.kind in _SUFFICIENT

    def applies_to(self, executor: str | None) -> str | None:
        """None if this evidence establishes termination of *executor*; else why not."""
        if not self.established:
            return self.limit
        if executor is None:
            return "the server the call was sent to has no executor identity to match"
        if self.executor != executor:
            return "the evidence names a different executor than the one the call was sent to"
        return None

    @property
    def limit(self) -> str:
        """Why this evidence does not establish termination ('' if it does)."""
        return _LIMITS.get(self.kind, "")

    @classmethod
    def not_reported(cls) -> TerminationEvidence:
        return cls(TerminationKind.NOT_REPORTED)

    @classmethod
    def direct_child_reaped(cls, returncode: int | None) -> TerminationEvidence:
        return cls(TerminationKind.DIRECT_CHILD_REAPED, f"exit status {returncode}")

    @classmethod
    def process_group_empty(cls) -> TerminationEvidence:
        return cls(TerminationKind.PROCESS_GROUP_EMPTY)

    @classmethod
    def scope_unconfirmed(cls, detail: str = "") -> TerminationEvidence:
        """The scope's end was attempted and not confirmed: never sufficient."""
        return cls(TerminationKind.SCOPE_UNCONFIRMED, str(detail)[:_MAX_EVIDENCE_TEXT])

    @classmethod
    def scope_ended(cls, scope: str, *, executor: str) -> TerminationEvidence:
        """
        The embedder asserts: every process and thread that could execute calls
        sent to *executor* ran inside *scope*, and it confirmed that scope ended
        (an in-process server whose thread was joined, a container whose exit
        was awaited). StateGuard cannot check this; it logs it. It says nothing
        about external systems the executor may have handed work to.
        """
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope_ended needs a non-empty description of the scope")
        return cls(
            TerminationKind.SCOPE_ENDED,
            scope.strip()[:_MAX_EVIDENCE_TEXT],
            executor_identity(executor),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind.value,
            "established": self.established,
            "detail": self.detail,
        }
        if self.executor is not None:
            out["executor"] = self.executor
        if not self.established:
            out["limit"] = self.limit
        return out


_EXECUTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,119}$")


def executor_identity(value: Any) -> str:
    """A valid executor identity (1-120 visible ASCII, no spaces), or ValueError."""
    if not isinstance(value, str) or not _EXECUTOR.match(value):
        raise ValueError(
            "an executor identity is 1-120 characters: letters, digits and . _ : / @ - "
            "(starting with a letter or digit)"
        )
    return value


#: What no reconciliation outcome establishes, whatever it is.
NEVER_ESTABLISHED = (
    "that this request executed",
    "that this request caused the observed state",
    "that the observed state still holds",
    "that the operation succeeded",
)


@dataclass(frozen=True)
class Verdict:
    postcondition: Postcondition
    reasons: tuple[str, ...]
    observations: tuple[ProbeObservation, ...] = ()
    #: Exactly what each probe checked (``Probe.predicate()``), in probe order.
    predicates: tuple[dict[str, Any], ...] = ()
    #: Whether every positive check read content (``ReconcileSpec.content_checked``).
    content_checked: bool = False
    #: When the observation was made (ISO-8601 UTC): first probe sent, last answer.
    observed_from: str | None = None
    observed_to: str | None = None
    #: Never changed by a probe.
    execution_state: str = "unknown"
    #: A probe sees state, not who produced it.
    attribution: str = "unknown"
    #: A probe sees the state when it ran, not afterwards.
    durability: str = "not_established"
    #: A probe never shows that the client's operation succeeded.
    operation_outcome: str = "unknown"

    def establishes(self) -> str:
        if self.postcondition is Postcondition.OBSERVED_SATISFIED:
            return "the declared predicate held when it was observed"
        if self.postcondition is Postcondition.OBSERVED_NOT_SATISFIED:
            return "the declared predicate did not hold when it was observed"
        return "nothing about the declared predicate"

    def does_not_establish(self) -> list[str]:
        out = list(NEVER_ESTABLISHED)
        if self.postcondition is Postcondition.OBSERVED_NOT_SATISFIED:
            out.insert(0, "that this request did not execute")
        if self.postcondition is Postcondition.OBSERVED_SATISFIED and not self.content_checked:
            out.append("that any content is correct (presence checks only)")
        return out

    def to_dict(self) -> dict[str, Any]:
        """Deterministic for the same scenario: no timestamps (see ``observation``)."""
        return {
            "postcondition": self.postcondition.value,
            "execution_state": self.execution_state,
            "attribution": self.attribution,
            "durability": self.durability,
            "operation_outcome": self.operation_outcome,
            "establishes": self.establishes(),
            "does_not_establish": self.does_not_establish(),
            "content_checked": self.content_checked,
            "reasons": list(self.reasons),
            "probes": [o.to_dict() for o in self.observations],
        }

    def observation(self) -> dict[str, Any] | None:
        """What was checked and when; None if nothing was probed."""
        if not self.observations:
            return None
        return {
            "observed_from": self.observed_from,
            "observed_to": self.observed_to,
            "predicates": [dict(p) for p in self.predicates],
            "content_checked": self.content_checked,
        }


def decide(
    spec: ReconcileSpec,
    observations: Sequence[ProbeObservation],
    keys: Sequence[str] | None = None,
    *,
    executor_replaced: bool,
    generation_stable: bool,
) -> Verdict:
    """The postcondition verdict. Deterministic; never "executed" or "not executed"."""
    obs = tuple(observations)
    preds = tuple(p.predicate() for p in spec.probes)
    content = spec.content_checked
    if not executor_replaced:
        return Verdict(
            Postcondition.UNDETERMINED,
            ("the original executor may still be running: no probe can settle the outcome",),
            obs,
            predicates=preds,
            content_checked=content,
        )
    if not generation_stable:
        return Verdict(
            Postcondition.UNDETERMINED,
            ("stale: the server changed while probing",),
            obs,
            predicates=preds,
            content_checked=content,
        )
    if len(obs) != len(spec.probes):
        return Verdict(
            Postcondition.UNDETERMINED,
            ("incomplete: not every probe ran",),
            obs,
            predicates=preds,
            content_checked=content,
        )
    unusable = [o for o in obs if o.status in _UNUSABLE]
    if unusable:
        return Verdict(
            Postcondition.UNDETERMINED,
            tuple(f"probe {o.index} ({o.tool}): {o.status.value}" for o in unusable),
            obs,
            predicates=preds,
            content_checked=content,
        )
    if keys is not None:
        forms: dict[str, set[str]] = {}
        for key, o in zip(keys, obs, strict=True):
            forms.setdefault(key, set()).add(o.form)
        if any(len(f) > 1 for f in forms.values()):
            return Verdict(
                Postcondition.UNDETERMINED,
                ("inconsistent: the same read gave different answers during one probe",),
                obs,
                predicates=preds,
                content_checked=content,
            )
    if all(o.status is ProbeStatus.SATISFIED for o in obs):
        kind = "content" if spec.content_checked else "presence-only"
        return Verdict(
            Postcondition.OBSERVED_SATISFIED,
            (
                f"the declared predicate held when {len(obs)} read-only probe(s) read it "
                f"({kind} checks); whether this request executed or caused it, and whether "
                "it still holds, is unknown",
            ),
            obs,
            predicates=preds,
            content_checked=content,
        )
    missing = tuple(
        f"probe {o.index} ({o.tool}): {o.status.value}"
        for o in obs
        if o.status is not ProbeStatus.SATISFIED
    )
    return Verdict(
        Postcondition.OBSERVED_NOT_SATISFIED,
        (*missing, "this does not show the request did not execute"),
        obs,
        predicates=preds,
        content_checked=content,
    )
