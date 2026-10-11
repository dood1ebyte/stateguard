# StateGuard runtime reliability — TASKMAP

This is the durable execution state. Update it at every checkpoint. **Code and
tests are ground truth. This file is a record of them, not a substitute.**

- **Direction.** StateGuard is an AI runtime reliability layer. MCP is the
  first deployment surface. Contract/schema repair is the first wedge, not the
  boundary.
- **Goal.** Measurably prevent or recover a defensible share of MCP runtime
  failures, and escalate what StateGuard cannot safely own.
- **Branch.** `claude/serene-bohr-o28snr`. Commit locally; never push, merge or
  touch `main` without instruction.

Evidence labels:

- **VERIFIED** — checked by running code or tests in this repository.
- **INFERRED** — follows from code that was read but not exercised.
- **UNVERIFIED** — not checked.

---

## Failure taxonomy (A–K, Z)

No taxonomy document existed in the repository at reconciliation
(2026-10-11). These categories are defined here from the target failure
surface. Rename them if the reference taxonomy differs; the code keys on these
letters.

| Cat | Name | Examples | Owner-boundary verdict |
|---|---|---|---|
| A | Contract / argument schema | renamed param, string-for-number, enum case, missing required | **OWN** (repair/refuse/escalate). Exists. |
| B | Tool discovery / staleness | schema changed mid-session, tool removed, stale listing | **OWN** (detect, re-list, never use stale). Partial. |
| C | Protocol / conformance | malformed envelope, protocol-invalid message, version mismatch | **OWN detect; pass through** — never rewrite protocol. |
| D | Session / capability | stateless vs `initialize`, capability absent, discover failure | **OBSERVE + classify**; no session mutation. |
| E | Transport / connection | stdio child killed, connection refused, EOF mid-message | **OWN reconnect only when no call is in flight**; in-flight calls escalate. |
| F | Timeout / transient / rate limit | hang, slow response, 429 + Retry-After | **OWN** only for not-executed or operator-declared-idempotent calls. |
| G | Tool execution failure | `isError: true`, server exception | **CLASSIFY + surface**; retry only per effect model. |
| H | Result integrity | truncation, `outputSchema` violation, duplicate response | **DETECT (Shadow-only)**; never repair a result into success. |
| I | Ambiguous completion | timeout after send, connection lost after send | **ESCALATE** unless the effect model makes retry safe. |
| J | Runtime state inconsistency | duplicate execution, response for unknown id, double-send | **DETECT + contain** (ledger). |
| K | Auth / authorization signal | expired auth, 401/403-equivalent | **CLASSIFY + operator callback only**; never touch credentials. |
| Z | StateGuard-induced | false repair, incorrect refusal/retry, crash, added outage, stale-schema decision | **MEASURED in every run.** |

---

## Reconciliation — status table (2026-10-11)

Baseline before any change: 3,538 tests pass, 97% line coverage of `src/`
(VERIFIED). The relay (`examples/`) is outside coverage.

