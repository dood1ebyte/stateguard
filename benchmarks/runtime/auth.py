"""
Auth-failure classification through the relay (TASKMAP, T11).

    python benchmarks/runtime/auth.py [--json OUT]

Each case is one ``tools/call`` whose first answer is fixed in advance (an
auth failure in one of its wire shapes, or a non-auth failure dressed in
auth-like words) and whose second answer, should the relay retry, is a
success. Every case runs in two relay configurations:

* ``auto`` -- StateGuard acting, retry in shadow (it decides, logs, never
  resends);
* ``auto_retry`` -- the operator's configuration under which a retry
  actually happens: ``--retry auto``, a 0.3 s deadline, and ``search``
  declared ``read_only`` (``write`` stays undeclared).

and reports, per case: the class the relay logged, retries sent, executions,
whether the client received exactly what the server answered, and whether
anything turned an auth failure into a success.

Fault injection on a synthetic workload -- not real-world evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    __package__ = "runtime"

from stateguard.adapters.mcp import EffectPolicy, classify_response  # noqa: E402

from .harness import DELAY_SECONDS, RelayMode, Rig, ScriptedServer, ServerTool  # noqa: E402

ARMS = ("auto", "auto_retry")
DEADLINE_SECONDS = 0.3

TOOLS = [
    {
        "name": "search",
        # A description full of auth words: never read by the classifier.
        "description": "Search tickets. Requires authentication; the token needs read permission.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "token_budget": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "write",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]


def rpc_error(message: str, code: int = -32603, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"error": error}


def tool_error(text: str) -> dict[str, Any]:
    return {
        "result": {
            "resultType": "complete",
            "content": [{"type": "text", "text": text}],
            "isError": True,
        }
    }


@dataclass(frozen=True)
class Case:
    name: str
    expected: str  # the FailureClass value the relay must log
    tool: str
    arguments: dict[str, Any]
    #: The first answer (the fault); every later attempt succeeds.
    first: dict[str, Any] | None
    #: Answer only after DELAY_SECONDS (past the auto_retry deadline).
    late: bool = False
    note: str = ""
    #: The expected class where no deadline turns the slow answer into UNKNOWN.
    expected_without_deadline: str | None = None

    def expected_in(self, arm: str) -> str:
        if arm != "auto_retry" and self.expected_without_deadline is not None:
            return self.expected_without_deadline
        return self.expected


CASES: tuple[Case, ...] = (
    # -- auth failures, in each wire shape --------------------------------------
    Case(
        "rpc_reason_phrase",
        "authentication",
        "search",
        {"query": "a"},
        rpc_error("Unauthorized: token expired", code=-32001),
        note="JSON-RPC error; -32001 is the SDK's request timeout, so only the text decides",
    ),
    Case(
        "rpc_structured_401",
        "authentication",
        "search",
        {"query": "b"},
        rpc_error("Request failed", data={"status": 401, "error": "invalid_token"}),
    ),
    Case(
        "rpc_structured_403",
        "authorization",
        "search",
        {"query": "c"},
        rpc_error("Request failed", data={"status": 403, "error": "insufficient_scope"}),
    ),
    Case(
        "tool_result_401",
        "authentication",
        "search",
        {"query": "d"},
        tool_error("GET https://api.example.com/v1/tickets: 401 Unauthorized"),
        note="an upstream API's 401 delivered as a tool result: retried before T11",
    ),
    Case(
        "tool_result_403",
        "authorization",
        "search",
        {"query": "e"},
        tool_error("HTTP Error 403: Forbidden"),
    ),
    Case(
        "tool_result_bearer",
        "authentication",
        "search",
        {"query": "f"},
        tool_error('upstream refused: WWW-Authenticate: Bearer error="invalid_token"'),
    ),
    # -- not auth, whatever the words ------------------------------------------
    Case(
        "fp_permission_word",
        "tool",
        "search",
        {"query": "g"},
        tool_error("Permission denied: field 'token' needs access_level=admin"),
    ),
    Case(
        "fp_business_authorization",
        "tool",
        "search",
        {"query": "h"},
        tool_error("Order rejected: the card's authorization hold expired"),
    ),
    Case(
        "fp_validation_access",
        "tool",
        "search",
        {"query": "i"},
        tool_error("Validation failed: access must be one of [read, write]"),
    ),
    Case(
        "fp_forbidden_city",
        "tool",
        "search",
        {"query": "j"},
        tool_error("403 Forbidden City tours are sold out"),
    ),
    Case(
        "fp_rpc_timeout_code",
        "protocol",
        "search",
        {"query": "k"},
        rpc_error("Request timed out", code=-32001),
        note="the SDK's -32001 is a timeout, not auth",
    ),
    Case(
        "fp_transport_auth_words",
        "transport",
        "search",
        {"query": "l"},
        rpc_error(
            "auth gateway unreachable: request not sent",
            code=-32000,
            data={"io.stateguard/executionState": "not_sent"},
        ),
    ),
    Case(
        "fp_unknown_with_401",
        "unknown",
        "search",
        {"query": "m"},
        rpc_error(
            "401 Unauthorized",
            code=-32000,
            data={"io.stateguard/executionState": "unknown"},
        ),
        note="an upstream relay's 'outcome unknown': never reclassified by its text",
    ),
    Case(
        "fp_token_argument",
        "none",
        "search",
        {"query": "n", "token_budget": 5},
        None,
        note="an argument named like a credential, and a successful call",
    ),
    # -- UNKNOWN locally: the auth answer arrives after the deadline ------------
    Case(
        "late_auth_after_deadline",
        "unknown",
        "write",
        {"text": "o"},
        tool_error("401 Unauthorized"),
        late=True,
        expected_without_deadline="authentication",
        note="auto_retry: resolved UNKNOWN at the deadline; the late 401 is dropped, not "
        "classified. auto (no deadline): just a slow 401",
    ),
)


@dataclass
class CannedServer(ScriptedServer):
    """Answers each call's first attempt with a fixed reply; later attempts succeed."""

    replies: dict[str, Case] = field(default_factory=dict)
    seen: dict[str, int] = field(default_factory=dict)

    def _call(
        self, identifier: Any, params: dict[str, Any], message: dict[str, Any], writer: IO[bytes]
    ) -> bool:
        arguments = params.get("arguments")
        key = json.dumps(arguments, sort_keys=True)
        case = self.replies.get(key)
        attempt = self.seen.get(key, 0)
        self.seen[key] = attempt + 1
        if case is None or case.first is None or attempt > 0:
            return super()._call(identifier, params, message, writer)
        if case.late:
            time.sleep(DELAY_SECONDS)
        self._write(writer, json.dumps({"jsonrpc": "2.0", "id": identifier, **case.first}).encode())
        return True


