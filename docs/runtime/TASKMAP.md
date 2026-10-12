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
| Safe server restart (E) | COMPLETE (T12, `9d1f401`, `24011f5`; T14 race fix) | `test_relay_restart.py` (19, incl. 40-example property), real-subprocess e2e, `recovery.py` (M10) | **opt-in** (`--restart N`), Auto only; shadow never restarts | Default policy idle-only. Registry cleared on restart. 0 replays / 0 duplicate executions in every measurement. |
| Audit log | PARTIAL | relay JSON lines, value-free; `category` on Z / E / I / J events | both | Not exported as structured telemetry. |
| Telemetry hooks | PARTIAL | `telemetry/hooks.py` `ITelemetryHook` (repair events only) | n/a | No runtime-event hooks. |
| Effect classification | COMPLETE (T7, `ad6f041`) | `adapters/mcp/effects.py`; `test_effects.py` exhaustive (1,344 cases) | n/a | Operator-declared only; default UNKNOWN; annotations = recorded hints, never decide (VERIFIED incl. contradicting hints). |
| Outcome / invocation ledger | COMPLETE (T6, `c1c9acc`) | `adapters/mcp/ledger.py`; `test_ledger.py` (13 unit + 600-example property) | both | Library primitive; the relay is its first user. |
| Execution state | COMPLETE (T6) | `ExecutionState`: PENDING / ANSWERED_LOCALLY / SENT / COMPLETED / UNKNOWN | both | UNKNOWN is first-class; never rounded. |
| Duplicate-response containment (J) | COMPLETE (T6) | `test_relay_ledger.py`; M5 J 8 → 0 | auto drops / shadow logs | |
| Surface unknown outcome on transport close (E/I) | COMPLETE (T6) | `test_relay_ledger.py`; M5 hidden failures 53 → 25 | auto answers / shadow logs | JSON-RPC `-32000` error with `data["io.stateguard/executionState"] = "unknown"`. |
| Call deadline (F/H/I) | PARTIAL (T6) — opt-in only | M5: hidden 25 → 1, but **8 Z** on slow successes | auto, opt-in | Off by default; the operator must set it above legitimate tool latency. |
| Retry decision logic | COMPLETE (T8, `ad6f041`, `88a5afa`) | `decide_retry`; relay `--retry`; `test_relay_retry.py` (server ledger checked) | **Shadow by default**; Auto opt-in with `--effects` | Never after auth/protocol error; never after an UNKNOWN outcome, whatever the effect (T11 follow-up); one retry max; only known tool errors on declared-safe tools are retried. |
| Circuit breaking | MISSING | — | — | |
| Result validation (`outputSchema`) | COMPLETE, detection only (T10) | `adapters/mcp/results.py`; `test_results.py`, `test_relay_results.py`; M8: 8/8 injected detected, 0 elsewhere | observe in all modes; **never alters** | Missing `structuredContent` = advisory (2026-07-28 text: optional). Non-object `outputSchema` not checked (advisory). |
| Auth signal classification | COMPLETE (T11) | `adapters/mcp/failures.py`; `test_failures.py` (174), `test_relay_auth.py` (36), `auth.py` (M13) | both: classified and logged in shadow and auto; never retried | Deterministic signals only; text fallback deliberately narrow. Classification, not resolution: credentials are never touched. |
| Fault injection | PARTIAL (T4, `198aa25`) | `benchmarks/runtime/harness.py`: 11 server fault actions; StateGuard-internal faults in `test_relay_fail_open.py` | — | Seeded; one fault per invocation. Missing: 429/Retry-After (no HTTP transport), delayed-but-arriving responses, session faults (D). |
| Property tests (repair layer) | COMPLETE | `test_mcp_properties.py`, `test_diff.py` | — | None for runtime primitives (do not exist). |
| Replay / benchmark | PARTIAL (T13) | `stateguard replay`, `mcp_bench.py`, `calibrate.py`, `measure.py`, `recovery.py`, **`replay.py` (recorded sessions × raw / shadow / auto / auto_ops / as-recorded)** | — | Record/replay works end to end (M11). **No real-traffic recording exists yet**: the committed corpus is fault injection. |
| Traffic recording + redaction | COMPLETE (T13) | `adapters/mcp/recording.py`; relay `--record`; `test_recording.py`, `test_relay_recording.py`, `test_replay.py` | observe only, every mode | Deterministic rule-based redaction, mask by default. Recorder never raises; background mode never blocks the relay. |
| Real-capture readiness | COMPLETE (RC) — **no capture taken** | `recording.py` provenance families, `evidence.py`, relay/CLI refusals; `test_real_capture.py` ×2 | **shadow only**, mask only, numbers masked | Evidence collection, not proof of reliability improvement. Procedure: `docs/runtime/REAL_CAPTURE.md`. |
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
| T11 | Auth-failure classification: authentication / authorization vs protocol / transport / tool / UNKNOWN; never retried; category K | 4 | DONE (see T11 section) | T6, T8, T13 |
| T12 | Safe server restart: idle death only by default; in-flight → UNKNOWN, never replayed; re-initialize + re-list + registry reset; bounded; session recovery workload | 5 | DONE (`9d1f401`, `24011f5`, + measurement commit) | T6, T8 |
| T13 | Record/replay with redaction: versioned format, deterministic redaction, fail-open relay taps, replay engine + A–H fault-injection corpus, provenance-labelled report | 6 | DONE (`bbb660b` + replay commit) | T4, T12 |
| T14 | Mid-restart race: gate `tools/call` on a live, listed server; bind every decision to its server generation | 1 | DONE (see T14 section) | T12, T13 |
| RC | Real-capture readiness: shadow-only + mask-only `real_capture`, no ground truth on observed traffic, `replay` provenance, evidence summary, first-capture procedure | 6 | DONE (see Real capture section); **no capture taken** | T11, T13, T14 |

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
  - **Never after an UNKNOWN outcome (deadline or transport loss), whatever
    the declared effect: escalated.** This was tightened after T11; T8
    originally retried unknown outcomes on declared read-only/idempotent
    tools.
  - Never beyond one retry.
  - Never for an undeclared or non-idempotent tool.
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