| Capability | Status | Evidence | Shadow/Auto | Notes |
|---|---|---|---|---|
| TrustPolicy (APPLY/AMBIGUOUS/REJECT by risk band, contradiction → AMBIGUOUS) | COMPLETE | `core/trust.py`; calibration 100% applied precision on *its* corpus | both | Precision claim is corpus-bounded (see A-UNSAFE). |
| Shadow/Auto outcome machinery + forwarding gate | COMPLETE | `adapters/mcp/outcomes.py`; truth table, properties | both | Shadow never blocks (VERIFIED). |
| ToolRegistry + server-scoped identity | COMPLETE | `adapters/mcp/registry.py`; `test_registry.py` | n/a | `(name, server)` keys; never guesses across servers. |
| Schema/contract fingerprints | COMPLETE | `jsonschema/identity.py` | n/a | |
| Contract diff (sound COMPATIBLE) | COMPLETE | `jsonschema/diff.py`; mutation properties | n/a | |
| Stale-schema prevention (library) | COMPLETE | registry replaces unconditionally; cache keys on content | n/a | Correct *given* a fresh listing. |
| Stale-listing detection (relay, protocol 2026-07-28) | COMPLETE (T3, `3be1e3e`) — was **UNSAFE** | SDK 2.3.0 `mcp/server/mcpserver/context.py:136`, `mcp/shared/subscriptions.py:84`; relay never opens `subscriptions/listen` | auto | **VERIFIED from spec source**: list-changed rides only on listen streams. If the client does not subscribe, the relay validates against a stale listing indefinitely. |
| A: opposite-meaning rename guard | COMPLETE (T1, `dc67d52`) | was: 102/132 single-word opposite renames FORWARDED. Now 0/152 (probe), 0/118 (calibration family) | both | VERIFIED. `fuzzy.opposed_names` → contradiction → TrustPolicy AMBIGUOUS. |
| JSON Schema 2020-12 coverage | PARTIAL (T5, `3d93145`) | probe: 7/22 previously refused constructs now read (annotations, `true`, single-branch `allOf`, `multipleOf`, `uniqueItems`); `test_2020_12_conformance.py` differential vs `jsonschema` 0 disagreements | n/a | Still refused, deliberately: unknown keywords (some loosen, e.g. `nullable`), multi-branch `allOf`, `not`, conditionals, `false`, object-level assertions (`min/maxProperties`, `dependentRequired`), `prefixItems`, `contains`. Refusal fails open in the relay. |
| Relay fail-open on unreadable schema | COMPLETE (T2, `384fe2a`) — was **UNSAFE** | default now `--unreadable forward`; `test_stdio_relay_protocol.py` | both | VERIFIED. |
| Relay fail-open on internal error | COMPLETE (T2, `384fe2a`) — was **UNSAFE** | `test_relay_fail_open.py`: exception / RecursionError / gate failure / sync failure / client-message failure → forwarded once, unmodified, logged `category: "Z"` | both | VERIFIED. Single-write structure: no StateGuard-made double send. |
| Kill switch | COMPLETE (T2) | `--passthrough`; `test_passthrough_is_a_byte_relay` | — | Also the raw-baseline arm. |
| Protocol-aware refusal (`resultType`) | COMPLETE | `jsonrpc.tool_error_response`; SDK e2e test | auto | |
| Stdio relay | PARTIAL | `examples/mcp/stdio_proxy.py`; SDK e2e + scripted + in-process rig tests | both | Example, not a gateway: one server per relay. |
| Safe server restart (E) | COMPLETE (T12, `9d1f401`, `24011f5`) | `test_relay_restart.py` (19, incl. 40-example property), real-subprocess e2e, `recovery.py` (M10) | **opt-in** (`--restart N`), Auto only; shadow never restarts | Default policy idle-only. Registry cleared on restart. 0 replays / 0 duplicate executions in every measurement. |
| Audit log | PARTIAL | relay JSON lines, value-free; `category` on Z / E / I / J events | both | Not exported as structured telemetry. |
| Telemetry hooks | PARTIAL | `telemetry/hooks.py` `ITelemetryHook` (repair events only) | n/a | No runtime-event hooks. |
| Effect classification | COMPLETE (T7, `ad6f041`) | `adapters/mcp/effects.py`; `test_effects.py` exhaustive (1,344 cases) | n/a | Operator-declared only; default UNKNOWN; annotations = recorded hints, never decide (VERIFIED incl. contradicting hints). |
| Outcome / invocation ledger | COMPLETE (T6, `c1c9acc`) | `adapters/mcp/ledger.py`; `test_ledger.py` (13 unit + 600-example property) | both | Library primitive; the relay is its first user. |
| Execution state | COMPLETE (T6) | `ExecutionState`: PENDING / ANSWERED_LOCALLY / SENT / COMPLETED / UNKNOWN | both | UNKNOWN is first-class; never rounded. |
| Duplicate-response containment (J) | COMPLETE (T6) | `test_relay_ledger.py`; M5 J 8 → 0 | auto drops / shadow logs | |
| Surface unknown outcome on transport close (E/I) | COMPLETE (T6) | `test_relay_ledger.py`; M5 hidden failures 53 → 25 | auto answers / shadow logs | JSON-RPC `-32000` error with `data["io.stateguard/executionState"] = "unknown"`. |
| Call deadline (F/H/I) | PARTIAL (T6) — opt-in only | M5: hidden 25 → 1, but **8 Z** on slow successes | auto, opt-in | Off by default; the operator must set it above legitimate tool latency. |
| Retry decision logic | COMPLETE (T8, `ad6f041`, `88a5afa`) | `decide_retry`; relay `--retry`; `test_relay_retry.py` (server ledger checked) | **Shadow by default**; Auto opt-in with `--effects` | Never after auth/protocol error; one retry max; undeclared + unknown outcome → ESCALATE. |
| Circuit breaking | MISSING | — | — | |
| Result validation (`outputSchema`) | COMPLETE, detection only (T10) | `adapters/mcp/results.py`; `test_results.py`, `test_relay_results.py`; M8: 8/8 injected detected, 0 elsewhere | observe in all modes; **never alters** | Missing `structuredContent` = advisory (2026-07-28 text: optional). Non-object `outputSchema` not checked (advisory). |
| Auth signal classification | MISSING | — | — | |
| Fault injection | PARTIAL (T4, `198aa25`) | `benchmarks/runtime/harness.py`: 11 server fault actions; StateGuard-internal faults in `test_relay_fail_open.py` | — | Seeded; one fault per invocation. Missing: 429/Retry-After (no HTTP transport), delayed-but-arriving responses, session faults (D). |
| Property tests (repair layer) | COMPLETE | `test_mcp_properties.py`, `test_diff.py` | — | None for runtime primitives (do not exist). |
| Replay / benchmark | PARTIAL | `stateguard replay`, `mcp_bench.py`, `calibrate.py`, **`benchmarks/runtime/measure.py` (before/after per category, 4 arms)** | — | No record/replay of real traffic yet (T13); the corpus interface is pluggable. |
| Category Z tracking | PARTIAL (T2, T4) | relay logs `category: "Z"`; `measure.py` counts StateGuard-induced failures, incorrect repair/refusal, duplicates, crashes per category | — | Counted per measurement run; not yet exported as runtime telemetry. |

**Contradictions found.** The docs claimed applied precision of 100%
(CHANGELOG, calibration). That is true only of the calibration corpus, which
contains no single-word opposite pairs. The relay README says it "speaks both
protocol generations"; that holds for message shapes but not for
list-changed delivery at 2026-07-28.

**Known-risk re-verification.**

- **A** (false repair on opposites): **UNSAFE, VERIFIED.**
- **B** (2020-12): coverage gap **VERIFIED**; traffic-blocking only through the
  relay default.
- **C** (protocol): stale-listing **VERIFIED**; message shapes OK (SDK e2e).
- **D** (annotations): **safe, VERIFIED** — never read by a decision.
- **E** (results): **not owned, VERIFIED** — pass through.

---

## Tasks (dependency-ordered)

