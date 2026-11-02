"""
Reconciliation of an UNKNOWN tools/call: configuration safety, probe classification,
and the verdict rules ("observe a declared predicate, or escalate"; never executed / not executed).
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from stateguard.adapters.mcp import (
    EffectPolicy,
    ExecutionState,
    InvocationLedger,
    Postcondition,
    ProbeStatus,
    ReconcileConfigError,
    ReconcilePolicy,
    ReconcileSpec,
    ResultValidity,
)
from stateguard.adapters.mcp.reconcile import (
    MAX_PROBES,
    Expect,
    ProbeObservation,
    bind_probes,
    decide,
    observe,
    probe_tool_problem,
)

READ = {"name": "read", "effect": "read_only"}


def _move(probes: list[dict[str, Any]] | None = None, **block: Any) -> dict[str, Any]:
    return {
        "name": "move",
        "effect": "non_idempotent",
        "reconcile": {
            "probes": probes
            if probes is not None
            else [
                {
                    "tool": "read",
                    "arguments": {"path": "$source"},
                    "expect": "absent",
                    "absent_marker": "ENOENT",
                },
                {
                    "tool": "read",
                    "arguments": {"path": "$destination"},
                    "expect": "present",
                    "absent_marker": "ENOENT",
                },
            ],
            **block,
        },
    }


def _policy(*entries: dict[str, Any]) -> ReconcilePolicy:
    return ReconcilePolicy.from_entries(entries, EffectPolicy.from_entries(entries))


# ---------------------------------------------------------------------------
# Configuration: missing, invalid, unsafe
# ---------------------------------------------------------------------------


def test_a_valid_block_loads_and_names_only_its_tool() -> None:
    policy = _policy(READ, _move())
    spec = policy.of("move")
    assert spec is not None and len(spec.probes) == 2 and policy.tools() == ["move"]
    assert policy.of("read") is None and policy.of("move", server="other") is None


def test_no_reconcile_blocks_means_an_empty_policy() -> None:
    assert len(_policy(READ, {"name": "move", "effect": "non_idempotent"})) == 0


def test_from_file(tmp_path: Path) -> None:
    path = tmp_path / "effects.json"
    path.write_text(json.dumps({"tools": [READ, _move()]}))
    assert ReconcilePolicy.from_file(path, EffectPolicy.from_file(path)).of("move") is not None


@pytest.mark.parametrize(
    "entries, needle",
    [
        # the probe tool is not explicitly read-only
        ([_move()], "read_only"),
        ([{"name": "read", "effect": "idempotent"}, _move()], "read_only"),
        ([{"name": "read", "effect": "unknown"}, _move()], "read_only"),
        # resend / exclusivity is never accepted
        ([READ, _move(exclusive=True)], "not supported"),
        ([READ, _move(resend_if_not_executed=True)], "not supported"),
        ([READ, _move(retry=True)], "not supported"),
        # malformed blocks
        ([READ, _move(probes=[])], "non-empty 'probes'"),
        (
            [
                READ,
                _move(
                    probes=[{"tool": "read", "arguments": {"path": "$s"}, "expect": "present"}]
                    * (MAX_PROBES + 1)
                ),
            ],
            "at most",
        ),
        ([READ, _move(timeout_seconds=0)], "timeout_seconds"),
        ([READ, _move(timeout_seconds=31)], "timeout_seconds"),
        ([READ, _move(timeout_seconds=True)], "timeout_seconds"),
        ([READ, _move(surprise=1)], "unknown key"),
        (
            [READ, _move(probes=[{"tool": "move", "arguments": {"p": "$s"}, "expect": "present"}])],
            "itself",
        ),
        (
            [
                READ,
                _move(
                    probes=[
                        {"tool": "read", "arguments": {"path": "/etc/passwd"}, "expect": "present"}
                    ]
                ),
            ],
            r"'\$name'",
        ),
        (
            [READ, _move(probes=[{"tool": "read", "arguments": {}, "expect": "present"}])],
            "at least one",
        ),
        (
            [
                READ,
                _move(probes=[{"tool": "read", "arguments": {"path": "$s"}, "expect": "absent"}]),
            ],
            "absent_marker",
        ),
        (
            [
                READ,
                _move(probes=[{"tool": "read", "arguments": {"path": "$s"}, "expect": "maybe"}]),
            ],
            "'expect'",
        ),
        (
            [
                READ,
                _move(
                    probes=[
                        {
                            "tool": "read",
                            "arguments": {"path": "$s"},
                            "expect": "present",
                            "extra": 1,
                        }
                    ]
                ),
            ],
            "unknown key",
        ),
        ([READ, {**_move(), "name": "peek", "effect": "read_only"}], "nothing to reconcile"),
        ([READ, _move(), _move()], "declared twice"),
        ([READ, {"name": "move", "effect": "non_idempotent", "reconcile": "yes"}], "object"),
    ],
)
def test_unsafe_or_invalid_configuration_is_rejected_at_load(
    entries: list[dict[str, Any]], needle: str
) -> None:
    with pytest.raises(ReconcileConfigError, match=needle):
        _policy(*entries)


def test_server_annotations_can_disqualify_but_never_qualify_a_probe_tool() -> None:
    assert probe_tool_problem(True, {"readOnlyHint": True}) is None
    assert probe_tool_problem(True, None) is None
    assert probe_tool_problem(True, {"readOnlyHint": False}) is not None
    assert probe_tool_problem(True, {"destructiveHint": True}) is not None
    assert probe_tool_problem(False, {"readOnlyHint": True}) is not None  # not listed


# ---------------------------------------------------------------------------
# Binding and observation
# ---------------------------------------------------------------------------

_SPEC = _policy(READ, _move()).of("move")
assert _SPEC is not None
SPEC: ReconcileSpec = _SPEC
ARGS = {"source": "a.txt", "destination": "b.txt"}


def _ok(text: str = "content") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}


def _err(text: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def test_probe_arguments_come_only_from_the_call() -> None:
    bound = bind_probes(SPEC, ARGS)
    assert not isinstance(bound, str)
    args, keys = bound
    assert args == [{"path": "a.txt"}, {"path": "b.txt"}] and len(set(keys)) == 2
    assert isinstance(bind_probes(SPEC, {"source": "a.txt"}), str)  # missing reference
    assert isinstance(bind_probes(SPEC, {"source": 1, "destination": "b"}), str)  # not a string
    assert isinstance(bind_probes(SPEC, ["a", "b"]), str)


@pytest.mark.parametrize(
    "response, absent_status, present_status",
    [
        (_ok(), ProbeStatus.UNSATISFIED, ProbeStatus.SATISFIED),
        (_err("ENOENT: no such file"), ProbeStatus.SATISFIED, ProbeStatus.UNSATISFIED),
        (_err("permission denied"), ProbeStatus.TOOL_ERROR, ProbeStatus.TOOL_ERROR),
        (None, ProbeStatus.UNAVAILABLE, ProbeStatus.UNAVAILABLE),
        (
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}},
            ProbeStatus.PROTOCOL_ERROR,
            ProbeStatus.PROTOCOL_ERROR,
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "result": {"text": "no content list"}},
            ProbeStatus.MALFORMED,
            ProbeStatus.MALFORMED,
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": "yes"}},
            ProbeStatus.MALFORMED,
            ProbeStatus.MALFORMED,
        ),
        ("not a response", ProbeStatus.MALFORMED, ProbeStatus.MALFORMED),
    ],
)
def test_observe(response: Any, absent_status: ProbeStatus, present_status: ProbeStatus) -> None:
    absent, present = SPEC.probes
    assert observe(absent, 0, ARGS, response).status is absent_status
    assert observe(present, 1, ARGS, response).status is present_status


def test_text_equals_argument() -> None:
    write = _policy(
        READ,
        {
            "name": "write",
            "effect": "idempotent",
            "reconcile": {
                "probes": [
                    {
                        "tool": "read",
                        "arguments": {"path": "$path"},
                        "expect": {"text_equals_argument": "$content"},
                    }
                ]
            },
        },
    ).of("write")
    assert write is not None and write.probes[0].expect is Expect.TEXT_EQUALS_ARGUMENT
    args = {"path": "p", "content": "hello"}
    probe = write.probes[0]
    assert observe(probe, 0, args, _ok("hello")).status is ProbeStatus.SATISFIED
    assert observe(probe, 0, args, _ok("hellO")).status is ProbeStatus.UNSATISFIED
    no_text = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "image"}]}}
    assert observe(probe, 0, args, no_text).status is ProbeStatus.MALFORMED


def test_an_observation_never_carries_the_result() -> None:
    o = observe(SPEC.probes[1], 1, ARGS, _ok("secret file content"))
    assert "secret" not in json.dumps(o.to_dict())


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def _obs(*statuses: ProbeStatus, forms: tuple[str, ...] | None = None) -> list[ProbeObservation]:
    forms = forms or tuple("present" for _ in statuses)
    return [
        ProbeObservation(i, "read", s, f)
        for i, (s, f) in enumerate(zip(statuses, forms, strict=True))
    ]


SAT, UNSAT = ProbeStatus.SATISFIED, ProbeStatus.UNSATISFIED


def test_all_satisfied_is_observed_satisfied_and_leaves_execution_and_attribution_unknown() -> None:
    v = decide(SPEC, _obs(SAT, SAT), executor_replaced=True, generation_stable=True)
    assert v.postcondition is Postcondition.OBSERVED_SATISFIED
    assert v.execution_state == "unknown" and v.attribution == "unknown"


def test_a_definite_mismatch_is_observed_not_satisfied_and_never_not_executed() -> None:
    v = decide(SPEC, _obs(UNSAT, SAT), executor_replaced=True, generation_stable=True)
    assert v.postcondition is Postcondition.OBSERVED_NOT_SATISFIED
    assert v.execution_state == "unknown"
    assert any("does not show the request did not execute" in r for r in v.reasons)


def test_a_live_executor_is_always_undetermined() -> None:
    v = decide(SPEC, _obs(SAT, SAT), executor_replaced=False, generation_stable=True)
    assert v.postcondition is Postcondition.UNDETERMINED


def test_stale_inconsistent_or_incomplete_evidence_is_undetermined() -> None:
    assert (
        decide(SPEC, _obs(SAT, SAT), executor_replaced=True, generation_stable=False).postcondition
        is Postcondition.UNDETERMINED
    )
    assert (
        decide(SPEC, _obs(SAT), executor_replaced=True, generation_stable=True).postcondition
        is Postcondition.UNDETERMINED
    )
    same_key = ["k", "k"]
    v = decide(
        SPEC,
        _obs(SAT, SAT, forms=("absent", "present")),
        same_key,
        executor_replaced=True,
        generation_stable=True,
    )
    assert v.postcondition is Postcondition.UNDETERMINED and "inconsistent" in v.reasons[0]


@pytest.mark.parametrize(
    "bad",
    [
        ProbeStatus.TOOL_ERROR,
        ProbeStatus.PROTOCOL_ERROR,
        ProbeStatus.MALFORMED,
        ProbeStatus.UNAVAILABLE,
        ProbeStatus.INVALID,
    ],
)
def test_any_unusable_probe_makes_the_verdict_undetermined(bad: ProbeStatus) -> None:
    v = decide(SPEC, _obs(SAT, bad), executor_replaced=True, generation_stable=True)
    assert v.postcondition is Postcondition.UNDETERMINED


def test_no_combination_yields_an_execution_claim() -> None:
    """Exhaustive over two probes' statuses, forms, executor and stability."""
    forms = ("present", "absent", "error", "none")
    for s1, s2, f1, f2, replaced, stable in itertools.product(
        ProbeStatus, ProbeStatus, forms, forms, (True, False), (True, False)
    ):
        v = decide(
            SPEC,
            _obs(s1, s2, forms=(f1, f2)),
            ["a", "b"],
            executor_replaced=replaced,
            generation_stable=stable,
        )
        assert v.execution_state == "unknown" and v.attribution == "unknown"
        if v.postcondition is Postcondition.OBSERVED_SATISFIED:
            assert replaced and stable and s1 is SAT and s2 is SAT