def run_case(case: Case, arm: str) -> dict[str, Any]:
    server = CannedServer(
        [ServerTool(t) for t in TOOLS],
        replies={json.dumps(case.arguments, sort_keys=True): case},
    )
    retrying = arm == "auto_retry"
    rig = Rig(
        server,
        RelayMode.AUTO,
        call_deadline=DEADLINE_SECONDS if retrying else None,
        effects=EffectPolicy({"search": "read_only"}) if retrying else None,
        retry="auto" if retrying else "shadow",
    )
    try:
        rig.request("server/discover")
        rig.wait_synced()
        answer = rig.call(case.tool, case.arguments, timeout=3.0)
        time.sleep(DELAY_SECONDS + 0.1 if case.late else 0.05)
    finally:
        rig.close()
    identifier = rig.sent_ids[-1]
    sent = [
        m
        for m in (json.loads(line) for line in rig.all_received)
        if m.get("method") == "tools/call"
    ]
    auth_events = [e for e in rig.audit if e.get("event") == "auth_failure"]
    logged = auth_events[0]["failure"] if auth_events else None
    state = rig.relay.ledger.get(identifier)
    unknown = state is not None and state.state.value == "unknown"
    # The class as the relay saw it: an auth event, else what the reply is.
    if logged is not None:
        seen = logged
    elif unknown:
        seen = "unknown"
    elif case.first is None:
        seen = "none"
    else:
        seen = classify_response({"jsonrpc": "2.0", "id": identifier, **case.first}).failure.value
    expected_answer = (
        {"jsonrpc": "2.0", "id": identifier, **case.first} if case.first is not None else None
    )
    client_success = (
        isinstance(answer, dict)
        and isinstance(answer.get("result"), dict)
        and answer["result"].get("isError") is not True
    )
    expected = case.expected_in(arm)
    return {
        "case": case.name,
        "arm": arm,
        "expected": expected,
        "classified": seen,
        "correct": seen == expected,
        "auth_events": len(auth_events),
        "sends": len(sent),
        "retries": max(0, len(sent) - 1),
        "executions": len(rig.all_executed),
        "unknown": unknown,
        "client_success": client_success,
        "delivered_as_sent": (
            answer == expected_answer if expected_answer is not None and not unknown else None
        ),
    }


def measure() -> dict[str, Any]:
    rows = [run_case(case, arm) for case in CASES for arm in ARMS]
    auth = [r for r in rows if r["expected"] in ("authentication", "authorization")]
    not_auth = [r for r in rows if r["expected"] not in ("authentication", "authorization")]
    return {
        "provenance": "fault injection on a synthetic workload -- not real-world evidence",
        "cases": len(CASES),
        "runs": len(rows),
        "correctly_classified": sum(1 for r in rows if r["correct"]),
        "auth_runs": len(auth),
        "auth_retried": sum(r["retries"] for r in auth),
        "auth_turned_into_success": sum(1 for r in auth if r["client_success"]),
        "auth_delivered_as_sent": sum(1 for r in auth if r["delivered_as_sent"]),
        "false_positive_auth": sum(1 for r in not_auth if r["auth_events"]),
        "unknown_reclassified_as_auth": sum(
            1 for r in rows if r["expected"] == "unknown" and r["auth_events"]
        ),
        "rows": rows,
    }


def render(summary: dict[str, Any]) -> str:
    out = [
        "StateGuard auth-failure classification (T11)",
        f"CORPUS: {summary['provenance']}",
        f"{'case':28} {'arm':10} {'expected':15} {'classified':15} {'retries':>7} "
        f"{'exec':>4} {'success':>7} {'as sent':>7}",
    ]
    for r in summary["rows"]:
        out.append(
            f"{r['case']:28} {r['arm']:10} {r['expected']:15} {r['classified']:15} "
            f"{r['retries']:>7} {r['executions']:>4} {str(r['client_success']):>7} "
            f"{str(r['delivered_as_sent']):>7}"
        )
    out += [
        "",
        f"correctly classified {summary['correctly_classified']}/{summary['runs']}  "
        f"auth runs {summary['auth_runs']}: retried {summary['auth_retried']}, "
        f"turned into success {summary['auth_turned_into_success']}, "
        f"delivered as sent {summary['auth_delivered_as_sent']}/{summary['auth_runs']}",
        f"false-positive auth {summary['false_positive_auth']}  "
        f"UNKNOWN reclassified as auth {summary['unknown_reclassified_as_auth']}",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    summary = measure()
    print(render(summary))
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0 if summary["correctly_classified"] == summary["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