| # | Task | Priority class | Status | Depends on |
|---|---|---|---|---|
| T1 | Opposite-meaning rename guard (extend contradiction evidence) + calibration family | 1 unsafe defect | DONE `dc67d52` | — |
| T2 | Relay call path fails open: unexpected error / gate failure → raw passthrough + Z record; `--unreadable` default `forward`; `--passthrough` kill switch | 1 unsafe defect | DONE `384fe2a` | — |
| T3 | Stale-listing safety at 2026-07-28: never act (repair/refuse/escalate) on a listing not known fresh — re-list then re-decide | 2 spec compat | DONE `3be1e3e` | T2 |
| T4 | Measurement vertical slice: seeded workload × fault injectors × arms (raw, naive retry, shadow, auto) → per-category report incl. Z | 3 measurement | DONE `198aa25` | T1–T3 |
| T5 | JSON Schema 2020-12 coverage: annotation keywords, simple assertions, single-branch `allOf`, `true` subschema; differential-verified | 2/4 | DONE `3d93145` | T4 |
| T6 | Execution state + invocation ledger; relay: dedupe (J), surface unknown on transport close (E/I), opt-in deadline | 5 | DONE `c1c9acc` | T4 |
| T7 | Effect model: operator-declared only; annotations recorded as untrusted hints | 5 | DONE `ad6f041` | T6 |
| T8 | Retry decision (TrustDecision vocabulary + Shadow/Auto): operator-declared read-only/idempotent only; UNKNOWN on undeclared → ESCALATE; relay integration | 5 | DONE `88a5afa` | T6, T7 |
| T9 | Circuit breaker per (server, tool), Shadow-first | 5 | TODO | T6 |
| T10 | Result integrity: `outputSchema` / `structuredContent` validation, detection only | 4 | DONE | T4 |
| T11 | Auth-signal classification + operator callback | 4 | TODO | T6 |
| T12 | Safe server restart: idle death only by default; in-flight → UNKNOWN, never replayed; re-initialize + re-list + registry reset; bounded; session recovery workload | 5 | DONE (`9d1f401`, `24011f5`, + measurement commit) | T6, T8 |
| T13 | Record/replay with redaction; pluggable corpus | 6 | TODO | T4 |

## Primitive records

Every new primitive gets a record here (ownable / deterministic / state /
safety boundary / Z modes / Shadow-first / Auto conditions / evidence).

### P-A1 Opposite-meaning rename guard (T1)

- **Ownable:** yes — it narrows an existing repair; it adds no action.
- **Deterministic:** yes — a fixed lexicon plus an affix rule; no scoring, no
  model.
- **State:** none.
- **Safety boundary:** it can only turn APPLY into AMBIGUOUS (ESCALATE). It can
  never create a repair.
- **Z modes:** over-flagging a real typo as an opposite turns a correct repair
  into an escalation (a missed repair, never a wrong one). Measured by the
  existing must-apply calibration family.
- **Shadow-first:** not needed — it only removes Auto actions.
- **Auto:** active in both modes (it gates APPLY).
- **Evidence:** M1 below. No Auto *promotion* is involved: the rule only
  withholds.

### P-T2 Relay fail-open (T2)

- **Ownable:** yes. It is StateGuard's own failure handling.
- **Deterministic:** yes.
- **State:** none.
- **Safety boundary:** fallback = the raw baseline (forward the client's bytes
  unmodified). Exactly one write per call (`_decide` computes, `_on_call`
  writes), so the fallback can never double-send.
- **Z modes it removes:** outage from a StateGuard bug; blocked valid calls
  to tools with unreadable schemas.
- **Z mode it accepts:** a call StateGuard *would* have refused, when
  StateGuard is broken, is forwarded; the server still validates it.
- **Shadow-first / Auto:** active in both; it only removes StateGuard
  actions.
- **Evidence:** `tests/examples/test_relay_fail_open.py` (7 tests).

### P-T3 Refresh-before-intervening (T3)

- **Ownable:** yes. The relay owns its own `tools/list` requests, which are
  read-only on the server.
- **Deterministic:** yes, by epoch ordering; the only timing is the
  `LISTING_WAIT_SECONDS` bound, whose expiry fails open.
- **State:** listing epochs (relay-local).
- **Safety boundary:** it never intervenes on an unconfirmed listing. On
  timeout or error it forwards unchanged.
- **Z modes:** one extra `tools/list` round trip per intervention (latency
  and server load). It blocks the client pump while waiting (bounded). A
  server that rate-limits `tools/list` turns interventions into
  pass-throughs (a missed repair, not a wrong one).
- **Shadow-first / Auto:** active in both (it keeps shadow records honest).
- **Evidence:** `tests/examples/test_relay_staleness.py` (14 tests); M3.

### P-T6 Invocation ledger + relay containment (T6)

- **Ownable:** yes. The relay sees every request and response on the wire.
- **Deterministic:** yes. The deadline is time-based by definition and
  opt-in.
- **State:** the ledger, per request id, with bounded retention (4,096
  finished).
- **Safety boundary:**
  - It never retries.
  - It never claims success or failure it does not know.
  - It drops a response only when the client already has its one answer
    (DUPLICATE / LATE). Id reuse after completion is delivered as without a
    proxy.
- **Z modes:**
  - The deadline cuts slow successes (measured: 8/8 slow-success injections
    at 0.3 s). This is why it is opt-in.
  - A response to a reused-but-finished id beyond retention is passed
    through (as raw).
  - Shadow never alters traffic (VERIFIED).
- **Auto conditions:**
  - Dedupe and surface-on-close are on in auto, because neither can make an
    outcome worse than raw: dedupe only removes a second answer, and
    surfacing replaces silence with a truthful error. VERIFIED: Z = 0
    (M5).
  - The deadline needs an operator-chosen value above legitimate tool
    latency.
- **Evidence:** `test_ledger.py`, `test_relay_ledger.py`, M5.

### P-T7 Effect model (T7)

- **Ownable:** the *mechanism* is. The *facts* are the operator's; StateGuard
  infers nothing.
