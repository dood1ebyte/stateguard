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

**Decided: option 2.** See `docs/adr/0001-json-schema-source-of-truth.md`.

**Correction to this section.** The claim that `DictContractAdapter` "makes
this choice by accident and nobody recorded it" is **wrong** — it documents the
delegation deliberately, at both class and method level. The correction
matters, and it cuts the other way: for the dict adapter `ContractValidator` is
authoritative *by definition*, because StateGuard owns that contract format and
there is no external spec to diverge from. JSON Schema is the first case where
`ContractValidator` stands in for a specification it does not fully implement,
so the ADR must enumerate the unchecked keywords rather than merely record the
delegation. That is the real requirement, and it is stricter than what this
section asked for.

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

### Phase 0 — Unblock ✅ COMPLETE

**Status:** closed 2026-08-25. 0.1 and 0.3 were absorbed by the core-hardening
work; only the ADR remained.

| # | Task | Est. | Status |
|---|---|---|---|
| 0.1 | Fix P0-3 regression guard: compare against previous iteration, not initial | 1d | ✅ landed in `CORE_HARDENING_PLAN.md` Phase 1 (multi-step repair) |
| 0.2 | Write the source-of-truth ADR (§3, P1-3); record option 2 + rationale | 0.5d | ✅ `docs/adr/0001-json-schema-source-of-truth.md` |
| 0.3 | *(parallel, 5 min)* P0-1 packaging fix — unrelated but live on PyPI | — | ✅ distribution renamed to `sguard` |

**Exit criterion met.** Verified against the live engine, not assumed — the
combined rename+coerce case the plan calls the most representative input in
the category, plus the enum drift §8 flagged as unrepairable at the time:

```
rename+coerce  -> success | days=5 location='Mumbai'
enum drift     -> success | unit='celsius' city='Mumbai'
all three      -> success | unit='celsius' days=5 location='Mumbai'  (4 attempts, one call)
```

P1-1 (enum repair) is also closed — `EnumNormalizationStrategy` shipped in
`CORE_HARDENING_PLAN.md` Phase 5, so §8's "nothing repairs them today" no
longer holds.

### Phase 1 — JSON Schema core ✅ COMPLETE

**Status:** closed 2026-08-25. 137 tests in `tests/adapters/jsonschema/`.

| # | Task | Status |
|---|---|---|
| 1.1 | `refs.py` — `$ref`/`$defs` resolution + cycle detection | ✅ |
| 1.2 | `type_mapper.py` — §5 table, `type` arrays, `anyOf`/`oneOf` → `UnionMember` | ✅ |
| 1.3 | `extractor.py` — recursive walk → `ContractSpec`, constraints, defaults, `strict_mode` | ✅ |
| 1.4 | `adapter.py` + `ContractGuard.with_json_schema()` | ✅ (also threaded `policy` through the other two factories) |
| 1.5 | Unknown-keyword rejection + structured error messages | ✅ folded into `extractor.py` |

**Exit criterion met.** A Pydantic-generated schema with `$defs` extracts and
repairs; a self-referential schema raises `SchemaReferenceError` rather than
hanging. Both cycle shapes are mutation-tested.

**Better than planned.** §8 lists enum drift as the #1 real MCP failure and
"nothing repairs them today"; enum normalisation shipped with the core
hardening, so `"Celsius"` → `"celsius"` now repairs through this adapter for
free. §7's "be honest about what it does not repair" caveat is obsolete for
enums.

**One divergence from the Pydantic adapter, on purpose.** This extractor
emits `NOT_NULL` for non-nullable fields; `PydanticExtractor` does not.
Pydantic's own validator rejects a stray `None`, so it needs no constraint —
this adapter is its own source of truth (ADR-0001), and without it
`ContractValidator` accepts `{"location": null}` against
`{"location": {"type": "string"}}`.

**Two seam findings were recorded as `xfail(strict=True)` in
`tests/adapters/jsonschema/test_end_to_end.py`. Both are now decided — see
Phase 1b.**

This is the "is `IContractAdapter` the right seam" evidence §11 hoped for:
the interface itself held up, but `GuardConfig`'s relationship to
adapter-derived contract settings did not.

### Phase 1b — Review findings and seam decisions ✅ COMPLETE

**Status:** closed 2026-09-07. Not in the original plan; added after a review
of the Phase 1 work found that the adapter's central invariant — *refuse
rather than under-validate* — leaked in three places, all from one root
cause.

