# Changelog

All notable changes to StateGuard will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **MCP traffic recording with redaction** (`stateguard.adapters.mcp.recording`:
  `Recorder`, `Redactor`, `RedactionPolicy`, `Provenance`, `read_recording`).
  - **Format.** Versioned JSON lines (`stateguard.mcp.recording` v1). It
    holds every wire line in all four directions with ids, methods, tools and
    server generation; relay audit events; injected faults; and ground-truth
    labels.
  - **Diagnostics are kept apart.** Timing lives under `diag`, so it is
    never needed to replay.
  - **Redaction runs before anything is written.** It is deterministic and
    rule-based:
    - credential key names remove the value;
    - credential value shapes are removed anywhere;
    - other free-form text is masked shape-preservingly;
    - only protocol structure and schema-vouched argument values are kept.
  - **A recorder never raises into its host.** On failure it stops, and the
    file says it is incomplete.
  - **The parser raises `RecordingError` only**, and never echoes content.
- **The stdio relay records** (`--record FILE`, `--record-provenance`,
  `--record-redaction`). The taps run after a line is written or read, so
  they cannot change traffic. A failing recorder is logged once
  (`recording_stopped`) and traffic is unchanged.
- **Replay benchmark** (`benchmarks/runtime/replay.py`, corpus
  `benchmarks/runtime/tapes/` from `record_corpus.py`).
  - It replays recorded sessions A–H through the relay in the raw, shadow,
    auto, auto_ops and as-recorded arms. The server side is an in-process
    `TapeServer` built from the recording.
  - **Fidelity is checked.** Replaying under the recorded configuration
    must reproduce every recorded answer and StateGuard decision.
  - **Counterfactuals are marked and counted.** These are answers to
    traffic the recording never saw.
  - **The report says when the corpus is not real traffic.** The committed
    corpus is fault injection.
- **Session recovery workload** (`benchmarks/runtime/recovery.py`):
  deterministic multi-call sessions with a server failure injected, run in
  raw, shadow, auto, restart idle-only and restart after-unknown arms.
  - Every invocation ends as exactly one of: succeeded, recovered (ran once
    on a restarted server), unknown, not sent, connection closed, silent, or
    other error.
  - Counted separately, never folded in: replays, duplicate executions,
    StateGuard-induced failures, server deaths, and reconnects attempted,
    succeeded and failed.
- The relay clears its tool registry when it restarts a server, so nothing
  is validated against the dead process's schemas.
- **Safe server restart in the stdio relay** (`--restart N`, off by
  default).
  - **When it restarts.** A server that exits while idle is restarted. The
    `initialize` handshake is replayed if the session used one (a
    protocol-version change is refused), and the tool list is fetched again
    before it is trusted.
  - **Requests in flight are never re-sent.** Each is answered "outcome
    unknown" and stays `UNKNOWN`, even if the new server succeeds.
    `--restart-policy idle-only` (the default) does not restart at all after
    such a death; `after-unknown` restarts once they have been answered.
  - **Requests arriving mid-restart** wait, bounded. If they cannot be sent,
    they are answered "not sent" (a certain non-execution).
  - **Restarts are bounded and logged.** When they run out, the relay stops.
- **Result integrity, detection only** (`check_tool_result`, `ResultCheck`,
  `ResultIssueKind`):
  - A `tools/call` result is checked for `CallToolResult` shape, and for
    `structuredContent` against the tool's `outputSchema`. Findings name
    paths and violation types, never values.
  - A missing `structuredContent`, or an `outputSchema` StateGuard cannot
    read, is an advisory, not a failure.
  - The relay logs findings as category H and delivers every result
    unchanged. StateGuard never repairs a result.
- **More of JSON Schema 2020-12 is read instead of refused.**
  - Supported now: the annotations `$anchor`, `contentEncoding`,
    `contentMediaType`, `contentSchema` and OpenAPI `example`; the boolean
    schema `true`; and single-branch `allOf` beside annotation-only siblings
    (the `{"allOf": [{"$ref": ...}], "description": ...}` shape generators
    emit).
  - `multipleOf` and `uniqueItems` are enforced. They agree with the
    `jsonschema` reference validator on generated instances, in both
    directions.
  - Keywords that may loosen validation (`nullable`) stay refused, as do
    multi-branch `allOf`, `not`, conditionals and object-level assertions.