- **Deterministic:** yes (a lookup).
- **State:** operator configuration.
- **Safety boundary:** the default is UNKNOWN. Annotations never change a
  declaration (exhaustive test). Lookups never cross servers.
- **Z modes:** a wrong operator declaration (e.g. declaring a payment
  "idempotent") lets a retry run it twice. StateGuard cannot detect this. It
  logs every retry with the declaration that allowed it, and the
  measurement reports `repeated_declared` so the effect of declarations is
  visible.
- **Evidence:** `test_effects.py`.

### P-T8 Retry (T8)

- **Ownable:** yes, for tools the operator declares repeatable. Never
  otherwise.
- **Deterministic:** yes (`decide_retry` is a pure rule; the attempt
  budget is 2).
- **State:** ledger state + attempts per call + retry-id → client-id map.
- **Safety boundary:**
  - Never after AUTH_ERROR or PROTOCOL_ERROR.
  - Never beyond one retry.
  - Never for an undeclared or non-idempotent tool.
  - No connection → escalate.
  - A shadow relay never retries.
- **Z modes:**
  - A wrong declaration (see P-T7).
  - With a deadline, a slow non-repeatable call is surfaced as unknown
    (counted: 5 in M6).
  - Retries double load on repeatable tools (17 retries in M6).
- **Shadow-first:** yes. `--retry shadow` is the default and logs
  APPLY / AMBIGUOUS / REJECT per call.
- **Conditions for Auto (per deployment):**
  - An operator effects file reviewed against tool behaviour.
  - A shadow period whose `retry_decision` log shows the APPLY set is
    exactly the intended tools.
  - A deadline above the tools' legitimate latency, if deadlines are used.
- **Evidence:** `test_effects.py`, `test_relay_retry.py` (10, server ledger
  checked), M6. Real-traffic evidence: none yet.

### P-T10 Result integrity check (T10)

- **Ownable:** detection yes. Repair no, ever: a repaired result is
  fabricated.
- **Deterministic:** yes, by the JSON Schema adapter.
- **State:** none.
- **Safety boundary:** read-only, and value-free findings. Exceptions fail
  open (the result is delivered).
- **Z modes:** a false finding (log noise only; results are never altered).
  Measured: 0 false findings in M8.
- **Auto:** there is no Auto action to promote. Surfacing findings *to the
  client* (e.g. via `_meta`) would alter the result. That is a product
  decision not taken here.
- **Evidence:** M8.

### T12 Safe server restart: implementation, lifecycle, boundary

**Where it lives.** `examples/mcp/stdio_proxy.py` (`StdioRelay`). It adds
link state beside the T6 `InvocationLedger`, plus a restart factory that
`main()` builds from the server command. No new library primitive: restart is
a host concern, and the ledger already holds the execution state. This is not
a parallel architecture.

**Lifecycle / state model.**

```
                 server EOF                     restart allowed + factory ok
   UP ─────────────────────────▶ (settle in-flight) ──────────────────────────▶ RESTARTING
   ▲                               │ every SENT request → UNKNOWN,                │ registry cleared
   │                               │ answered "outcome unknown",                  │ initialize replayed
   │                               │ never re-sent                                │ (if the session used it)
   │                               ▼                                              ▼
   │                 restart off / shadow / idle-only        re-init ok (or stateless) → UP + re-list
   │                 with in-flight / client gone            re-init refused (version) → close it,
   │                 / budget spent                          counts as a failed attempt
   │                               ▼                                              │
   └────────────────────────────  DOWN  ◀───────────── budget spent ─────────────┘
                                   │ waiting requests answered "not sent"
                                   ▼ relay stops (client sees its server exit)
```

Per-request states are T6's: PENDING, ANSWERED_LOCALLY, SENT, COMPLETED,
UNKNOWN.

| Situation at death | Request state | Relay action |
|---|---|---|
| Request SENT (written), no answer | UNKNOWN | Answered "outcome unknown". Never re-sent. Stays UNKNOWN whatever happens later. |
| Request PENDING (arrived during RESTARTING) | PENDING → SENT → … | Waits (≤ `RECONNECT_WAIT_SECONDS`), then sent **once** to the new server. |
| Request PENDING, link ends DOWN | ANSWERED_LOCALLY | Answered "not sent". Certainly not executed. |
| Request COMPLETED before the death | COMPLETED | Unaffected. |
| Write to a server just dying (before EOF) | UNKNOWN | It may have been read, so it counts as in flight (pinned by a test). |

**Automatic vs escalated.**

| Behaviour | Automatic (auto mode) | Escalated / surfaced |
|---|---|---|
| Restart after idle death | yes, if `--restart N` > 0 | — |
| Restart after a death with requests in flight | only with `--restart-policy after-unknown` | idle-only (default): no restart; the relay stops |
| Re-send of an in-flight request | **never** | "outcome unknown" to the client |
| Send of a never-written request after restart | yes, once | — |
| Re-initialize (initialize-era session) | yes, replaying the client's `initialize` | protocol-version change → refused, attempt counted |
| Tool listing after restart | registry cleared, then re-listed | listing fails → calls forwarded unvalidated (logged) |
| Restart budget spent | — | logged `restart_exhausted`; relay stops |
| Shadow mode | nothing | `restart_would_attempt` logged |

**Recovery safety boundary.**

- **What is recovered.** Only *subsequent* invocations, by sending them to a
  new process. No invocation that may have executed is ever re-sent.
- **What a restart implies.** A successful restart says nothing about any
  earlier invocation. UNKNOWN is resolved by nothing. A late answer from
  the dead process cannot arrive, and the ledger never moves out of UNKNOWN.

**Fault-injection coverage** (`tests/examples/test_relay_restart.py`; its
docstring maps tests to A–F):

