# ADR-0001: `ContractValidator` is the source of truth for the JSON Schema / MCP adapter

**Status:** Accepted
**Date:** 2026-08-25
**Deciders:** Arnav (engineering)
**Gates:** `MCP_ADAPTER_PLAN.md` Phase 0, task 0.2 — blocks day 1 of Phase 1

## Context

`IContractAdapter.validate` is documented as delegating to the framework's own
validator:

> The framework's native validator is the **source of truth** for what "valid"
> means. The engine's own `ContractValidator` is used only for pre-repair
> violation analysis; it does not override this method's result.

That holds for `PydanticAdapter`, which calls `model_validate` and maps
Pydantic's errors into our violation vocabulary. It cannot hold for JSON
Schema, because **JSON Schema has no native validator in this process** — it is
a specification, not a library. An adapter for it either takes a dependency or
validates the schema itself.

Two forces make this worth recording rather than deciding in passing:

1. **`DictContractAdapter` faces the same question but is not a precedent.**
   It also validates via `ContractValidator`, and — contrary to
   `MCP_ADAPTER_PLAN.md` §3, which calls this an accident nobody recorded —
   it documents the choice deliberately, at both class and method level:
   *"Since there is no 'native' validator for a plain dict-based schema,
   `validate` delegates entirely to the framework-agnostic
   `ContractValidator`."*

   The plan's premise is wrong, but the correction cuts the other way and
   makes this decision *more* consequential, not less. For the dict adapter,
   `ContractValidator` is authoritative **by definition**: StateGuard owns the
   dict contract format, so there is no external specification it can diverge
   from. "Valid" means what our validator says because nothing else defines
   it.

   JSON Schema is the first case where `ContractValidator` stands in for a
   **specification it does not fully implement**. That is a genuinely new
   situation, and it is what this ADR decides — not a repeat of a choice
   already made.
2. **The MCP adapter makes it load-bearing.** MCP tool-call payloads are the
   category StateGuard is entering deliberately (`StateGuard_v2_Proposal.md`,
   item 2). Whatever "valid" means for the JSON Schema adapter is what it means
   for every repaired tool call, and a gap there is invisible: the engine
   reports `SUCCESS` on a payload a spec-compliant validator would reject.

The repair engine is now hardened (`CORE_HARDENING_PLAN.md`, all phases
complete), so this decision is being made against a stable core rather than
alongside a moving one.

## Decision

**`ContractValidator` is the source of truth for the JSON Schema / MCP adapter
in v1, recorded explicitly and surfaced in user-facing docs.**

`jsonschema` stays available as a future opt-in `sguard[jsonschema]` extra
behind the same `IContractAdapter.validate` seam. Nothing in this decision
forecloses it; the adapter interface is exactly the seam that lets it be added
without touching the engine.

## Options Considered

### Option A: Delegate to `ContractValidator` — **chosen**

| Dimension | Assessment |
|---|---|
| Complexity | Low — no mapping layer, the vocabulary is already ours |
| Cost | Zero new dependencies; preserves the zero-dep core property |
| Coverage | Partial — bounded by `FieldConstraintType`, see Consequences |
| Team familiarity | High — same validator the engine already reasons over |

**Pros:**
- We own the violation vocabulary end-to-end. Repair strategies consume
  `ViolationType` and `FieldConstraintType` directly; nothing has to be
  round-tripped through a third party's error shapes and back.
- MCP `inputSchema` uses a narrow subset (`MCP_ADAPTER_PLAN.md` §5) that
  `ContractValidator` largely covers already.
- Keeps `sguard` installable with no dependencies for this path.

**Cons:**
- StateGuard becomes the authority on what "valid JSON Schema" means, and it
  is not spec-complete. Anything outside the supported subset goes
  unvalidated unless explicitly rejected.
- Two validators now disagree in principle: a payload we call `SUCCESS` may
  fail a spec-compliant validator downstream.

### Option B: Depend on `jsonschema` as an extra

| Dimension | Assessment |
|---|---|
| Complexity | Medium — needs a violation mapper, as Pydantic has |
| Cost | Optional dependency; breaks zero-dep for this path |
| Coverage | High — full keyword set, `$ref`, `oneOf`/`allOf`, `format` |
| Team familiarity | Medium |

**Pros:** spec-correct validation; the disagreement in Option A disappears.
**Cons:** a `violation_mapper` equivalent for someone else's error format is
real work (the Pydantic one is 268 lines); `jsonschema` error output is
famously awkward to localise to a field path, which is precisely what the
repair strategies need; and it front-loads dependency cost onto the phase whose
job is proving the adapter seam.

### Option C: Both from day 1

| Dimension | Assessment |
|---|---|
| Complexity | High — two validation paths through all of Phase 1 |
| Cost | Zero-dep default preserved, extra available immediately |
| Coverage | High where enabled, partial by default |

**Pros:** no forced dependency and no correctness gap for those who opt in.
**Cons:** doubles the surface Phase 1 has to build and test, against a plan
that budgets 5 days for one path. Defers no risk that Option A does not already
defer, since the seam makes Option B additive later.