- **Effect model and retry decision** (`EffectPolicy`, `Effect`,
  `decide_retry`, `Observed`, `RetryVerdict`):
  - Effects are declared by the operator per `(tool, server)`: read-only,
    idempotent, non-idempotent, or unknown (the default). MCP tool
    annotations are recorded as untrusted hints and never change a
    declaration.
  - `decide_retry` is a rule, not a score. It never retries after an auth or
    protocol error, never beyond one retry, and never a tool not declared
    safe to repeat. An unknown outcome on such a tool is escalated.
- **The stdio relay can retry**, with `--effects FILE` and `--retry auto`.
  The default `--retry shadow` logs every decision and retries nothing.
- **`InvocationLedger`** (`stateguard.adapters.mcp`): per-request execution
  state for a proxy. The states are `PENDING`, `ANSWERED_LOCALLY` (never sent,
  certainly not executed), `SENT`, `COMPLETED`, and `UNKNOWN` (sent, no answer
  coming: may or may not have executed). `response(id)` says whether to
  deliver a server response or drop it as a duplicate or late answer.
  `transport_closed()` and `expire(deadline)` resolve in-flight requests as
  `UNKNOWN`. It decides nothing alone and does no I/O. A property test drives
  random interleavings: at most one answer per request, and `UNKNOWN` is never
  rounded to success or failure.
- **The stdio relay uses the ledger** (auto mode; shadow only logs):
  - A second response for an answered request is dropped.
  - If the server dies, every request in flight gets an explicit JSON-RPC
    error saying the outcome is unknown, instead of silence.
  - `--call-deadline` (opt-in) does the same for unanswered requests and
    drops the late answer.
- **`benchmarks/runtime/`**: before/after measurement per failure category,
  with seeded fault injection and raw, naive-retry, shadow and auto arms.
- **`ToolRegistry`** (`stateguard.adapters.mcp`): the in-memory record of a
  server's tools, keyed by `ToolKey(name, server)` so two servers' `search`
  tools never collide. `sync(tools, server=)` replaces a listing atomically
  and returns a `SyncReport` (added / removed / updated / unchanged /
  unusable / breaking, with `explain()` and `to_dict()`); `register`,
  `compare` (dry run), `remove`, `clear`, `get`, `find`, `tools`, `servers`.
  `process_call(name, arguments, server=)` validates against the *current*
  definition only: a replacement StateGuard cannot read makes the tool
  unusable (`ToolSchemaUnusableError`) rather than leaving the old schema
  in force. Each `RegisteredTool` is a frozen snapshot with its state
  (`READY` / `INVALID` / `UNSUPPORTED`), error, fingerprints, extraction
  warnings and revision. Description changes are reported even when the
  schema is unchanged. Re-syncing an unchanged listing reuses entries
  (about 0.6 ms for 35 tools). Thread-safe.
- **Schema identity** (`stateguard.adapters.jsonschema`):
  `fingerprint_schema` returns a `SchemaFingerprint` with a *document*
  digest (exact canonical JSON) and a *contract* digest (over
  `normalize_schema`, which drops annotations and `x-*` keys, resolves
  local `$ref`s, sorts order-free lists and unifies equivalent spellings).
  The MCP contract cache now keys on the document fingerprint, so the
  cache, the registry and change detection share one definition of "same
  schema".
- **Structural schema diff.** `diff_schemas(before, after)` → `SchemaDiff`
  with a status (`UNCHANGED`, `EQUIVALENT`, `COMPATIBLE`, `INCOMPATIBLE`,
  `INVALID`, `UNSUPPORTED`) and every `SchemaChange` (kind, path, keyword,
  before/after, `breaking`, detail). `COMPATIBLE` is a guarantee — every
  payload the old schema accepted, the new one accepts — held to by
  mutation property tests against the validator; anything not provably
  safe is breaking.
- **JSON-RPC boundary helpers** (`stateguard.adapters.mcp.jsonrpc`, main
  names re-exported): `ToolCallRequest.parse`, `forward_message` (the
  original message, unmodified, when arguments are unchanged, otherwise
  only `params.arguments` replaced — `id` and `_meta` preserved),
  `refusal_response` / `tool_error_response` (a `CallToolResult` with
  `isError: true`, with `resultType` for protocol `2026-07-28`+),
  `error_response`, and `ToolListAccumulator` for paginated `tools/list`.
