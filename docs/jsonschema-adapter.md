# The JSON Schema and MCP adapters

How StateGuard reads a JSON Schema, exactly which part of the specification
it implements, and what a `SUCCESS` from it does and does not claim.

> **The one thing to know first.** This adapter validates with StateGuard's
> own `ContractValidator`, not with a JSON Schema implementation. A `SUCCESS`
> is **not** a claim of JSON Schema compliance. The gap is bounded and
> enumerated below, and everything outside the supported subset is *refused*
> rather than ignored — see [ADR-0001](adr/0001-json-schema-source-of-truth.md)
> for why that trade was made.

---

## Two entrypoints

`JSONSchemaAdapter` reads a JSON Schema document. `MCPToolAdapter` is a thin
layer over it that knows where to find `inputSchema` inside an MCP tool
definition and caches the result. Neither needs a third-party package — a
schema arriving over the wire is just a `dict` — and an import-isolation test
enforces that.

```python
from stateguard import ContractGuard

# A JSON Schema document
guard = ContractGuard.with_json_schema()
result = guard.repair(schema, payload)

# An MCP tool definition, or a bare inputSchema
guard = ContractGuard.with_mcp()
result = guard.repair(tool, arguments)
result = guard.repair(tool["inputSchema"], arguments)
```

Both spellings of the key are accepted. `inputSchema` is what MCP puts on the
wire; `input_schema` is the same field as the Python SDK names it, because on
the SDK's `Tool` model the camelCase form is a Pydantic *alias*. If both are
present and they disagree, that is genuinely ambiguous and raises.

### Deciding what to do with the result

A proxy in front of `tools/call` has one question after StateGuard runs: *do
I forward this, and with what?* Derive it once, from `outcome_for`, rather
than branching on `RepairStatus` at each call site:

```python
from stateguard.adapters.mcp import MCPAction, outcome_for

outcome = outcome_for(guard.repair(tool, arguments), arguments)
```

| `outcome.action` | Meaning | What to send |
|---|---|---|
| `FORWARD` | Valid, or repaired and trusted | `outcome.arguments` |
| `HOLD` | Shadow mode — a change was determined and withheld | the model's original arguments; log `outcome.preview` |
| `ESCALATE` | A repair was found but not trusted unsupervised | nothing; `outcome.result.ambiguous` carries the candidates |
| `REFUSE` | No repair brings the payload up to the schema | nothing |

`arguments` is `None` for everything except `FORWARD`, deliberately, so a
caller who forwards without checking `action` sends nothing rather than
something unintended.

### Caching

`MCPToolAdapter` caches extracted contracts on **`(tool name, schema
content)`**, bounded by an LRU (default 128 tools). Both halves of that key
are load-bearing:

- Not `contract_id`, because two tools with coincidentally identical
  signatures — and many tools take a single `query` string — produce the same
  id, so one tool's contract would be served for another.
- Not the tool name alone, because a tool's name survives a schema change.
  That is the exact drift this adapter exists to catch, and keying on the name
  would pin the first version ever seen.

To clear the cache when a server may have changed underneath you — a proxy
reconnecting, say — hold the adapter yourself rather than reaching into the
guard:

```python
from stateguard.adapters.mcp import MCPToolAdapter

adapter = MCPToolAdapter()
guard = ContractGuard(adapter=adapter)
...
adapter.cache.clear()
```

---

## The supported subset