def test_decide_is_deterministic() -> None:
    obs = _obs(UNSAT, ProbeStatus.TOOL_ERROR)
    results = {
        json.dumps(decide(SPEC, obs, executor_replaced=True, generation_stable=True).to_dict())
        for _ in range(5)
    }
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Ledger: separate fields, append-only, no relabelling
# ---------------------------------------------------------------------------


def _unknown_ledger() -> InvocationLedger:
    ledger = InvocationLedger()
    ledger.received(1, "tools/call", "move")
    ledger.sent(1)
    ledger.transport_closed()
    return ledger


def test_reconciliation_is_appended_and_never_changes_the_execution_state() -> None:
    ledger = _unknown_ledger()
    assert ledger.record_reconciliation(1, {"event": "pending"}, Postcondition.PENDING)
    assert ledger.record_reconciliation(1, {"event": "verdict"}, Postcondition.OBSERVED_SATISFIED)
    inv = ledger.get(1)
    assert inv is not None
    assert inv.state is ExecutionState.UNKNOWN and ExecutionState.COMPLETED not in inv.history
    assert inv.postcondition is Postcondition.OBSERVED_SATISFIED and inv.attribution == "unknown"
    assert inv.result_validity is ResultValidity.NO_RESULT
    assert [e["event"] for e in inv.reconciliation] == ["pending", "verdict"]
    # final: a second verdict is refused and recorded as an anomaly
    assert not ledger.record_reconciliation(
        1, {"event": "verdict"}, Postcondition.OBSERVED_NOT_SATISFIED
    )
    assert inv.postcondition is Postcondition.OBSERVED_SATISFIED and inv.anomalies


