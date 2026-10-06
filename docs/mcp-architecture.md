# MCP integration architecture

This document describes how StateGuard's MCP integration is put together:

- the layers and what each one owns;
- the interfaces between the layers;
- the invariants each layer guarantees;
- the dependency rules that keep the layers separable.

For how to *use* the integration, see [`mcp-adapter.md`](mcp-adapter.md).
For why JSON Schema is the source of truth, see
[ADR-0001](adr/0001-json-schema-source-of-truth.md).

---

## Layers

```
  MCP client                                                       MCP server
      │                                                                ▲
      │ JSON-RPC                                                       │ JSON-RPC
      ▼                                                                │
┌──────────────────────────────────────────────────────────────────────────────┐
│ 7  Transport / host           (yours, or examples/mcp/stdio_proxy.py)        │
│    owns: bytes, framing, sessions, timeouts, policy for unreadable tools      │
├──────────────────────────────────────────────────────────────────────────────┤
│ 6  JSON-RPC boundary          adapters/mcp/jsonrpc.py                         │
│    ToolCallRequest · forward_message · refusal_response · ToolListAccumulator │
├──────────────────────────────────────────────────────────────────────────────┤
│ 5  Tool registry              adapters/mcp/registry.py                        │
│    ToolKey(name, server) → RegisteredTool · sync/register/compare · process_call│
├───────────────────────────────┬──────────────────────────────────────────────┤
│ 4  Outcome policy             │ 4' Change detection                          │
│    adapters/mcp/outcomes.py   │    adapters/jsonschema/diff.py               │
│    RepairResult → action      │    SchemaDiff · SchemaChangeStatus           │
│    forwarding gate            │                                              │
├───────────────────────────────┴──────────────────────────────────────────────┤
│ 3  Guard                      guard.py  (ContractGuard.with_mcp)              │
│    adapter + engine + config (AUTO / SHADOW)                                  │
├──────────────────────────────────────────────────────────────────────────────┤
│ 2  Contract extraction        adapters/mcp/{adapter,cache}.py                 │
│                               adapters/jsonschema/{extractor,type_mapper,     │
│                               refs,keywords,patterns,identity}.py             │
│    tool definition → inputSchema → ContractSpec   (cached by fingerprint)     │
├──────────────────────────────────────────────────────────────────────────────┤
│ 1  Core                       core/{validator,engine,trust,explain}.py,       │
│                               core/strategies/*                               │
│    ContractValidator · RepairEngine · strategies · TrustPolicy · explain      │
└──────────────────────────────────────────────────────────────────────────────┘
```

Each layer depends only on the layers below it. Layers 1–6 are pure
functions over dicts plus in-memory state, with no I/O. Layer 7 is the only
layer that touches a wire, and StateGuard ships it only as an example.

### 1. Core

`ContractValidator` checks a payload against a `ContractSpec`. That spec is
the framework-agnostic contract: fields, types, constraints, nested specs,
and an `item_spec` for arrays of objects. The validator reports
`Violation`s with paths such as `options.hourly` and `files[0].path`.

`RepairEngine` runs repair strategies in a fixed order: exact alias,
normalised name, fuzzy rename, type coercion, enum normalisation, then
default fill. Each strategy *proposes* an operation with evidence, and
`TrustPolicy` decides whether each proposal is APPLY, AMBIGUOUS or REJECT,
based on its risk band. The engine applies only APPLY operations, then
re-validates. It returns a `RepairResult` with one of five statuses
(`ALREADY_VALID`, `SUCCESS`, `PARTIAL`, `AMBIGUOUS`, `FAILED`), the
payload, every attempt, and the ambiguity candidates.

`core/explain.py` turns a `RepairResult` into per-field decisions
(`REPAIRED`, `HELD`, `DISCARDED`, `UNRESOLVED`) and into text that contains
no argument values.

The core knows nothing about MCP, JSON Schema or Pydantic.

### 2. Contract extraction

`MCPToolAdapter` finds `inputSchema` in a tool definition
(`split_tool_definition`) and delegates to the JSON Schema adapter. The
extractor turns the supported subset into a `ContractSpec`, and refuses
everything else:

- `UnsupportedSchemaError` for valid JSON Schema it cannot represent;
- `MalformedSchemaError` for input that is not valid JSON Schema.

Supporting modules:

- **`refs.py`** resolves local `$ref`s. It bounds depth, refuses recursion,
  and caps total expansion at 10,000.
- **`patterns.py`** screens regexes before they are compiled.
- **`keywords.py`** sorts each keyword into one of four groups: supported,
  rejected, ignored as annotation, or unknown (which is refused).
- **`identity.py`** provides `canonical_json`, `document_fingerprint`,
  `normalize_schema` and `fingerprint_schema`. These are the single
  definition of "same schema", shared by the cache, the registry and the
  diff.

