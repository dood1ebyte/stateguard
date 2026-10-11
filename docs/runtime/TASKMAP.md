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
| JSON Schema 2020-12 coverage | PARTIAL | probe: refused `allOf` (even single `$ref`), `uniqueItems`, `multipleOf`, `min/maxProperties`, `dependentRequired`, `contains`, `contentMediaType`, `$id`/`$anchor`, `true` subschema, unknown keywords | n/a | VERIFIED. Refusal makes the tool unvalidated, not broken, *if the host fails open*. |
| Relay fail-open on unreadable schema | COMPLETE (T2, `384fe2a`) — was **UNSAFE** | default now `--unreadable forward`; `test_stdio_relay_protocol.py` | both | VERIFIED. |
| Relay fail-open on internal error | COMPLETE (T2, `384fe2a`) — was **UNSAFE** | `test_relay_fail_open.py`: exception / RecursionError / gate failure / sync failure / client-message failure → forwarded once, unmodified, logged `category: "Z"` | both | VERIFIED. Single-write structure: no StateGuard-made double send. |
| Kill switch | COMPLETE (T2) | `--passthrough`; `test_passthrough_is_a_byte_relay` | — | Also the raw-baseline arm. |
| Protocol-aware refusal (`resultType`) | COMPLETE | `jsonrpc.tool_error_response`; SDK e2e test | auto | |
| Stdio relay | PARTIAL | `examples/mcp/stdio_proxy.py`; SDK e2e + scripted tests | both | Example only; no reconnect, no timeouts. |
| Audit log | PARTIAL | relay JSON lines, value-free | both | No invocation ledger / execution state. |
| Telemetry hooks | PARTIAL | `telemetry/hooks.py` `ITelemetryHook` (repair events only) | n/a | No runtime-event hooks. |
| Effect classification | MISSING | grep: no effect model | — | Annotations stored only, never used in decisions (VERIFIED, risk D safe). |
| Outcome / invocation ledger | MISSING | — | — | |
| Execution state | MISSING | — | — | |
| Retry decision logic | MISSING | — | — | |
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
| T5 | JSON Schema 2020-12 coverage: annotation keywords, simple assertions, single-branch `allOf`, `true` subschema; differential-verified | 2/4 | TODO | T4 (to measure) |
| T6 | Execution state + invocation ledger (request id → SENT / RESPONDED / FAILED_BEFORE_SEND / UNKNOWN) | 5 | TODO | T4 |
| T7 | Effect model: operator-declared only; annotations recorded as untrusted hints | 5 | TODO | T6 |
| T8 | Retry decision via TrustPolicy: not-sent or operator-idempotent only; ambiguous → ESCALATE | 5 | TODO | T6, T7 |
| T9 | Circuit breaker per (server, tool), Shadow-first | 5 | TODO | T6 |
| T10 | Result integrity: `outputSchema` / `structuredContent` validation, Shadow-only | 4 | TODO | T4 |
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

## Known risks / unresolved categories

- **Unresolved:** C (detect only), D, E, F, G, H, I, J, K — no runtime
  primitives yet. Z is not counted per run until T4.
- **Opposite lexicon is finite.** Pairs outside it and outside the affix rule
  (e.g. `buy`/`sell` as single tokens) fall back to similarity, which is
  usually low for true antonyms. This is measured, not assumed.

## Next task

T6: execution state + invocation ledger in the relay. It is deterministic,
and needs no effect model, for three uses:

- (a) drop a second response for an id already answered (J);
- (b) when the server's pipe closes, answer every outstanding call with an
  explicit "delivery uncertain, may have executed" error instead of
  silence (E, I): surface, never hide;
- (c) record per-call execution state for T7/T8.

The measurement gains a "hidden vs surfaced" split.
