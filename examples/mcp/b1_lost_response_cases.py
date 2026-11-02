"""
StateGuard's idempotency-key adapter on the B1 lost-response scenario of
modelcontextprotocol/modelcontextprotocol#3394.

    pip install "mcp==2.2.0"   # or 2.3.0; Python 3.11+
    pip install "sguard @ git+https://github.com/dood1ebyte/stateguard@<commit>"
    python examples/mcp/b1_lost_response_cases.py [--json results.json]

The fault is the one in arjun2075/mcp-b1-lost-response-repro@6373793: the real
``mcp`` Python SDK (``ClientSession`` -> ``MCPServer`` over memory streams), with
a relay that drops the first ``tools/call`` response. A response reaches the
relay only after the server's handler has returned, so the effect is committed
before the drop. The client sees its read timeout and resends once.

Each case asserts two things separately:

- the authoritative effect count, from an in-process ledger that only the tool
  writes (or, for ``transactional``, a table in the store's database);
- the client-visible outcome of the resend: a result and its
  ``io.stateguard/idempotencyOutcome``, or a JSON-RPC error and its code.

The adapter is one middleware (``KeyedTools(store).middleware()``). A client
opts in by sending ``params._meta["io.stateguard/idempotencyKey"]``. A call
without a key is served unchanged.

Cases:

``baseline``                 no adapter, no key: 2 effects (the B1 duplicate)
``adapter_without_key``      adapter installed, client sends no key: 2 effects;
                             the adapter changes nothing for unkeyed calls
``stable_key_replay``        same key on both attempts: 1 effect; the resend
                             gets the stored result (``replayed``), same effect_id
``fresh_key_per_attempt``    a new key per attempt is a new operation: 2 effects;
                             distinct keys are never collapsed
``changed_arguments``        same key, different arguments: 1 effect; the resend
                             is rejected (-32602, ``rejected_mismatch``)
``record_lost_unresolved``   the effect commits, then writing its result to the
                             store fails (TEST-ONLY fault); the resend is rejected
                             with ``in_doubt`` (-32010): 1 effect, "retry
                             rejected; original outcome unresolved"
``record_lost_recovered``    as above, but the effect carries its key and the
                             server has a recovery check: 1 effect, the resend
                             gets the original result (``recovered``)
``transactional``            the effect is a row in the store's own database,
                             committed in one transaction with its result: the
                             record cannot be lost after the effect; 1 effect,
                             ``replayed``
``concurrent_same_key``      two same-key calls in flight at once (no drop):
                             1 effect; one ``executed``, the other ``replayed``

Evidence boundary: in-process, memory streams, deterministic; the fault and
the record failure are injected by this script. It does not cover durable
recovery across a process crash (StateGuard's own suite kills real server
processes for that) or more than one server instance.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import platform
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

import anyio
import mcp_types as types
from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp_types.jsonrpc import JSONRPCError, JSONRPCRequest, JSONRPCResponse

from stateguard.adapters.mcp.idempotency import (
    IDEMPOTENCY_KEY,
    IDEMPOTENCY_OUTCOME,
    IdempotencyStore,
)
from stateguard.adapters.mcp.idempotency_sdk import KeyedTools, Recovery, current_key

ARGUMENTS = {"item": "widget", "quantity": 2}
SHUTDOWN_GRACE_SECONDS = 2.0


# -- the effect and its oracle ---------------------------------------------------


@dataclass
class Ledger:
    """Append-only effect ledger: one row per execution. The only oracle."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    def commit(self, item: str, quantity: int) -> dict[str, str]:
        effect_id = uuid.uuid4().hex
        self.rows.append(
            {"effect_id": effect_id, "item": item, "quantity": quantity, "key": current_key()}
        )
        return {"effect_id": effect_id, "result": "committed"}


def key_meta(key: str) -> types.RequestParamsMeta:
    """``params._meta`` carrying the key (``RequestParamsMeta`` admits extra items)."""
    return cast(types.RequestParamsMeta, {IDEMPOTENCY_KEY: key})