def test_reconciliation_is_refused_for_anything_but_unknown() -> None:
    ledger = InvocationLedger()
    ledger.received(1, "tools/call", "move")
    assert not ledger.record_reconciliation(1, {}, Postcondition.OBSERVED_SATISFIED)  # PENDING
    ledger.sent(1)
    assert not ledger.record_reconciliation(1, {}, Postcondition.OBSERVED_SATISFIED)  # SENT
    ledger.response(1, is_error=False)
    assert not ledger.record_reconciliation(1, {}, Postcondition.OBSERVED_SATISFIED)  # COMPLETED
    assert ledger.get(1).postcondition is Postcondition.NOT_CHECKED  # type: ignore[union-attr]


def test_result_validity_is_separate_from_the_postcondition() -> None:
    ledger = InvocationLedger()
    ledger.received(1, "tools/call", "move")
    ledger.sent(1)
    assert not ledger.record_result_check(1, ResultValidity.VALID)  # not answered yet
    ledger.response(1, is_error=False)
    assert ledger.record_result_check(1, ResultValidity.INVALID)
    inv = ledger.get(1)
    assert inv is not None
    assert inv.result_validity is ResultValidity.INVALID
    assert inv.postcondition is Postcondition.NOT_CHECKED and inv.attribution is None
    assert inv.state is ExecutionState.COMPLETED


