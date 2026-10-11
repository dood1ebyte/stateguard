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
| Stdio relay | PARTIAL | `examples/mcp/stdio_proxy.py`; SDK e2e + scripted tests | both | Example only; no reconnect, no timeouts. |
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
| Result validation (`outputSchema`) | MISSING | results pass through untouched (VERIFIED) | — | Safe absence; must start Shadow-only. |
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
| T10 | Result integrity: `outputSchema` / `structuredContent` validation, Shadow-only | 4 | IN PROGRESS | T4 |
| T11 | Auth-signal classification + operator callback | 4 | TODO | T6 |
| T12 | Stdio reconnect when idle; in-flight → escalate | 5 | TODO | T6, T8 |
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

## Known risks / unresolved categories

- **Unresolved:** C (detect only), D, E, F, G, H, I, J, K — no runtime
  primitives yet. Z is not counted per run until T4.
- **Opposite lexicon is finite.** Pairs outside it and outside the affix rule
  (e.g. `buy`/`sell` as single tokens) fall back to similarity, which is
  usually low for true antonyms. This is measured, not assumed.

## Next task

T10: result integrity (Shadow-only).

Check `CallToolResult` shape, and `structuredContent` against a declared
`outputSchema`. Log the finding (category H) and never alter the result.

Then T12 (reconnect), T11 (auth classification), T9 (breaker), T13
(record/replay).