`SchemaCache` is an LRU keyed by `(tool name, document fingerprint)`.
Because the key is the schema's content, a changed schema is a cache miss
and is never served stale. `MCPToolAdapter.compile()` is the path the registry
uses. It skips the cache lookup, so that extraction warnings are recorded
on every registration, and then stores its result in the cache.

### 3. Guard

`ContractGuard.with_mcp(config=…)` combines an adapter, an engine and a
`GuardConfig`. The config sets the mode:

- **`AUTO`** applies the repairs the policy approves;
- **`SHADOW`** computes the same repairs and withholds the payload
  (`proposed_output` instead of `repaired_output`).

The guard holds no MCP-specific state. Layers 4 and 5 call it for repair
and validation. The registry also calls the adapter directly, for
extraction at registration.

### 4. Outcome policy

`outcomes.py` maps a `RepairResult` onto the decision a proxy acts on.

**`_verdict(result, original)`** is the mode-independent decision. It
returns `(auto_action, payload, reason)`:

| `RepairStatus` | `auto_action` | payload |
|---|---|---|
| `ALREADY_VALID`, `SUCCESS` | `FORWARD` | the validated payload |
| `AMBIGUOUS` | `ESCALATE` | none |
| `PARTIAL`, `FAILED` | `REFUSE` | none |

**`outcome_for(result, original)`** derives the mode's view from it:

- In auto mode, `action` is `auto_action`.
- In shadow mode, `action` is `FORWARD` if the auto payload equals the
  original, and `HOLD` otherwise. Either way the original is forwarded.

The order matters. The auto decision is computed first, and the shadow view
is derived from it, so the two modes cannot disagree about a call.

**`repair_tool_call(guard, tool, arguments)`** is the public entry point.
It runs `guard.repair`, then `outcome_for`, and then the *forwarding gate*:

1. `_check_forwarding_invariants` checks structure:
   - `arguments` is set exactly when the call is forwarded;
   - shadow is never `ESCALATE` or `REFUSE`, and forwards only the original;
   - auto mode never produces `HOLD`.
2. `_check_forwarded_payload_validates` re-validates every auto `FORWARD`
   payload against the tool's schema.

If either check fails, `ForwardingInvariantError` is raised. That signals a
StateGuard bug and is never an outcome.

### 4′. Change detection

`diff.py` compares two schemas after normalisation and lists every
structural change. Each change is marked as breaking or not. `COMPATIBLE`
means every payload the old schema accepted, the new schema accepts. A
change is non-breaking only when a rule *proves* it, and the rules are
checked against the validator by property tests. Anything unproven is
breaking.

This layer is a peer of layer 4, not above it. The registry uses both.

### 5. Tool registry

`ToolRegistry` is the in-memory state of "which tools exist and what they
accept". Each entry is a frozen `RegisteredTool`, keyed by
`ToolKey(name, server)`.

A `register` or `sync` does the following for each definition:

1. Computes the definition fingerprint. If it is unchanged, the existing
   entry is kept, which is the fast path.
2. Otherwise, takes a deep copy of the definition and extracts the
   contract, recording warnings, or the `INVALID`/`UNSUPPORTED` state and
   its error. It also fingerprints the schema.
3. Diffs the new schema against the previous entry's schema.
4. Swaps the new entry in under the lock and returns a `ToolChange`.
   `sync` swaps a server's whole listing at once.

`remove` and `clear` only drop entries under the lock.

A **read** (`process_call`) does the following:

1. Under the lock, takes the current entry and the registry's private
   definition copy.
2. Raises `ToolNotFoundError` if there is no entry, or
   `ToolSchemaUnusableError` if the entry is unusable.
3. Calls `repair_tool_call` outside the lock.

**The registry never falls back to a previous definition.** A replacement
that cannot be read makes the tool unusable. It does not leave the old
schema in force.

### 6. JSON-RPC boundary

`jsonrpc.py` is the library's home for the shape of MCP messages. The CLI's
`replay` also reads recorded `tools/call` messages and `tools/list`
results, for its own input formats. It
handles four things:

- **Parsing a `tools/call`.** The message is deep-copied and its depth is
  bounded.
- **Building the forwarded request.** When the arguments are unchanged, it
  is the original message, unmodified. Otherwise only `params.arguments` is
  replaced.
- **Building tool-error results for refusals.** These are shaped for the
  protocol version the request declares.
- **Accumulating paginated `tools/list` responses**, so that `sync` sees a
  complete listing.

It does not handle transport, sessions, handshakes or results.

### 7. Transport / host

This layer is not part of the library. `examples/mcp/stdio_proxy.py` shows
it with a 400-line SDK-free stdio relay, and `examples/mcp/proxy.py` shows
it inside an MCP SDK client. The host decides three things:

- **Which server a listing belongs to**, which sets `ToolKey.server`.
- **What to do with a tool StateGuard cannot read**: block it, or forward
  it unvalidated.