### T13 Record / replay with redaction

**Record/replay infrastructure enables real-traffic measurement but does not
itself constitute real-traffic evidence.** Every number below comes from the
committed corpus, which is deterministic fault injection on a synthetic
workload.

**Architecture.** No second event system: the recorder taps what already
exists.

- **Library.** `stateguard.adapters.mcp.recording`:
  - `Recorder`;
  - `Redactor` and `RedactionPolicy`;
  - `Provenance`;
  - the parser: `parse_recording` / `read_recording`.

  It is zero-dependency.
- **Relay taps.** Five points in `examples/mcp/stdio_proxy.py`:
  - the client read loop;
  - `_send_server`, after the write succeeded;
  - the server read loop;
  - `_send` to the client, after the write;
  - `_log` (audit events, which already carry every StateGuard decision,
    execution-state change, restart and generation).
- **Harness.** `Rig(recorder=…)`, and `ScriptedServer.on_fault`, which
  records the injected faults.
- **Replay.** `benchmarks/runtime/replay.py`:
  - `derive()` turns a recording into a `TapeEnvironment`: client steps,
    listings by position, tool-call attempts per client id, idle deaths,
    restart possibility and labels.
  - `TapeServer` is a `ScriptedServer` subclass that answers from it.
  - `run_arm()` drives the existing `Rig`.
- **Corpus.** `benchmarks/runtime/record_corpus.py` produces
  `benchmarks/runtime/tapes/` (A–H).

**Format** (`stateguard.mcp.recording`, version 1). JSON lines with keys
sorted; `header` first, `end` last; `seq` strictly increasing.

- **Replay-required.**
  - `message` records: direction, generation, kind
    (request / notification / response / error / unparsed / other), id,
    method, tool, and the redacted message.
  - `audit` records: StateGuard decisions, `outcome_unknown`,
    `server_lost`, restarts.
  - `fault` records: injected failures, keyed by request id.
  - `label` records: ground truth, redacted like the request.
- **Diagnostic.** `diag`: byte length, and a monotonic timestamp only when
  `deterministic=False`. Never read by replay.
- **Sensitive.** Never written. The header names the policy; `end` counts
  values changed.
- **Version gate.** Unknown versions raise
  `UnsupportedRecordingVersionError`. The parser raises `RecordingError`
  only, and its messages name a line and a reason, never content.

**Redaction model.** Deterministic and rule-based; no probabilistic
detector. Three layers, recursive at every depth:

1. **Key names.** A normalized name that marks a credential (`authorization`,
   `*token*`, `*secret*`, `password`, `api_key`, `cookie`, `key`, `auth*`
   but not `author*`, …) scrubs the subtree and keeps only JSON types. This
   covers arguments, `_meta`, results, errors, audit and labels. A schema
   property with such a name has its `default` / `enum` / `const` /
   `examples` scrubbed.
2. **Value shapes.** Twelve patterns replace the whole string anywhere, even
   in positions otherwise kept: auth schemes, JWT, AWS, GitHub, Slack,
   Stripe and `sk-` keys, Google keys, PEM private keys, URL userinfo,
   secret query params, `password=` assignments.
3. **Mask mode (the default).** Fail-safe: anything not vouched for is
   masked, shape-preservingly (letters → `a`/`A`, digits and numerals → `1`).
   - **Kept verbatim:**
     - protocol structure: `jsonrpc`, `method`, ids, tool names, protocol
       versions, `resultType`, `isError`, content `type`;
     - error codes and StateGuard execution states;
     - published schemas, except prose;
     - argument values the tool's own schema vouches for: an enum/const
       member up to case and separators or within two edits, a numeric
       string under 10⁶ in a numeric field, a boolean word in a boolean
       field;
     - small numbers and booleans.
   - **Reduced:** integers of seven or more digits.
   - **Keys:** undeclared dict keys are kept only when word-like (≤ 40
     characters, ≤ 3 digits); otherwise masked, with collisions suffixed,
     never overwritten.
   - **JSON in a string:** a JSON object or array sent as a string is
     redacted as structure.
   - **Unclassifiable:** unknown types and nesting beyond depth 64 become
     `[redacted]`.

`secrets_only` mode is opt-in: it keeps free text and is declared in the
header. The server command line is never written.

**Recorder failure semantics.**

- **It never raises into the host.** Recorder bugs are also caught at the
  relay taps.
- **On any failure** (sink, redaction, queue overflow, close timeout) it
  stops. `end` says `complete: false`, or is absent if the sink failed.
  Replay refuses incomplete files by default.
- **The relay** logs `recording_stopped` once, with exception type names
  only, and traffic continues unchanged.
- **`background=True`** (used by `--record`) redacts and writes on a writer
  thread behind a bounded queue. A blocked sink makes records drop
  (recording stops) and never stalls the host. `close()` waits at most
  `close_timeout`.
- **This is not category Z.** A recorder failure that changed traffic would
  be Z. The tests show it does not:
  - three failure modes × three relay modes;
  - client bytes, server lines and executions are identical to an
    unrecorded run.

**Replay semantics.**

- **Sequential.** Recorded client lines are sent in order with their ids.
  Each request waits for its answer. No line is sent mid-restart or while
  the relay is stopping, which keeps the recorded order.
- **The server comes from the recording.**
  - The listing in effect at each step is the latest one recorded before
    the next client step.
  - Each `tools/call` attempt replays the recorded response bytes (id
    rewritten), garbage, a silent drop, or a closed pipe. Matching uses
    exact JSON identity after declared defaults, so `2` ≠ `2.0`.
  - Idle deaths are replayed before the same step. A restart fails if the
    recording's did.
- **Counterfactuals** are answers from the `TapeServer`'s model. They cover
  arguments StateGuard changed, retries, and calls on a server generation
  the recording never had. Each is marked `io.stateguard/replay:
  counterfactual` and counted per arm and per "prevented" / "recovered"
  figure.
- **Ground truth for execution.**
  - Fault-injection recordings: from the `fault` records.
  - Real captures: assumed executed unless refused at the protocol level
    (conservative for duplicates).
  - Without faults, the unanswered request a server died on is the latest
    one; earlier ones are drops (stated convention, tested).