def test_answered_locally_reports_whether_the_transition_was_accepted() -> None:
    ledger = InvocationLedger()
    ledger.received(1, "tools/call", "move")
    ledger.received(2, "tools/call", "move")
    ledger.sent(2)
    assert ledger.answered_locally(1) is True
    assert ledger.answered_locally(2) is False  # SENT: may have executed
    assert ledger.answered_locally(3) is False  # never seen
    assert ledger.get(2).state is ExecutionState.SENT  # type: ignore[union-attr]
    assert ledger.unknown(2, "not_sent_unconfirmed") is not None
    assert ledger.get(2).state is ExecutionState.UNKNOWN  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Contract v2: predicate strength, content checks, ambiguity, executor binding
# ---------------------------------------------------------------------------

_SHA = hashlib.sha256(b"order 001").hexdigest()


def test_a_declared_sha256_is_a_content_check() -> None:
    probes = [
        {"tool": "read", "arguments": {"path": "$source"}, "expect": "absent",
         "absent_marker": "ENOENT"},
        {"tool": "read", "arguments": {"path": "$destination"},
         "expect": {"text_sha256_equals": _SHA}},
    ]  # fmt: skip
    spec = _policy(READ, _move(probes)).of("move")
    assert spec is not None and spec.content_checked
    probe = spec.probes[1]
    assert probe.expect is Expect.TEXT_SHA256_EQUALS and probe.strength == "content"
    assert probe.predicate()["check"] == {"expect": "text_sha256_equals", "sha256": _SHA}
    args = {"destination": "/x"}
    assert observe(probe, 1, args, _ok("order 001")).status is ProbeStatus.SATISFIED
    assert observe(probe, 1, args, _ok("order: forged")).status is ProbeStatus.UNSATISFIED
    malformed = {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}
    assert observe(probe, 1, args, malformed).status is ProbeStatus.MALFORMED