- **`stateguard diff BEFORE AFTER`** compares two tool definitions, bare
  schemas or whole `tools/list` results; exits 1 if anything is breaking,
  for CI gating. **`stateguard replay --tools --calls`** runs recorded calls
  (JSON array or JSON lines; plain or raw `tools/call` messages) and
  summarises the outcomes; with `--shadow` it also prints what auto mode
  would do.
- **An SDK-free stdio relay** (`examples/mcp/stdio_proxy.py`): launched in
  place of a server, it passes every message through byte for byte, keeps
  a registry synced from `tools/list` and `list_changed`, repairs or
  answers `tools/call`, and writes a value-free JSON-lines audit log.
  Tested end to end against the MCP Python SDK and byte by byte against a
  scripted server that changes a schema mid-session.
- **Array-of-object validation.** `FieldSpec.item_spec` carries the element
  contract and `ContractValidator` checks each element, reporting paths
  like `files[0].path`. Element violations are refused, never repaired.
- **`exclusiveMinimum` / `exclusiveMaximum` are enforced**
  (`FieldConstraintType.EXCLUSIVE_MINIMUM` / `EXCLUSIVE_MAXIMUM`), in the
  numeric form and draft 4's boolean form. They were dropped with a
  warning, so `mcp-server-fetch`'s `max_length: 0` passed StateGuard and
  was rejected by the server. A differential run against the `jsonschema`
  reference validator now finds no disagreement in either direction.
- **`MalformedSchemaError`** (a subclass of `UnsupportedSchemaError`)
  separates "not valid JSON Schema" from "valid but unsupported";
  `MalformedReferenceError` likewise for `$ref`. MCP-specific errors share
  a base, `MCPAdapterError`: `InvalidToolDefinitionError`,
  `ToolNotFoundError` (with `known` and `elsewhere` — the servers that do
  have that name), `ToolSchemaUnusableError` (`.cause`),
  `MalformedMessageError`, `ForwardingInvariantError`.
- **An MCP drift corpus** (`tests/adapters/mcp/corpus/drift_corpus.json`):
  14 tools, 37 labelled calls and 20 labelled schema changes, each checked
  for its label and for the cross-cutting invariants; plus an outcome truth
  table, property tests over generated drifted calls, and hostile-input
  tests.
- **`benchmarks/mcp_bench.py`**: per-listing and per-call timings for the
  MCP hot paths.
- **`docs/mcp-adapter.md`** (usage, outcome table, registry, diff, CLI,
  bounds, non-claims) and **`docs/mcp-architecture.md`** (layers,
  interfaces, invariants, dependency rules).
- **`repair_tool_call(guard, tool, arguments)`** in `stateguard.adapters.mcp`:
  repair plus `outcome_for` in one step, reading `arguments=None` as `{}`
  as MCP specifies. The recommended entry point for a proxy.
- **Per-field explanations.** `RepairResult.field_decisions()` reports each
  field as `repaired` / `held` / `discarded` / `unresolved`, with the
  operation, its trust, risk and evidence, and the recorded reason.
  `RepairResult.explain()` renders that as deterministic, value-free text
  (safe to log); `RepairResult.to_dict()` is the full record as JSON-ready
  data. `ToolCallOutcome.explain()` / `to_dict()` lead with the MCP action.
  All derived from what the engine already recorded — nothing is re-scored.
- **`stateguard check --tool FILE`** checks a call's arguments against an
  MCP tool definition or a bare JSON Schema and prints what a repairing
  proxy would do, field by field (`--json` for the full record). An
  unreadable schema exits 2 with the named error.
- **`RepairAttempt.kept`**, `False` for an attempt the engine rolled back.
  Repair-history records carry it too.
- **Contract-drift tests** over the real-schema corpus
  (`tests/adapters/mcp/test_contract_drift.py`), pinning: safe drift is
  repaired exactly, ambiguous and invalid calls are never forwarded,
  unsupported schemas raise, and no forwarded value lacks provenance.