- **Refused, never guessed:** incomplete recordings, client ids in the
  relay's reserved prefixes, and reused request ids.
- **Isolation.** Replay never starts a process or opens a socket (tested by
  forbidding both).
- **Race-provoking mode** (T14): `run_arm(race_provoking=True)` sends each
  line as soon as the previous answer arrived, even mid-restart, which is
  the ordering a real client produces. `unsafe_bypasses(audit)` counts the
  T13 path.

**Provenance classes:** `synthetic`, `fault_injection`, `local_development`,
`real_capture`; `replay` added by the real-capture task (see that section).

- Only `real_capture` is real traffic.
- The report's first line states the corpus's evidence class, and mixed
  corpora report as not real.
- The relay CLI defaults to `local_development`.

**Tests** (VERIFIED):

- `tests/adapters/mcp/test_recording.py`:
  - 35 key-name and 14 value-pattern cases;
  - masking, schema vouching, `_meta`, errors and results, listings,
    `secrets_only`;
  - id and key aliasing, depth and unparsed lines, audit and labels;
  - redactor isolation per recording;
  - recorder sink, redaction, overflow and close-timeout failures, and
    background ≡ synchronous output;
  - 25 malformed-recording cases and an unsupported version;
  - 6 hypothesis properties: no credential under any key in any mode, no
    credential shape anywhere, determinism and structure, no free text in
    mask mode, parser fuzz, damaged recordings.
- `tests/examples/test_relay_recording.py`:
  - recording changes no traffic, in all three modes;
  - the 3 × 3 recorder-failure isolation matrix;
  - the wire order and ids are what crossed;
  - an UNKNOWN stays unknown, with no fabricated server response;
  - generations across a restart;
  - the CLI `--record`, end to end.
- `tests/examples/test_replay.py`:
  - corpus provenance, complete, no fake credential;
  - re-recording gives identical per-stream content;
  - as-recorded fidelity;
  - replaying twice is identical (D, E, H × 3 arms);
  - per-scenario A–H semantics;
  - no Z and no duplicates;
  - counterfactual marking;
  - no process or socket;
  - refusals for incomplete files, reserved ids and reused ids;
  - malformed and unsupported files;
  - the real-capture convention;
  - provenance reporting;
  - a damaged-record derive property.

**Security review (T13).**

| Concern | Finding | Status |
|---|---|---|
| Secret leakage into recordings | Key-name + shape rules everywhere; mask mode persists no free text; property-tested in both modes. Residual: in `secrets_only` mode, or in a word-like dict key, an unshaped secret under an innocuous name persists. | Mitigated; residual stated. |
| Raw credential persistence | Server command never written; header settings are flags; `relay_started` audit `server` field is masked like any value. | VERIFIED (test). |
| Secrets in exception messages | Recorder records exception *type names* only; parser and replay errors name line/seq and reason only. | VERIFIED (tests with a secret in the exception and in the bad line). |
| Secrets in benchmark output | The report holds counts, session names, answer classes. No message content. | INFERRED (by construction). |
| Replay contacting external systems | In-process `TapeServer`; no command in recordings. | VERIFIED (Popen/socket forbidden). |
| Cross-session leakage | One `Redactor` (schemas, aliases) per recording; one `ReplayState` per run. | VERIFIED (test). |
| Identifier collisions | Ids keyed by canonical JSON (`1` ≠ `"1"`); reserved prefixes and reuse refused; aliases deterministic per recording. Residual: a real client id spelled `[redacted-id-N]`. | VERIFIED / negligible residual. |
| Malformed records | Parser: strict, `RecordingError` only (fuzzed). `derive`: `RecordingError` only (fuzzed on damaged bodies). | VERIFIED. |
| Recorder failure affecting live calls | Exceptions: isolated (matrix). **A blocking sink would have stalled the relay** in synchronous mode: found in review, fixed with `background=True` (bounded queue, drop, never block), used by `--record`. Residual: synchronous mode (library default, used by the harness) still waits for its sink. | Fixed for the relay; residual stated. |
| Pre-existing: the relay's *audit log* is not redacted | `internal_error` carries exception text; `relay_started` carries the server command. Outside T13's recording path. | Not changed; recorded as a risk. |

**Findings outside the recorder** (surfaced by replay; not fixed in T13):

- **Mid-restart refresh race (relay, T12 path).** RESOLVED in T14; kept
  here as found.
  - **What happens:** a call that arrives while an after-unknown restart is
    in progress gets its relay `tools/list` refresh written to the dead
    server's pipe (`tools_list_unsent`). The call is then forwarded
    unvalidated.
  - **Reproduced:** 16 of 20 runs. The call was repairable (`limit: "5"`)
    and failed at the server; it would have been repaired with a confirmed
    listing.
  - **Severity:** fails open, so not unsafe. It is a lost improvement, not
    a regression against raw.
  - **Fix:** `_refresh_listing` should wait for the link (bounded) before
    requesting.
  - **Why replay sees it:** replay avoids the race by never sending
    mid-restart; the corpus generator does the same.

**What is required before real-world validation:**

- Recordings with provenance `real_capture` from real MCP deployments,
  captured in shadow mode with `--record`, ideally per server type.
- A redaction review of a sample by a human: masking is fail-safe, but the
  vouching rules decide what stays.
- Ground truth for real calls. Labels do not exist for captured traffic, so
  correctness can only be judged on the subset someone labels. The rest
  reports outcome distributions only.
- Concurrent-traffic replay (replay is sequential today) and timing replay
  (deadline effects are not reproduced).

### T14 Mid-restart race: gate calls on a live, listed server

**Objective.** A `tools/call` that arrives while the relay restarts its
server must never bypass StateGuard's decision (repair, validation,
refusal) because the server is temporarily unavailable.