def order_result(effect: dict[str, str]) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(effect))])


class ResultWriteFailsOnce(IdempotencyStore):
    """
    TEST-ONLY fault: the first write of a result to the store fails, after the
    effect has committed. The store's own ``complete`` runs; SQLite refuses its
    UPDATE, which the store reports as unavailable, as it would a full disk.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._armed = True
        self._denying = False
        #: The fault happened: SQLite refused the result's UPDATE.
        self.fired = False
        super().__init__(*args, **kwargs)

    def complete(self, key: str, result: Any) -> None:
        self._denying, self._armed = self._armed, False
        try:
            super().complete(key, result)
        finally:
            self._denying = False

    def connect(self) -> sqlite3.Connection:
        db = super().connect()
        if self._denying:

            def deny_updates(action: int, *_: Any) -> int:
                if action == sqlite3.SQLITE_UPDATE:
                    self.fired = True
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            db.set_authorizer(deny_updates)
        return db


# -- one case --------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    name: str
    adapter: str | None  # None, "two_phase", "recover" or "transactional"
    keys: tuple[str | None, str | None]  # the key each attempt sends
    expect_effects: int
    expect_final: str  # the resend's client-visible outcome
    drop_first_response: bool = True
    record_fails: bool = False
    second_arguments: dict[str, Any] | None = None
    concurrent: bool = False


@dataclass
class Attempt:
    jsonrpc_id: Any = None
    seen: str = ""  # "timeout", "result" or "error"
    outcome: str | None = None  # the idempotency outcome, when the answer names one
    code: int | None = None
    effect_id: str | None = None


@dataclass
class CaseResult:
    case: str
    effects: int
    attempts: list[Attempt]
    final: str
    expected_effects: int
    expected_final: str
    #: Every effect_id the client was answered with is the one committed effect's.
    same_effect_id: bool
    #: For the record-lost cases: the injected record failure really happened.
    fault_fired: bool | None
    passed: bool


K1, K2 = "op-" + uuid.uuid4().hex[:12], "op-" + uuid.uuid4().hex[:12]

CASES = [
    Case("baseline", None, (None, None), 2, "result:none"),
    Case("adapter_without_key", "two_phase", (None, None), 2, "result:none"),
    Case("stable_key_replay", "two_phase", (K1, K1), 1, "result:replayed"),
    Case("fresh_key_per_attempt", "two_phase", (K1, K2), 2, "result:executed"),
    Case(
        "changed_arguments",
        "two_phase",
        (K1, K1),
        1,
        "error:-32602:rejected_mismatch",
        second_arguments={"item": "widget", "quantity": 3},
    ),
    Case(
        "record_lost_unresolved",
        "two_phase",
        (K1, K1),
        1,
        "error:-32010:in_doubt",
        record_fails=True,
    ),
    Case("record_lost_recovered", "recover", (K1, K1), 1, "result:recovered", record_fails=True),
    Case("transactional", "transactional", (K1, K1), 1, "result:replayed"),
    Case(
        "concurrent_same_key",
        "two_phase",
        (K1, K1),
        1,
        "result:executed+replayed",
        drop_first_response=False,
        concurrent=True,
    ),
]


@dataclass
class Rig:
    server: MCPServer
    #: The effect ids the oracle holds, in commit order.
    committed: Callable[[], list[str]]
    #: Set once both concurrent calls have been forwarded to the server.
    gate: anyio.Event
    store: IdempotencyStore | None = None


def build(case: Case, workdir: Path) -> Rig:
    ledger = Ledger()
    gate = anyio.Event()
    db = workdir / "keys.db"
    store: IdempotencyStore | None = None

    def ledger_ids() -> list[str]:
        return [row["effect_id"] for row in ledger.rows]

    committed: Callable[[], list[str]] = ledger_ids
    if case.adapter is None:
        server = MCPServer(name="b1-stateguard")
    else:
        store_type = ResultWriteFailsOnce if case.record_fails else IdempotencyStore
        store = store_type(db, scope="b1", exclusive=case.adapter == "recover")
        keyed = KeyedTools(store)
        if case.adapter == "two_phase":
            keyed.two_phase("create_order")
        elif case.adapter == "recover":

            def ledger_has_key(key: str, arguments: dict[str, Any]) -> Recovery:
                for row in ledger.rows:
                    if row["key"] == key:
                        return Recovery.found(
                            order_result({"effect_id": row["effect_id"], "result": "committed"})
                        )
                return Recovery.absent()

            keyed.two_phase("create_order", recover=ledger_has_key)
        else:

            def insert_order(
                conn: sqlite3.Connection, arguments: dict[str, Any]
            ) -> types.CallToolResult:
                effect_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO orders (effect_id, item, quantity) VALUES (?, ?, ?)",
                    (effect_id, arguments["item"], arguments["quantity"]),
                )
                return order_result({"effect_id": effect_id, "result": "committed"})

            with contextlib.closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE orders (effect_id TEXT, item TEXT, quantity INTEGER)")
                conn.commit()
            keyed.transactional("create_order", insert_order)

            def table_ids() -> list[str]:
                with contextlib.closing(sqlite3.connect(db)) as conn:
                    return [
                        row[0]
                        for row in conn.execute("SELECT effect_id FROM orders ORDER BY rowid")
                    ]

            committed = table_ids

        server = MCPServer(name="b1-stateguard", middleware=[keyed.middleware()])

    @server.tool()
    async def create_order(item: str, quantity: int) -> types.CallToolResult:
        if case.concurrent:
            # Hold the first call until the second has been forwarded, so both
            # are in flight before either is answered.
            with anyio.move_on_after(5):
                await gate.wait()
            await anyio.sleep(0.2)
        return order_result(ledger.commit(item, quantity))

    return Rig(server, committed, gate, store)


async def run_case(case: Case, read_timeout: float) -> CaseResult:
    with tempfile.TemporaryDirectory() as tmp:
        rig = build(case, Path(tmp))
        gate = rig.gate
        attempts = [Attempt(), Attempt()]
        calls: list[Any] = []

        streams = create_client_server_memory_streams()
        (c_read, c_write), (s_read, s_write) = await streams.__aenter__()
        c2r_send, c2r_recv = anyio.create_memory_object_stream[SessionMessage](256)
        r2c_send, r2c_recv = anyio.create_memory_object_stream[SessionMessage | Exception](256)
        server_done = anyio.Event()

        async def client_to_server() -> None:
            async with c2r_recv, c_write:
                async for msg in c2r_recv:
                    if (
                        isinstance(msg, SessionMessage)
                        and isinstance(msg.message, JSONRPCRequest)
                        and msg.message.method == "tools/call"
                    ):
                        calls.append(msg.message.id)
                        if len(calls) == 2:
                            gate.set()
                    await c_write.send(msg)

        async def server_to_client() -> None:
            dropped = False
            async with c_read, r2c_send:
                async for msg in c_read:
                    if (
                        case.drop_first_response
                        and not dropped
                        and isinstance(msg, SessionMessage)
                        and isinstance(msg.message, JSONRPCResponse | JSONRPCError)
                        and calls
                        and msg.message.id == calls[0]
                    ):
                        dropped = True  # after commit: the handler has returned
                        continue
                    await r2c_send.send(msg)

        async def run_server() -> None:
            lowlevel: Server[Any] = rig.server._lowlevel_server
            try:
                await lowlevel.run(
                    s_read,
                    s_write,
                    lowlevel.create_initialization_options(),
                    raise_exceptions=False,
                )
            finally:
                server_done.set()

        async def call(session: ClientSession, attempt: int) -> None:
            key = case.keys[attempt]
            arguments = (
                case.second_arguments if attempt == 1 and case.second_arguments else ARGUMENTS
            )
            seen = attempts[attempt]
            try:
                result = await session.call_tool(
                    "create_order",
                    arguments,
                    read_timeout_seconds=read_timeout,
                    meta=key_meta(key) if key else None,
                )
            except MCPError as exc:
                seen.code = exc.code
                data = exc.data if isinstance(exc.data, dict) else {}
                seen.outcome = data.get(IDEMPOTENCY_OUTCOME)
                seen.seen = "timeout" if exc.code == types.REQUEST_TIMEOUT else "error"
                return
            assert isinstance(result, types.CallToolResult)
            seen.seen = "result"
            seen.outcome = (result.meta or {}).get(IDEMPOTENCY_OUTCOME)
            text = next(b.text for b in result.content if isinstance(b, types.TextContent))
            seen.effect_id = json.loads(text).get("effect_id")

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_server)
            tg.start_soon(client_to_server)
            tg.start_soon(server_to_client)
            session = ClientSession(r2c_recv, c2r_send)
            await session.__aenter__()
            await session.initialize()
            if case.concurrent:
                async with anyio.create_task_group() as both:
                    both.start_soon(call, session, 0)
                    both.start_soon(call, session, 1)
            else:
                await call(session, 0)
                await call(session, 1)
            await session.__aexit__(None, None, None)
            await c2r_send.aclose()
            with anyio.move_on_after(SHUTDOWN_GRACE_SECONDS):
                await server_done.wait()
            tg.cancel_scope.cancel()
        await streams.__aexit__(None, None, None)

        for attempt, call_id in zip(attempts, calls, strict=False):
            attempt.jsonrpc_id = call_id
        committed = rig.committed()
        fault_fired = rig.store.fired if isinstance(rig.store, ResultWriteFailsOnce) else None
        if rig.store is not None:
            rig.store.close()
    effects = len(committed)

    if case.concurrent:
        final = "result:" + "+".join(sorted(str(a.outcome) for a in attempts))
    else:
        last = attempts[1]
        final = (
            f"result:{last.outcome or 'none'}"
            if last.seen == "result"
            else f"{last.seen}:{last.code}:{last.outcome}"
        )
    # A replayed or recovered answer must carry the committed effect's own id.
    answered = [a.effect_id for a in attempts if a.effect_id]
    same_effect_id = bool(answered) and effects == 1 and all(i == committed[0] for i in answered)
    passed = effects == case.expect_effects and final == case.expect_final
    if "replayed" in case.expect_final or "recovered" in case.expect_final:
        passed = passed and same_effect_id
    if case.record_fails:
        passed = passed and fault_fired is True
    return CaseResult(
        case.name,
        effects,
        attempts,
        final,
        case.expect_effects,
        case.expect_final,
        same_effect_id,
        fault_fired,
        passed,
    )


async def run_all(read_timeout: float) -> list[CaseResult]:
    return [await run_case(case, read_timeout) for case in CASES]


def environment() -> dict[str, str]:
    def version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "not installed (source tree)"

    return {"python": platform.python_version(), "mcp": version("mcp"), "sguard": version("sguard")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", help="also write the results here")
    parser.add_argument("--read-timeout", type=float, default=1.0, help="client read timeout (s)")
    args = parser.parse_args(argv)
    results = anyio.run(run_all, args.read_timeout)
    env = environment()
    print(f"python {env['python']}  mcp {env['mcp']}  sguard {env['sguard']}")
    print(f"{'case':<26} {'effects':>7}  {'resend (client-visible)':<34} verdict")
    for r in results:
        verdict = (
            "PASS" if r.passed else f"FAIL (expected {r.expected_effects}, {r.expected_final})"
        )
        print(f"{r.case:<26} {r.effects:>7}  {r.final:<34} {verdict}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"environment": env, "cases": [asdict(r) for r in results]}, indent=1)
        )
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