**Root cause.** The subset screen was a private method on
`JSONSchemaExtractor`, so it only ran where *that* module walked.
`type_mapper` walks too — through `anyOf`/`oneOf` branches and through
`items` — and everything it reached went unscreened. The screen now lives in
`keywords.py` and both walkers call it.

| # | Finding | Fix |
|---|---|---|
| 1 | `Optional[$ref]` dropped constraints, defaults **and** the reject list. `anyOf: [{$ref}, {null}]` is what Pydantic emits for every `Optional[X]`; `effective_schema` is a bare `{"$ref": ...}` there, carrying none of them. `allOf` behind such a ref became `FieldType.ANY` — a schema written to forbid a payload accepted every payload | Extractor resolves the effective schema once, in a context that stays open for the whole field |
| 2 | Rejected keywords inside union branches and array `items` were ignored | Screen moved to `keywords.py`, called by both walkers. **Keywords only — see the limitation below** |
| 3 | A deep-but-finite schema raised `RecursionError` (a `RuntimeError`, so it escaped every caller catching `JSONSchemaError`). Measured: 496 levels | `RefResolver` bounds resolution depth, alongside its two cycle guards |
| 4 | An untyped `enum` with a `null` member picked up `NOT_NULL`, making `{"m": null}` a **false violation** against a schema that permits null | Nullability inferred from the members on the untyped path |
| 5 | `additionalProperties` as a schema object silently ignored | Warns; a non-boolean, non-object value raises |
| 6 | A `required` name with no `properties` entry was dropped, so a payload missing it was reported valid | Emitted as a required `ANY` field |
| 7 | Tuple-form `items` widened to `ANY` silently, while `exclusiveMinimum` warned | Warns, consistently |

**A second review pass, of the fixes themselves, found two more:**

| # | Finding | Fix |
|---|---|---|
| 8 | `JSONSchemaAdapter.wrap` (finding 2 of the seam decisions below) wrote declared defaults **after** the engine finished, so nothing re-checked them. A schema declaring `{"type": "integer", "default": "abc"}` — or a default left stale when a server's `enum` changed, which is the drift this adapter is *for* — had `repair` return `ALREADY_VALID` and `validate` then reject its own output. The engine refuses to do this on the path it controls: `DefaultValueFillStrategy` fills, revalidates, and fails the repair | Defaults screened at extraction against the field's own type and constraints; an unusable one is dropped with a warning rather than refusing the document |
| 9 | Finding 1's fix missed *chained* optional refs. `anyOf:[{$ref A},{null}]` where A is itself `anyOf:[{$ref B},{null}]` still lost B's constraints and default, because `_map_union` overwrote `effective_schema` at every level and the outermost `$ref` won. The reject list *did* survive, since the screen runs per branch | The overwrite is skipped when the recursive call already narrowed further |

**Known limitation, stated plainly because finding 2's wording overstated it.**
The screen reaches every subschema's *keywords*. It does not give union
branches or array elements a `nested_spec`, so the **contents** of an object
inside a union or an array are not validated at all:

```jsonc
{"anyOf": [{"type": "string"},
           {"type": "object", "properties": {"n": {"type": "integer"}},
            "required": ["n"]}]}
```

`{"u": {"n": "not-an-int"}}` extracts and validates clean. Same for
`items: {"type": "object", ...}`. This is pre-existing — `union_members` and
`item_type` each carry a single `FieldType` and nothing else — not a
regression from Phase 1b, and not something Phase 1b fixed. It belongs in
Phase 4's corpus work, where real schemas will show how often it bites.

**Both seam findings decided:**

1. **`strict_mode` composes as a floor** — strict if either the schema or
   the config says so. `ContractGuard._extract_contract` used to let the
   config overwrite the adapter in *both* directions, and
   `ContractSpec.strict_mode` documented the opposite precedence, so the
   model and the guard disagreed. A schema that declares itself closed now
   stays closed; `GuardConfig.strict_mode=True` can still tighten a format
   with no way to say it. A contract is never *loosened* by configuration.
2. **`JSONSchemaAdapter.wrap` materialises declared defaults.** Defaults are
   deep-copied; nested specs are filled too. **This makes §7's demo step 3
   true for the first time.**

   The original justification here — that `PydanticAdapter.wrap` already
   does this, so it is consistency rather than a new hazard — was checked
   and is *half* right. Pydantic does apply defaults, but with
   `validate_default=False`, so it does not check them either. The
   behaviours match; the shared behaviour was unsound. It mattered more
   here, because ADR-0001 makes `ContractValidator` the sole authority on
   this path and nothing downstream would catch the bad write. Closed by
   finding 9 above.