| Scenario | Tests |
|---|---|
| A idle death → restart → success | `test_idle_death_restarts_and_the_next_call_succeeds`; real subprocess `test_restart_spawns_a_real_new_process_after_an_idle_exit` |
| B death before dispatch → executed once | `test_a_request_arriving_during_the_restart_waits_and_is_sent_once` |
| C death after dispatch → UNKNOWN, no replay | `test_death_after_dispatch_is_unknown_and_blocks_restart_by_default`; `test_after_unknown_policy_restarts_but_never_replays` (2); `test_a_request_written_to_a_server_that_just_died_is_unknown_not_lost` |
| D several calls, own states | `test_several_requests_in_flight_are_all_unknown_and_none_replayed`; `test_each_call_keeps_its_own_state_across_a_restart` |
| E restart keeps failing | `test_repeated_restart_failure_is_bounded_and_observable`; `test_a_server_that_dies_on_start_uses_up_the_budget_then_stops`; `test_a_restarted_server_on_another_protocol_version_is_refused` |
| F listing changed | `test_a_changed_listing_after_restart_replaces_the_old_registry`; `test_a_removed_tool_is_not_validated_against_its_old_schema`; `test_if_the_new_listing_never_comes_nothing_is_checked_against_the_old_one` |
| Z attribution | `test_a_stateguard_failure_during_restart_is_category_z_and_fails_safe` |
| Shadow / defaults | `test_shadow_never_restarts`; `test_restart_off_by_default`; `test_without_restart_the_relay_exits_with_its_server` |
| Initialize replay | `test_an_initialize_session_is_reinitialized_after_restart` |

**Property test** `test_property_restart_invariants`: 40 examples over random
call / in-flight-death / idle-death sequences, both policies, budgets 0–2,
and four factory modes (ok, fails, dies on start, changed listing). It checks:

- no request is sent twice and nothing executes twice;
- UNKNOWN stays UNKNOWN through later restarts and successes, and is never
  answered as success;
- "not sent" means never received by any server;
- restarts stay within budget, and a dying server ends with the relay
  stopped;
- the post-restart registry is either empty or the live server's.

The measurement invariants (`test_recovery_workload.py`,
`test_runtime_measurement.py`) add: replayed = 0 and duplicate = 0 in every
arm, Z = 0, shadow = raw, and the UNKNOWN set identical across StateGuard
arms.

### Primitive records (T12)

**P-T12a Restart decision** (when to restart):

- **Ownable:** yes. The relay owns its child process.
- **Deterministic:** yes. It depends on in-flight count, policy, budget,
  mode and client presence.
- **State:** link state, restarts used, generation.
- **Safety boundary:** idle-only by default. It never restarts in shadow, or
  when the client has gone.
- **Z modes:**
  - **Late death detection.** A request written before the EOF blocks an
    idle-only restart. That is a missed recovery, never a wrong one.
  - **Restart latency:** requests wait up to 10 s.
- **Shadow-first:** yes. Opt-in in auto.
- **Evidence for broader Auto:**
  - Per deployment: shadow `server_lost` logs showing deaths are mostly idle.
  - The server's startup has no side effects.
  - For `after-unknown`: evidence that the dead process cannot leave work
    running (e.g. it runs in its own process group that the factory kills).

**P-T12b In-flight settlement on transport loss:**

- **Ownable:** yes (ledger).
- **Deterministic:** yes.
- **State:** the ledger.
- **Safety boundary:** SENT → UNKNOWN, surfaced, never re-sent, never
  upgraded.
- **Z modes:** none measured. It replaces EOF with an explicit "unknown" per
  request.
- **Shadow / Auto:** shadow logs only; auto answers.
- **Evidence:** M9, M10 (UNKNOWN set identical across arms, replays 0).

**P-T12c Session re-establishment** (initialize replay + re-list +
registry reset):

- **Ownable:** yes. The relay replays only what the client itself sent
  (`initialize` params), under a relay id.
- **Deterministic:** yes.
- **State:** the client's `initialize`, the negotiated version.
- **Safety boundary:** a version change is refused. The old registry is
  dropped, so stale schemas are never used.
- **Z modes:**
  - The client is not told that its server was restarted. Server-side
    session state (resources, subscriptions) is not re-established, and only
    tools are re-listed. **UNVERIFIED** for servers with session state
    beyond tools.
  - An initialize-era server that sends server→client requests during init
    is not handled.
- **Shadow / Auto:** auto only, as part of restart.
- **Evidence for broader Auto:** a corpus of real initialize-era servers
  re-initialized after restart.

**P-T12d Not-sent answering:**

- **Ownable:** yes.
- **Deterministic:** yes.
- **State:** link state.
- **Safety boundary:** only for requests never written (PENDING).
- **Z modes:** a client may treat the JSON-RPC error as fatal where EOF would
  have been handled the same way. No new failure.
- **Shadow / Auto:** auto.
- **Evidence:** unit tests; M10 shows 0 because the workload's calls come
  after exhaustion, so the relay has stopped. This path is exercised in
  `test_repeated_restart_failure_is_bounded_and_observable`.

**P-T12e Bounded restart:**

- **Ownable:** yes.
- **Deterministic:** yes, by budget count.
- **State:** restarts used.
- **Safety boundary:**
  - Total attempts ≤ N per relay lifetime.
  - A failed factory, a server dying on start, and a refused re-initialize
    each consume one.
  - Exhaustion is logged and the relay stops.
- **Z modes:** a lifetime budget can be spent by early flapping, leaving none
  for later deaths. This is deliberate: there is no reset timer, so it
  cannot loop.
- **Evidence:** E tests; property (dies-on-start mode); M10.