Chosen from what tool definitions actually emit, not from what was convenient
to implement. The claim is checked: 21 schemas from four public MCP servers
all extract cleanly — see [Corpus evidence](#corpus-evidence).

### Types

| JSON Schema | `FieldType` | Notes |
|---|---|---|
| `string` | `STRING` | |
| `integer` | `INTEGER` | |
| `number` | `FLOAT` | JSON draws the same distinction StateGuard does |
| `boolean` | `BOOLEAN` | |
| `object` | `OBJECT` + `nested_spec` | |
| `array` | `ARRAY` + `item_type` from `items` | |
| `null` | `NULL` | |
| `type: [...]` | `UNION`, or optional-X | See [Unions](#unions-and-optionals) |
| `anyOf` / `oneOf` | `UNION`, or optional-X | Not both on one schema |
| no `type` at all | inferred from `enum`/`const`, else `ANY` | The one legitimate `ANY` |

An unknown type name is an **error**, never an `ANY` fallback: silently
widening a field to "accepts anything" is the under-validation this adapter
must not do.

### Structure and constraints

| Keyword | Maps to |
|---|---|
| `properties` | `ContractSpec.fields` |
| `required` | `FieldSpec.required` (entries must be strings) |
| `default` | `FieldSpec.default`, after screening — see [Defaults](#declared-defaults) |
| `enum` | `ENUM_VALUES` constraint, declaration order, duplicates dropped |
| `const` | `ENUM_VALUES` with one member |
| `$ref` / `$defs` | resolved inline, same-document only |
| `minimum` / `maximum` | `MINIMUM` / `MAXIMUM` |
| `minLength` / `maxLength` | `MIN_LENGTH` / `MAX_LENGTH` |
| `minItems` / `maxItems` | `MIN_LENGTH` / `MAX_LENGTH` (the validator sizes lists too) |
| `pattern` | `PATTERN`, after a ReDoS screen — see [Patterns](#patterns) |
| `additionalProperties: false` | `ContractSpec.strict_mode = True` |

Every non-nullable field also picks up a `NOT_NULL` constraint (except those
typed `ANY` or `null`, which accept it legitimately). The Pydantic
adapter does not emit one and is right not to — Pydantic's own validator
rejects the `None`. This adapter has no such backstop, so without it
`{"location": null}` would pass against `{"location": {"type": "string"}}`,
which JSON Schema plainly forbids.

### Ignored, harmlessly

`$comment`, `$schema`, `deprecated`, `description`, `examples`, `format`,
`readOnly`, `title`, `writeOnly`, and any keyword prefixed `x-`.

`format` is advisory in practice and ignored without a warning. `description`
is ignored for now and is the obvious signal for semantic repair later.

### Refused

Each of these changes what "valid" means in a way the contract model cannot
express. Honouring them is impossible; ignoring them would report a payload
valid that the schema forbids, and with `ContractValidator` as the source of
truth nothing downstream would catch it.

| Keyword | Why |
|---|---|
| `allOf` | conjunction of subschemas has no single contract shape |
| `not` | negation cannot be expressed as a field contract |
| `if` / `then` / `else` | conditional subschemas have no static contract shape |
| `dependentSchemas` / `dependentRequired` | conditional requirements, same reason |
| `patternProperties` | property sets defined by regex are not addressable as paths |
| `propertyNames` | constraints on key names are not expressible |
| `unevaluatedProperties` / `unevaluatedItems` | depend on evaluation order this adapter does not model |

**Any keyword not listed anywhere above is also refused**, so a keyword nobody
anticipated is noticed rather than silently tolerated. Use an `x-` prefix for
extensions.

The screen runs on every subschema *after* `$ref` resolution — including
inside `anyOf`/`oneOf` branches and inside array `items`, which two walkers
reach and only one used to screen.

Also refused:

- **Remote `$ref`** (`http://`, `file://`, anything not starting with `#`).
  Following one would turn schema extraction into an outbound request to a URL
  chosen by whoever wrote the schema. This is not a limitation to be lifted
  later; it is the correct behaviour.
- **`$anchor` / plain-name fragments.** Use a JSON Pointer (`#/$defs/Name`).
- **`$id` on a subschema.** It rebases reference resolution; this adapter
  resolves against the document root only, so honouring it is impossible and
  ignoring it would silently resolve to the wrong schema. Root-level `$id` is
  fine.
- **Recursive schemas.** A structure that refers to itself has no finite
  expansion. Both shapes are caught — a `$ref` chain that loops, and a
  definition reached while it is already being expanded.
- **Schemas deeper than 100 resolution levels.** Far past any tool signature,
  and inside CPython's stack. Without the bound a deep schema raises
  `RecursionError`, which is a `RuntimeError` and escapes every caller
  catching `JSONSchemaError`.
- **Property names containing `.`**, which StateGuard's dot-notation paths
  cannot tell apart from a nested path. Accepting one would let a repair write
  to the wrong field.
- **Boolean schemas** (`true` / `false` in place of a schema object).
- **`enum` with `const`**, **`anyOf` with `oneOf`**, and empty `enum`,
  `anyOf`/`oneOf`, or `type: []` — each accepts nothing or means two things.

Every refusal raises `UnsupportedSchemaError` or `SchemaReferenceError`, both
subclasses of `JSONSchemaError` and therefore of `ValueError`.

### Dropped with a warning

A `SchemaFeatureWarning` means extraction continued with **less** validation
than the schema asked for. It is never silent, and a caller who wants these
fatal can say so:

```python
warnings.simplefilter("error", SchemaFeatureWarning)
```

| Situation | Effect |
|---|---|
| `exclusiveMinimum` / `exclusiveMaximum` | dropped — an exclusive bound is not an inclusive one, and rounding it would accept a value the schema forbids |
| `additionalProperties` as a *schema object* | undeclared properties are accepted untyped; StateGuard can require the declared set be exhaustive, not type the members it has no name for |
| tuple-form `items` (a positional list) | element typing widened to `ANY`; one element type applies to the whole array |
| a `default` that fails its own field | the default is dropped, and the field is not auto-filled |

`exclusiveMinimum` is not hypothetical: `mcp-server-fetch` ships it today.

---

## Behaviours worth knowing

### Unions and optionals

`null` members are **dropped**, never carried as union members — nullability
is reported separately. So:

- `["string", "null"]` is a **nullable string**, not a union.
- `anyOf: [{$ref}, {"type": "null"}]` — the shape Pydantic emits for every
  `Optional[X]` — collapses to X, nullable.
- Two or more surviving members produce `UNION` with `union_members`.

This mirrors `PydanticTypeMapper` exactly, on purpose: the same repair
strategies consume both adapters, and a field that read as `UNION` through one
path and `STRING` through the other would be priced differently by the trust
model for no reason a user could see.

### Declared defaults

`wrap()` materialises a schema's declared defaults into the payload — the
Pydantic path already does this via `model_validate`, and returning the dict
untouched made the two adapters disagree about the same schema.

Consequently a default is the one value StateGuard *writes* rather than
merely checks, and it is written after the engine has finished, so nothing
re-checks it. A default is therefore validated against its own field at
extraction time; one that fails is dropped with a warning. Without that
screen, `{"type": "integer", "default": "abc"}` made `repair` return
`ALREADY_VALID` and `validate` then reject its own output.

Note that filling a default can add a key to a payload that was
`ALREADY_VALID` — an absent optional is not a violation, so no repair strategy
ever sees it. In shadow mode that change is withheld and reported as a `HOLD`
preview like any other.

### `strict_mode`

`additionalProperties: false` sets it. `GuardConfig.strict_mode` composes with
it as a **floor**: strict if either says so, at every level of nesting. A
contract is never *loosened* by configuration — a schema that declared itself
closed stays closed.

### Patterns

`pattern` is the only keyword in the subset whose value is *executed*, and it
comes from a third party. Python's `re` is a backtracking engine, so a nested
unbounded quantifier can take exponential time on a short input — `(a+)+$`
against 40 characters did not finish in 15 seconds.

Patterns are therefore screened at extraction and refused if they carry the
`(X+)+` shape. Two honest caveats:

- **It is a heuristic, not a proof.** Detecting catastrophic backtracking in
  general is undecidable. It catches the nested-quantifier family, which is
  behind essentially every real-world ReDoS report; it does not catch
  overlapping alternation such as `(a|a)+`.
- **It over-rejects.** `^(?:[a-z0-9-]+\.)+[a-z]{2,}$` — an ordinary
  domain-name regex that cannot actually blow up, because the separator makes
  each partition unique — is refused, and a refusal fails the whole schema, so
  one such pattern makes a tool unusable. Anchor the pattern or bound the inner
  quantifier (`{1,64}`) to get past it.

A deployment accepting genuinely adversarial schemas should not rely on this
alone; it should run extraction where a hung thread is survivable.

At validation time a `PATTERN` constraint is an **unanchored** match
(`re.search`), matching both JSON Schema's deferral to ECMA-262 and Pydantic's
`Field(pattern=)`.

---

## Known limitations

**Objects inside a union branch or an array element are typed, not
contracted.** `union_members` and `item_type` each carry a single `FieldType`
and no nested spec, so:

```json
{"anyOf": [{"type": "string"},
           {"type": "object", "properties": {"n": {"type": "integer"}}}]}
```

accepts `{"n": "not-an-int"}`. Unsupported *keywords* in those positions are
refused; their *contents* are not checked.

**Drift on an optional parameter is not repaired.** The fuzzy rename strategy
pairs an unexpected key with a *missing required* field. An optional field is
never reported missing — that is what optional means — so a misspelled
optional parameter has no repair target, and even a one-character typo goes
uncorrected. 12 of the corpus's 41 parameters (29%) are optional.

The sharp edge is when that optional field has a declared default:
`mcp-server-git`'s `git_diff` takes `context_lines` with a default of 3, so a
model writing `context: 10` gets `context_lines: 3` filled in beside its
unrecognised key — the server honours 3 and the 10 is silently ignored. This
is a *missed* repair, not a wrong one; nothing is written into a declared
parameter. Repairing onto optional fields is a change to the repair model and
is deferred rather than smuggled into a hardening pass.

**Tool-call repair covers arguments, not results.** Repairing what a server
sends *back* (`outputSchema` / `structuredContent`) is not built. Argument
drift is where the model is, and the model is what drifts.

---

## Corpus evidence

The subset claim is tested against schemas captured by **running** four public
MCP reference servers and recording what `tools/list` actually put on the
wire — not by transcribing their source.

| Server | Version | Tools |
|---|---|---|
| `mcp-server-git` | 2026.8.18 | 12 |
| `mcp-server-sqlite` | 2025.4.25 | 6 |
| `mcp-server-time` | 2026.8.18 | 2 |
| `mcp-server-fetch` | 2026.8.18 | 1 |

**21 tool schemas, all extracting without refusal**, producing exactly two
`SchemaFeatureWarning`s (both `mcp-server-fetch`'s `exclusiveMinimum` /
`exclusiveMaximum`). Every one round-trips: a payload built to satisfy the
extracted contract comes back `ALREADY_VALID`, and the output survives
`validate`.

The false-positive suite runs over the same corpus. An unrecognisable key is
never renamed onto a declared field (swept across all 29 required parameters);
a plausible abbreviation either repairs to the *correct* field or refuses
(18 of 18 land correctly, including `source` → `source_timezone` and
`target` → `target_timezone` on the same call); and a genuinely ambiguous key
is refused rather than guessed.

- Fixtures and provenance: `tests/adapters/jsonschema/corpus/`
- Re-capture: `tests/adapters/jsonschema/corpus/capture.py`
- Tests: `tests/adapters/jsonschema/test_corpus.py`,
  `tests/adapters/mcp/test_false_positives.py`

---

## See also

- [ADR-0001 — JSON Schema source of truth](adr/0001-json-schema-source-of-truth.md)
- [`MCP_ADAPTER_PLAN.md`](../MCP_ADAPTER_PLAN.md) — scope, phases, risks
- [`examples/mcp/`](../examples/mcp/README.md) — a runnable before/after demo
  over real MCP stdio