def test_presence_checks_are_labelled_as_such() -> None:
    spec = _policy(READ, _move()).of("move")
    assert spec is not None and not spec.content_checked
    assert [p.strength for p in spec.probes] == ["presence", "presence"]
    only_absent = _policy(READ, _move([
        {"tool": "read", "arguments": {"path": "$source"}, "expect": "absent",
         "absent_marker": "ENOENT"}]))  # fmt: skip
    spec = only_absent.of("move")
    assert spec is not None and not spec.content_checked  # nothing positive was read


@pytest.mark.parametrize(
    "expect",
    [
        {"text_sha256_equals": "ABC"},
        {"text_sha256_equals": _SHA.upper()},
        {"text_sha256_equals": 123},
        {"text_sha256_equals": _SHA, "text_equals_argument": "$content"},
    ],
)
def test_an_unusable_content_check_is_rejected_at_load(expect: Any) -> None:
    probes = [{"tool": "read", "arguments": {"path": "$destination"}, "expect": expect}]
    with pytest.raises(ReconcileConfigError):
        _policy(READ, _move(probes))


def test_comparing_a_probe_with_its_own_input_is_rejected_as_ambiguous() -> None:
    probes = [
        {"tool": "read", "arguments": {"path": "$destination"},
         "expect": {"text_equals_argument": "$destination"}}
    ]  # fmt: skip
    with pytest.raises(ReconcileConfigError, match="ambiguous"):
        _policy(READ, _move(probes))


def test_repeated_keys_are_rejected_only_where_reconciliation_is_declared(tmp_path: Path) -> None:
    plain = tmp_path / "plain.json"
    plain.write_text('{"tools": [{"name": "read", "effect": "idempotent", "effect": "read_only"}]}')
    effects = EffectPolicy.from_file(plain)
    assert len(ReconcilePolicy.from_file(plain, effects)) == 0  # read as before
    declared = tmp_path / "declared.json"
    move = json.dumps(_move())
    declared.write_text(
        '{"tools": [{"name": "read", "effect": "read_only", "effect": "read_only"}, ' + move + "]}"
    )
    with pytest.raises(ReconcileConfigError, match="appears twice"):
        ReconcilePolicy.from_file(declared, EffectPolicy.from_file(declared))


