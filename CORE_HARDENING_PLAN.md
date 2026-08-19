# Plan — Core Repair Engine Hardening

**Decision:** harden the engine before adding surfaces. This is the right
call, and it now matches the updated proposal (`StateGuard_v2_Proposal.md`
item 1, priority 10/10). `MCP_ADAPTER_PLAN.md` is gated on this document
completing.

**Companion docs:** `NEXT_STEPS.md` (full defect inventory),
`MCP_ADAPTER_PLAN.md` (next surface, gated on this).

---

## 0. Scope reconciliation

Your five items vs. the proposal's item 1:

| # | Item | In your list | In proposal |
|---|---|---|---|
| A | Confidence → trust score | ✅ #1 | ✅ |
| B | **Ambiguous as a first-class outcome** | — | ✅ |
| C | Multi-step / iterative repair | ✅ #2 | ✅ |
| D | Non-dict input handling | ✅ #3 | ✅ |
| E | Enum repair | ✅ #4 | ✅ |
| F | Case-insensitive matching | ✅ #5 | ✅ |
| G | **Repair explanations / audit output** | — | ✅ |
| H | **Shadow Mode / Auto Mode** | — | ✅ |

**B and G are not separable from A.** The "ambiguous" outcome *is* the
abstain region of a calibrated trust model, and the explanation *is* the
evidence object that produces the score. Building A without them means
building A twice. They're folded into Phase 3 below.

**H is separable and unusually cheap** — see Phase 6. Flagging it because it
is the highest product-value item in the proposal's list relative to its cost,
and it's the thing that lets a team adopt StateGuard without trusting it
blindly on day one.

**Nothing is dropped.** All eight land.

---

## 1. Recommended sequence (differs from your ordering — reasons given)

You listed confidence first. I'd put it third. Three reasons:

1. **C (multi-step) is a 1-day fix that currently returns `FAILED` on correct
   repairs.** Until it lands you cannot test A, E, or F end-to-end, because
   every one of them fires on a second pass. Fixing the loop first gives you a
   working baseline to measure the confidence change *against*.
2. **F (normalized matching) shrinks what A has to cover.** Every rename that
   a normalized-exact matcher catches is a rename fuzzy scoring no longer has
   to price. Do the thing that *reduces* risk before the thing that *reprices*
   risk.
3. **E (enum repair) should be priced under the new model, not the old one.**
   Build it before A and you'll invent fresh magic constants and immediately
   reprice them.

| Phase | Contents | Est. |
|---|---|---|
| 1 | C — multi-step repair + convergence | ✅ **DONE** |
| 2 | D — non-dict input handling | ✅ **DONE** |
| 2b | D — root recovery widened (Mapping/dataclass/namedtuple, pair lists, `deepcopy` bug) | ✅ **DONE** |
| 3 | **A + B + G** — trust model, ambiguous outcome, explanations | ✅ **DONE** |
| 3b | Phase 3 review follow-ups (14 findings) | ✅ **DONE** |
| 4 | F — normalized / case-insensitive matching | ✅ **DONE** |
| 4b | Phase 4 review follow-ups (15 findings) | ✅ **DONE** |
| 5 | E — enum extraction + enum repair strategy | ✅ **DONE** |
| 6 | H — Shadow / Auto modes | ✅ **DONE** |
| 7 | Calibration harness + false-positive corpus | ✅ **DONE** |

**Total: ~15.5 days.** Phases 1, 2, 4 are independent and parallelisable if
two people are on it.

> **Note on F's position:** I've put it after Phase 3 rather than before,
> because the trust model gives it a principled score instead of another
> hand-picked constant. If you'd rather have the quick win early, it moves to
> Phase 2.5 at the cost of one repricing pass. Your call — flagging the
> tradeoff rather than deciding it.

---

## Phase 1 — Multi-step / iterative repair ✅ COMPLETE

**Status:** implemented and verified on Python 3.13.14 / pydantic 2.13.4.

| | |
|---|---|
| Baseline | 1,493 passing |
| After | **1,516 passing** (+23 new), 0 regressions |
| Coverage | 99% total; `engine.py` 98%, `config.py` 100% |
| `ruff check` / `ruff format` | clean |
| `mypy --strict src/` | clean, 36 files |
| Benchmarks | 9/9, avg confidence 0.928 |
| Isolation suite | 7/7 |

**Files changed**

| File | Change |
|---|---|
| `core/engine.py` | Progress metric + regression/cycle rules; `_progress_key`; status determination; module docstring |
| `core/models/config.py` | `max_attempts` 3 → 6, docstring |
| `cli.py` | Defaults derived from `RepairConfig` (they had drifted: CLI 5 vs. library 3) |
| `tests/core/test_engine_convergence.py` | **new** — 23 tests |
| `tests/core/models/test_config.py` | 3 assertions updated for the new default |

**Verified before → after**

| Case | Before | After |
|---|---|---|
| `{"temp_celsius": "31.5"}` → `temperature: float` | `FAILED`, output `None` | `SUCCESS`, `{"temperature": 31.5}` |
| `{"agee": -5}` → `age: int = Field(ge=0)` | `FAILED`, output `None` | `PARTIAL`, `{"age": -5}` — rename kept |
| `{"station_id": "KBOS", "temp_celsius": "31.5"}` (3 issues) | n/a | `SUCCESS` in one call — alias + fuzzy + coerce + default-fill |

---

**The bug:** `engine.py:469-481` compared revalidation signatures against the
**initial** violation set. Any repair that legitimately exposes a *different
kind* of problem is misread as a regression → `FAILED`, `repaired_output=None`.

```
contract: temperature: float      payload: {"temp_celsius": "31.5"}
initial:  {(temperature, missing), (temp_celsius, unexpected)}
attempt1: rename -> {"temperature": "31.5"}          # correct
revalidate: {(temperature, type_mismatch)}           # not a subset
result:   FAILED, repaired_output=None               # wrong
```

### 1.1 Replace the regression rule

A regression is *not* "a new kind of violation appeared." It is **"we made
things worse."**

```python
prev_errors = count_errors(current_violations)
new_errors  = count_errors(revalidation.violations)
introduced  = new_signatures - previous_signatures
introduced_errors = {s for s in introduced if severity(s) is ERROR}

if new_errors < prev_errors:
    continue                      # progress -- kinds may change, count fell
if introduced_errors and new_errors >= prev_errors:
    revert_to_last_good(); REGRESSION
if new_hash in seen_hashes:
    stop                          # cycle
stop                              # no progress
```

Two changes beyond the comparison target:

- **Track a last-good snapshot.** Today a regression on attempt 3 discards
  the correct work of attempts 1–2. Keep the best state reached and return it
  as `PARTIAL` instead of throwing everything away.
- **Cycle detection across *all* iterations,** not just the immediately
  previous one. Keep a `seen_hashes: set[str]`. Today `previous_hash` only
  catches A→A, not A→B→A.

### 1.2 Convergence guarantee

With "errors strictly decrease or we stop" as the loop invariant, termination
no longer depends on `max_attempts`. That makes the default safe to raise from
3 to 6 — enough for rename → coerce → enum → default-fill in one call, which
is the proposal's success criterion ("a multi-issue payload is fully repaired
in one call").

Write the invariant down in the module docstring. It is the argument that the
loop cannot spin.

### 1.3 Tests