## Trade-off Analysis

The real question is not "which validator is more correct" — Option B plainly
is — but **what the JSON Schema adapter is for in this phase**.

Its stated purpose in `MCP_ADAPTER_PLAN.md` §11 is to test whether
`IContractAdapter` is the right seam, and to repair drifted tool-call
arguments. Both goals are served by a validator that produces violations the
repair strategies can act on. Neither is served by spec completeness on
keywords that MCP tool schemas do not emit — `allOf`, `not`, `if`/`then`/`else`
and `patternProperties` are explicitly rejected rather than supported precisely
because they do not appear in real tool definitions.

The cost of Option A is therefore bounded and knowable, while the cost of
Option B is paid in full up front on the phase least able to absorb it. And
because the choice sits behind `IContractAdapter.validate`, Option B remains
purely additive: adopting it later changes one adapter, not the engine.

What makes Option A acceptable is not that the gap is small, but that it is
**bounded and written down**. `DictContractAdapter` needed only to record the
delegation, because it has no external spec to fall short of. This adapter does,
so recording the delegation is not enough on its own — the specific keywords
that go unchecked have to be enumerated and surfaced to users, which is what
the Consequences and Action Items below commit to.

## Consequences

**Easier:**
- `sguard` stays dependency-free for the MCP path.
- Violations arrive in the vocabulary the strategies already consume; no
  mapping layer, no field-path localisation problem.
- The adapter stays small enough to be honest evidence about the seam.

**Harder / accepted risk:**
- `ContractValidator` is bounded by `FieldConstraintType`:
  `MINIMUM`, `MAXIMUM`, `MIN_LENGTH`, `MAX_LENGTH`, `PATTERN`, `ENUM_VALUES`,
  `NOT_NULL`. Keywords with no home are dropped **with a warning**, never
  silently faked — `exclusiveMinimum` / `exclusiveMaximum` are the known
  cases, since an exclusive bound is not expressible as an inclusive one.
- Unsupported structural keywords (`allOf`, `not`, `if`/`then`/`else`,
  `patternProperties`, `dependentSchemas`, `propertyNames`,
  `unevaluatedProperties`, remote `$ref`) are rejected loudly at extraction.
  Under-validating silently is the failure mode this avoids.
- `format` is ignored without a warning — advisory in practice.
- A `SUCCESS` from StateGuard is **not** a claim of JSON Schema compliance.
  This must be stated in the adapter docstring and the README, not just here.
- Because this adapter *is* its own source of truth, `pattern` is executed
  against untrusted input with no second validator behind it. That made regex
  handling a correctness and availability concern rather than a detail — see
  "Resolved since" below.

**To revisit:**
- If a real MCP server emits a schema the subset cannot express, that is the
  signal to promote Option B — not a reason to widen `ContractValidator`
  ad hoc.
- `IContractAdapter.validate`'s docstring currently frames "the framework's
  native validator is the source of truth" as an invariant. This adapter does
  not break the interface, but it does contradict that sentence. The docstring
  should be amended to state the actual rule: *the adapter's `validate` is the
  source of truth, and adapters must delegate to a native validator where one
  exists.*
- JSON Schema property names may legally contain `.`, which breaks dotted-path
  navigation. Guarded in Phase 1 per `MCP_ADAPTER_PLAN.md` §7 — refused at
  extraction rather than silently mis-addressed.

**Resolved since this ADR was accepted:**
- `$id` on a **subschema** rebases reference resolution, so `#/$defs/A` inside
  it means that subschema's `$defs`, not the document's. It was being ignored,
  which silently resolved to the *wrong* target — precisely the failure mode
  this ADR exists to prevent. Now refused at extraction; root-level `$id` stays
  allowed because it does not change what `#` addresses.
- `pattern` was matched with `re.match` (anchored) where JSON Schema and
  Pydantic are both unanchored, producing **false** violations, and a
  schema-supplied regex could backtrack catastrophically. Now `re.search`, with
  unusable patterns raising a clear error and the nested-quantifier family
  refused at extraction (`jsonschema/patterns.py`). The ReDoS screen is a
  documented heuristic, not a proof.

## Action Items

1. [ ] Record this choice in `JSONSchemaAdapter`'s class docstring, not just in this ADR.
2. [ ] State in the README that `SUCCESS` is not a JSON Schema compliance claim, and name the unsupported keywords.
3. [ ] Leave `DictContractAdapter` as it stands. Its delegation is already documented and is correct by definition — StateGuard owns that format. Recorded here so the distinction is not "tidied away" later by someone applying this ADR uniformly.
4. [ ] Amend `IContractAdapter.validate`'s docstring so the interface states the rule it actually enforces.
5. [ ] Emit a warning for every dropped keyword; add a test asserting `exclusiveMinimum` warns rather than being silently ignored.
6. [ ] Reject unsupported structural keywords at extraction time with a clear error naming the keyword.