def test_the_worst_case_duration_is_stated_and_bounded() -> None:
    from stateguard.adapters.mcp.reconcile import (
        DISCOVERY_BUDGET_SECONDS,
        MAX_TIMEOUT_SECONDS,
        TERMINATION_WAIT_SECONDS,
    )

    from stateguard.adapters.mcp.reconcile import DEFAULT_BUDGET_SECONDS, MAX_BUDGET_SECONDS

    # One overall budget bounds everything; the individual waits are caps within it.
    spec = _policy(READ, _move(timeout_seconds=3)).of("move")
    assert spec is not None
    assert spec.max_seconds == spec.worst_case_seconds == DEFAULT_BUDGET_SECONDS == 30.0
    assert spec.to_dict()["worst_case_seconds"] == 30.0
    assert TERMINATION_WAIT_SECONDS < DEFAULT_BUDGET_SECONDS
    assert DISCOVERY_BUDGET_SECONDS < DEFAULT_BUDGET_SECONDS
    tight = _policy(READ, _move(timeout_seconds=2, max_seconds=12)).of("move")
    assert tight is not None and tight.worst_case_seconds == 12.0
    for bad in (
        {"max_seconds": 0},
        {"max_seconds": MAX_BUDGET_SECONDS + 1},
        {"max_seconds": True},
        {"max_seconds": "30"},
        {"timeout_seconds": MAX_TIMEOUT_SECONDS, "max_seconds": MAX_BUDGET_SECONDS - 1},
    ):
        with pytest.raises(ReconcileConfigError):
            _policy(READ, _move(**bad))
    # The probes alone must fit in the budget.
    assert MAX_PROBES * MAX_TIMEOUT_SECONDS > MAX_BUDGET_SECONDS
    with pytest.raises(ReconcileConfigError, match="exceed 'max_seconds'"):
        _policy(READ, _move(timeout_seconds=10, max_seconds=15))


def test_scope_ended_is_an_assertion_about_one_named_executor() -> None:
    from stateguard.adapters.mcp import TerminationEvidence

    with pytest.raises(TypeError):
        TerminationEvidence.scope_ended("joined")  # type: ignore[call-arg]
    for bad in ("", " x", "has space", "x" * 121, None):
        with pytest.raises(ValueError):
            TerminationEvidence.scope_ended("joined", executor=bad)  # type: ignore[arg-type]
    evidence = TerminationEvidence.scope_ended("joined", executor="exec-1")
    assert evidence.established
    assert evidence.applies_to("exec-1") is None
    assert "different executor" in str(evidence.applies_to("exec-2"))
    assert "no executor identity" in str(evidence.applies_to(None))
    for weak in (
        TerminationEvidence.not_reported(),
        TerminationEvidence.direct_child_reaped(0),
        TerminationEvidence.process_group_empty(),
    ):
        assert not weak.established and weak.applies_to("exec-1") == weak.limit != ""


def test_every_outcome_states_what_it_does_not_establish() -> None:
    spec = _policy(READ, _move()).of("move")
    assert spec is not None
    satisfied = decide(spec, _obs(ProbeStatus.SATISFIED, ProbeStatus.SATISFIED),
                       executor_replaced=True, generation_stable=True)  # fmt: skip
    unsatisfied = decide(spec, _obs(ProbeStatus.SATISFIED, ProbeStatus.UNSATISFIED),
                         executor_replaced=True, generation_stable=True)  # fmt: skip
    undetermined = decide(spec, _obs(ProbeStatus.UNAVAILABLE, ProbeStatus.SATISFIED),
                          executor_replaced=True, generation_stable=True)  # fmt: skip
    base = {
        "that this request executed",
        "that this request caused the observed state",
        "that the observed state still holds",
        "that the operation succeeded",
    }
    for verdict in (satisfied, unsatisfied, undetermined):
        record = verdict.to_dict()
        assert base <= set(record["does_not_establish"])
        assert record["execution_state"] == "unknown" and record["attribution"] == "unknown"
        assert record["durability"] == "not_established"
        assert record["operation_outcome"] == "unknown"
        assert "observed_from" not in json.dumps(record)  # deterministic: no timestamps
    assert satisfied.postcondition is Postcondition.OBSERVED_SATISFIED
    assert "that any content is correct (presence checks only)" in satisfied.does_not_establish()
    assert "presence-only" in satisfied.reasons[0]
    assert "that this request did not execute" in unsatisfied.does_not_establish()
    assert undetermined.establishes() == "nothing about the declared predicate"
    assert undetermined.observation() is not None  # probes ran, so what was checked is stated
    assert decide(spec, [], executor_replaced=False, generation_stable=True).observation() is None