### Phase 2 — MCP layer ✅ COMPLETE

**Status:** closed 2026-09-07. 47 tests in `tests/adapters/mcp/`, 99% branch
coverage.

| # | Task | Status |
|---|---|---|
| 2.1 | `MCPToolAdapter` — full tool def *or* a bare input schema | ✅ |
| 2.2 | `ContractGuard.with_mcp()` | ✅ |
| 2.3 | Schema cache keyed by tool name + schema hash | ✅ `cache.py` |
| 2.4 | `RepairStatus` → an actionable result | ✅ `outcomes.py` |

**Exit criterion met.** `guard.repair(tool_def, arguments)` works end to end.

**On 2.3.** The `contract_id` collision is real and is now asserted rather
than assumed: `get_weather(location, days)` and `get_traffic(location, days)`
produce the same id, because it hashes field shapes. The schema *content* is
in the key too, because a tool that changes its schema keeps its name — that
is the drift this adapter exists to catch, so keying on the name alone would
pin the first version seen forever. Bounded LRU: a proxy holds definitions
from servers it does not control.

**On 2.4.** Four actions, not two. `ESCALATE` is kept distinct from `REFUSE`
because `AMBIGUOUS` carries candidates an agent can re-prompt with, and
folding it into failure would discard what the hardening phase built.
`HOLD` is kept distinct from `FORWARD` because shadow withholds the payload
on `proposed_output` — a proxy reading the usual field must forward nothing
rather than apply repairs the caller asked it to withhold.

### Phase 3 — Demo ✅ COMPLETE

**Status:** closed 2026-09-07. `examples/mcp/`, with 11 integration tests in
`tests/examples/`.

| # | Task | Status |
|---|---|---|
| 3.1 | MCP server with 2 tools (`examples/mcp/server.py`) | ✅ |
| 3.2 | `proxy.py` — caches `tools/list`, repairs `arguments`, forwards | ✅ |
| 3.3 | Before/after script + README with real terminal output | ✅ |

**Exit criterion met.** `pip install 'sguard[mcp]'` then `python
examples/mcp/demo.py`. The demo launches the server as a real subprocess and
speaks MCP over stdio, so it exercises the protocol rather than simulating
it. Three calls the server rejects now succeed; a fourth (`limit: 999`
against `maximum: 10`) is refused and never sent.

**Spec drift, as §2 warned.** The plan was written against a May 2026 cutoff.
The installed SDK is **2.x**, where `FastMCP` was renamed to `MCPServer`.
More consequentially: on the SDK's `Tool` model, `inputSchema` is a Pydantic
*alias*, so `tool.input_schema` is the Python attribute and
`tool.model_dump()` emits snake_case unless the caller passes
`by_alias=True`. `split_tool_definition` initially refused `input_schema` on
the mistaken grounds that it was the Anthropic Messages API's spelling; both
are now accepted, and a test pins the aliasing so the reasoning is revisited
if the SDK changes.

**Correction to §7.** The fuzzy match on `loc` → `location` scores **0.854**
(jaro-winkler), not the 0.8125 `_token_prefix_boost` figure written there.
The repair lands either way; the number was wrong.

### Phase 4 — Hardening ✅ COMPLETE

**Status:** closed 2026-09-12.

| # | Task | Status |
|---|---|---|
| 4.0 | Close the review findings this phase's own code review raised | ✅ |
| 4.1 | Corpus: 15–20 real `inputSchema` blobs from public MCP servers as fixtures | ✅ 21, from 4 servers |
| 4.2 | Assert every corpus schema extracts without error and round-trips | ✅ `tests/adapters/jsonschema/test_corpus.py` |
| 4.3 | **False-positive tests** — near-miss params that must *refuse* | ✅ `tests/adapters/mcp/test_false_positives.py` |
| 4.4 | Docs: adapter guide, supported-subset table, the source-of-truth decision | ✅ `docs/jsonschema-adapter.md` |