- **Tool-call repair for MCP.** `ContractGuard.with_mcp()` repairs the
  arguments an agent sends to an MCP tool, against the server's declared
  `inputSchema`. Accepts a full tool definition or a bare input schema, in
  either spelling of the key (`inputSchema` on the wire, `input_schema` as
  the Python SDK names it). Extracted contracts are cached on
  `(tool name, schema content)` — not `contract_id`, which collides between
  tools with coincidentally identical signatures — so a proxy fetches
  `tools/list` once, and a server that changes a signature is picked up
  rather than papered over. `stateguard.adapters.mcp.outcome_for` maps a
  `RepairResult` onto `FORWARD` / `HOLD` / `ESCALATE` / `REFUSE`. Zero extra
  dependencies, enforced by an import-isolation test.
- **A runnable MCP demo** in `examples/mcp/` — an ordinary MCP server, a
  `RepairingClient` proxy, and a before/after script that speaks real MCP
  over stdio. Three drifted calls the server rejects now succeed; a fourth
  is refused rather than repaired, because no honest repair exists for it.
- **`ContractGuard.with_json_schema()`** and the `JSONSchemaAdapter` behind
  it: a supported subset of JSON Schema (`MCP_ADAPTER_PLAN.md` §5) with
  `$ref`/`$defs` resolution, cycle *and* depth guards, and a ReDoS screen on
  schema-supplied `pattern` values. Keywords outside the subset raise rather
  than being ignored, so a `SUCCESS` never quietly means "validated less
  than the schema asked". See `docs/adr/0001-json-schema-source-of-truth.md`.

- **A real-schema corpus.** 21 tool schemas captured from four public MCP
  servers (`mcp-server-git`, `-sqlite`, `-time`, `-fetch`) by running them
  over stdio and recording `tools/list` verbatim, with package version and
  capture date. All 21 extract without refusal and round-trip; the capture
  script is committed so the corpus is reproducible, and the fixtures are
  committed so the suite needs no network.
- **A false-positive suite over that corpus.** An unrecognisable key is never
  renamed onto a declared field (swept across 29 required parameters); a
  plausible abbreviation either repairs to the correct field or refuses
  (18/18 correct, including `source`/`target` → `source_timezone`/
  `target_timezone` on one call); a genuinely ambiguous key is refused rather
  than guessed.
- **`docs/jsonschema-adapter.md`** — the adapter guide: supported-subset
  table, what refuses versus what warns, the caching key, and the known
  limitations.

### Fixed
- **The stdio relay aborted (SIGABRT) when its server exited first.**
  Interpreter shutdown found a daemon thread holding stdin's lock. The relay
  now flushes and exits directly with the server's exit code.
- **The relay could act on a stale tool listing.** Under protocol
  `2026-07-28`, servers send `tools/list_changed` only on
  `subscriptions/listen` streams (verified in MCP SDK 2.3.0), and the relay
  holds none. After an unannounced schema change it refused or rewrote calls
  that were valid for the new schema: 5 of 5 in the staleness scenarios.
  The relay now re-lists before any intervention (repair, refusal,
  escalation) and decides again on a listing requested after the call
  arrived. If none arrives, it forwards unchanged. Calls that pass unchanged
  cost no extra round trip. `--no-listing-refresh` turns this off.
- **The stdio relay failed closed.** By default it refused every call to a
  tool whose schema StateGuard cannot read (valid JSON Schema such as
  `allOf` or `uniqueItems`), so valid calls to valid tools were blocked. An
  internal StateGuard error in auto mode reached the client as `-32603`, and
  any other exception escaped the relay thread and stopped all relaying.
  The relay now fails open: it forwards the original call exactly once and
  logs the failure as category `Z`. `--unreadable` defaults to `forward`,
  and `--passthrough` is a kill switch that turns StateGuard off.
- **Opposite-meaning parameter names were renamed onto each other.**
  `unlock` became `lock`, `invisible` became `visible`, `maximum` became
  `minimum`, `no_cache` became `cache`, and the call was forwarded meaning the
  reverse of what the model wrote. The earlier word-substitution guard only
  saw multi-word names that shared a word. A lexical, closed opposition rule
  (`opposed_names`: an opposite-word list, negating prefixes, opposed prefix
  pairs, negator words) now records a contradiction, which `TrustPolicy`
  never applies. Measured on a new 118-case calibration family: 103 wrong
  renames applied before, 0 after; applied precision on the expanded corpus
  28.7% → 100%; all 41 must-apply repairs unchanged; 0 of 1,161 legitimate
  drift variants of real parameter names misflagged.
