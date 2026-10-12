"""
Effects and the retry decision.

The property test enumerates every (observed outcome, effect, declared?,
attempts, connection) combination and checks the safety invariants directly:

* nothing is retried unless the operator declared it READ_ONLY or IDEMPOTENT;
* an unknown outcome on anything else is escalated, never delivered as a
  failure and never retried;
* authorization and protocol errors are never retried;
* the attempt budget is never exceeded;
* annotations never change a declaration.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from stateguard.adapters.mcp import (
    Effect,
    EffectDeclaration,
    EffectPolicy,
    Observed,
    decide_retry,
)
from stateguard.core.trust import TrustDecision

T = TrustDecision


class TestPolicy:
    def test_nothing_is_declared_by_default(self) -> None:
        decl = EffectPolicy().of("search")
        assert decl.effect is Effect.UNKNOWN and not decl.declared and not decl.repeatable

    def test_annotations_are_hints_only(self) -> None:
        decl = EffectPolicy().of(
            "search", annotations={"readOnlyHint": True, "idempotentHint": True, "title": "x"}
        )
        assert decl.effect is Effect.UNKNOWN and not decl.repeatable
        assert decl.hints == {"readOnlyHint": True, "idempotentHint": True}

    def test_a_declaration_is_exact_per_server(self) -> None:
        policy = EffectPolicy({("search", "a"): "read_only", "fetch": Effect.IDEMPOTENT})
        assert policy.of("search", server="a").repeatable
        assert not policy.of("search").repeatable  # never guessed across servers
        assert not policy.of("search", server="b").repeatable
        assert policy.of("fetch").repeatable
        assert not policy.of("fetch", server="a").repeatable

    def test_annotation_contradicting_a_declaration_does_not_change_it(self) -> None:
        policy = EffectPolicy({"pay": "non_idempotent"})
        decl = policy.of("pay", annotations={"readOnlyHint": True})
        assert decl.effect is Effect.NON_IDEMPOTENT and not decl.repeatable

    def test_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "effects.json"
        path.write_text(
            json.dumps(
                {
                    "tools": [
                        {"name": "read_file", "effect": "read_only"},
                        {"name": "pay", "server": "bank", "effect": "non_idempotent"},
                    ]
                }
            )
        )
        policy = EffectPolicy.from_file(path)
        assert len(policy) == 2 and policy.of("read_file").repeatable

    @pytest.mark.parametrize(
        "entries",
        [
            [{"effect": "read_only"}],
            [{"name": "x", "effect": "maybe"}],
            [{"name": "x", "server": 1, "effect": "read_only"}],
        ],
    )
    def test_bad_entries_are_refused(self, entries: list[dict[str, object]]) -> None:
        with pytest.raises(ValueError):
            EffectPolicy.from_entries(entries)


class TestRules:
    @pytest.mark.parametrize("observed", [Observed.UNKNOWN_DEADLINE, Observed.UNKNOWN_TRANSPORT])
    @pytest.mark.parametrize("effect", ["read_only", "idempotent", "non_idempotent", None])
    @pytest.mark.parametrize("attempts", [0, 1])
    def test_an_unknown_outcome_is_never_retried(
        self, observed: Observed, effect: str | None, attempts: int
    ) -> None:
        policy = EffectPolicy({"s": effect}) if effect else EffectPolicy()
        v = decide_retry(observed, policy.of("s"), attempts_made=attempts)
        assert v.decision is T.AMBIGUOUS and not v.retry
        assert "never re-sent" in v.reason

    @pytest.mark.parametrize("effect", ["read_only", "idempotent"])
    def test_a_known_tool_error_on_a_declared_safe_tool_is_retried(self, effect: str) -> None:
        v = decide_retry(Observed.TOOL_ERROR, EffectPolicy({"s": effect}).of("s"), attempts_made=1)
        assert v.retry and effect in v.reason

    def test_undeclared_unknown_outcome_escalates(self) -> None:
        v = decide_retry(Observed.UNKNOWN_DEADLINE, EffectPolicy().of("s"), attempts_made=1)
        assert v.decision is T.AMBIGUOUS and "may have executed" in v.reason

    def test_undeclared_tool_error_is_delivered(self) -> None:
        v = decide_retry(Observed.TOOL_ERROR, EffectPolicy().of("s"), attempts_made=1)
        assert v.decision is T.REJECT

    def test_no_connection_escalates_even_when_repeatable(self) -> None:
        v = decide_retry(
            Observed.UNKNOWN_TRANSPORT,
            EffectPolicy({"s": "idempotent"}).of("s"),
            attempts_made=1,
            connection_available=False,
        )
        assert v.decision is T.AMBIGUOUS


ALL = list(
    itertools.product(
        list(Observed),
        list(Effect),
        [True, False],  # declared
        [0, 1, 2, 3],  # attempts made
        [True, False],  # connection available
        [None, {"readOnlyHint": True, "idempotentHint": True}, {"destructiveHint": True}],
    )
)


@pytest.mark.parametrize(
    ("observed", "effect", "declared", "attempts", "connected", "hints"),
    ALL,
    ids=lambda v: str(v),
)
def test_property_the_safety_invariants_hold_everywhere(
    observed: Observed,
    effect: Effect,
    declared: bool,
    attempts: int,
    connected: bool,
    hints: dict[str, bool] | None,
) -> None:
    decl = EffectDeclaration(effect, declared=declared, hints=hints or {})
    v = decide_retry(
        observed, decl, attempts_made=attempts, max_attempts=2, connection_available=connected
    )
    repeatable = declared and effect in (Effect.READ_ONLY, Effect.IDEMPOTENT)
    if v.retry:
        assert repeatable
        assert attempts < 2
        assert observed not in (
            Observed.SUCCESS,
            Observed.ANSWERED_LOCALLY,
            Observed.AUTH_ERROR,
            Observed.PROTOCOL_ERROR,
        )
        assert observed is Observed.TOOL_ERROR  # only a knowingly failed attempt
    if observed.outcome_unknown:
        assert v.decision is T.AMBIGUOUS  # never retried, whatever the effect
    if observed.outcome_unknown:
        assert v.decision is not T.REJECT  # unknown is never delivered as a plain failure
    # hints never matter: same verdict without them
    bare = decide_retry(
        observed,
        EffectDeclaration(effect, declared=declared),
        attempts_made=attempts,
        max_attempts=2,
        connection_available=connected,
    )
    assert bare == v