## Measured results

(Populated as tasks land. Synthetic corpora are labelled as such; nothing here
is a production failure rate.)

**M1 — opposite-name false repairs (Category A / Z), T1. All corpora
synthetic; VERIFIED by running them.**

| Measure | Before (`41e7c5c`) | After (`dc67d52`) |
|---|---|---|
| Calibration family `must_abstain_opposites.json` (118 cases, generated from 60 pairs; only pairs similar enough to be proposed) | 103 wrong renames applied | 0 |
| Calibration applied precision, whole corpus (200 cases) | 28.7% (143 applied) | 100% (41 applied) |
| Calibration must-apply repairs | 41 | 41 (unchanged) |
| MCP probe, 152 directions (pairs I designed against) | 102 of 132 forwarded (first 132) | 0 forwarded |
| Hold-out probe, 118 directions (*not* designed against) | — | 4 forwarded by the first rule (`online`/`offline`, `uppercase`/`lowercase`), then covered. The hold-out is no longer independent. |
| Rule misfires on legitimate drift: 1,161 variants (deletion, transposition, case, separator, abbreviation) of 62 real parameter names | — | 0 |

Residual risk (INFERRED): opposite pairs outside the lexicon and affix rules
that are also similar enough to be proposed. The hold-out suggests a residual
in the low single-digit percent of such pairs. This has not been measured on
real traffic.

**M2 — relay fail-open (Category Z), T2. Fault injection into StateGuard,
VERIFIED.** 5 injected StateGuard failures (exception, `RecursionError`,
forwarding-gate failure, listing-sync failure, client-message failure): before
T2, gate failure → client got `-32603` (INFERRED from code); any other
exception in the call path → relay thread died (INFERRED). After T2: 5/5
forwarded once, unmodified; relay alive for the next call; each logged as Z.

**M3 — stale listing (Category B / Z), T3. Synthetic scenarios, VERIFIED.**

| Scenario (schema changed, no notification) | Without refresh | With refresh |
|---|---|---|
| param renamed / required added / enum extended / type widened / bound loosened | 5/5 valid calls blocked or rewritten (incorrect refusal/repair, Z) | 5/5 forwarded unchanged, executed once |
| Extra round trips for calls that pass unchanged | — | 0 (measured over 5 calls) |
| Extra round trips per intervention | 0 | 1 `tools/list` (+ pages) |

**M4 — first before/after slice, T4.** `python benchmarks/runtime/measure.py
--per-fault 8`, seed 0. **SYNTHETIC**: 14 drift-corpus tools, 37 labelled
calls, generated faults, a scripted in-process server with a deliberately
simple validator. There are 123 invocations per arm. These are not
production rates. VERIFIED by running.

| Cat | N | raw | naive retry | shadow | auto | auto: prevented / escalated / unresolved / Z | failure kinds (raw → auto) |
|---|---|---|---|---|---|---|---|
| A | 37 | 21 | 21 | 21 | 1 | 20 / 2 / 1 / 0 | raw: 17 not executed, 5 wrong execution, 5 phantom success → auto: 1 (known gap: object union) |
| B | 6 | 0 | 0 | 0 | 0 | 0 / 0 / 0 / 0 | (5 Z before T3; see M3) |
| C | 8 | 0 | 0 | 0 | 0 | 0 / 0 / 0 / 0 | auto refused 8 malformed envelopes the server would also reject |
| D | 0 | — | — | — | — | — | **not exercised** |
| E | 8 | 8 | 8 | 8 | 8 | 0 / 0 / 8 / 0 | no response (hidden) |
| F | 16 | 16 | **0** | 16 | 16 | 0 / 0 / 16 / 0 | naive "recovers" all, but only because these faults happened *before* execution — unknowable to a client |
| G | 8 | 8 | 8 (**8 dup**) | 8 | 8 | 0 / 0 / 8 / 0 | executed, reported failed |
| H | 8 | 8 | 8 (**8 dup**) | 8 | 8 | 0 / 0 / 8 / 0 | truncated response: hidden |
| I | 16 | 16 | 16 (**8 dup**) | 16 | 16 | 0 / 0 / 16 / 0 | response lost after execution: hidden |
| J | 8 | 8 | 8 | 8 | 8 | 0 / 0 / 8 / 0 | duplicate response delivered to client |
| K | 8 | 8 | 8 | 8 | 8 | 0 / 0 / 8 / 0 | auth error (persistent) |

- **Totals:** 93 failures before, 73 after, **0 StateGuard-induced**, 0 auto
  duplicate executions, 0 crashes. Shadow ≡ raw in every category.
- **Latency (category A, in-process):** raw p50 0.4 / p99 0.6 ms; auto p50
  2.7 / p99 4.0 ms. The difference is about 2.3 ms p50 of in-process overhead,
  which includes the refresh round trip on interventions.
- **Round trips:** A avoided 14 failing `tools/call` round trips and added 35
  refresh `tools/list` round trips. That is one per intervention, by design
  (T3).
- **The naive-retry arm is the hazard:** 24 duplicate executions across G,
  H and I. The same client-side signal (error or timeout) means "safe to
  retry" in F and "already executed" in G, H and I. **A retry primitive
  cannot be built on the client-visible signal alone.** It needs execution
  state plus an operator-declared effect model.

**M5 — after T6 (`c1c9acc`).** Same corpus and seed as M4, plus a new F
injector (`slow_success`: correct answer after 0.45 s). **SYNTHETIC.**
VERIFIED by running.

