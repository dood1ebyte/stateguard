# Plan — MCP Adapter

**Source:** item 1 of `StateGuard_v2proposal.md` (priority 9/10, "~1–2 weeks,
contingent on the adapter interface being frozen first").

**Verdict on the proposal:** the strategic call is right and this is the
correct thing to build first. The scoping has one significant error, corrected
in §1.

---

## 1. What this actually is

The proposal describes this as "not a new capability — a new surface for the
one that already exists," and estimates 1–2 weeks gated on the adapter
interface freeze.

**The real dependency is different, and larger.**

MCP tool definitions declare their parameters in `inputSchema`, and
`inputSchema` **is JSON Schema**:

```json
{
  "name": "get_forecast",
  "description": "Get a weather forecast",
  "inputSchema": {
    "type": "object",
    "properties": {
      "location": {"type": "string", "description": "City name"},
      "days":     {"type": "integer", "minimum": 1, "maximum": 14},
      "unit":     {"type": "string", "enum": ["celsius", "fahrenheit"]}
    },
    "required": ["location"]
  }
}
```

**StateGuard cannot read JSON Schema.** It reads Pydantic models, and it reads
its own invented dict format (`DictContractAdapter`, whose module docstring is
explicit: *"This is NOT JSON Schema"*).

So the actual shape of this work is:

```
MCP adapter  =  JSON Schema adapter  (the real work, ~80%)
              + MCP-specific wrapper (thin, ~20%)
```

This is §3.4 of `NEXT_STEPS.md` with an MCP-shaped delivery target. That's
good news — it means the highest-priority product item and the
highest-leverage engineering item are **the same piece of work**, and building
it for MCP gives it a concrete acceptance test instead of an abstract one.

But it means the blocker named in the proposal ("the adapter interface being
frozen") is not the blocker. `IContractAdapter` has been stable for months
and is already a three-method interface (`extract_contract` / `validate` /
`wrap`). The real prerequisites are in §3.

**Revised effort: 11–13 working days**, one engineer, including prerequisites.
9–11 if §3's P0-3 is already done. "1–2 weeks" is achievable only by cutting
the JSON Schema depth, which is the part that carries the value.

---

## 2. Scope decision — which direction do we repair?

There are two distinct integration points, and they are not equally valuable.

| | (A) Repair tool **arguments** | (B) Repair tool **results** |
|---|---|---|
| Schema | `tool.inputSchema` | `tool.outputSchema` |
| Payload | LLM-generated `arguments` | Server-generated `structuredContent` |
| Who drifts | **The model** — non-deterministic, drifts constantly | The server — deterministic code, drifts on version change |
| Frequency | High | Low, but this is the "abandoned server" case |
| Failure today | Server rejects the call, agent loop breaks | Model receives malformed data |

**Build (A) for v1.** It is where the drift actually is, it matches
StateGuard's core competency exactly (a model returns `loc` where the schema
says `location`), and it is what the proposal's own success criterion
describes.

**(B) is a fast follow** — the plumbing is identical, only the schema source
and payload location change. Roughly one extra day once (A) works. Note that
`outputSchema` / `structuredContent` arrived in a later MCP spec revision than
the base tool definition; **verify the current spec before implementing** —
this plan was written against knowledge with a May 2026 cutoff and MCP moves
fast.

---

## 3. Prerequisites — what actually blocks, what doesn't

Correcting the proposal's dependency analysis. From `NEXT_STEPS.md`:

### Hard blockers

**P0-3 — the regression guard** (`engine.py:469-481`). **Blocks.**

MCP argument drift is overwhelmingly the *combined* case: the model gets the
name wrong **and** the type wrong (`{"loc": "5"}` where the schema wants
`{"days": 5}`). That is a rename followed by a coercion — two attempts — and
the current regression guard aborts on the second pass and returns `FAILED`
with `repaired_output=None`.

Ship the MCP adapter before fixing this and the demo will fail on the most
representative input in the category. **Fix first. ~1 day.**

**P1-3 — the "source of truth" decision.** **Blocks, and it's a design
decision, not code.**

`IContractAdapter.validate` is documented as *"the framework's own validator
is the source of truth."* JSON Schema has no native validator unless we take a
dependency. Three options:

| Option | Cost | Consequence |
|---|---|---|
| Depend on `jsonschema` | Low | Correct validation. Breaks zero-dep for this path — acceptable as `sguard[jsonschema]` extra |
| Delegate to `ContractValidator` | Zero | **StateGuard's own validator silently becomes the authority.** Anything it doesn't implement goes unvalidated |
| Vendor a validator | Weeks | Not now |

**Recommendation: option 2 for v1, documented explicitly**, with option 1
available as an opt-in extra later. Rationale: we control the violation
vocabulary end-to-end, which is what the repair strategies need, and MCP
schemas use a narrow enough subset that `ContractValidator` covers it. But
this must be written down in the adapter's docstring and the README — the
current `DictContractAdapter` makes this choice *by accident* and nobody
recorded it.

Write the ADR before day 1 of implementation. **~0.5 day.**

### Not blockers (despite what it looks like)

**P1-2 — paths as tuples.** Does *not* block. Adapters only ever set
`FieldSpec.path` to a single local segment; the dotted-path machinery lives
in the engine, validator, and strategies. Building this adapter does not
deepen that coupling.

**One caveat:** JSON Schema property names may legally contain `.`
(`{"properties": {"user.name": {...}}}`), which silently breaks path
navigation. Handle it in this work with a guard (§7), not a full migration.

**P0-2 — the `conint`/`constr` crash.** Pydantic-specific; doesn't touch this
path. Ship it first anyway — it's 30 minutes and it's live on PyPI.

**P1-1 — enum extraction.** Doesn't block. JSON Schema `enum` maps cleanly to
`ENUM_VALUES` via this adapter without touching the Pydantic type mapper. But
see §8 — enum values are the single most common MCP drift after field names,
and *nothing repairs them today*.

**Telemetry, policy config, audit schema.** The proposal is correct that these
are parallel seams, not prerequisites. Agreed, no change.

---

## 4. Architecture

```
src/stateguard/adapters/
├── dict_adapter.py            (existing, unchanged)
├── pydantic/                  (existing, unchanged)
├── jsonschema/                ← NEW: the real work
│   ├── __init__.py
│   ├── adapter.py             JSONSchemaAdapter(IContractAdapter)
│   ├── extractor.py           schema dict -> ContractSpec
│   ├── type_mapper.py         JSON Schema type -> FieldType
│   └── refs.py                $ref/$defs resolution + cycle detection
└── mcp/                       ← NEW: thin
    ├── __init__.py
    ├── adapter.py             MCPToolAdapter -> delegates to JSONSchemaAdapter
    └── proxy.py               demo/integration surface, needs `mcp` extra
```

New factories on `ContractGuard`, matching the existing pattern
(`guard.py:115`, `guard.py:148`):

```python
ContractGuard.with_json_schema(config=..., telemetry=..., history=...)
ContractGuard.with_mcp(config=..., telemetry=..., history=...)
```

### The dependency property worth protecting

**`adapters/jsonschema/` and `adapters/mcp/adapter.py` need zero runtime
dependencies.** An MCP tool definition arriving over the wire is just a
`dict`. The adapter reads `tool["inputSchema"]` and walks it — no `mcp`
package required.

Only `adapters/mcp/proxy.py` — the runnable integration — needs the MCP SDK,
and that goes behind a `sguard[mcp]` extra.

This means **the entire adapter stays inside the existing zero-dependency
guarantee**, and the CI isolation job extends to cover it for free. That's a
real architectural win and it should be stated in the docs, not left implicit.

---

## 5. JSON Schema subset to support

Do **not** implement JSON Schema. Implement what tool definitions actually
emit. Everything else gets rejected loudly (see §7).

| Keyword | Maps to | Notes |
|---|---|---|
| `type: object` | `FieldType.OBJECT` + `nested_spec` | MCP `inputSchema` root is always this |
| `type: string` | `STRING` | |
| `type: integer` | `INTEGER` | |
| `type: number` | `FLOAT` | |
| `type: boolean` | `BOOLEAN` | |
| `type: array` | `ARRAY` + `item_type` from `items` | |
| `type: null` | `NULL` | |
| `type: [...]` (array form) | `UNION` + `union_members` | **`["string","null"]` is the common optional idiom** |
| `properties` | `ContractSpec.fields` | |
| `required` | `FieldSpec.required` | Absent from `required` ⇒ `required=False` |
| `default` | `FieldSpec.default` | Feeds `DefaultValueFillStrategy` for free |
| `enum` | `FieldConstraint(ENUM_VALUES, tuple(...))` | Infer `field_type` from member types |
| `const` | `ENUM_VALUES` with one member | |
| `anyOf` / `oneOf` | `UNION` + `union_members` | Collapse `[X, {"type":"null"}]` to optional-X, mirroring `unwrap_optional` |
| `$ref` / `$defs` | resolve inline | **Pydantic-backed servers emit these constantly** |
| `minimum` / `maximum` | `MINIMUM` / `MAXIMUM` | |
| `exclusiveMinimum`/`exclusiveMaximum` | — | No `FieldConstraintType` exists. **Drop with a warning**, don't fake it |
| `minLength` / `maxLength` | `MIN_LENGTH` / `MAX_LENGTH` | |
| `minItems` / `maxItems` | `MIN_LENGTH` / `MAX_LENGTH` | Validator already handles `list` via `_SIZED_TYPES` |
| `pattern` | `PATTERN` | ⚠️ see §7 — semantics + ReDoS |
| `additionalProperties: false` | `ContractSpec.strict_mode = True` | |
| `description` | ignored for v1 | Candidate signal for semantic repair later |
| `format` | ignored, no warning | Advisory in practice |

**Explicitly rejected with a clear error:** `allOf`, `not`, `if`/`then`/`else`,
`patternProperties`, `dependentSchemas`, `propertyNames`, `unevaluatedProperties`,
remote `$ref` (`http://`, `file://`). None appear in real tool schemas, and
silently ignoring them would mean silently under-validating.

---

## 6. Phased plan

### Phase 0 — Unblock (1.5 days)

| # | Task | Est. |
|---|---|---|
| 0.1 | Fix P0-3 regression guard: compare against previous iteration, not initial | 1d |
| 0.2 | Write the source-of-truth ADR (§3, P1-3); record option 2 + rationale | 0.5d |
| 0.3 | *(parallel, 5 min)* P0-1 packaging fix — unrelated but live on PyPI | — |

**Exit:** a rename→coerce repair returns `SUCCESS`, not `FAILED`. Test this
explicitly; it is the gate for everything below.

### Phase 1 — JSON Schema core (5 days)

| # | Task | Est. |
|---|---|---|
| 1.1 | `refs.py` — `$ref`/`$defs` resolution with a `seen` set for cycle detection | 1d |
| 1.2 | `type_mapper.py` — §5 table, incl. `type` arrays and `anyOf`/`oneOf` → `UnionMember` | 1.5d |
| 1.3 | `extractor.py` — recursive walk → `ContractSpec`, constraints, defaults, `strict_mode` | 1.5d |
| 1.4 | `adapter.py` + `ContractGuard.with_json_schema()` | 0.5d |
| 1.5 | Unknown-keyword rejection + structured error messages | 0.5d |

**Exit:** `JSONSchemaAdapter` extracts a correct `ContractSpec` from a
Pydantic-generated JSON Schema (with `$defs`), and a self-referential schema
raises a clear error instead of hanging.

### Phase 2 — MCP layer (1.5 days)

| # | Task | Est. |
|---|---|---|
| 2.1 | `MCPToolAdapter` — accept a full tool def *or* a bare `inputSchema`; delegate | 0.5d |
| 2.2 | `ContractGuard.with_mcp()` | 0.25d |
| 2.3 | Schema cache keyed by tool name + schema hash — **do not use `contract_id`**, it collides (`NEXT_STEPS.md` §4.5) | 0.5d |
| 2.4 | Error surface: map `RepairStatus` → an MCP-shaped result the caller can act on | 0.25d |

**Exit:** `guard.repair(tool_def, arguments)` works end to end.

### Phase 3 — Demo (2.5 days)

| # | Task | Est. |
|---|---|---|
| 3.1 | Minimal MCP server with 2–3 tools (`examples/mcp/server.py`) | 0.5d |
| 3.2 | `proxy.py` — intercepts `tools/call`, caches `tools/list` schemas, repairs `arguments`, forwards | 1.5d |
| 3.3 | Before/after script + README with real terminal output | 0.5d |

**Exit:** the §7 demo runs from a clean checkout with two commands.

### Phase 4 — Hardening (2 days)

| # | Task | Est. |
|---|---|---|
| 4.1 | Corpus: 15–20 real `inputSchema` blobs from public MCP servers as fixtures | 0.5d |
| 4.2 | Assert every corpus schema extracts without error and round-trips | 0.5d |
| 4.3 | **False-positive tests** — near-miss params that must *refuse* (`NEXT_STEPS.md` §7) | 0.5d |
| 4.4 | Docs: adapter guide, supported-subset table, the source-of-truth decision | 0.5d |

**Total: 12.5 days.**

---

## 7. The demo

The proposal's success criterion is *"An MCP tool-call payload with a drifted
schema is detected and repaired through the same ContractGuard entrypoint,
with a working example to show."* Sharpen it into a narrative that matches the
"52% of MCP servers are abandoned" framing:

**Story:** a server updated its tool signature. The agent is still calling the
old shape.

```jsonc
// Server's current inputSchema
{"type": "object",
 "properties": {"location": {"type":"string"},
                "days":     {"type":"integer","minimum":1,"maximum":14},
                "unit":     {"type":"string","enum":["celsius","fahrenheit"],
                             "default":"celsius"}},
 "required": ["location","days"]}

// What the model sends (old param name + stringified number + missing optional)
{"loc": "Mumbai", "days": "5"}
```

**Without StateGuard:** server returns a validation error; the agent loop
either retries blindly or fails.

**With StateGuard:**
1. `FuzzyFieldMatchStrategy` renames `loc` → `location`. *(Verified by hand:
   `_token_prefix_boost` scores 0.8125 — the token `loc` prefixes `location`,
   weight 3/8. Clears the 0.7 threshold.)*
2. `TypeCoercionStrategy` coerces `"5"` → `5`.
3. `DefaultValueFillStrategy` fills `unit` from the schema's `default`.
4. Call succeeds. Full audit trail with per-operation confidence.

**Steps 1 and 2 are the two-attempt path — this demo does not work until
Phase 0.1 lands.** That is the concrete reason the regression fix is
sequenced first.

**Be honest in the demo README about what it does *not* yet repair:**
`"Celsius"` → `"celsius"` (needs enum normalisation, `NEXT_STEPS.md` §3.3) and
double-encoded `arguments` as a JSON string (needs `NEXT_STEPS.md` §3.2).
Both are 0.5–1 day each and both make the MCP story materially stronger —
strong argument for scheduling them immediately after, as Phase 5.

---

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Enum drift is the #1 real MCP failure and we can't repair it** | High | Detection works via `ENUM_VALUES`; repair needs §3.3. Schedule as Phase 5, don't claim it before then |
| **Double-encoded `arguments`** — models emit JSON strings constantly | High | Needs §3.2. Same treatment: Phase 5 |
| **Recursive `$ref` hangs the process** | High | Cycle detection is Phase 1.1, non-negotiable. Also fixes the same latent bug in the Pydantic extractor |
| **Property names containing `.`** break path navigation silently | Medium | Detect in the extractor; raise a clear "unsupported property name" error. Must fail loudly, never repair wrongly |
| **`pattern` semantics + ReDoS** | Medium | `re.match` is anchored; JSON Schema `pattern` is unanchored. Fix to `re.search` in this work, and bound or reject pathological patterns from untrusted schemas |
| **Confident wrong rename** on near-miss param names | Medium | Phase 4.3 false-positive corpus. This is the failure that damages trust most |
| **MCP spec drift** | Medium | Pin the spec revision in the adapter docstring. Verify `outputSchema`/`structuredContent` against the live spec before Phase 5 |
| **Scope creep into real JSON Schema** | Medium | §5's reject-list is the contract. Unknown keyword ⇒ error, not silence |

---

## 9. Success criteria

Tighter than the proposal's, and each one is testable:

1. `ContractGuard.with_mcp().repair(tool_def, arguments)` repairs a
   rename + coercion + default-fill payload in one call, returning `SUCCESS`.
2. All 15–20 corpus schemas from real public MCP servers extract without error.
3. A recursive `$ref` raises a clear diagnostic in under 100ms — no hang.
4. An unsupported keyword (`allOf`) raises a named error identifying the
   keyword and the path.
5. The false-positive corpus produces **zero** wrong repairs; near-misses
   refuse.
6. `import stateguard.adapters.mcp` pulls in no third-party package —
   enforced by extending the existing CI isolation job.
7. The demo runs from a clean checkout in two commands with visible
   before/after output.

---

## 10. Out of scope for this work

- Output-schema repair (direction B) — fast follow, ~1 day
- Enum normalisation (§3.3) and JSON-string parsing (§3.2) — Phase 5
- Full JSON Schema draft 2020-12
- MCP resources, prompts, sampling — tools only
- Transport-level concerns (auth, stdio vs. HTTP) beyond what the demo proxy
  needs
- A production-grade proxy. Phase 3 builds a **demo**; a hardened proxy is a
  separate product decision with its own operational surface

---

## 11. Recommended sequencing against the rest of the proposal

The proposal's own sequencing holds, with one correction:

- **Items 2, 4, 5** (messaging/docs, <1 day each) — unblocked, run in parallel
  with Phase 0. No engineering time.
- **Item 3** (verify + name tool-call repair) — **fold into Phase 3.** The
  proposal treats it as separate verification work, but the MCP demo *is* the
  tool-call-repair demo. Building both separately duplicates ~2 days.
- **Item 1** (this plan) — starts at Phase 0 immediately. The gate named in
  the proposal (adapter interface freeze) is already satisfied; the real gate
  is P0-3.

**One thing the proposal gets exactly right and is worth restating:** this is
additive to the architecture work, not a detour from it. The JSON Schema
adapter is the strongest possible validation of whether `IContractAdapter` is
actually the right seam. If it turns out to be awkward here, that is a finding
worth having *before* Architecture Specification v1 freezes, not after.