- rename → coerce → `SUCCESS` (the README's headline case; fails today)
- rename → constraint violation → `PARTIAL` with the rename kept (not `FAILED`)
- 4-issue payload converging in one call
- a genuine regression still aborts, and returns the last-good state
- A→B→A cycle terminates
- `max_attempts=1` still behaves as documented

**Exit:** rename-then-coerce returns `SUCCESS`. This gates Phases 3–5.

---

## Phase 2 — Non-dict input handling ✅ COMPLETE

**Status:** implemented and verified on Python 3.13.14 / pydantic 2.13.4.

| | |
|---|---|
| Before Phase 2 | 1,516 passing |
| After | **1,668 passing** (+152 new), 0 regressions |
| Coverage | 99% total; `validator.py` **100%**, `guard.py` **100%**, `results.py` **100%**, `engine.py` 98% |
| `ruff check` / `ruff format` | clean |
| `mypy --strict src/` | clean, 36 files |
| Benchmarks | 9/9 |
| Isolation suite | 7/7 |

**Measured before → after** — 15 root types through `ContractGuard.repair`:

| Root | Before | After |
|---|---|---|
| `None`, `5`, `3.14`, `True` | `TypeError` | `FAILED` + `STRUCTURAL_MISMATCH` |
| `"abc"`, `"{not json"` | `TypeError` | `FAILED` |
| `b"bytes"` | `TypeError` | `FAILED` |
| `[1,2]`, `(1,2)` | `IndexError` | `FAILED` |
| `[{"a":1}, {"a":2}]` | `TypeError` | `FAILED` — refuses to pick |
| `[("a",1)]` | `TypeError` | `FAILED` — no longer `dict()`-coerced |
| `{1,2}`, `object()` | `TypeError` | `FAILED` |
| `[]` | **`failed`, silently `dict([]) == {}`** | `FAILED` + `STRUCTURAL_MISMATCH` |
| `'{"a":1,"b":"x"}'` | `TypeError` | **`SUCCESS`** → `{"a":1,"b":"x"}` |
| `b'{"a":1,"b":"x"}'` | `TypeError` | **`SUCCESS`** |
| `[{"a":1,"b":"x"}]` | `TypeError` | **`SUCCESS`** |

14 of 15 crashed before; all 15 now return a `RepairResult`.

**Note on the earlier characterisation.** The plan previously described
`"abc"` and `[("a",1)]` as *silent-nonsense* paths. That was measured by
probing the expressions in isolation. End-to-end they in fact **crashed**,
because `_validate_field` runs before `_detect_unexpected` and dies first.
The one genuinely silent path was `[]`, which `dict([])` turned into `{}`.
Correcting the record — the conclusion (all of these are broken) held, the
mechanism for two of them did not.

**Files changed**

| File | Change |
|---|---|
| `core/validator.py` | `root_structural_violation()` (new, exported); `validate()` guards a non-dict root; `data: Any` |
| `core/engine.py` | `_normalise_root_payload()` + `_unwrap_single_element()`; root handling in `repair()`; `_root_failure()`; `_validate()` guard; `ALREADY_VALID` → `SUCCESS` when the root was normalised |
| `core/errors/results.py` | `ValidationResult.raw_input` and `RepairResult.original_input` widened to `Any` |
| `guard.py` | `repair()` / `validate()` accept `Any` |
| `tests/core/test_engine_root_shape.py` | **new** — 152 tests |

**Design note — `SUCCESS`, not `ALREADY_VALID`.** When the root is recovered
and the object inside needs no field repair, the result is `SUCCESS`:
normalising the root *is* a repair, so `ALREADY_VALID` ("the input needed no
repair") would be a false statement. `ContractGuard.validate()` agrees —
`'{"a":1}'` reports `is_valid=False`, preserving the documented invariant
that `validate(...).is_valid` is true exactly when `repair(...)` returns
`ALREADY_VALID`.

---

### Original analysis

**Behaviour before the fix** (probed against the expressions in
`validator.py` and `engine.py`):

| Input | Result |
|---|---|
| `5`, `None` | `TypeError` — crash |
| `[{"a":1}]` | `TypeError: unhashable type: 'dict'` — crash |
| `(1,2)` | Passes field checks, then crashes at `dict(data)` |
| `[1,2]` | `TypeError` at `dict(data)` |
| `"abc"` | `_detect_unexpected` iterates *characters* (in isolation); crashes earlier end-to-end |
| `[("a",1)]` | `dict()` silently reinterprets it as `{"a": 1}` (in isolation); crashes earlier end-to-end |

### 2.1 Guard the boundary

Validate input shape once, at the top of `RepairEngine.repair`, before any
deepcopy or validation:

```python
if not isinstance(data, dict):
    # -> STRUCTURAL_MISMATCH at field_path "", severity ERROR
    # -> unless a root-level repair applies (2.2)
```

### 2.2 Two root shapes are *repairable*, not just rejectable

Both are real LLM failure modes and both are cheap:

- **`'{"a": 1}'`** — the model returned the entire payload as a JSON string.
  Parse; if it yields a `dict`, that's a root-level `COERCE`. Risk tier
  `REVERSIBLE` (round-trip is exact). This is `NEXT_STEPS.md` §3.2 applied at
  the root and it also unblocks the same fix for nested fields.
- **`[{"a": 1}]`** — single-element list wrapping the object. Unwrap.
  Risk tier `INFERRED` — refuse if the list has more than one element.

### 2.3 Fix the unguarded expressions

- `validator.py:94` and `engine.py:614` — `raw_input=dict(data)` must not be
  the type check. Store the object as-received; annotate `raw_input` as
  `Any`, not `dict[str, Any]`.
- `validator.py:287` — guard `_detect_unexpected` with `isinstance(data, dict)`.
- `validator.py:124` — guard `_validate_field`'s membership test.
- Same guards for **nested** non-dict values, which currently produce
  `STRUCTURAL_MISMATCH` correctly — verify with tests, don't assume.

### 2.4 Tests

Parametrised over `None`, `5`, `3.14`, `True`, `"abc"`, `b"bytes"`, `[]`,
`[1,2]`, `[{"a":1}]`, `[("a",1)]`, `(1,2)`, `set()`, a custom object:
**every one returns a `RepairResult`, none raises.** Plus positive tests for
the two repairable shapes in 2.2.

---

## Phase 2b — Root recovery, widened ✅ COMPLETE

**Status:** implemented and verified on Python 3.13.14 / pydantic 2.13.4.

| | |
|---|---|
| Before Phase 2b | 1,668 passing |
| After | **1,744 passing** (+76), 0 regressions |
| Coverage | 99% total; `engine.py` 98% |
| `ruff check` / `ruff format` | clean |
| `mypy --strict src/` | clean, 36 files |
| Benchmarks | 9/9 |
| Isolation suite | 7/7 |

**Measured before → after**

| Root | Before 2b | After |
|---|---|---|
| `MappingProxyType` | **raises** `TypeError: cannot pickle` | `SUCCESS` |
| `UserDict` | `FAILED` | `SUCCESS` |
| custom `Mapping` | `FAILED` | `SUCCESS` |
| dataclass instance | `FAILED` | `SUCCESS` |
| namedtuple instance | `FAILED` | `SUCCESS` |
| `[["a",1],["b","x"]]` | `FAILED` | `SUCCESS` |
| `[("a",1),("b","x")]` | `FAILED` | `SUCCESS` |
| `'[["a",1],["b","x"]]'` | `FAILED` | `SUCCESS` |
| `OrderedDict` | `ALREADY_VALID` | `ALREADY_VALID` (unchanged — it *is* a `dict`) |
| `BaseModel` instance | `FAILED` | `FAILED` (deliberate, §2b.1) |
| bare scalar | `FAILED` | `FAILED` (deliberate, §2b.4) |
| dup key / non-str key / arity-3 / positional / two dicts / `[]` / `None` / `set` | `FAILED` | `FAILED` |

**Files changed**

| File | Change |
|---|---|
| `core/engine.py` | `_safe_deepcopy`, `_pairs_to_dict`, `_mapping_like_to_dict`; `_normalise_root_payload` extended; `repair()` copies *after* normalisation |
| `tests/core/test_engine_root_shape.py` | +76 tests; pair-list cases moved from unrepairable to repairable; guard-failure cases added |

**A second bug found while implementing.** The recovery helpers return
*shallow* copies (`dict(sequence[0])`, `dict(mapping)`), and `repair()` was
assigning that straight to `working_data`. Nested values would therefore have
stayed shared with the caller's object and been mutated in place by the repair
loop — violating the documented "never mutated" guarantee for every recovered
root. Fixed by deep-copying after normalisation rather than before; covered by
`TestCallerDataIsNeverMutated`.

---

### Original analysis

Phase 2 treated every non-`dict` root except two shapes as unrepairable.
Probing that claim showed it was too strict, and that Phase 2's own "never
raises" guarantee had a hole.

### 2b.0 The `deepcopy` hole — a bug, not a scope question

`RepairEngine.repair` calls `deepcopy(data)` *before* the root-shape guard,
so a root that cannot be deep-copied still raises:

```python
guard.repair(SCHEMA, MappingProxyType({"a": 1}))
# TypeError: cannot pickle 'mappingproxy' object
```

This falsifies the "never raises on any root type" guarantee Phase 2 claims.
Fix regardless of whether 2b.1/2b.2 proceed: normalise the root *before*
copying, and take the `original_input` audit snapshot through a
copy-failure-tolerant helper (falling back to a reference, since the engine
never writes to it).

### 2b.1 Tier 1 — objects that already have named fields

Not inferences. Each is the type's own canonical dict form, defined by the
standard library. `isinstance(data, dict)` is simply too narrow a test for
"is this an object".

| Root | Conversion | Behaviour today |
|---|---|---|
| any `collections.abc.Mapping` that is not a `dict` subclass — `MappingProxyType`, `UserDict`, third-party mapping types | `dict(m)` | raises / `FAILED` |
| `dataclass` instance | `dataclasses.asdict()` | `FAILED` |
| `namedtuple` instance | `._asdict()` | `FAILED` |

**Deliberately excluded: framework-native objects.** A Pydantic `BaseModel`
instance has an equally canonical `model_dump()`, but recognising it in the
core engine would make Layer 5 implicitly aware of an adapter's type system
and break the layering rule the CI isolation job exists to enforce. It wants
an `IContractAdapter` normalisation hook — an interface change, and therefore
a decision to take **before Architecture Specification v1 freezes**, not
after.

### 2b.2 Tier 2 — key/value pair lists

`[["a", 1], ["b", "x"]]` → `{"a": 1, "b": "x"}`. This is the wire form of
`dict.items()` / JavaScript's `Object.entries()`, which some serialisers emit
in place of an object.

Accepted only when the reading is unambiguous and exactly reversible:

- every element is a 2-element sequence that is not itself a `str`/`bytes`;
- every key is a `str`;
- no key repeats — a duplicate makes the conversion lossy (one value silently
  wins), so refuse rather than resolve.

Under those conditions `list(d.items())` round-trips exactly. Note this is
*not* a reversal of Phase 2's refusal: Phase 2 rejected `dict()` swallowing
the shape **unvalidated**, which is a different thing from converting it
behind a guard.

### 2b.3 Stays `FAILED` — every remaining reading requires guessing at intent

| Root | Why refused |
|---|---|
| `None`, `[]` | Would mean fabricating an object out of nothing |
| `[1, 2]` | Positional-to-field mapping is a pure guess |
| `[{"a":1}, {"a":2}]` | Genuinely ambiguous — no principled element to pick |
| `set` | No key structure at all |
| plain `object()` | No conversion protocol to use |

### 2b.4 Logged for Phase 3 — the bare-scalar case

**Logged, not scheduled.** A bare scalar against a single-field contract:

```python
guard.repair({"fields": [{"path": "city", "type": "string"}]}, "Mumbai")
# plausible reading: {"city": "Mumbai"}     currently: FAILED
```

Real LLM behaviour — models routinely return a bare value when the schema has
one parameter. But unlike everything in 2b.1/2b.2 this is an **inference about
intent**, not a re-encoding of data that is already structured.

Implementing it here would mean applying it unconditionally and silently,
which is exactly the failure mode Phase 3 exists to eliminate. It belongs in
the trust model as an `INFERRED`-risk proposal carrying a trust score, so it
can land in the abstain band and surface as `AMBIGUOUS` rather than being
applied on a hunch.

**Pick this up in Phase 3, once `RepairRisk` and the abstain band exist.**

---

## Phase 3 — Trust model + ambiguous outcome + explanations ✅ COMPLETE

**Status:** implemented and verified on Python 3.13.14 / pydantic 2.13.4.

| | |
|---|---|
| Before Phase 3 | 1,744 passing |
| After | **1,800 passing**, 0 regressions |
| Coverage | 98% total; `trust.py` 96%, `fuzzy.py` 97%, `coerce.py` 97% |
| `ruff` / `ruff format` / `mypy --strict` | clean |
| Benchmarks | 9/9 |
| Isolation suite | 7/7 |

### The finding that changed the design

The plan assumed Jaro-Winkler would fix the `user_email` misrepair. **It does
not.** Measured against the corpus:

| pair | Jaro-Winkler | required outcome |
|---|---|---|
| `user_name` ← `user_email` | **0.913** | must NOT apply |
| `user_id` ← `user_email` | **0.891** | must NOT apply |
| `temperature` ← `temp_celsius` | **0.809** | must apply |

The dangerous pairing scores *higher* than the one that has to succeed. Name
similarity alone cannot separate them at any threshold.

What separates them is **competition**: `temp_celsius` beats its runner-up by
0.337; `user_email` is equally at home in two places (margin 0.021). So the
fix is global bipartite assignment plus margin-as-evidence — not the
similarity metric. Jaro-Winkler still earns its place by removing the
hand-tuned prefix floor, but it is not what closes the bug.

### Calibration

Bands were fitted to required outcomes, not chosen for looking round. With
`margin_tie_factor = 0.70`, the must-apply floor is **0.809** and the
must-abstain ceiling is **0.678**, so `INFERRED.apply_at` sits at **0.75**,
roughly midway. `TestCalibration` pins this.

### What shipped

| Area | Change |
|---|---|
| `core/errors/operations.py` | `RepairEvidence`, `RepairRisk`; `confidence` → `trust` + `evidence` + `risk`; `confidence` retained as a read-only property |
| `core/trust.py` | **new** — `TrustPolicy`, `TrustBand`, `TrustDecision`, per-risk bands, `explain()` |
| `core/strategies/*` | All four strategies report evidence and declare risk; none scores itself |
| `core/strategies/coerce.py` | Four magic constants replaced by measured round-trip fidelity; NaN/Infinity refused; `isdigit` → `isdecimal` |
| `core/strategies/fuzzy.py` | Jaro-Winkler; global assignment; bipartite margin |
| `core/engine.py` | Scores via policy; collects `AmbiguousRepair`; **an op that changes nothing is no longer recorded as applied** |
| `core/errors/results.py` | `RepairStatus.AMBIGUOUS`, `AmbiguousRepair`, `is_ambiguous`, `has_ambiguous_repairs` |
| `cli.py` | Exit code 3; ambiguous candidates in human and JSON output |
| `logging/repair_history.py` | JSONL now records `trust` + `risk` instead of `confidence` |

### Defects closed in passing

- **Values no longer leak into logs.** Rationales and explanations carry paths
  and scores only — `NEXT_STEPS.md` §5.
- **No-effect operations are no longer recorded as applied** — `NEXT_STEPS.md`
  §6. They move to `rejected_operations` with an `operation.no_effect` log.
- **`"nan"` / `"inf"` refused** as float coercions — `NEXT_STEPS.md` §5.
- **`isdigit` → `isdecimal`**, so `"²"` no longer produces a phantom applied
  operation — `NEXT_STEPS.md` §14.
- **The `M9_AUDIT` cross-branch limitation is fixed.** Global assignment pairs
  `branchA.cod`→`branchA.code` and `branchB.cod`→`branchB.code` correctly;
  per-field collision detection used to abandon both.

### Breaking changes

| Change | Note |
|---|---|
| `FieldOperation(confidence=...)` no longer constructs | `trust=` instead; `.confidence` still reads |
| Field order changed to `(op_type, target_path, rationale, ...)` | Positional construction must be updated |
| `RepairStatus.AMBIGUOUS` added | Exhaustive matches need a new arm |
| Fuzzy renames at 0.60–0.75 now abstain | **Intended** — this is the bug fix |
| `resolve_union_member` returns `(member, evidence, risk)` | Was `(member, confidence)` |
| History JSONL: `confidence` → `trust` + `risk` | Log format change |
| `FuzzyFieldMatchStrategy(min_confidence_threshold=…, score_collision_margin=…)` | Accepted and ignored; thresholds live on `TrustPolicy` |

### Deferred

- **§7 calibration harness** — the labelled corpus and reliability curve. The
  bands are fitted to required outcomes, which is not the same as being
  *calibrated*; that still needs the corpus.
- **§2b.4 bare-scalar root** — now unblocked, since `RepairRisk` and the
  abstain band exist.

---

### Original plan


The largest and most consequential phase. Also the one that makes Stage 2
(semantic repair) possible — an LLM-backed strategy needs somewhere to put
"the model thinks these correspond, at this strength," and today there is no
such slot.

### 3.1 What's wrong with the current model

| Problem | Evidence |
|---|---|
| **Scores aren't commensurable** | Fuzzy's 0.8 means *name similarity*; coercion's 0.85 means *type castability*. Both are compared against one global `min_confidence_threshold` and averaged into the benchmark's "average confidence" |
| **Confidence is manufactured to clear the bar** | `_PREFIX_MATCH_BASE_CONFIDENCE = 0.7`, with a comment saying it was "chosen to clear the engine's default min_confidence_threshold (0.7)". That is tuning the evidence to the threshold, not the threshold to the evidence |
| **`max()` is the most dangerous combiner** | `_combined_score = max(levenshtein, prefix_boost)` (`fuzzy.py:199`) lets *any single* signal carry a proposal over the line. This is precisely why `user_email` → `user_id` scores 0.82 |
| **No notion of consequence** | Renaming a field and *deleting* one are scored on the same scale. So are a reversible `"5"`→`5` and a lossy `{...}`→`'{"...":...}'` |
| **No abstain region** | Binary apply/reject. An ambiguous match is silently dropped — the caller can't see it, re-prompt on it, or review it |
| **Never calibrated** | Nothing measures whether 0.85 means "correct 85% of the time" |
| **Greedy alphabetical assignment** | `fuzzy.py:267` sorts missing fields alphabetically and lets the first one claim a candidate. Which field wins a contested rename is decided by the alphabet |

### 3.2 The core split: strategies report *evidence*, the policy computes *trust*

```python
@dataclass(frozen=True)
class RepairEvidence:
    """What was measured. Each field [0,1], meaning documented per field."""
    name_match: float | None = None         # strength of name correspondence
    value_preserved: float | None = None    # round-trip fidelity of the value
    schema_authority: float | None = None   # did the schema DECLARE this? alias/default/enum
    margin: float | None = None             # gap to the runner-up candidate
    alternatives_considered: int = 0
    signals: dict[str, float] = field(default_factory=dict)   # strategy extras
    notes: tuple[str, ...] = ()             # human-readable, feeds G
```

```python
class RepairRisk(IntEnum):
    """Consequence if WRONG. Not likelihood -- consequence."""
    REVERSIBLE  = 0   # exact round-trip: "5" -> 5
    DECLARED    = 1   # schema said so: alias rename, default fill
    INFERRED    = 2   # we inferred a correspondence: fuzzy rename, enum normalise
    LOSSY       = 3   # information invented or destroyed: dict->json str, array wrap
    DESTRUCTIVE = 4   # data removed: REMOVE
```

`FieldOperation` gains `evidence: RepairEvidence` and `risk: RepairRisk`, and
`confidence: float` becomes `trust: float` **computed by the policy, not the
strategy**. Keep `confidence` as a deprecated read-only alias for one release.

Why this matters beyond tidiness: it moves all the arithmetic into one place,
so you can recalibrate without touching any strategy, thresholds can vary by
consequence, and Stage 2 plugs in as another *evidence source* rather than
another invented constant.

### 3.3 Thresholds per risk tier, with an abstain band

This is Fellegi–Sunter's three-region model (match / possible-match /
non-match), which is the standard answer in record linkage and is what the
current single threshold is an impoverished version of.

| Risk | reject below | **ambiguous** | apply at/above |
|---|---|---|---|
| `REVERSIBLE` | 0.50 | 0.50–0.70 | 0.70 |
| `DECLARED` | — | — | always (the schema is the authority) |
| `INFERRED` | 0.60 | 0.60–0.85 | 0.85 |
| `LOSSY` | 0.70 | 0.70–0.95 | 0.95 |
| `DESTRUCTIVE` | — | always | never without explicit opt-in |

Note the effect: today's fuzzy rename at 0.82 is `INFERRED` and lands in the
**ambiguous** band rather than being silently applied. That single change
fixes the `user_email` → `user_id` case.

Defaults live in `TrustPolicy`, overridable via `RepairConfig`. This is also
the "confidence thresholds and repair policies are configuration within Auto
Mode" requirement from the proposal — it's config on one object, not a mode.

### 3.4 Fix the fuzzy matcher's three structural problems

**(a) Replace Levenshtein + `max()`-boost with Jaro–Winkler.**
The prefix boost exists because normalised Levenshtein scores
`temp_celsius`/`temperature` at ~0.42. Jaro–Winkler weights common prefixes
*by construction*, which is the same intuition without a hand-tuned floor
bolted on via `max()`. Keep `_normalized_score` and `_token_prefix_boost` as
tested building blocks; change what `_score_candidates` calls.

**(b) Global assignment, not greedy-alphabetical.**
Score every (missing_field × candidate) pair, then assign by descending global
score. `user_email` then competes for `user_id` *and* `user_name`, scores
nearly the same for both, margin ≈ 0 → **ambiguous**, abstain. Correct
outcome, and it falls out of the structure rather than needing a special case.

**(c) Margin becomes evidence, not a veto.**
`score_collision_margin` currently produces a silent drop. Make margin a field
on `RepairEvidence` that *reduces trust continuously*, so a near-tie degrades
into the ambiguous band rather than vanishing.

### 3.5 Measured evidence for coercion, not constants

Replace `_NUMERIC_COERCION_CONFIDENCE = 0.95` etc. with an actual round-trip
measurement:

| Input | Coerced | Round-trip | `value_preserved` |
|---|---|---|---|
| `"5"` | `5` | `"5"` == input | 1.0 |
| `" 5 "` | `5` | `"5"` == input.strip() | 0.95 |
| `"05"` | `5` | `"5"` ≠ `"05"` | 0.85 |
| `"5.0"` → int | — | not exact | reject |
| `"nan"` → float | `nan` | — | **reject** (`NEXT_STEPS.md` §5) |
| `{"a":1}` → str | `'{"a": 1}'` | parses back equal | 1.0 but risk = `LOSSY` |

This converts three magic constants into one measurable property, and it fixes
the NaN/Infinity acceptance for free.

### 3.6 Ambiguous as a first-class outcome (item B)

```python
class RepairStatus(StrEnum):
    SUCCESS, PARTIAL, FAILED, ALREADY_VALID = ...
    AMBIGUOUS = "ambiguous"        # NEW

@dataclass
class AmbiguousRepair:
    target_path: str
    candidates: list[tuple[FieldOperation, float]]   # ranked by trust
    reason: str        # "2 candidates within 0.03; risk=INFERRED needs 0.85"
```

`RepairResult.ambiguous: list[AmbiguousRepair]`.

`AMBIGUOUS` is distinct from `FAILED`: *"I found a repair but won't apply it
unsupervised."* That's actionable output — an agent can re-prompt with the
candidates, a human can pick, Shadow Mode displays it. `FAILED` stays "no
repair found."

**Callers must be able to branch on this**, which is the proposal's success
criterion. Add `RepairResult.is_ambiguous`, and a CLI exit code (`3`).

### 3.7 Explanations and audit output (item G)

The `RepairEvidence` object *is* the explanation — it just needs rendering.
Replace today's f-string rationale (which also leaks raw values into logs,
`NEXT_STEPS.md` §5) with a structured `explain()` that emits:

```
RENAME  user_email -> user_id     trust 0.71  risk INFERRED  → AMBIGUOUS
  name_match      0.78   (jaro-winkler, common prefix "user_")
  margin          0.03   (runner-up: user_name @ 0.75)
  decision        needs 0.85 for INFERRED; 2 candidates within margin
```

Three consumers, one source: CLI human output, the JSONL history record, and
Shadow Mode.

**While here, close two open defects:**
- Values must not appear in explanations unless
  `RepairConfig.include_values_in_log` is `True` — the flag is currently read
  nowhere (`NEXT_STEPS.md` §5).
- An operation that silently no-ops must not be recorded as applied
  (`NEXT_STEPS.md` §6). Assert `data_before != data_after` per applied op.

### 3.8 Tests

- Every risk tier's threshold boundary: reject / ambiguous / apply
- `user_email` vs `{user_id, user_name}` → `AMBIGUOUS`, both candidates present
- `temp_celsius` → `temperature` still applies (the boost's legitimate case)
- Round-trip evidence table (3.5) exactly
- `"nan"` / `"inf"` rejected
- A `DECLARED` alias rename applies at any margin
- Deprecated `confidence` alias still reads

---

## Phase 3b — Review follow-ups ✅ COMPLETE

A review of the Phase 3 work found 14 defects. All are closed.

| # | Defect | Fix |
|---|---|---|
| 1 | An abstaining strategy ended the whole repair, so one uncertain rename suppressed a certain, schema-declared fill | Engine tries applicable strategies in priority order until one has something to apply |
| — | *That fix re-opened the `user_email` bug*: filling the competing field made the refused rename look unopposed, and the email landed in `user_id` after all | A source key withheld once stays withheld for the run (`_hold_tainted`) |
| 2 | `--confidence-threshold` / `min_confidence_threshold` read by nothing — 0.1 and 0.99 gave identical output | Becomes `TrustPolicy.minimum_trust`, a floor that only ever *raises* a tier's bar |
| 3 | `AmbiguousRepair.candidates` never held the competing option | `_record_ambiguous` merges rivals for one target, ranked by trust |
| 4 | Duplicate ambiguous entries across attempts | Same merge, de-duplicated by (op_type, source, value) |
| 5 | No-effect check used `==`, so `5` and `5.0` looked identical | `_apply_operation` returns whether it changed anything; `_identical` compares types |
| 6 | Abstained ops in neither `applied` nor `rejected` | `RepairAttempt.abstained_operations` |
| 7 | Trust bands unconfigurable by any caller | `ContractGuard(policy=...)` |
| 8 | A full deepcopy per operation | Removed — the appliers report their own effect |
| 9 | `alternatives_considered` ignored target-side competition | Counts both sides |
| 10 | `runner_up` conflated targets and candidates | `runner_up_kind` + direction named in the note |
| 11 | `with_trust()` dead code | Deleted |
| 12 | Greedy assignment documented as "global" | Limitation documented |
| 13 | `explain()` doubled every log entry | Only withheld operations get one |
| 14 | Human CLI said "confidence", JSON said "trust" | Both say trust + risk; benchmark label updated |

**Verified before → after**

| Case | Before 3b | After |
|---|---|---|
| Contested rename + unrelated default fill | `AMBIGUOUS`, no output | `PARTIAL`, default applied, email still withheld |
| `min_confidence_threshold=0.99` | ignored | `AMBIGUOUS` — the knob works |
| Duplicate ambiguous entries | 2 | 1 |
| `cli.py` coverage | 89% | **99%** |

---

## Phase 4 — Normalized / case-insensitive matching ✅ COMPLETE

**Status:** implemented and verified on Python 3.13.14.

| | |
|---|---|
| Tests | **1,871 passing**, 0 regressions |
| Coverage | 98% total; `normalized.py` 97% |
| ruff / format / mypy --strict | clean |
| Benchmarks / isolation | 9/9, 7/7 |

`NormalizedNameStrategy` at priority 15, between `ExactAlias` (10) and
`Fuzzy` (20). Normalisation is a closed list: `casefold` → `strip` → remove
`_`, `-`, spaces. Only the final path segment is normalised, so a rename can
never jump between branches.

**Measured**

| Payload key | Declared field | Before | After |
|---|---|---|---|
| `UserID` | `user_id` | fuzzy, trust ~0.86 | **1.00**, `NormalizedNameStrategy` |
| `firstName` | `first_name` | fuzzy | **1.00** |
| `ZIP-CODE` | `zip_code` | fuzzy | **1.00** |
| `user id` | `user_id` | fuzzy | **1.00** |

The point is not the higher number — it is that these were never guesses.
Fuzzy priced `UserID`→`user_id` at 0.86, indistinguishable in the trust model
from a genuine near-miss like `user_email`→`user_id` at 0.891. Matching after
normalisation makes it the exact match it always was, and takes the case out
of fuzzy's hands entirely — which shrinks the riskiest surface in the engine.

**Collision guard.** Normalisation is many-to-one. A contract declaring both
`user_id` and `userId`, or a payload carrying both `USER_ID` and `user-id`,
produces no proposal at all; fuzzy still gets its turn with the margin
machinery built for exactly that. Verified that a collision on one field does
not block an unrelated pair in the same payload.

**Guarded against regression:** `test_the_contested_case_is_still_withheld`
asserts the `user_email` case remains `AMBIGUOUS` — adding a strategy must not
open a new route to the repair the trust model exists to refuse.

---

## Phase 4b — Review follow-ups ✅ COMPLETE

A review before starting Phase 5 found 15 defects. 14 are closed; 1 is
deliberately not taken.

| | |
|---|---|
| Tests | **1,903 passing**, 0 regressions |
| Coverage | **99%** total (98.95%, up from 98.59% on `main`); `trust.py`, `normalized.py`, `fuzzy.py`, `operations.py`, `results.py` **100%** |
| ruff / format / mypy --strict | clean |
| Benchmarks / isolation | 10/10, 7/7 |

### The one that mattered

**`score_assignments` measured each pairing's margin against only the
*unassigned* endpoints.** The last assignment in a multi-rename payload
therefore faced no remaining rivals *by construction*, scored `margin = 1.0`,
and collected full trust however contested it had actually been.

```python
guard.repair(SCHEMA, {"user_email": "a@b.com", "user_names": "arnav"})
# user_names -> user_name is taken first, consuming user_name;
# user_email -> user_id then applied at 0.891 -> SUCCESS
# {'user_name': 'arnav', 'user_id': 'a@b.com'}     <- the email address
```

That is success criterion #2 failing, reachable from any payload carrying one
extra rename. `_hold_tainted` could not catch it because the operation was
never withheld in the first place. Margins now range over the full problem,
and a pairing that is not the best use of its own endpoints floors at 0.0.
`temp_celsius` → `temperature` (0.809, margin 1.0) is unaffected; the
calibration table in `trust.py` is unchanged.

### Everything else

| # | Defect | Fix |
|---|---|---|
| 1 | Margins measured against unassigned endpoints only | Full-problem margins, floored at 0.0 |
| 2 | `AmbiguousRepair.reason` said "trust 0.89 is below the threshold of 0.75" for ops held by `_hold_tainted` | `_ambiguity_reason` distinguishes the two ways to abstain; merged entries describe the top-ranked candidate |
| 3 | JSON parse asserted `value_preserved = 1.0`; `'{"a":1,"a":2}'` silently became `{"a": 2}` | Duplicate keys refused via `json_loads_strict` (root path too); fidelity measured by re-serialisation |
| 4 | `explain()` read raw bands, contradicting its own decision under `minimum_trust` | Uses `band_for` |
| 5 | `propose` scored `violation.expected_type`, applier cast `field_spec.field_type` — array-item mismatch produced a trust-1.0 op that could never apply | Both read the declared type |
| 6 | `repair_rate` counted a `success`→`partial` degradation as repaired | Numerator is `o.passed` |
| 7 | `proposed_operations` spanned several strategies under one `strategy_name` | `RepairAttempt.considered_strategies`; docstrings corrected |
| 8 | `withheld_sources` updated only after the selection loop | Tainted as each strategy is consulted |
| 9 | Same JSON string parsed at propose *and* apply | **Not taken** — see below |
| 10 | `_score_candidates` / `_check_collision` / `_combined_score` dead | Deleted with the Levenshtein scorer, prefix boost, and 218 lines of tests |
| 11 | Class docstring documented the two ignored constructor params as functional | Rewritten with the `TrustPolicy` equivalents |
| 12 | New guards untested (`RepairEvidence` range check, `bands=`, `margin_full_credit=0`, signals rendering, untyped-array parse) | Covered |
| 13 | Source cited `CORE_HARDENING_PLAN.md`, which `.gitignore` excluded | Un-ignored — **needs `git add`** |
| 14 | `with_trust` docstring reference (deleted in 3b); `AmbiguousRepair` missing from `__all__` | Both fixed |
| 15 | Benchmark runner still read `op.confidence`; two names for one number in one results file | `op.trust`, `average_trust` throughout |

### #9 — deliberately not taken

Removing the second parse means carrying the coerced value on the
`FieldOperation`. `FieldOperation` is frozen *and hashed* (there are tests
placing operations in dicts), and a parsed object or array is unhashable — so
this would widen an existing wart on a public dataclass to buy work that is
bounded by `max_attempts` anyway. It is the right change once `value` stops
being part of the hash; it is not worth doing on the way into Phase 5.

---

### Original plan

**Correction worth knowing before you scope this:** case-insensitive matching
*partly exists already*. `_normalized_score` lowercases both sides
(`fuzzy.py:119`), so `userId` vs `userid` already scores 1.0 through the fuzzy
path.

What's missing is a **high-trust exact-after-normalization match that doesn't
go through fuzzy at all.** Today `UserID` → `user_id` scores 0.857 via
Levenshtein and is priced as a guess, when it is nearly a certainty.

### 4.1 `NormalizedNameStrategy`, priority 15

Between `ExactAlias` (10) and `Fuzzy` (20).

Normalisation — a **closed, ordered** list: `casefold` → `strip` → remove
`_`, `-`, and spaces.

```
"UserID"  -> "userid"
"user_id" -> "userid"      exact match
"USER-ID" -> "userid"
```

- Risk: `INFERRED`, but `name_match = 1.0` and `margin = 1.0` → trust ≈ 0.97,
  clears the `INFERRED` bar comfortably.
- **Collision guard is mandatory:** if two contract fields normalise to the
  same key, refuse both and record `AMBIGUOUS`. Same for two payload keys.
- Do **not** extend to stemming, synonyms, or abbreviation expansion. Those
  are fuzzy's job or Stage 2's.

**Secondary benefit:** every rename this catches is one fuzzy no longer has to
price, which shrinks the riskiest surface in the engine.

---

## Phase 5 — Enum repair ✅ COMPLETE

**Status:** implemented and verified on Python 3.13.14 / pydantic 2.13.4.

| | |
|---|---|
| Tests | **2,003 passing** (+100), 0 regressions |
| Coverage | **99%** total; `enum_normalize.py` **100%**, `type_mapper.py` **100%**, `extractor.py` **100%** |
| ruff / format / mypy --strict | clean |
| Benchmarks | 11/11, repair rate 100% (9/9), precision 100% |
| Isolation | 7/7 |

**Measured before → after** — `status` declared `open | in_progress | done`:

| Payload | Before | After | trust |
|---|---|---|---|
| `"DONE"` | `FAILED` | `SUCCESS` → `"done"` | 1.00 |
| `"  done  "` | `FAILED` | `SUCCESS` → `"done"` | 0.95 |
| `"IN PROGRESS"` | `FAILED` | `SUCCESS` → `"in_progress"` | 0.85 |
| `"in-progress"` | `FAILED` | `SUCCESS` → `"in_progress"` | 0.85 |
| `"in progres"` (near miss) | `FAILED` | `FAILED` — deliberate | — |
| `"cancelled"` | `FAILED` | `FAILED` | — |
| `Level(IntEnum)` given `5` | `FAILED` | `FAILED` | — |

The 5.1 claim was re-probed rather than trusted: all three Enum flavours
(`Enum`, `IntEnum`, `class X(str, Enum)`) did map to `FieldType.ANY` with no
constraint, while `Literal` of the same values mapped correctly.

**Files changed**

| File | Change |
|---|---|
| `adapters/pydantic/type_mapper.py` | `get_literal_values` returns Enum member *values*; `_is_closed_enum` |
| `adapters/pydantic/extractor.py` | Docstring — the ENUM_VALUES path now serves Enum too |
| `core/strategies/enum_normalize.py` | **new** — `EnumNormalizationStrategy`, `normalize_enum_value` |
| `core/errors/operations.py` | `FIDELITY_EXACT` / `_WHITESPACE` / `_NORMALISED` promoted to the shared `value_preserved` scale |
| `core/models/contract.py` | `find_field_spec` — one implementation, was five |
| `core/paths.py` | **new** — `NOT_FOUND` + `get_nested_value`, one sentinel |
| `guard.py`, `strategies/__init__.py` | Register at priority 35 |
| `benchmarks/cases/11_enum_value_normalization.json` | **new** |

### 5.1 — one change, not two

`map_annotation` already consults `get_literal_values` *before* the primitive
table, so teaching that one function about Enum gave both the `FieldType` and
the `ENUM_VALUES` constraint. A `str`-valued Enum and the `Literal` of the
same strings are now indistinguishable to the engine, which is the point.

Two exclusions were added that the plan did not call for, both correctness:

- **`Flag` / `IntFlag`** — members are designed to combine (`READ | WRITE`),
  so a combined value is legitimately valid while being no single member. An
  `ENUM_VALUES` constraint built from the members would reject correct data.
  They keep `FieldType.ANY` and defer to Pydantic.
- **Empty Enum** — declares no restriction, so it must not produce an empty
  set that nothing can satisfy.

Member aliases (two names, one value) collapse to a single allowed value.

### 5.2 — what the collision guard had to become

The plan said "if two enum members normalise to the same key, abstain."
`NormalizedNameStrategy` implements its collision guard by proposing nothing,
because `FuzzyFieldMatchStrategy` still gets a turn. **Nothing runs after this
strategy**, so proposing nothing would make the repair vanish silently.
Every colliding member is proposed instead, at `margin = 0.0`, and the engine
merges them into one `AmbiguousRepair` for the caller to choose from.

The first cut of that scored each candidate on its own fidelity, and it was
wrong in a way worth recording. Against members `{"in progress",
"in_progress"}` a received `"IN PROGRESS"` differs from the first by case
alone (1.0) and from the second by a separator (0.85). Times the tie factor
those land at 0.70 and 0.595 — straddling `INFERRED.reject_below`. One
candidate was surfaced on `RepairResult.ambiguous` and the other quietly
rejected, so the caller saw a **one-item list for a two-option decision**,
which reads as unambiguous. Tied candidates now share one score (the best
fidelity available) and always land in the same band together.

### Phase 5 review — 4 defects closed

| Defect | Fix |
|---|---|
| `enum_values` declared as a bare `str` was split into characters, so `"O"` was silently rewritten to `"o"` and reported SUCCESS | Only a real collection is read as an enum set |
| `NOT_FOUND.__bool__` returned `False`, aliasing the sentinel to `0`/`""`/`[]`/`None` — the conflation it exists to prevent | Truthiness raises; `is NOT_FOUND` is the only test |
| Competing `SET_VALUE` candidates rendered identically in CLI *and* JSON, so the ambiguous block could not be acted on | Candidates name their discriminator: source key for a rename, value for a set |
| `_is_closed_enum` could reach `issubclass` with a parameterised generic | `get_origin` guard — safe on 3.11 (the low end of `requires-python`), which cannot be tested locally |

### Open calibration item for Phase 7

A collision where *every* candidate needs separator rewriting scores
0.85 × 0.70 = **0.595**, just under `INFERRED.reject_below` of 0.60, so both
candidates are rejected rather than surfaced. The behaviour is at least
consistent, and the operations are in `rejected_operations` with a full
`explain()` — but the caller does not get the choice.

**Deliberately not fixed by moving the band.** The bands were fitted to the
rename corpus; nudging one so a strategy written afterwards clears it is
exactly the "tune the evidence to the threshold" failure the trust model
exists to prevent. It belongs in §7's calibration pass, with the labelled
corpus behind it.

**Second calibration item: the `LOSSY` tier is inert.** Measured across every
evidence source in the engine, both `LOSSY` producers — wrap-in-list and
JSON-serialise — report `value_preserved = 1.0` with no margin, so both score
exactly **1.000** and always apply. The tier's 0.95 bar has never rejected or
abstained anything, and `"x" -> ["x"]` (which *invents* cardinality) scores
identically to `"5" -> 5` (which invents nothing). The `structure_preserved`
signal that would separate them is excluded from `applicable_scores`, so it
contributes nothing to trust. §7 should either price invention on
`value_preserved` or promote `structure_preserved` into the score.

### Scope note — helper consolidation

Phase 5 needed a third copy of `_find_field_spec` and a third payload reader.
Both were consolidated rather than copied: `find_field_spec` went from five
implementations to one, and `NOT_FOUND` / `get_nested_value` from two
sentinels to one. The sentinel matters — two modules each defining `_NotFound`
produce values that are never `is`-equal across a module boundary, so a
helper returning one module's sentinel reads as a real value to the other.

---

### Original plan

### 5.1 Prerequisite — `enum.Enum` is currently invisible (0.5d)

**Verified:** a real Enum field falls through every branch of
`PydanticTypeMapper.map_annotation` and lands on the terminal
`return FieldType.ANY` (`type_mapper.py:201`).

```python
class Status(str, Enum): OPEN="open"; DONE="done"
class M(BaseModel): s: Status
# -> FieldType.ANY, no ENUM_VALUES constraint. StateGuard cannot see the constraint.
```

`Literal["open","done"]` works. `Status` does not. Fix in `map_annotation` +
`get_literal_values`; handle `Enum`, `IntEnum`, and `str, Enum`.

### 5.2 `EnumNormalizationStrategy`, priority 35 (1d)

Between `TypeCoercion` (30) and `DefaultFill` (40). Targets
`VALUE_CONSTRAINT_VIOLATION` on fields carrying an `ENUM_VALUES` constraint —
the first strategy to target that violation type, which is why Phase 1 must
land first.

Normalisations, closed and ordered — casefold, strip, `-`/space → `_`, and
the inverse `_` → space. Nothing fuzzier; Levenshtein over enum *values* is a
different and riskier feature.

- Risk: `INFERRED`. `value_preserved` = 1.0 when normalisation is
  information-preserving (case only), lower when it isn't.
- **Collision guard:** if two enum members normalise to the same key, abstain.
- Works for `Literal`, `enum.Enum` (after 5.1), and JSON Schema `enum`
  (which arrives via the same constraint) — so this lands ready for the MCP
  adapter.

---

## Phase 6 — Shadow Mode / Auto Mode ✅ COMPLETE

**Status:** implemented and verified on Python 3.13.14 / pydantic 2.13.4.

| | |
|---|---|
| Tests | **2,041 passing** (+38), 0 regressions |
| Coverage | **99%** total (99.05%); `config.py`, `results.py`, `repair_history.py` **100%** |
| ruff / format / mypy --strict | clean |
| Benchmarks / isolation | 11/11, 7/7 |

**Measured** — same schema, same payload `{"temp_celsius": "31.5"}`:

| | `AUTO` | `SHADOW` |
|---|---|---|
| `status` | `SUCCESS` | `SUCCESS` |
| `repaired_output` | `{"temperature": 31.5, …}` | **`None`** |
| `proposed_output` | `None` | `{"temperature": 31.5, …}` |
| applied operations | rename, coerce, default-fill | *identical* |
| trust scores | 1.00 / 1.00 / 1.00 | *identical* |
| CLI heading | `Repaired payload:` | `Proposed payload (SHADOW — not applied):` |
| exit code | 0 | 0 |

**Files changed**

| File | Change |
|---|---|
| `core/models/config.py` | `RepairMode`; `GuardConfig.mode`, default `AUTO` |
| `core/errors/results.py` | `RepairResult.proposed_output`, `.mode`, `.is_shadow` |
| `core/engine.py` | `mode` param; `_present()`; `mode` on the terminal telemetry event |
| `guard.py` | Passes the mode; rehydrates whichever field carries the payload |
| `cli.py` | `--shadow`; mode-aware heading; `mode` + `proposed_output` in JSON |
| `logging/repair_history.py` | `mode` on every record |

### The one real design decision

Everything upstream of the result is **identical** in both modes: the same
violations are detected, the same operations proposed, scored, applied to the
engine's working copy and revalidated. Shadow's plan is known to be sound for
exactly the reason auto's is — it was tried. `_present()` then chooses which
field the caller finds it on, and the two are never both populated.

That non-negotiable equivalence is what makes a shadow rollout informative;
`TestShadowIsTheSameEngine` pins status, payload, applied operations and trust
scores as equal across modes.

**`repaired_output` is `None` under `SHADOW` for every status, including
`ALREADY_VALID`.** Carving out the case where nothing needed fixing would
make the invariant conditional, and a conditional safety property is the kind
that gets forgotten. Code written against `repaired_output` gets `None` and
fails visibly rather than quietly receiving uncommitted data.

**Mode is not a confidence setting.** `TrustPolicy` is untouched and identical
in both modes — thresholds live on the policy, per the proposal.

### Why the engine knows about the mode

The plan said "not new engine work — it's a decision about what the caller
gets back", and behaviourally that holds. But the mode still has to *reach*
the engine, because operation-level telemetry and history records are
byte-identical in both modes — shadow really does apply, to its own copy. A
team watching a shadow rollout would otherwise see `OPERATION_APPLIED` events
and a full history file with no way to tell nothing was committed. `mode` now
rides on the terminal telemetry event and on every history record.

### Fixed in passing

`logging/repair_history.py` carries its own `_NotFound` / `_get_nested_value`
— a sixth copy that Phase 5's consolidation deliberately should not have
touched, because `core.errors.results` imports `logging.logger` and a runtime
`logging -> core` edge would invert that dependency. Phase 5's test-rewrite
script had nevertheless redirected its tests at the shared helper, orphaning
the copy and dropping it to 98%. Tests restored to the real target and the
reason for the duplicate written down so the next consolidation pass leaves
it alone.

---

### Original plan

Not on your list; flagging because it is **the cheapest high-value item in the
proposal.**

The engine already computes everything needed without committing to it:
`RepairAttempt` carries `data_before`, `data_after`, `applied_operations`, and
(after Phase 3) full evidence. Shadow Mode is not new engine work — it's a
decision about what the caller gets back.

```python
class RepairMode(StrEnum):
    SHADOW = "shadow"   # detect, plan, validate the plan, report. Do not apply.
    AUTO   = "auto"     # detect, plan, validate, apply, continue.
```

- `GuardConfig.mode`, default `AUTO` (preserves current behaviour exactly).
- In `SHADOW`, `repaired_output` is `None` and a new
  `RepairResult.proposed_output` holds what *would* have been produced — so a
  team can diff it in production against the real payload for a week before
  flipping the switch.
- **Thresholds and policies are config on `TrustPolicy`, not modes.** Two
  modes, as the proposal specifies.

This is the adoption path. Nobody turns on automatic production mutation of
their data on day one; Shadow Mode is what makes the first install possible.

---

## Phase 7 — Calibration + false-positive corpus ✅ COMPLETE

**Status:** implemented and verified on Python 3.13.14.

| | |
|---|---|
| Tests | **2,091 passing** (+50), 0 regressions |
| Coverage | **99%** total |
| ruff / format / mypy --strict | clean |
| Benchmarks / isolation | 11/11, 7/7 |
| Corpus | **80 labelled cases**, 75 pass, 5 documented gaps |
| Precision over applied operations | **90.9%** (40/44) |
| Expected calibration error | **0.050** |

**Reliability curve** — applied operations bucketed by trust:

| bucket | n | correct | accuracy | expected | gap |
|---|---:|---:|---:|---:|---:|
| [0.75, 0.80) | 3 | 2 | 66.7% | 77.5% | +10.8% |
| [0.80, 0.85) | 7 | 6 | 85.7% | 82.5% | −3.2% |
| [0.85, 0.90) | 6 | 5 | 83.3% | 87.5% | +4.2% |
| [0.90, 0.95) | 3 | 2 | 66.7% | 92.5% | +25.8% |
| [0.95, 1.00) | 25 | 25 | 100.0% | 97.5% | −2.5% |

An ECE of 0.050 means the score is usable as a rough probability. It is
**overconfident in the middle** (0.75–0.95) and exact at the top, which is
the shape the findings below explain — every operation above 0.95 is a
declared or measured-reversible repair, and every miss sits in the fuzzy
band.

> These numbers were initially published as ECE 0.045 with `[0.90, 0.95)` at
> 80%. A review caught that `int(trust / 0.05)` misbuckets exact multiples of
> 0.05 — `0.95 / 0.05` is `18.999999999999996` in binary float — so two
> trust-0.95 operations were filed one bucket too low. Fixed in
> `reliability_curve`, with the band-boundary values (0.60, 0.70, 0.85, 0.95)
> pinned by test. The table above is the corrected measurement.

**Files added**

| File | Purpose |
|---|---|
| `benchmarks/calibration/must_apply.json` | 35 cases a reviewer would sign off unsupervised |
| `benchmarks/calibration/must_abstain.json` | 10 cases that must surface, not apply |
| `benchmarks/calibration/must_refuse.json` | **35 cases — the false-positive block** |
| `benchmarks/calibrate.py` | Harness: verdict judging, per-operation scoring, reliability curve, ECE |
| `tests/test_calibration_harness.py` | 50 tests — the harness makes a correctness claim, so its own scoring is tested |
| `.github/workflows/ci.yml` | Corpus runs in CI |

### The corpus caught two defects

Both were fixed; both were invisible to the 11-case benchmark suite.

1. **Array wrap produced silently-wrong data.** `{"tags": "[1, 2]"}` against
   `array<string>` became `["[1, 2]"]` — a one-element list containing raw
   JSON text, which validates cleanly. This is precisely the outcome
   `coerce.py`'s own docstring says parse-before-wrap exists to prevent; the
   ordering only helped while the parse *succeeded and the elements fit*.
   When the parse succeeded but items mismatched, control fell through to
   wrap. A string that is a serialised array names its author's intent, so
   `_array_wrap_is_safe` now refuses it outright. Precision 85.7% → **90.9%**
   from this one fix.

2. **Five corpus cases were mislabelled by me**, which is the harness earning
   its keep in the other direction: `int` is already valid in a `float` field
   (no violation, nothing to coerce), and `required: false` makes an absent
   field a non-violation so no default fills it.

### The finding that matters: uncontested renames are unguarded

Four `refuse` cases still apply, and they share one mechanism.

When there is exactly **one** missing field and **one** unexpected key there
is no competitor, so `margin = 1.0`, the margin factor is 1.0, and **trust
collapses to raw name similarity**. The only thing between a payload and a
rename is `jaro_winkler >= 0.75`.

| target ← candidate | J-W | trust | verdict |
|---|---:|---:|---|
| `is_archived` ← `is_active` | 0.923 | 0.923 | **applied** — must refuse |
| `max_price` ← `min_price` | 0.867 | 0.867 | **applied** — must refuse |
| `last_name` ← `first_name` | 0.826 | 0.826 | **applied** — must refuse |
| `temperature` ← `temp_celsius` | 0.809 | 0.809 | applied — **must apply** |
| `updated_at` ← `created_at` | 0.752 | 0.752 | **applied** — must refuse |
| `output_path` ← `input_path` | 0.717 | 0.717 | abstained ✓ |
| `order_id` ← `user_id` | 0.713 | 0.713 | abstained ✓ |
| `end_date` ← `start_date` | 0.575 | 0.575 | rejected ✓ |

**The must-refuse ceiling (0.923) sits above the must-apply floor (0.809).**
No threshold on name similarity separates them — which is exactly what
`trust.py`'s own docstring says ("name similarity alone cannot separate them
at any threshold"), except the Phase 3 calibration assumed competition would
always be present to do the discriminating. For the single-field case there
is none, and the model degenerates to the thing it was built to replace.

Winkler's prefix bonus actively makes this worse: it rewards the shared
`is_a` prefix that makes `is_active`/`is_archived` look alike.

**Not fixed here, deliberately.** Raising `INFERRED.apply_at` above 0.923
would break `temp_celsius` (0.809), the headline case. Separating them needs a
*new evidence signal* — token-head comparison, or the semantic check that is
explicitly Stage 2 — not a moved threshold. Redesigning the fuzzy evidence
model is a phase, not a Phase 7 line item. The corpus now measures it, and the
four cases are marked `known_gap` so a fix would be detected immediately.

### Still open: the enum-collision floor

Carried from Phase 5 and now reproduced by the corpus. A collision where every
candidate needs separator rewriting ties at fidelity 0.85; 0.85 × the 0.70 tie
factor = **0.595**, just under `INFERRED.reject_below` of 0.60, so both
candidates are rejected rather than surfaced. The reading that resolves it:
`REJECT` should mean "this is not a repair at all", and a normalisation onto a
declared member *is* a repair whose target is merely unknown — so it should
never be rejected on margin alone. That is a change to how collisions reach
the caller, not a change to a band, and it belongs with the fuzzy-model work
above.

### Closed: the LOSSY tier is no longer inert

Phase 6 recorded that both `LOSSY` producers reported `value_preserved = 1.0`,
so the tier's 0.95 bar had never fired. Half of that is now fixed — the array
wrap refuses the JSON-array-string case rather than scoring it 1.0. The
remaining `LOSSY` path (dict → JSON string) still scores 1.0, and the corpus
contains no case where that is the wrong answer, so there is nothing to
recalibrate against yet. Recorded rather than guessed at.

---

### Original plan

**Without this, "trust score" is a claim you cannot defend.** The proposal's
own criterion — *"validated against a set of known-ambiguous vs.
known-confident repair cases"* — requires labelled data.

### 7.1 Labelled corpus (1d)
60–100 cases: `(schema, payload, expected: correct_repair | AMBIGUOUS | None)`.
Must include a **false-positive block** — near-miss field names where a repair
is *possible* and the correct answer is "refuse." That block is what catches
the `user_email` class of bug, and the current 9-case benchmark suite has zero
of them.

### 7.2 Reliability curve (0.5d)
Bucket applied repairs by trust (0.5–0.6, 0.6–0.7, …) and report the fraction
correct per bucket. A calibrated model has bucket accuracy ≈ bucket midpoint.
Publish the table. This is the artifact that turns "trust score" from a rename
into a claim.

### 7.3 Wire into the benchmark runner (0.5d)
Report **precision** alongside repair rate. A run that repairs 90% and gets 5%
wrong is worse than one that repairs 70% and gets none wrong; the current
summary cannot distinguish them.

---

## Breaking changes (this is v0.2.0)

| Change | Mitigation |
|---|---|
| `FieldOperation.confidence` → `trust` + `evidence` + `risk` | Keep `confidence` as a deprecated read-only alias for one release |
| `RepairStatus.AMBIGUOUS` added | New enum member — callers matching exhaustively will need updating. Document in CHANGELOG |
| Fuzzy renames at 0.7–0.85 now abstain instead of applying | **Intended.** This is the bug fix. Call it out prominently — some previously "successful" repairs become `AMBIGUOUS` |
| `ValidationResult.raw_input` type widens to `Any` | Was already a lie for non-dict input |
| Default `max_attempts` 3 → 6 | Safe under the Phase 1 convergence invariant |
| New CLI exit code `3` = `AMBIGUOUS` | Document alongside 0/1/2 |

Ship with a migration note. The fuzzy-threshold change is the one that will
surprise people, and it should — quietly applying 0.82-confidence guesses is
the behaviour being fixed.

---

## Success criteria

Mapped to the proposal's, made testable:

1. Trust scores validated against the Phase 7 corpus, with a published
   reliability curve — not raw lexical similarity.
2. `user_email` against `{user_id, user_name}` returns `AMBIGUOUS` with both
   candidates and their margins, not a silent 0.82 rename.
3. A 4-issue payload (rename + coerce + enum + default) fully repairs in one
   `repair()` call.
4. Every input type in Phase 2.4 returns a `RepairResult`; **zero** raise.
5. `'{"a":1}'` and `[{"a":1}]` at the root are repaired, not just rejected.
6. Enum repair passes for `Literal`, `enum.Enum`, and JSON Schema `enum`.
7. `UserID` → `user_id` repairs at trust ≥ 0.95 via the normalized matcher,
   not via fuzzy.
8. Shadow and Auto run the same engine; thresholds are `TrustPolicy` config,
   not modes.
9. Every op in `applied_operations` provably changed the data.
10. No raw field values in logs when `include_values_in_log=False`.

---

## Out of scope

- Any MCP work (`MCP_ADAPTER_PLAN.md`, gated on this)
- LLM/semantic strategies — Phase 3 builds the *slot* for them, not a body
- `REMOVE` strategy (`NEXT_STEPS.md` §3.5) — `DESTRUCTIVE` tier exists after
  Phase 3; wiring it up is a later, opt-in decision
- Paths → tuples (`NEXT_STEPS.md` §P1-2) — does not block any phase here, but
  if Architecture Spec v1 is genuinely freezing, raise it before the freeze,
  not after
- Performance work

---

## Environment — resolved

The earlier blocker (Python 3.9 only, package requires 3.11+) is gone: a
framework install of **Python 3.13.14** was already present at
`/Library/Frameworks/Python.framework/Versions/3.13`. A venv there with
`pip install -e ".[pydantic,dev]"` runs the full suite in ~3s.

All Phase 1 results above were measured in that environment, not inferred.

```bash
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m venv .venv
.venv/bin/pip install -e ".[pydantic,dev]"
.venv/bin/python -m pytest tests/
```

Worth adding `.venv/` to the repo's dev docs — `README.md`'s Development
section currently assumes `python` is already 3.11+, which it is not by
default on this machine.
