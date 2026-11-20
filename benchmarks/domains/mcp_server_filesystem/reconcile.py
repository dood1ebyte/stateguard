"""
Reconciliation verdicts for an invocation whose response was lost (Week 3 experiment).

Pure logic, no I/O. It never runs in the relay: the experiment harness
(``reconcile_experiment.py``) feeds it evidence gathered from the real server
and compares its verdicts with on-disk ground truth.

The question it answers: given a lost response, does a declared read-only
probe show that

``satisfied``         the intended postcondition holds now;
``not_executed``      the operation definitely did not execute;
``inconclusive``      the evidence cannot say (stale, inconsistent, incomplete,
                      unavailable, contradictory -- or the pre-state is seen but
                      non-execution is not established);
``unsatisfied_open``  the postcondition does not hold, and the original executor
                      may still execute the operation.

Five things are kept apart and never derived from one another:

* **execution state** -- always ``unknown`` here; a probe never changes it.
  An UNKNOWN invocation is never relabelled "executed" because the desired
  state is observed.
* **result validity** -- the response was lost; there is no result to judge.
* **postcondition** -- what the probe says about the state now.
* **attribution** -- always ``unknown``: a probe sees state, not who made it.
* **resend eligibility** -- only after ``not_executed``, and only if declared.

Two policies are compared:

``declared``  the Week 2 handoff: an operator's ``exclusive: true`` declaration,
              plus a confirmed-stopped executor and a fresh pre-state probe, is
              accepted as proof of non-execution.
``strict``    the Week 3 safety rules: a declaration is not evidence. Non-execution
              needs *verified* exclusivity, which nothing in the probe provides --
              so in practice ``strict`` never says ``not_executed``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class Verdict(StrEnum):
    SATISFIED = "satisfied"
    NOT_EXECUTED = "not_executed"
    INCONCLUSIVE = "inconclusive"
    UNSATISFIED_OPEN = "unsatisfied_open"


class Executor(StrEnum):
    """What is known about the process the lost request was written to."""

    TERMINATED = "terminated"  # its exit was observed (reaped); it cannot act again
    ALIVE = "alive"  # still running: it may yet execute the request
    UNKNOWN = "unknown"  # not established either way


class Policy(StrEnum):
    DECLARED = "declared"
    STRICT = "strict"


ABSENT = "<absent>"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Read:
    """One probe read. ``status``: ok / enoent / error / missing (never answered)."""

    path: str
    status: str
    digest: str | None
    observed_at: float
    generation: int | None  # which server process answered it


@dataclass(frozen=True)
class Expectation:
    """The state a probe must see, path -> sha256 of the content, or ``ABSENT``."""

    paths: dict[str, str]

    def matches(self, observed: dict[str, str]) -> bool:
        return all(observed.get(p) == v for p, v in self.paths.items())


@dataclass(frozen=True)
class Declaration:
    """What an operator declares about one operation. Declarations are not evidence."""

    exclusive: bool = False
    resend_if_not_executed: bool = False


@dataclass(frozen=True)
class Evidence:
    reads: Sequence[Read]
    #: When the client learned the outcome was unknown (monotonic seconds).
    loss_at: float
    executor: Executor
    #: The server generation the lost request was written to.
    executor_generation: int
    #: Verified (not declared) proof that no other actor touched the paths.
    #: Nothing in this experiment can produce it; it exists so the rule is explicit.
    exclusivity_verified: bool = False


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    postcondition: str  # satisfied / absent / contradictory / unknown
    reasons: tuple[str, ...]
    late_execution_possible: bool
    execution_state: str = "unknown"  # never changed by a probe
    attribution: str = "unknown"  # a probe cannot attribute state to an actor
    result_validity: str = "no_result"  # the response was lost
    observed: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "postcondition": self.postcondition,
            "reasons": list(self.reasons),
            "late_execution_possible": self.late_execution_possible,
            "execution_state": self.execution_state,
            "attribution": self.attribution,
            "result_validity": self.result_validity,
        }


def _quality(evidence: Evidence) -> tuple[dict[str, str] | None, str | None]:
    """The observed state, or why the evidence cannot be used."""
    reads = list(evidence.reads)
    if not reads or all(r.status == "missing" for r in reads):
        return None, "probe unavailable: no read was answered"
    if any(r.status in ("missing", "error") for r in reads):
        return None, "probe incomplete: a read failed or was never answered"
    for r in reads:
        if r.observed_at <= evidence.loss_at:
            return None, "stale: a read predates the loss of the response"
        if r.generation == evidence.executor_generation:
            return None, "stale: a read came from the process whose outcome is unknown"
    observed: dict[str, str] = {}
    for r in reads:
        value = ABSENT if r.status == "enoent" else str(r.digest)
        if observed.setdefault(r.path, value) != value:
            return None, f"inconsistent: {r.path!r} changed between reads of one probe"
    return observed, None


def assess(
    pre: Expectation,
    post: Expectation,
    evidence: Evidence,
    declaration: Declaration,
    policy: Policy,
) -> Assessment:
    """The verdict for one lost invocation. Deterministic: same inputs, same output."""
    alive = evidence.executor is not Executor.TERMINATED
    observed, problem = _quality(evidence)
    if observed is None:
        return Assessment(Verdict.INCONCLUSIVE, "unknown", (str(problem),), alive)
    if post.matches(observed):
        reasons = ["intended postcondition observed; attribution unknown"]
        if alive:
            reasons.append("the original executor may still run the operation again")
        return Assessment(Verdict.SATISFIED, "satisfied", tuple(reasons), alive, observed=observed)
    if not pre.matches(observed):
        return Assessment(
            Verdict.INCONCLUSIVE,
            "contradictory",
            ("contradictory: the state is neither the pre-state nor the intended post-state",),
            alive,
            observed=observed,
        )
    # The pre-state is observed: the postcondition is absent.
    if alive:
        return Assessment(
            Verdict.UNSATISFIED_OPEN,
            "absent",
            ("pre-state observed while the original executor may still execute",),
            True,
            observed=observed,
        )
    if evidence.exclusivity_verified or (policy is Policy.DECLARED and declaration.exclusive):
        why = (
            "verified exclusivity"
            if evidence.exclusivity_verified
            else "declared (not verified) exclusivity"
        )
        return Assessment(
            Verdict.NOT_EXECUTED,
            "absent",
            (f"pre-state observed, executor terminated, {why}",),
            False,
            observed=observed,
        )
    return Assessment(
        Verdict.INCONCLUSIVE,
        "absent",
        (
            "pre-state observed and executor terminated, but non-execution is not "
            "established: an execution undone by another actor cannot be excluded "
            "without verified exclusivity",
        ),
        False,
        observed=observed,
    )


def resend_eligible(
    assessment: Assessment, declaration: Declaration, *, resends_made: int, executor: Executor
) -> tuple[bool, str]:
    """One resend at most: after ``not_executed``, only if declared, never to a live executor."""
    if assessment.verdict is not Verdict.NOT_EXECUTED:
        return False, f"verdict is {assessment.verdict.value}; no resend"
    if executor is not Executor.TERMINATED:
        return False, "the original executor is not confirmed stopped"
    if not declaration.resend_if_not_executed:
        return False, "the operation is not declared resend-eligible"
    if resends_made >= 1:
        return False, "the resend budget (1) is spent"
    return True, "not executed under the policy's assumptions; one resend allowed"


# ---------------------------------------------------------------------------
# Operation specs for the testbed (declared by the operator, built from the pre-image)
# ---------------------------------------------------------------------------


def move_spec(src: str, dst: str, pre_src_text: str) -> tuple[Expectation, Expectation, list[str]]:
    """move_file(src, dst): pre = src as captured, dst absent; post = src absent, dst = src."""
    d = digest(pre_src_text)
    return (
        Expectation({src: d, dst: ABSENT}),
        Expectation({src: ABSENT, dst: d}),
        [src, dst, src],  # src read twice: a change during the probe is detected
    )


def append_spec(
    path: str, pre_text: str, marker: str, line: str
) -> tuple[Expectation, Expectation, list[str]]:
    """edit_file insert-before-marker: post = the pre-image with exactly one insertion."""
    after = pre_text.replace(marker, f"{line}\n{marker}", 1)
    return Expectation({path: digest(pre_text)}), Expectation({path: digest(after)}), [path, path]