| Cat | N | raw | auto | auto + 0.3 s deadline | hidden raw → auto → auto+dl | notes |
|---|---|---|---|---|---|---|
| A | 37 | 21 | 1 | 1 | 5 → 1 → 1 | unchanged from M4 |
| B, C | 6, 8 | 0 | 0 | 0 | 0 | |
| E | 8 | 8 | 8 | 8 | **8 → 0 → 0** | server death now answered "outcome unknown" |
| F | 24 | 16 | 16 | **24** | 8 → 8 → 0 | deadline: hangs surfaced, but 8 slow successes cut (**Z = 8**) |
| G | 8 | 8 | 8 | 8 | 0 | executed, reported failed |
| H | 8 | 8 | 8 | 8 | 8 → 8 → 0 | |
| I | 16 | 16 | 16 | 16 | **16 → 8 → 0** | |
| J | 8 | 8 | **0** | 0 | 8 → 0 → 0 | duplicate responses dropped |
| K | 8 | 8 | 8 | 8 | 0 | |

- **Totals:** 93 failures before, 65 after (auto), StateGuard-induced 0
  (auto) / 8 (deadline arm).
- **Hidden failures:** raw 53, naive 29, shadow 53, auto 25, auto+deadline 1.

**M6 — after T8 (`88a5afa`).** Same corpus and seed. New arm `auto_ops`:
0.3 s deadline, *simulated* operator effects (`SIMULATED_EFFECTS`: 5
read-only, 6 non-idempotent, 3 undeclared), `--retry auto`. **SYNTHETIC.**
VERIFIED by running.

| Cat | N | raw | auto | auto_ops | recovered by retry (ops) |
|---|---|---|---|---|---|
| A | 37 | 21 | 1 | 1 | 0 |
| B, C | 14 | 0 | 0 | 0 | 0 |
| E | 8 | 8 | 8 | 8 | 0 (no reconnect: read-only kill escalated) |
| F | 24 | 16 | 16 | 16 | 5 (hang/transient/slow on read-only); 5 slow non-read-only cut = Z |
| G | 8 | 8 | 8 | 4 | 4 |
| H | 8 | 8 | 8 | 6 | 2 |
| I | 16 | 16 | 16 | 13 | 3 |
| J | 8 | 8 | 0 | 0 | 0 |
| K | 8 | 8 | 8 | 8 | 0 |

- **Totals:** raw 93 → auto 65 → auto_ops 56.
- **Hidden:** 53 → 25 → 1.
- **StateGuard-induced:** auto 0, auto_ops 5.
- **Retries:** 17 sent, 14 recoveries, 12 repeated executions (all declared
  read-only), **0 duplicate executions of anything else**.
- **Per-effect breakdown (VERIFIED from the JSON report):**
  - Read-only tools: every injected fault was recovered, except server death
    (no reconnect).
  - Non-idempotent and undeclared tools: never retried.

**M7 — T5 schema reading.** VERIFIED.

- **Keyword probe:** 0/22 → 7/22 constructs read.
- **Differential vs `jsonschema` Draft 2020-12:**
  - `uniqueItems`: 500 generated instances, 0 disagreements.
  - `multipleOf`: 500 generated instances, 0 disagreements.
  - Corpus differential: 6,000 payloads, 0 disagreements, unchanged.
- **Calibration:** unchanged (199/200, precision 100%).
- **Coverage on real traffic:** not measured. No corpus of real MCP schemas
  using these keywords exists in the repo.

**M8 — after T10.** Same corpus and seed. New H injector
`output_schema_violation` (8 invocations). **SYNTHETIC.** VERIFIED by
running.

- **Failures:** raw 101 → auto 73 → auto_ops 64. N rose by 8; the new
  violations fail in every arm by design.
- **Hidden failures:** raw 61 → auto 33 → auto_ops 9 (8 of those are the
  result-contract violations, detected but not altered).
- **Result-integrity findings:** shadow 8, auto 8, auto_ops 8; raw 0. That
  is exactly the injected count, so 0 false findings.
- **Unchanged from M6:** StateGuard-induced auto 0 / auto_ops 5; duplicate
  executions of non-declared tools 0.

**M9 — after T12.** Seed 0, 8 per fault. Two new E injectors:
`server_dies_idle`, and `server_dies_idle_restart_fails` (the server cannot be
started again). `auto_ops` now also has `--restart 3` with the idle-only
policy. **SYNTHETIC.** VERIFIED by running.

**Comparability.** The new injectors consume seeded draws, so the tools
picked for later injectors changed. Per-category F/G/H/I numbers are **not
directly comparable** with M8. For example, I's "response lost" injection now
drew no read-only tool, so I shows 0 retry recoveries (checked per scenario).

**Correction to M5–M8.** The judge counted "no response" as a *hidden*
failure even when the client's connection had closed. A raw relay exits when
its server dies, so the client *sees* EOF: a visible failure with an unknown
outcome, not silence. A new `transport_closed` outcome separates the two.
Earlier "hidden" totals overstated StateGuard's benefit for server-death
scenarios. With the correction: hidden raw 45 → auto 33 → auto_ops 9.

Transport outcomes, distinct per arm (restarts / recovered by restart /
UNKNOWN surfaced / not sent / connection closed / silent / replayed /
duplicate executions):

| Cat | Arm | restarts | recovered | UNKNOWN | not sent | conn. closed | silent | replayed | dup. exec |
|---|---|---|---|---|---|---|---|---|---|
| E (24) | raw | 0 | 0 | 0 | 0 | 24 | 0 | 0 | 0 |
| E | auto | 0 | 0 | 8 | 0 | 16 | 0 | 0 | 0 |
| E | auto_ops | 8 | **8** | 8 | 0 | 8 | 0 | **0** | **0** |
| I (16) | raw | 0 | 0 | 0 | 0 | 8 | 8 | 0 | 0 |
| I | auto | 0 | 0 | 8 | 0 | 0 | 8 | 0 | 0 |
| I | auto_ops | 0 | 0 | 16 | 0 | 0 | 0 | **0** | **0** |