- **Exponential `$ref` expansion.** A schema of nested `$ref`s that each
  reference the previous level twice took 3.4 s to read at 16 levels and
  grew ×2 per level. Expansion is now capped at 10,000 resolutions
  (`DEFAULT_MAX_EXPANSIONS`); beyond it the schema is refused with
  `SchemaReferenceError`.
- **Deeply nested arguments crashed the engine** with `RecursionError`
  (about 900 levels). Payloads nested deeper than `MAX_PAYLOAD_DEPTH` (100)
  are refused with a value-free reason, checked iteratively.
- **Arrays of objects were reported as matched without being checked.**
  See *Array-of-object validation* above.
- **`properties: []`** and property names containing `[` / `]` are refused
  rather than mis-read.
- **A shadow-mode call that needed nothing** explained itself as
  "proposed, not applied".
- **Shadow mode blocked calls.** A call auto mode would refuse or escalate
  was refused or escalated in shadow too, so a shadow deployment blocked
  real traffic — including every call StateGuard refused *wrongly*, which is
  the error shadow exists to surface. Shadow outcomes are now only `FORWARD`
  or `HOLD`, always with the arguments as received. `ToolCallOutcome` gains
  `auto_action` (what auto mode does, in either mode), and
  `original_arguments` / `proposed_arguments`. **Behaviour change** for
  shadow callers that relied on `REFUSE`/`ESCALATE`: read `auto_action`.
- **A final forwarding boundary.** Every `ToolCallOutcome` passes one
  invariant gate (shadow forwards only what it received; auto forwards only
  the committed payload; no held key is moved), and `repair_tool_call`
  re-validates an auto-mode payload before releasing it. A violation raises
  `ForwardingInvariantError` — a StateGuard bug, never forwarded.
- **`pip install "sguard[all]"` installed an unrelated package.** The `all`
  extra named `stateguard[...]`, but this distribution is `sguard` and
  `stateguard` on PyPI is a different project (with a torch /
  sentence-transformers dependency tree); `uv.lock` had resolved it that
  way. It now self-references `sguard[...]`, the lock is regenerated (it
  had also never locked the `mcp` extra), and the install hints in
  `with_pydantic()` and the pydantic adapter say `sguard[pydantic]`.
- **`with_pydantic()` without pydantic** constructed a guard that then
  failed on the first `repair()` with a bare `ModuleNotFoundError`. It now
  raises the documented `ImportError` with the install line at construction.
- **`5.0` for an integer field was refused.** JSON Schema counts it as an
  integer and Pydantic accepts it, but StateGuard reported `FAILED` (an MCP
  `REFUSE`). An integral float is now coerced to `int` — exact both ways,
  `REVERSIBLE`. `5.5`, NaN and infinities are still refused.
- **`arguments=None` for a tool without parameters was refused.** Via
  `repair_tool_call`, absent arguments are `{}`.
- **`HOLD` carried `arguments=None`** while `should_forward` was `True`, so
  `if o.should_forward: send(o.arguments)` sent nothing in shadow mode.
  `arguments` is now the received payload under `HOLD` (never the preview),
  and is non-`None` exactly when `should_forward` is true.
- **Reasons for a JSON-encoded argument object** read "Repaired 0
  field(s)" (auto) or claimed the arguments "matched the tool's schema"
  (shadow). They now name the root normalisation.
- **A rolled-back attempt was indistinguishable from a kept one** in
  `result.attempts` and in the repair history, so an audit reported
  repairs that are not in the payload. See `RepairAttempt.kept`.
- **Opposite-meaning renames were applied unsupervised.** With one field
  missing and one unexpected key, `max_price` became `min_price`,
  `updated_after` became `created_after`, `last_name` became `first_name` —
  a confident wrong repair, forwarded. A whole-word substitution (same word
  count, a shared word, and a differing pair that is neither an
  abbreviation nor a one-edit typo) is now recorded as
  `RepairEvidence.contradiction`, and `TrustPolicy` never applies a
  contradicted operation: it is surfaced as ambiguous or rejected. Scores
  and bands are unchanged. Four calibration known gaps closed; applied
  precision on the corpus 91.1% → 100%, with every must-apply case intact.
- The CLI's `--version` is read from the package rather than duplicated.