**Root cause** (VERIFIED from the audit trail of a failing run, 11/20 in
the isolated repro; 16/20 in T13's replay of the E tape):

1. The server dies with a call in flight. `_on_server_lost` sets the link to
   `restarting` and answers that call "unknown".
2. In the same step, `_abandon_own_listings` **sets `_listing_ready`** ("calls
   are not held for a listing that cannot come").
3. The client, told "unknown", sends its next call at once. `_decide`
   passes the listing wait immediately and evaluates against the **dead
   generation's registry**. The registry is cleared only later, in
   `_connect`.
4. The call needs repair, so the relay re-lists first. The `tools/list`
   is written to the dead pipe (`tools_list_unsent`), the refresh fails, and
   the call is decided "unvalidated, listing unconfirmed".
5. `_send_tracked` then *waits for the link* and writes that unvalidated,
   unrepaired call to the **new** server.

The structural defect: a decision was never bound to the server generation
it was made against. Waiting happened *after* deciding, so a decision made
against generation N was sent to generation N+1.

**Invariant.** No `tools/call` is written to a server until:

- that server's link is up; and,
- after a restart, its own tool listing has been synced into the registry.

The decision must be made against that registry and written only to that
generation. Otherwise the call waits (bounded), or is answered "not sent".

**Fix** (`examples/mcp/stdio_proxy.py`; no new threads, and no concurrency
redesign):

- **`_gate_call` runs before deciding.**
  - It waits for the link (`_await_link`, bounded as before).
  - After a restart it also waits for `_synced_generation == generation`
    (`_await_discovery`, on the existing `_listing_done` condition, bounded
    by `LISTING_WAIT_SECONDS`).
  - If that server is lost first, it waits for the next one (bounded by the
    restart budget).
  - If the restart fails, the budget is spent, or the new server cannot
    list its tools, the call gets `_answer_not_sent`. That is the existing
    certain-non-execution answer, and the call is never forwarded.
  - A call that had to wait is logged `call_gated`.
- **A lost refresh means decide again.** `_refresh_listing` now returns
  `fresh` / `failed` / `lost`. `lost` covers unsent, or abandoned because
  the server died. On `lost`, or a generation or link change during the
  decision, `_decide` raises `_ServerChangedError` and `_on_call` gates and
  decides again (`call_regated`). "Unvalidated, unconfirmed" remains only
  for a *live* server's failed refresh: the documented T3 fail-open.
- **`_send_decided` writes once, to one generation.** It checks the link is
  up and the generation unchanged, records the ledger's `SENT`, and writes,
  all under `_to_server`.
  - `_on_server_lost` takes `_to_server` to move the link off `up`, and
    `_connect` swaps the stream and increments the generation under it. A
    call is therefore written to the generation it was decided for, or not
    at all: `False`, nothing written, nothing recorded, decide again.
  - A call that won the race is `SENT` before the loss is settled, so it is
    UNKNOWN, as before.
- **Discovery is requested before the link comes up.** `_connect` and
  `_on_reinit` request the listing (`_discovery_epoch`) before setting
  `up`, so a gated call waits for exactly that listing.
- **Retries keep their generation.** `_calls` stores the generation; a retry
  is never decided for, or sent to, another generation (`retry_not_sent` if
  the server changed before the write).

**Behaviour change (deliberate, fail-closed).** If a *restarted* server
cannot list its tools:

- **T12:** calls were forwarded unvalidated.
- **T14:** they are answered "not sent".

The initial generation keeps T2's fail-open policy: no listing at all means
forward unvalidated, logged. The T12 test that pinned the old behaviour now
pins the new one. In shadow mode, with the server gone, a call is no longer
*decided* before it is answered "not sent". The client sees the same answer,
and one fewer meaningless decision appears in the audit.

**Tests** (VERIFIED):

- `tests/examples/test_relay_restart_race.py`, 12 tests. The restart is made
  a fact with a factory that blocks until released, and each test checks
  the call is held at the gate first: a link waiter, PENDING, not decided,
  not forwarded.
  - **A:** waits, then decided after the new `tools_synced`, sent exactly
    once to the new server.
  - **B:** restart fails, so the call is "not sent", never decided or
    forwarded.
  - **C:** repaired after the restart, including a schema that changed
    across the restart (decided on the new schema).
  - **D:** ambiguous, so escalated or refused locally, never forwarded.
  - **E:** an UNKNOWN in-flight call is never re-sent; the gated call goes
    once.
  - **F:** three gated calls, each sent once, in arrival order.
  - **G:** the new server dies before listing, so the call goes to the
    next server; with no budget left it is "not sent".
  - The generation binding at unit level, for calls and for retries.
  - The exact T13 ordering, unsynchronised, ×15: zero unrepaired calls
    reach a server.
  - All 12 fail against the pre-fix relay. The race test fails because an
    unrepaired call reached the server ("Invalid arguments"); C because the
    call was decided "unvalidated".
- **H:** `test_relay_restart.py` (19), `test_relay_ledger.py`,
  `test_relay_retry.py`, `test_recovery_workload.py` and the real-subprocess
  restart tests all pass.
- `tests/examples/test_replay.py` adds the bypass counter and a
  race-provoking replay with zero bypasses.

**Replay evidence** (fault injection on a synthetic workload; VERIFIED by
running):

| Measurement | Pre-fix relay | T14 relay |
|---|---|---|
| T13 corpus, E tape, as-recorded, race-provoking order, 20 runs: unsafe bypasses | **16/20 runs** (T13's 16/20 reproduced); decisions diverged in 16/20 | **0/20**; decisions match the recording 20/20 |
| T13 corpus, D tape, same conditions | 0/20 | 0/20 |
| `race.py`, 3 × 20 runs (5 calls each, 2 after-unknown restarts): unsafe bypasses | 25, 32, 26 (17, 20, 18 of 20 runs affected) | **0, 0, 0** |
| `race.py`, repairable calls repaired | 35, 28, 34 of 60 | **60, 60, 60 of 60** |
| `race.py`, calls forwarded "unvalidated" | 25, 32, 26 | **0** |
| `race.py`, restart-gated calls | 0 (no gate) | 38, 38, 39 of 40 (the rest arrived after the restart completed) |
| `race.py`: UNKNOWN / duplicate executions / not sent | 40 / 0 / 0 | 40 / 0 / 0 (the 40 are the calls the server died on: executed once, never re-sent) |

"Unsafe bypass" (`replay.unsafe_bypasses`) is a call decided "unvalidated,
listing unconfirmed" after a server loss or an unsent listing since the
previous decision: the T13 path. The T14 relay can no longer produce it.
The structural guarantee is the generation binding; the counter is the
evidence.

**Unchanged by the fix** (re-run):

- **M9:** raw 117 → auto 89, Z 0 (auto_ops 6, deadline cuts as before).
- **M10:** identical table; 0 replays, 0 duplicate executions, Z 0.
- **M11:** identical, except decisions 58/58. C's call is no longer decided
  against a dead server.
- **Non-restart path:** the gate is one uncontended condition check. Auto
  p50 is 1.92 ms vs 2.02 ms before, in-process; no regression visible.

**Remaining limits:**

- **The client pump is one thread.** A gated call holds every later client
  message, notifications included, until it is decided or answered "not
  sent". This is the same blocking as T12's link wait, now also covering
  the discovery wait (≤ `LISTING_WAIT_SECONDS`).
- **Late death detection is unchanged.** A call written in the window
  between a server's death and the relay reading its EOF is SENT, then
  UNKNOWN (never replayed). The fix closes the decision/generation gap, not
  that physical window.
- **Generation 0 keeps fail-open.** No listing ever means forward
  unvalidated, logged (T2 policy). Only post-restart discovery is
  fail-closed.
- **Session state beyond tools** (resources, subscriptions) is still not
  re-established after a restart (T12 limit).

### T11 Auth-failure classification

**Objective.** Recognise authentication and authorization failures as their
own class, so the runtime never retries them, repairs around them, counts
them as transient or as ordinary tool failures, or claims to have recovered
them. StateGuard does not authenticate, authorize, or handle credentials.

**Ground truth found before the change** (VERIFIED by reading the code and
the SDK):

- **The retry rule already existed; its input did not.** `Observed.AUTH_ERROR`
  existed and `decide_retry` already rejected it, but nothing produced it
  (its comment said "classified by the host").
- **How the relay mapped errors:**
  - every JSON-RPC error became `PROTOCOL_ERROR` (never retried, but logged
    as category I, not K);
  - every `isError` result became `TOOL_ERROR`. This **was retried** for
    tools declared read-only/idempotent under `--retry auto`, even when the
    result was an upstream 401.
- **MCP defines no JSON-RPC error code for auth.** Authorization is
  HTTP-transport-level: 401 with `WWW-Authenticate: Bearer
  error="invalid_token"`, 403 with `error="insufficient_scope"` (RFC 6750),
  as in the SDK's bearer middleware and OAuth client.
- **`-32001` is a timeout, not auth.** It is the SDK's `REQUEST_TIMEOUT`,
  yet this repository's harness fault `AUTH_EXPIRED` sent `-32001
  "Unauthorized: token expired"`. Classifying by that code would have been
  wrong.
- **Recordings lost the signal.** T13's mask-mode redaction rewrote the
  auth signals ("Unauthorized", "invalid_token") into `Aaaaa`, so a recording
  could not classify as the live traffic did.

**Classes** (`FailureClass`): `AUTHENTICATION`, `AUTHORIZATION`,
`PROTOCOL`, `TRANSPORT`, `TOOL`, `UNKNOWN`, `NONE`.

**Signals**, in precedence order. Every result names the rule that fired
(`signal`, value-free):

1. **StateGuard's own marker wins.** An `error.data["io.stateguard/executionState"]`
   of `unknown` is UNKNOWN, and `not_sent` is TRANSPORT. Auth-like text or
   fields never override it.
2. **Structured `error.data` fields:**
   - an HTTP status under `status` / `statusCode` / `status_code` /
     `httpStatus` / `http_status`, integer 401 or 403 only;
   - an OAuth code under `error` / `error_code` / `errorCode`: exactly
     `invalid_token` / `invalid_client` / `invalid_grant` (authentication),
     or `insufficient_scope` / `access_denied` / `unauthorized_client`
     (authorization).

   A tool's `structuredContent` is never read: it is the tool's data, and a
   status there may describe another resource.
3. **HTTP metadata, for hosts that have it** (`classify_http_status`): 401 /
   403, refined by the `WWW-Authenticate` `error` parameter. The stdio
   relay has none.
4. **Narrow text fallback.** It reads only a JSON-RPC `error.message` or the
   text items of an `isError` result, at most 4096 characters. It matches
   only:
   - `401 Unauthorized` / `403 Forbidden`, not followed by a capitalised
     word;
   - `HTTP 401`, `HTTP/1.1 403`, `HTTP Error 401`;
   - `401 Client Error: Unauthorized`;
   - `error="invalid_token"` / `error="insufficient_scope"`;
   - a message that *is* `Unauthorized` / `Unauthenticated` / `Forbidden`,
     optionally followed by punctuation.
5. **JSON-RPC codes never count.**

If both kinds are present, the result is AUTHENTICATION (`…+conflicting`).
If no rule fires, the pre-T11 class stands: PROTOCOL for an error, TOOL for
an `isError` result.

**Retry behaviour.** `classification.observed` feeds the unchanged
`decide_retry`; the effect semantics are not changed.

| Class | `Observed` | Retried |
|---|---|---|
| AUTHENTICATION / AUTHORIZATION | `AUTH_ERROR` | **never**, whatever the declared effect, attempt count or connection |
| PROTOCOL / TRANSPORT / upstream UNKNOWN | `PROTOCOL_ERROR` | never (as before) |
| TOOL | `TOOL_ERROR` | only if declared read-only/idempotent under `--retry auto` (as before) |
| local UNKNOWN (deadline, transport loss) | `UNKNOWN_*` | **never**, whatever the declared effect (escalated); see the note below |

- **What the relay logs.** Every delivered auth failure is logged as
  `auth_failure` (category K: id, tool, failure, signal). `retry_decision`
  for it carries category K, and `tools_list_failed` carries the class.
- **The answer is never altered.** A late answer to a request already
  resolved UNKNOWN is dropped as before and is never classified.

**UNKNOWN is never retried (policy correction after T11).** The invariant
is that an uncertain side effect is never blindly replayed.

- **Old rule (T8).** `decide_retry` retried an unknown *outcome* (no answer
  by the deadline, or the connection lost with the request in flight) when
  the operator had declared the tool read-only or idempotent, under opt-in
  `--retry auto`.
- **New rule.** One rule, placed right after the protocol-error rule: an
  unknown outcome is always `AMBIGUOUS`, escalated, never re-sent, whatever
  the effect.
  - Retry is now reachable only for a *known* failure: a tool error on a
    declared-safe tool, within the one-retry budget. Those behave exactly
    as before.
  - The `UNKNOWN_TRANSPORT and no connection` branch became unreachable and
    was removed.
  - UNKNOWN is also never reclassified (as auth or anything else) to enable
    a retry.
- **Measured cost** (M9, synthetic `auto_ops`):

  | | Before | After |
  |---|---|---|
  | recovered | 11 | 7 |
  | retries sent | 13 | 7 |
  | failures | 76 | 82 (F +5, H +1) |
  | StateGuard-induced | 6 | 8 |
  | duplicate executions | 0 | 0 |
  | hidden failures | 9 | 9 |

  The deadline's cuts of slow-but-successful read-only calls used to be
  rescued by a retry; they now stay explicitly UNKNOWN. The default `auto`
  arm, M10, M11, T14's race and M13 are unchanged.
- **Tests.**
  - `test_effects.py`: unknown × {read_only, idempotent, non_idempotent,
    undeclared} × both unknown kinds × attempts 0/1 gives `AMBIGUOUS`; the
    exhaustive property now asserts every retry is a `TOOL_ERROR` and every
    unknown is `AMBIGUOUS`.
  - `test_relay_retry.py`: hang and dropped response × 4 tools; sent once,
    no duplicate execution, decision ambiguous. A known tool error on
    read-only and idempotent tools is still retried.
  - `auth.py` / `test_relay_auth.py`: a late 401 on a declared read-only
    tool stays UNKNOWN and is not retried (`unknown_retried` = 0).
  - Against the old rule, 4 of the new relay tests fail and the auth
    measurement shows `unknown_retried` 1.

**Recording.** Mask-mode redaction now keeps two things verbatim, both
fixed vocabulary and never data:

- the spans the text rules match (`failures.signal_spans`);
- the six OAuth codes.

Masking cannot create a match, because masked text holds only a / A / 1.
The audit fields `failure` and `signal` are kept as well. Property-tested:
redaction neither creates nor destroys an auth class. In secrets-only or
mask mode, a whole string replaced because it matches a credential shape
(e.g. `auth: 401 Unauthorized` meets the `auth:` assignment rule) loses its
signal. That is intentional: secret safety first.

**Tests** (VERIFIED):

- `tests/adapters/mcp/test_failures.py`, 174 tests:
  - structured signals (13 positive, 13 negative shapes);
  - JSON-RPC codes never auth (including -32001);
  - `structuredContent` never read;
  - the execution-state marker beats every auth signal;
  - 18 text positives in both carriers, and **21 adversarial false
    positives**: "permission", "authorization hold", "access", "token
    budget", "403 Forbidden City", "401 Unauthorized Biography", "retry
    after 401 seconds", auth words in docs and logs, and more;
  - the scan limit, conflicts, HTTP metadata (10 cases), malformed shapes;
  - a never-raises / deterministic property;
  - an auth-word-soup property;
  - auth is never retried for every effect × attempts × connection (72
    combinations);
  - non-auth tool errors keep the T8 policy.
- `tests/examples/test_relay_auth.py`, 36 tests:
  - every `auth.py` case × both arms;
  - the clean-measurement assertion;
  - shadow logs and changes nothing;
  - `retry_decision` is K;
  - an auth failure on the *retry* attempt ends there (2 attempts, no
    third);
  - the relay's own `tools/list` 401 is K;
  - a late 401 after the deadline stays UNKNOWN, is dropped and is not
    classified.
  - Against the pre-T11 relay, 18 of the 36 fail.
- `tests/adapters/mcp/test_recording.py`, +54: redaction preserves the
  class (both modes), auth signals survive masking while the surrounding
  data does not, and a property.
- The existing `test_an_auth_error_is_never_retried` (T8) still passes.

**Measurement M13** (`benchmarks/runtime/auth.py`; 15 cases × 2 arms; fault
injection on a synthetic workload, not real-world evidence):

| | pre-T11 relay | T11 relay |
|---|---|---|
| auth runs retried | **3** (all three auth-as-tool-result cases, `auto_retry`) | **0** |
| auth failures turned into a client-visible success | 3 (the fixture's retry succeeds, to make a retry visible) | **0** |
| auth answers delivered exactly as sent | 10/13 | **13/13** |
| K events logged | 0 | 13 |
| non-auth runs classified as auth (false positives) | 0 | **0** |
| UNKNOWN reclassified as auth | 0 | **0** |
| runs classified as expected | — | **30/30** |

After the UNKNOWN policy correction, a 16th case was added: a late 401 on
a tool declared read-only. The measurement then gives:

- 32/32 runs classified as expected;
- 14/14 auth answers delivered as sent, 0 retried;
- `unknown_retried` 0. Under the old rule this was 1: the read-only tool's
  UNKNOWN was re-sent.

The committed T13 corpus, re-recorded with auth signals kept, has 33
failure responses: 31 tool, 1 protocol and **1 authentication** (G's
injected auth fault, `text:reason_phrase`). There are no false positives,
and the recording's own audit has the K event.

**Known limitations:**

- **The stdio relay has no HTTP metadata.** Auth reaches it only as JSON-RPC
  errors or tool results, so coverage depends on servers using a
  structured field or one of the narrow text shapes. Servers that say
  "Bad credentials", "invalid API key" or "Resource not accessible" are
  **not** classified. They are treated as before: PROTOCOL (never retried)
  or TOOL (retried only if declared safe). That is the deliberate fail-safe
  cost of not guessing.
- **A tool's report of someone else's 401 is classified as auth.** An
  `isError` text with `401 Unauthorized` may describe a third-party
  resource a tool fetched; it is still treated as auth. The effect is no
  retry, which is the safe direction.
- **No operator callback.** `auth_failure` in the audit log is the signal;
  routing it anywhere is the host's job.
- **The corpus is synthetic.** Real servers' auth error shapes have not been
  sampled (needs a `real_capture` corpus).

### Real capture (shadow-mode evidence collection)

**Objective.** Make it safe to record real MCP traffic through the relay
in shadow mode, and keep what happened apart from what StateGuard only
proposed. Procedure and privacy guarantees: `docs/runtime/REAL_CAPTURE.md`.
**real_capture provides evidence collection, not proof of reliability
improvement.**

**Found before the change** (VERIFIED by reading code and running it):

- `real_capture` was a provenance label only. It recorded in auto mode,
  with `secrets_only` redaction, and accepted `fault()` / `label()` ground
  truth; the parser accepted fault records in a real capture.
- Replay's recording-overhead files were labelled `synthetic`.
- No wall-clock start time; the server's `serverInfo.name` was masked.
- Shadow `call` events said `action` and `auto_action` but not whether auto
  mode would have changed the arguments (`auto_action: forward` covers both).
- Nothing summarized a recording; replay's limitation text always said the
  corpus came from `record_corpus.py`.

**Decisions** (conservative; documented in REAL_CAPTURE.md):

- **Shadow only, enforced twice:** `StdioRelay` raises on a `real_capture`
  recorder outside shadow (or with passthrough); the CLI exits 2 before
  starting the server. `--retry auto` / `--restart N` / `--call-deadline` stay
  allowed: in shadow they only add *proposals* (tested with them on).
- **Mask only:** `Recorder` raises `ValueError` at construction for
  `real_capture` + `secrets_only` (the one place a recorder raises); the
  parser refuses such a header. One redaction path; a test checks a real
  capture's records equal a fault-injection recording's of the same traffic.
- **Ground truth refused, not faked:** `fault()` / `label()` are no-ops on
  `local_development`, `real_capture`, `replay`, counted in `end`
  (`ground_truth_refused`); the parser refuses such files.
- **Byte identity without content:** `diag.digest` = HMAC-SHA256 under a
  per-recorder random key never written. Found necessary by a flaky test:
  redaction is stateful (schemas are learned mid-session), so comparing
  redacted bodies called an identical call "changed" when it was recorded
  before the listing arrived.
- **Audit log:** with `real_capture`, `relay_started` names only the
  server program and argument count. The audit log stays unredacted
  otherwise (documented, pre-existing).

**Evidence summary** (`adapters/mcp/evidence.py`, schema
`stateguard.mcp.evidence/1`): `wire`, `observed` (T11 classes of server
answers), `stateguard_observed`, `proposed` (`hypothetical: true` in shadow;
`acted` must be zero), `unknown` (never success or failure;
`answer_seen_later` kept apart), `not_claimed`.

**Tests** (VERIFIED, 53 at `7984fc3`; 65 after the number hardening, 45 + 20): `tests/adapters/mcp/test_real_capture.py` (33:
provenance families, mask requirement, ground-truth refusal in recorder and
parser, truncated capture, identical redaction, secret fragments absent,
one-time-code keys, server identity, digest, evidence separation and
determinism) and `tests/examples/test_real_capture.py` (20, **harness, not
production evidence**: in-process shadow session with repairable,
refusable, transient, auth, dropped and late calls and a server death,
`retry="auto"` and `max_restarts=1` set; byte-identical forwarding, no
retry/restart/drop/invented answer, K event, UNKNOWN unsurfaced and
unretried, recording real_capture with no fault/label and no secrets,
replay of the capture; relay refusal in auto/passthrough; five CLI refusal
combinations with the server never started; defaults never claim
real_capture; CLI capture against the local fake server with a credential in
the server command line, absent from recording and audit log).

**Hardening: numbers** (separate commit). Review found that mask mode kept
integers below 1,000,000 and floats, so a numeric OTP/PIN under an ordinary
key would reach a real capture. Now `RedactionPolicy.mask_numbers` (forced
for `real_capture`, header rule checked by the parser) reduces every number
to sign and order of magnitude, stops numeric schema types vouching for
numeric strings, and masks pure-number identifiers and dict keys. Kept, as
protocol/contract: ids, `error.code`, tool schemas, published
`enum`/`const` values, HTTP statuses 100-599 under `error.data` status keys
(T11 still classifies recordings), the relay's audit counters and `config`.
Generated recordings are unchanged (the committed tapes re-record
byte-identically). Tests: OTP/PIN/verification numbers, numeric strings,
ordinary numbers, enum/const, floats, nested dicts, lists, embedded JSON,
`_meta`, structured results, `error.data`, numeric keys and identifiers,
non-obvious keys, credential strings, real vs every other provenance, forced
policy, parser refusal.

**Benchmarks after the change** (VERIFIED, synthetic, not real-world
evidence):

- `replay.py`: outcome table identical to main (fail raw 31 / shadow 31 /
  auto 11 / auto_ops 8; prevented auto 20; Z 0; duplicate executions 0);
  fidelity 68/68 answers, 58/58 decisions; determinism 40 runs, 0
  mismatched. Tapes re-recorded only for the new fields (`would_modify`,
  server name kept).
- `race.py --runs 20`: 0 unsafe bypasses.
- `auth.py`: 32/32 classified, 0 auth retried, 0 UNKNOWN retried.

**Not done:** no real capture has been taken. Nothing connected to a public
MCP server or external service.

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
| restart idle-only | 109 | **58** | 20 | 0 | 38 | 0 | **0** | **0** | **0** | 45 | 40 | 20 | 20 |
| restart after-unknown | 109 | **82** | 20 | 0 | 14 | 0 | **0** | **0** | **0** | 45 | 55 | 35 | 20 |

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
  - Failed attempts: 20. That is 5 unrestartable sessions × 3, plus the 5
    restarted servers in the dies-on-start sessions that die before serving
    anything.
  - Succeeded: a restarted server that then served its tool list.
  - **Corrected after T14 CI.** The dies-on-start fixture used to kill its
    server with a 20 ms wall-clock timer. That server sometimes listed its
    tools first, so it counted as a successful reconnect (the earlier 25/15
    and 40/15). On slow CI runners (macOS) a call could also be written
    inside the window: it died in flight, was correctly UNKNOWN, and was
    never replayed, but `test_unknown_is_never_resolved_by_a_restart`
    failed.
  - The fixture now marks that server dead before it serves anything, which
    is deterministic. Invocation outcomes are unchanged: recovered 58 / 82,
    UNKNOWN 20, replays 0, duplicate executions 0, Z 0.

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

**M11 — T13 record/replay** (`benchmarks/runtime/replay.py` on
`benchmarks/runtime/tapes/`). **FAULT INJECTION ON A SYNTHETIC WORKLOAD — NOT
REAL-WORLD EVIDENCE.** VERIFIED by running.

The corpus is 8 sessions (A–H), 68 client requests, 59 `tools/call`, of
which 58 are labelled (category labels: A 45, B 2, E 5, F 1, G 1, H 1, I 2,
K 1). Redaction: mask mode, 847 values changed, every recording complete.

| Arm | Failures (of 58) | Hidden | Prevented (on counterfactual) | Recovered (on counterfactual) | Safely escalated | Z | UNKNOWN | Dup. exec | Counterfactual answers | Restarts | Retries | p50 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| raw | 31 | 12 | — | — | — | — | 0 | 0 | 0 | 0 | 0 | 0.18 |
| shadow | 31 | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1.99 |
| auto | 11 | 6 | 20 (17) | 0 | 2 | 0 | 2 | 0 | 17 | 0 | 0 | 2.02 |
| auto_ops | 8 | 3 | 20 (17) | 3 (1) | 3 | 0 | 3 | 0 | 18 | 1 | 1 | 2.03 |
| as_recorded | 28 | 7 | 0 | 3 (0) | 2 | 0 | 2 | 0 | 0 | 2 | 0 | 1.97 |

How to read it:

- **Prevented rests mostly on counterfactuals.** 17 of auto's 20
  prevented failures are H (drift) calls whose repaired arguments the
  recording never saw. The server's answer to them is the `TapeServer`'s
  model (its own argument check), not an observation. Only 3 are fully
  observed.
- **Recovered.**
  - D: 2 calls ran exactly once on the restarted server, observed (the
    recording restarted too).
  - E (as-recorded only): 1 call after an after-unknown restart.
  - B (auto_ops): 1 retry of a transient error. This is counterfactual:
    the recording never retried.
- **UNKNOWN is contained, not resolved.**
  - C: the in-flight call when the server died before executing.
  - E: one dropped response, one death after executing. These count as
    "safely escalated" where raw left them silent or closed.
  - Each executed once and was never re-sent.
- **Fidelity:** replaying under the recorded configuration reproduces
  68/68 client-visible answers and 59/59 StateGuard decisions.
- **Determinism:** 40/40 (session, arm) runs give identical outcomes when
  replayed twice. Re-recording the corpus gives identical per-stream
  content (byte-identical in 3 of 3 trials, apart from the interleaving of
  concurrent lines).
- **Z = 0 and duplicate executions = 0** in every arm. Shadow ≡ raw for
  every outcome.
- **Unmoved by any arm:** B's tool error after a side effect; G's auth
  error; H's result that breaks its `outputSchema` (detected, never
  altered).
- **Latency** is in-process pipes on one machine, not network latency.
  Raw's p50 (0.18 ms) is a byte relay; the StateGuard arms add about 1.8 ms
  p50.
- **Recording overhead** (auto arm, 189 calls × 3 runs), p50 per call:
  - none 1.92 ms;
  - synchronous recorder 3.80 ms (+1.88);
  - background recorder 4.44 ms (+2.52).

  In one process the background writer still competes for the interpreter.
  Its benefit is that a stalled sink cannot block traffic, not lower CPU.
  About 39 KB per session (H, 38 requests, dominates).
- **Replay overhead:** parse + derive 0.6–0.9 ms per small session, 8 ms
  for H.

## Known risks / unresolved categories

- **Unresolved or partly resolved:**
  - **C:** detect only.
  - **D (session):** not exercised by the measurement. The initialize replay
    is unit-tested only.
  - **E:** an in-flight death and an unrestartable server stay unresolved;
    in-flight deaths are contained as UNKNOWN.
  - **F / G / H / I:** only operator-declared read-only/idempotent tools are
    recovered. The rest is surfaced, not resolved.
  - **K:** classified (T11) and never retried; still resolved only by the
    operator. No operator callback exists; the audit event is the signal.
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
- **Measurement is synthetic throughout.** Record/replay infrastructure
  enables real-traffic measurement but does not itself constitute
  real-traffic evidence. No `real_capture` recording exists yet.
- **Replay limits** (T13): sequential only; timing not replayed (deadline
  effects not reproduced); counterfactual answers are model predictions;
  decisions that depend on masked values can differ (fidelity is measured,
  59/59 on this corpus).
- **Redaction residuals** (T13): `secrets_only` mode, and a credential
  without a recognised shape stored as a word-like dict key, persist. Mask
  mode keeps string *lengths*.
- **Recording overhead** is +1.9 ms (synchronous) / +2.5 ms (background)
  p50 per call in-process. A synchronous recorder (library default) waits
  for its sink.
- **The relay's own audit log is not redacted** (`internal_error` exception
  text, decision reasons; `relay_started` server command except under
  `real_capture`). Pre-existing; only the recording path is redacted. For a
  real capture it must be kept private (REAL_CAPTURE.md).
- **Real-capture residuals** (after the number-masking hardening): numbers
  keep sign, type and order of magnitude; strings keep length; values equal
  to a published `enum`/`const` member, JSON-RPC ids, `error.code`, schema
  contents and digits inside identifier-shaped protocol strings are kept.
  Full list: REAL_CAPTURE.md "Residual risks". Other provenances still keep
  numbers below 1,000,000.
- **Mid-restart refresh race:** RESOLVED in T14 (0 unsafe bypasses across 60
  race-provoking replays; 83 across the same 60 before the fix). See the T14 section for the
  residual limits.

## Next task

Recommended next:

- **First real capture:** follow `docs/runtime/REAL_CAPTURE.md`
  ("Procedure") against a stdio MCP server you control. This needs a
  deployment and an operator decision, not code.
- **T9:** circuit breaker.