E breaks down by injector:

- **Idle death (8):** recovered by a restart in auto_ops. Raw and auto see
  the connection close.
- **Death with the request in flight (8):** UNKNOWN in auto and auto_ops,
  not restarted (idle-only), never replayed.
- **Restart impossible (8):** bounded at 3 failed attempts, then the relay
  stops (connection closed, visible).

What this measurement shows, and what it doesn't:

- **Recovered** means the call executed exactly once on the restarted server
  and the client got its result. That is 8/8 idle deaths.
- **UNKNOWN** cases are *contained*: the client is told, and nothing is
  re-sent. They are not resolved.
- **Connection closed** cases are visible but unresolved.
- Nothing is prevented here.

Totals: raw 117 → auto 89 → auto_ops 80 failures. StateGuard-induced: auto 0,
auto_ops 6 (deadline cuts on slow non-repeatable calls, as in M6). Replays: 0
in every arm.

**M10 — T12 session recovery workload** (`benchmarks/runtime/recovery.py`,
seed 0, 5 sessions per kind, 9 kinds, 5 calls per session: 225 invocations
per arm). Tool `write_note`: side-effecting, not idempotent, no retry
involved. **SYNTHETIC.** VERIFIED by running.

| Arm | succeeded | **recovered** | UNKNOWN | not sent | conn. closed | silent | replayed | dup. exec | Z | deaths | rc. attempted | rc. succeeded | rc. failed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| raw | 109 | 0 | 0 | 0 | 116 | 0 | 0 | 0 | 0 | 40 | 0 | 0 | 0 |
| shadow | 109 | 0 | 0 | 0 | 116 | 0 | 0 | 0 | 0 | 40 | 0 | 0 | 0 |
| auto (restart off) | 109 | 0 | 20 | 0 | 96 | 0 | 0 | 0 | 0 | 40 | 0 | 0 | 0 |
| restart idle-only | 109 | **58** | 20 | 0 | 38 | 0 | **0** | **0** | **0** | 45 | 40 | 25 | 15 |
| restart after-unknown | 109 | **82** | 20 | 0 | 14 | 0 | **0** | **0** | **0** | 45 | 55 | 40 | 15 |

How to read it:

- **Recovered** means it executed exactly once *on a restarted server*,
  with the client told it succeeded. These are the calls after a death that
  raw loses to a closed connection.
- **UNKNOWN (20)** is the same 20 invocations in every StateGuard arm: the
  in-flight requests of the death-after-dispatch, death-before-execute and
  two-in-flight sessions. They are contained (told, never re-sent), **not
  resolved**.
- **Connection closed** remains where no restart is allowed:
  - restart-fails sessions (14 invocations in every arm);
  - in-flight deaths under idle-only.
- **Deaths:** 45 vs 40 because the dies-on-start sessions lose a restarted
  server too.
- **Reconnects:**
  - Failed attempts: 15, which is 5 unrestartable sessions × 3.
  - Succeeded: a restarted server that then served its tool list.

Per kind (restart idle-only vs raw):

- idle death 15/15 recovered;
- request racing the restart 15/15;
- dies-on-start 15/15;
- changed listing 13/13;
- in-flight deaths 0 recovered (idle-only refuses); after-unknown recovers
  the 10 calls after them;
- unrestartable 0.

Single-call measurement (`measure.py`, same seed), E category with the new
counters, as deaths / reconnect attempted / failed / restarts / recovered /
UNKNOWN / connection closed / replayed / duplicate:

| Arm | Counts |
|---|---|
| raw | 24 / 0 / 0 / 0 / 0 / 0 / 24 / 0 / 0 |
| auto | 24 / 0 / 0 / 0 / 0 / 8 / 16 / 0 / 0 |
| auto_ops | 24 / 32 / 24 / 8 / 8 / 8 / 8 / 0 / 0 |

## Known risks / unresolved categories

- **Unresolved or partly resolved:**
  - **C:** detect only.
  - **D (session):** not exercised by the measurement. The initialize replay
    is unit-tested only.
  - **E:** an in-flight death and an unrestartable server stay unresolved;
    in-flight deaths are contained as UNKNOWN.
  - **F / G / H / I:** only operator-declared read-only/idempotent tools are
    recovered. The rest is surfaced, not resolved.
  - **K:** the server's error is passed through, with no classification
    (T11).
- **Real process restart** is verified for one idle exit (VERIFIED). Not
  covered:
  - a server that ignores `terminate` (the `kill` path);
  - a server whose children outlive it;
  - slow startup under load.
- **Session state beyond tools is not re-established after a restart**
  (resources, prompts, subscriptions, server-side session data), and the
  client is not told its server was restarted. UNVERIFIED against real
  stateful servers.
- **The restart budget is per relay lifetime.** Early flapping can spend it.
  Deliberate (it cannot loop), but it means a long-lived relay eventually
  stops restarting.
- **Late death detection.** Requests written between a death and its EOF are
  UNKNOWN, and block an idle-only restart (a missed recovery, never a wrong
  one).
- **Opposite lexicon is finite.** Pairs outside it and outside the affix rule
  (e.g. `buy`/`sell` as single tokens) fall back to similarity, which is
  usually low for true antonyms. This is measured, not assumed.
- **Measurement is synthetic throughout.** No real-traffic corpus exists yet
  (T13).

## Next task

Recommended next:

- **T13:** record/replay with redaction. This is the only route to
  non-synthetic numbers.
- **T11:** auth-signal classification + operator callback.
- **T9:** circuit breaker. It can now build on `recovery.py`'s multi-call
  sessions.