- **JSON nesting depth is now StateGuard's decision, not CPython's.**
  `json_loads_strict` applies an explicit depth bound
  (`DEFAULT_MAX_JSON_DEPTH`, 100) and length bound
  (`DEFAULT_MAX_JSON_LENGTH`, 10 MiB) *before* the parser runs, so both
  `json_parsed` and the engine's root-shape recovery refuse pathological text
  identically on every interpreter. Previously they relied on `json.loads`
  raising `RecursionError`, which is not one behaviour: measured on
  `"[" * 20_000 + "]" * 20_000`, 3.11 parses it, 3.12 refuses it regardless of
  stack size or `sys.setrecursionlimit` (3.12 added a hard C recursion limit
  that `setrecursionlimit` does not reach), and 3.14 parses it on a 64 MiB-stack
  thread while raising on the main thread — because on 3.14 the ceiling is the
  real C stack. "Does this parse?" was therefore a question about how the
  interpreter happened to be launched. The bound removes the question. This is
  the decision `RefResolver` already made for schema nesting; the JSON path had
  simply never been given it. Normal payloads are unaffected: the depth scan
  skips text with 100 or fewer opening brackets via two C-level `str.count`
  passes, and the bounds sit orders of magnitude past real tool-call traffic.
- `SchemaCache._key` no longer lets a `RecursionError` escape. `json.dumps`
  recurses per level, so a deeply nested `inputSchema` raised it from inside
  the cache lookup — which runs *before* extraction, and so pre-empted
  `RefResolver`'s depth bound, the guard that exists to stop exactly that
  error escaping. The MCP path therefore crashed uncatchably on a schema the
  JSON Schema path refused cleanly, which is the wrong way round: the MCP
  path is the one reading schemas from servers nobody controls. It now misses
  the cache and the extractor refuses it as a `SchemaReferenceError`.
- `outcome_for`'s reason line no longer claims "forwarded unchanged" about a
  payload it changed. The wording was chosen by whether a *top-level* key had
  been added, while `JSONSchemaAdapter.wrap` fills declared defaults at every
  depth — so a default on a nested property produced a changed payload under
  an "unchanged" log line, and the shadow variant rendered as "would also
  have filled none named". Reason lines are the audit trail a team reads
  during a shadow rollout to decide whether to turn auto on, so one that
  disagrees with what was sent is wrong in the direction that matters. The
  filled paths are now named at any depth (`'outer.inner'`).
- Shadow mode no longer drops its diff on an `ALREADY_VALID` payload.
  `outcome_for` inferred the mode from `proposed_output` rather than reading
  `RepairResult.is_shadow`, and never asked the question for that status — so
  a payload whose only change was a declared default being filled came back
  as `FORWARD` with no preview and a reason reading "forwarded unchanged",
  about a call auto mode *would* have changed. The payload sent was correct;
  what was lost was the one signal shadow mode exists to give.
- A tool definition with no `inputSchema` now says so, instead of reporting
  `<root>: unrecognised keyword(s) ['name']` and sending the reader after a
  JSON Schema problem that is not there.
- `ContractValidator` now uses `re.search` for `PATTERN` constraints, not
  `re.match`. JSON Schema defers to ECMA-262 and Pydantic's
  `Field(pattern=)` searches, so anchoring at the start produced *false*
  violations — `"abc123"` against `"[0-9]+"` was reported broken while
  Pydantic, the documented source of truth on that path, accepted it. A
  false violation can trigger a repair of a field that was never broken. An
  unusable pattern now raises an error naming the field instead of a bare
  `re` exception.
- `GuardConfig.strict_mode` composes with a contract's own `strict_mode` as
  a **floor** (strict if either says so) rather than overwriting it in both
  directions. A schema declaring `additionalProperties: false` was being
  silently relaxed by the config default of `False`, contradicting
  `ContractSpec.strict_mode`'s own documented precedence. A contract is
  never loosened by configuration. The floor now also reaches *nested*
  contracts: it was applied to the root `ContractSpec` only, so a single
  flag that says nothing about depth made an undeclared key an `ERROR` at
  the top level and a `WARNING` one level down.
- Nine under-validation defects in the JSON Schema adapter, found by two
  rounds of review before release. Most consequential: for
  `anyOf: [{$ref}, {null}]` — the shape Pydantic emits for every
  `Optional[X]` — constraints, declared defaults, and the
  unsupported-keyword screen were all read off a bare `{"$ref": ...}` and
  therefore silently dropped; `allOf` behind such a reference produced a
  field that accepted anything. The same loss survived one level deeper, in
  a chained `Optional[Optional[X]]`, until a second pass caught it. Also:
  unsupported keywords inside union branches and array `items` went
  unscreened; a deep schema raised `RecursionError` instead of a catchable
  error; an untyped `enum` containing `null` produced a false `NOT_NULL`
  violation; a `required` name with no `properties` entry was dropped, so a
  payload missing it was reported valid; and a `required` entry that was not
  a string was coerced rather than refused.