- **Where audit records go.** `explain()` contains no values and
  `to_dict()` does.

---

## Interfaces between layers

| From → to | Interface | Data |
|---|---|---|
| 7 → 6 | `ToolCallRequest.parse(message)`, `ToolListAccumulator.observe_*` | JSON-RPC dicts |
| 6 → 5 | `registry.sync(tools, server=)`, `registry.process_call(name, arguments, server=)` | tool definition dicts, argument values |
| 5 → 4 | `repair_tool_call(guard, definition, arguments)` | → `ToolCallOutcome` |
| 5 → 4′ | `diff_schemas(before, after)` | → `SchemaDiff` |
| 5 → 2 | `MCPToolAdapter.compile(definition)`, `fingerprint_schema(schema)` | → `ContractSpec`, `SchemaFingerprint` |
| 4 → 3 | `guard.repair(tool, arguments)`, `guard.validate(tool, payload)` | → `RepairResult`, `ValidationResult` |
| 3 → 2 | `IContractAdapter.extract_contract / validate / wrap` | `ContractSpec` |
| 3 → 1 | `RepairEngine.repair(contract, data, adapter)` | → `RepairResult` |
| 6 ← 4 | `forward_message(request, outcome)`, `refusal_response(request, outcome)` | → JSON-RPC dicts |

Every value that crosses a layer boundary is either an immutable dataclass
or a fresh copy. No layer keeps a reference to a caller's dict, and no
layer hands one out:

- `ToolCallRequest.parse` copies its input;
- the registry copies definitions on the way in and keeps a private copy
  for calls;
- the engine deep-copies the data it repairs.

---

## Invariants and where they are enforced

| Invariant | Enforced by | Tested by |
|---|---|---|
| Shadow never blocks a call | `_check_forwarding_invariants` | truth table, `test_mcp_properties.py`, drift corpus |
| Shadow forwards exactly the original | `_check_forwarding_invariants` | as above, plus the relay byte-level tests |
| A forwarded auto payload validates | `_check_forwarded_payload_validates` | truth table, properties, corpus |
| Forwarding is idempotent (a forwarded payload is a fixed point) | engine re-validation | properties, corpus |
| Every forwarded value was sent, repaired on record, or is a declared default | engine and explain | properties, corpus |
| Escalated keys are never moved | `TrustPolicy`, engine | properties |
| Decisions are deterministic | no randomness, sorted iteration | truth table, corpus |
| The auto decision is the same in either mode | `_verdict` is mode-independent | truth table, properties |
| A stale schema is never used | the registry replaces unconditionally; the cache keys on content | `test_registry.py` |
| `COMPATIBLE` means every old-valid payload is new-valid | rules in `diff.py` | mutation soundness properties in `test_diff.py` |
| Equal contract fingerprints mean an empty diff | `compare_normalized` on normalised input | `test_diff.py` property |
| Hostile input cannot crash or hang the process | expansion budget, depth bounds, pattern screen | `test_hostile_payloads.py`, `test_refs.py`, registry fuzz (out of tree) |
| StateGuard accepts nothing the reference validator rejects | the extractor refuses what it cannot model | differential run against `jsonschema` (out of tree) |

---

## Dependency rules

- **`stateguard.core`** imports only the standard library.
- **`stateguard.adapters.jsonschema`** and **`stateguard.adapters.mcp`**
  import only the standard library and `stateguard` itself. In particular,
  they never import the `mcp` SDK or Pydantic. Subprocess tests in
  `tests/isolation/test_import_isolation.py` enforce this.
- **The `mcp` extra exists only for the examples.** The library is
  usable with any MCP implementation, or none, because it works on the
  dicts that implementations exchange.
- **Layer 6 imports layer 4's types but not the registry.** A host can use
  the boundary helpers with `repair_tool_call` alone.

---

## Design decisions

- **Refuse, don't ignore.** A schema feature StateGuard cannot represent
  makes the tool unreadable, rather than being validated loosely. Looseness
  that *is* tolerated (a schema-valued `additionalProperties`, tuple
  `items`) always produces a `SchemaFeatureWarning`.
- **Decide once, view twice.** Shadow is a view of the auto decision, not a
  second decision.
- **Gate at the boundary.** Invariants that hold by construction are still
  checked where the payload leaves the library, because that is the last
  point where a bug can be stopped.
- **Content identity everywhere.** The cache, the registry fast path and
  change detection all key on fingerprints from one module. "Same schema"
  therefore means one thing everywhere.
- **The host owns policy.** What to do with unreadable tools, unknown tools,
  audit storage and transport errors is decided by the host, not inferred
  by the library.
- **Additive API.** Every change to the MCP surface since the first release
  added fields, defaults or functions. Nothing existing changed meaning,
  except where an old behaviour was a bug:
  - `HOLD` carrying `None`;
  - shadow mode blocking calls;
  - exclusive bounds going unenforced.

  The `CHANGELOG` lists each one.