**4.0 — added, not in the original plan.** A code review of Phases 1–3 found
four defects, and a phase named *Hardening* is where they belong rather than
after it. The load-bearing one: `SchemaCache._key` let a `RecursionError`
escape from `json.dumps` on a deeply nested schema. Because the cache lookup
runs *before* extraction, it pre-empted `RefResolver`'s depth bound — the
guard added in Phase 1b for precisely that error — so the MCP path crashed
uncatchably on input the JSON Schema path refused cleanly. That is the wrong
way round: the MCP path is the one facing servers nobody controls. Also
fixed: shadow mode dropped its diff on `ALREADY_VALID` (inferring the mode
from `proposed_output` rather than reading it), `GuardConfig.strict_mode`
tightened the root contract only, and a tool definition with no `inputSchema`
reported a JSON Schema keyword error.

**4.1 — captured by running the servers, not reading their source.**
`mcp-server-git` (12 tools), `mcp-server-sqlite` (6), `mcp-server-time` (2)
and `mcp-server-fetch` (1) were installed from PyPI into an isolated
environment, launched over stdio, and asked for `tools/list`; what is
committed is what came back on the wire, with package version and capture
date. `capture.py` makes it reproducible; the fixtures are checked in, so the
suite needs no network.

**What the corpus found.** All 21 extract without refusal, which is criterion
2 — but the more useful result is what it exposed:

- `exclusiveMinimum` is not hypothetical. `mcp-server-fetch` ships it, so §5's
  decision to drop-with-warning rather than round into `MINIMUM` is
  load-bearing rather than theoretical. The corpus test pins the exact warning
  set, so a new loosening has to be acknowledged.
- **Drift on an optional parameter is not repaired at all.**
  `FuzzyFieldMatchStrategy` pairs an `UNEXPECTED_FIELD` with a
  `MISSING_REQUIRED_FIELD`, and an optional field is never missing — so even a
  one-character typo on one goes uncorrected. 12 of the corpus's 41
  parameters (29%) are optional. The sharp edge is an optional field with a
  declared default: `git_diff` takes `context_lines` defaulting to 3, so a
  model writing `context: 10` gets 3 filled in beside its unrecognised key and
  the 10 is silently ignored. This is a *missed* repair, not a wrong one —
  nothing is written into a declared parameter — so it is recorded as a
  limitation rather than patched here. Repairing onto optional fields needs a
  view on whether an unexpected key is evidence that an optional field was
  meant, which is a change to the repair model and belongs with Phase 5.

**4.3 — asymmetric on purpose.** The tests do not assert that a repair
happens; they assert that when one happens it lands on the right field, and
that thin evidence produces nothing. Swept across the corpus: an
unrecognisable key was never renamed onto a declared field (29 required
parameters), and a plausible abbreviation either refused or landed correctly
(18 of 18), including `source` → `source_timezone` and `target` →
`target_timezone` on the same `convert_time` call. A bare `timezone` against
those two is refused rather than guessed — the case where a wrong repair
would produce a plausible wrong answer instead of an error.

**Total: 12.5 days** (plus ~2.25 unplanned for Phase 1b, and ~0.25 for 4.0).

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

**With StateGuard:** *(all four steps verified running — see
`examples/mcp/README.md` for the captured transcript)*
1. `FuzzyFieldMatchStrategy` renames `loc` → `location`. Trust **0.854**
   (jaro-winkler). *(This section previously claimed 0.8125 from
   `_token_prefix_boost`; that figure was wrong. The repair lands either
   way.)*
2. `TypeCoercionStrategy` coerces `"5"` → `5`. Trust 1.0 — it round-trips
   exactly.
3. `DefaultValueFillStrategy` fills `unit` from the schema's `default`.
   **This step did not actually happen until Phase 1b** — the engine had
   nothing to repair (an absent optional is not a violation) and
   `JSONSchemaAdapter.wrap` returned the dict untouched.
4. Call succeeds. Full audit trail with per-operation trust and evidence.

**Steps 1 and 2 are the two-attempt path — this demo does not work until
Phase 0.1 lands.** That is the concrete reason the regression fix is
sequenced first.

**What the demo README says it does *not* repair.** `"Celsius"` →
`"celsius"` is **no longer on that list** — enum normalisation shipped with
the core hardening, and the demo now shows it working. Still outstanding:
double-encoded `arguments` (a model emitting the whole argument object as a
JSON string), and `outputSchema` / `structuredContent` — repairing what a
server sends *back*, which §2 scopes out of v1 deliberately.