- A declared `default` that does not satisfy its own field is now dropped
  with a `SchemaFeatureWarning` instead of being written into the payload.
  Filling defaults happens after the engine has finished, so nothing
  re-checked the value: a schema declaring
  `{"type": "integer", "default": "abc"}` — or a default left stale when a
  server's `enum` changed — made `repair` return `ALREADY_VALID` and
  `validate` then reject that same output.

### Known limitation

- **Drift on an optional parameter is not repaired.** Found by the new
  corpus. `FuzzyFieldMatchStrategy` pairs an unexpected key with a *missing
  required* field, and an optional field is never missing, so even a
  one-character typo on one goes uncorrected — 29% of the corpus's parameters
  are optional. The sharp edge is an optional field with a declared default:
  `git_diff` takes `context_lines` defaulting to 3, so a model writing
  `context: 10` gets 3 filled in beside its unrecognised key and the 10 is
  silently ignored. It is a *missed* repair, never a wrong one — nothing is
  written into a declared parameter.
- Objects inside a union branch or an array element are validated by *type*
  only; their properties are not. `{"anyOf": [{"type": "string"},
  {"type": "object", "properties": {"n": {"type": "integer"}}}]}` accepts
  `{"n": "not-an-int"}`. `union_members` and `item_type` each carry a single
  `FieldType` and no nested contract. Unsupported *keywords* in those
  positions are refused; their *contents* are not checked.
- `FieldType.BYTES` — declared binary fields (e.g. Pydantic `bytes`
  annotations, previously extracted as `ANY`) are now a first-class
  contract type accepting `str | bytes` values, mirroring the lax
  wire-format rule of the frameworks that declare them. The dict-schema
  adapter accepts `"type": "bytes"`.
- `dict`/`list` → string JSON-serialise coercion: `TypeCoercionStrategy`
  now repairs a `TYPE_MISMATCH` on `STRING`/`BYTES` targets by
  `json.dumps`-ing container values (confidence 0.85; refused when the
  container holds non-JSON values). Also applies to `STRING`/`BYTES`
  members of `UNION` targets. Repairs the failure mode of
  openai-python#2702, where an agent harness passes a parsed JSON object
  to a tool argument declared `str`/`bytes`.
- Python 3.14 compatibility: added `__init__.py` to all test directories
  (`tests/`, `tests/core/`, `tests/core/models/`, `tests/core/errors/`,
  `tests/integration/`, `tests/isolation/`, `tests/logging/`). The mixed
  presence of these files caused pytest collection failures under Python
  3.14's stricter import-system behavior. The fix also resolves the same
  latent inconsistency on Python 3.11 / 3.12 on macOS (zsh, case-insensitive
  APFS) and Linux. Discovered during macOS validation on Python 3.14.6.

### Added

#### M0 — Repository Bootstrap
- Project structure with `src/` layout
- `pyproject.toml` with zero runtime dependencies for core and `pydantic` as optional extra
- GitHub Actions CI pipeline: isolation → lint/typecheck/test (parallel)
- Import isolation test suite (`tests/isolation/`) — verifies core never loads pydantic
- `.python-version` pinned to 3.11
- `py.typed` marker for PEP 561 compliance

#### M1 — Domain Enums and Value Objects
- `FieldType` — abstract field type vocabulary used by the core engine
- `FieldConstraintType` — categories of field-level constraints
- `FieldConstraint` — immutable constraint descriptor (frozen dataclass)
- `RepairConfig` — repair engine configuration with `__post_init__` validation
- `GuardConfig` — top-level guard configuration composing `RepairConfig`
- `ViolationType` — categories of detectable contract violations
- `ViolationSeverity` — ERROR / WARNING severity levels
- `ContractViolation` — mutable violation descriptor with auto-generated UUID
- `FieldOpType` — atomic repair operation types
- `FieldOperation` — immutable repair operation proposed by strategies (frozen dataclass)
- Full test suite for all M1 domain objects