---

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Enum drift is the #1 real MCP failure and we can't repair it** | High | Detection works via `ENUM_VALUES`; repair needs §3.3. Schedule as Phase 5, don't claim it before then |
| **Double-encoded `arguments`** — models emit JSON strings constantly | High | Needs §3.2. Same treatment: Phase 5 |
| **Recursive `$ref` hangs the process** | High | Cycle detection is Phase 1.1, non-negotiable. Also fixes the same latent bug in the Pydantic extractor |
| **Property names containing `.`** break path navigation silently | Medium | Detect in the extractor; raise a clear "unsupported property name" error. Must fail loudly, never repair wrongly |
| **`pattern` semantics + ReDoS** | Medium | ✅ **Closed.** `ContractValidator` now uses `re.search` (the old `re.match` produced *false* violations and disagreed with Pydantic itself on the shipped path); an unusable pattern raises a clear error naming the field instead of a bare `re` exception; and `jsonschema/patterns.py` refuses the nested-quantifier family at extraction, so `(a+)+$` never reaches a match. The screen is a documented heuristic, not a proof — overlapping alternation like `(a\|a)+` is not caught |
| **Confident wrong rename** on near-miss param names | Medium | Phase 4.3 false-positive corpus. This is the failure that damages trust most |
| **MCP spec drift** | Medium | Pin the spec revision in the adapter docstring. Verify `outputSchema`/`structuredContent` against the live spec before Phase 5 |
| **Scope creep into real JSON Schema** | Medium | §5's reject-list is the contract. Unknown keyword ⇒ error, not silence |
| **`$id` on a subschema rebases `$ref` resolution** | Medium | ✅ **Closed.** Was silently misresolving: a nested `$id` scope resolved `#/$defs/A` against the document root instead of the subschema, returning the *wrong* target with no error. Now refused at extraction; root-level `$id` stays allowed since it does not change what `#` addresses |

---

## 9. Success criteria

Tighter than the proposal's, and each one is testable. Status as of
2026-09-07:

| # | Criterion | Status |
|---|---|---|
| 1 | `ContractGuard.with_mcp().repair(tool_def, arguments)` repairs a rename + coercion + default-fill payload in one call, returning `SUCCESS` | ✅ all three in one call; `tests/adapters/mcp/test_outcomes.py` |
| 2 | All 15–20 corpus schemas from real public MCP servers extract without error | ✅ 21 schemas from 4 servers, captured by running them; `tests/adapters/jsonschema/test_corpus.py` |
| 3 | A recursive `$ref` raises a clear diagnostic in under 100ms — no hang | ✅ both cycle shapes, plus a depth bound added in Phase 1b |
| 4 | An unsupported keyword (`allOf`) raises a named error identifying the keyword and the path | ✅ and, since Phase 1b, from inside union branches, `items`, and behind an `Optional[$ref]` (including a chained one). Keywords only — object *contents* in those positions stay unvalidated; see §6 Phase 1b |
| 5 | The false-positive corpus produces **zero** wrong repairs; near-misses refuse | ✅ zero across the corpus; 18/18 plausible abbreviations land correctly, garbage names and genuine ambiguity refuse; `tests/adapters/mcp/test_false_positives.py` |
| 6 | `import stateguard.adapters.mcp` pulls in no third-party package | ✅ `tests/isolation/` — no MCP SDK, no pydantic |
| 7 | The demo runs from a clean checkout in two commands with visible before/after output | ✅ `examples/mcp/`, over real stdio MCP |

**7 of 7 met**, as of Phase 4 closing on 2026-09-12.

The two that were open were about *evidence at scale* rather than capability,
and the corpus earned its place: it confirmed the subset (21 of 21 extract)
and the no-wrong-repairs property, and it surfaced one real gap no
hand-written fixture would have — optional-parameter drift is not repaired at
all, because the fuzzy strategy has no missing-field violation to pair
against. That is recorded as a limitation (§6 Phase 4, §10) rather than
counted against criterion 5, which it does not violate: the failure is a
missed repair, never a wrong one.

---

## 10. Out of scope for this work

- Output-schema repair (direction B) — fast follow, ~1 day
- ~~Enum normalisation (§3.3)~~ — shipped with the core hardening; the demo
  repairs `"Celsius"` → `"celsius"` at trust 1.0
- Repairing drift onto an *optional* parameter — Phase 5. Surfaced by the
  Phase 4 corpus: `FuzzyFieldMatchStrategy` pairs an unexpected key with a
  *missing required* field, and an optional field is never missing, so a typo
  on one is never corrected. Fixing it means deciding whether an unexpected
  key is evidence that an optional field was meant — a change to the repair
  model, not a hardening task
- JSON-string parsing (§3.2), for double-encoded `arguments` — Phase 5
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
