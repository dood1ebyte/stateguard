# M9 Production Readiness Audit

**Date:** M9 completion
**Scope:** Full repository (`src/stateguard/`, `tests/`, `benchmarks/`, `src/stateguard/cli.py`)
**Method:** Static review of every module, `mypy --strict`, `ruff check`, targeted performance probes, and a deliberate adversarial test (`tests/core/strategies/test_fuzzy_strategy.py::TestNestedDepth3::test_depth3_known_limitation_adversarial_cross_branch_collision`).

**Headline numbers at time of writing:** 1403 tests passing, 99% statement/branch coverage, 0 known functional bugs, 4 `mypy --strict` findings (non-functional), 26 `ruff` findings (all style/modernization, 0 correctness).

---

## 1. Production Readiness Assessment

**Verdict: ready for controlled production use; not yet ready for unsupervised use on adversarial or fully-untrusted input.**

What's solid:
- Every public entry point (`ContractGuard.repair`/`validate`, the CLI, both adapters) has end-to-end test coverage including failure paths.
- The repair engine never raises on malformed input — it degrades to `RepairStatus.FAILED` with a populated `remaining_violations` list. Confirmed via `TestDeeplyNestedInvalidPaths` and the CLI's own error-handling tests.
- `RepairHistoryRecorder` and `NoopTelemetry` are both fail-safe by construction (swallow their own exceptions); `ContractGuard.repair` additionally wraps the history call in its own try/except as a second layer. This was verified, not assumed — `tests/test_guard.py::TestRepairHistoryIntegration::test_history_recording_failure_does_not_break_repair` monkeypatches `record` to always raise and confirms `repair()` still returns `SUCCESS`.
- Core/adapter isolation, the architectural property most likely to silently rot, is enforced by `tests/isolation/` and was re-verified after M9 (the new `stateguard.logging` package additions do not pull pydantic into `stateguard.core`'s import graph).

What gives me pause for **unsupervised, adversarial-input** use specifically:
- The fuzzy-matching cross-branch collision risk (§2.1) is real, not theoretical — demonstrated, not just described, by a passing test that constructs the exact scenario.
- There is no upper bound on repair-loop cost for pathological inputs (§3.1) — a payload with hundreds of misnamed fields will measurably slow down a request.
- The CLI's `--model` flag does a dynamic `importlib.import_module` + `getattr` from a string the caller controls. In a context where the *payload* is untrusted but the *schema reference* is operator-controlled (the expected CLI usage), this is fine. If someone exposes the CLI's argument parsing to untrusted input, it's a code-execution-adjacent surface (importing an arbitrary installed module's arbitrary attribute) — not in scope for V1 but worth flagging before any "stateguard-as-a-service" use case.

## 2. Architecture Risks

### 2.1 Fuzzy matching has no parent-scope awareness (carried over from M9 audit, now demonstrated)

`FuzzyFieldMatchStrategy._score_candidates` scores every missing-field/unexpected-key pair by `_combined_score` on their **full dotted paths** — there is no explicit "only consider candidates under the same parent object" rule. Safety today is *emergent*: differing branch prefixes (`address.` vs `billing.`) usually suppress cross-branch similarity enough that real-world renames aren't confused with each other.

This is not hypothetical. `test_depth3_known_limitation_adversarial_cross_branch_collision` constructs two missing fields (`branchA.code`, `branchB.code`) and two unexpected fields (`branchA.cod`, `branchB.cod`) and shows the same-branch candidate only wins by a **0.14** margin — just under the default `score_collision_margin` of 0.15. With branch names one edit-distance closer together, or threshold tuning, this tips into either:
- a blocked legitimate repair (the actual failure mode observed — collision detection fires, both renames are withheld), or
- in principle, with different margin/threshold configuration, a **wrong-branch rename** that the engine cannot distinguish from a correct one.

The current failure mode (refuse rather than guess) is the *safe* one and is by design. But it means: in adversarial multi-branch inputs, **valid nested repairs may be silently withheld** with no indication beyond "FAILED/PARTIAL" + a generic remaining-violation entry — there's no specific "this looked ambiguous" signal surfaced to the caller today.

**Recommendation (M10):** Add an opt-in parent-scope-aware matching mode to `FuzzyFieldMatchStrategy` — restrict `available` candidates to those sharing the same parent path prefix as the missing field before scoring, falling back to global matching only if no same-scope candidate exists. This is additive (new constructor parameter, default off or correctness-gated) and would not require touching `RepairEngine` or any other strategy.

### 2.2 `RepairEngine._correlate_violations` is O(missing × unexpected) and runs every iteration

Correlation builds a full cross-product of `related_ids` between every `MISSING_REQUIRED_FIELD` and `UNEXPECTED_FIELD` violation, every repair-loop iteration (up to `max_attempts` times). For most real payloads (tens of fields) this is irrelevant. For payloads with hundreds of drifted fields (see §3.1), this is one of the two compounding O(n²) costs.

**Recommendation (M10):** Either (a) cache the correlation result when the violation set hasn't changed since the last iteration, or (b) make correlation lazy — only compute `related_ids` for violations a strategy actually inspects, rather than eagerly for all of them up front. Low priority unless real users report large-payload latency.

### 2.3 No JSON Schema adapter; `DictContractAdapter`'s format is StateGuard-proprietary

`DictContractAdapter` (introduced this milestone for the CLI's `--schema` flag and the benchmark harness) is explicitly **not** real JSON Schema, and says so in its own docstring. This was a deliberate, scoped decision — implementing real JSON Schema (with `$ref`, `oneOf`/`anyOf`, `additionalProperties`, format validators, etc.) is a meaningfully larger surface than this milestone's mandate. But it means anyone who already has JSON-Schema-described contracts (a common format for tool-calling specs, e.g. OpenAI function-calling schemas) cannot point StateGuard at them directly today — they'd need to hand-translate into StateGuard's format.

**Recommendation (M10, if there's user demand):** A real `JSONSchemaAdapter` implementing the same `IContractAdapter` interface. The extension point already exists and needs no core changes — this is purely additive, following the exact pattern `DictContractAdapter` itself just followed.

### 2.4 `ContractSpec.contract_id` is a 16-character SHA256 prefix — collision risk is non-zero but unaudited

`ContractSpec.contract_id` (used as the file-history join key and the CLI's reported identifier) is a truncated hash. For a single user's local contract set this is effectively never going to collide. No code currently *assumes* uniqueness in a way that would cause incorrect behavior on collision (it's used for grouping/display, not as a database primary key with integrity guarantees) — but this hasn't been explicitly stress-tested, and the truncation rationale isn't documented anywhere.

**Recommendation:** Low priority; document the truncation choice and birthday-bound math in `docs/architecture.md` so it's a documented decision rather than an implicit one.

## 3. Performance Risks

### 3.1 Fuzzy-match scoring is quadratic in the number of simultaneously-broken fields

Measured directly (not estimated) via `ContractGuard.with_dict_schema()` with every field independently misnamed:

| Broken fields (N) | Wall time |
|---|---|
| 10 | 0.004s |
| 50 | 0.073s |
| 100 | 0.286s |
| 200 | 1.351s |

This tracks closely with the expected O(N²) cost of `_score_candidates` (every missing field scored against every remaining unexpected key, each scoring call itself O(string length) via Levenshtein) compounded by `_correlate_violations` (§2.2) and up to `max_attempts` repair iterations. For realistic tool-output payloads (single-digit to low-double-digit field counts) this is a non-issue — sub-millisecond. For a pathological payload (a buggy upstream system renaming *every* field, or a malicious payload designed to be slow), this is a genuine, demonstrated cost.

**Recommendation (M10):**
- Document a recommended/soft field-count ceiling in the README so users with very wide schemas (hundreds of fields) know to test their own latency before relying on this in a hot path.
- Consider a configurable cap on the number of (missing × unexpected) pairs `FuzzyFieldMatchStrategy` will score per call, with documented graceful degradation (skip fuzzy matching beyond the cap, fall through to other strategies) rather than silently taking longer.

### 3.2 `RepairHistoryRecorder` opens and closes the file on every `record()` call

By design (§ no concurrent-process locking is implemented, per the class's own docstring), but worth being explicit: in a high-throughput server scenario calling `ContractGuard.repair()` many times per second with a history recorder attached, this is one `open()`/`close()` syscall pair per repair call — not amortized. For CLI and moderate-throughput library use this is irrelevant overhead. For a server hot path doing thousands of repairs/second, this would be measurable.

**Recommendation (M10, only if a server use case materializes):** Optional buffered/batched writer mode, still defaulting to the current safe-but-unbuffered behavior.

### 3.3 No benchmark coverage for engine performance itself (only correctness)

The M9 benchmark harness (`benchmarks/`) measures **correctness** (does the right repair happen, at what confidence) — it deliberately does not measure latency/throughput. The numbers in §3.1 came from an ad-hoc probe during this audit, not from a repeatable, version-tracked benchmark.

**Recommendation (M10):** A `benchmarks/perf/` companion harness that tracks wall-clock time for a fixed set of payload sizes across versions, so performance regressions (like a future change to `_combined_score` that's algorithmically more expensive) are caught the same way correctness regressions are.

## 4. API Ergonomics Issues

### 4.1 `ContractGuard.repair()`'s `repaired_output` type is status-dependent and only documented in prose

`repaired_output` is: a framework-native object (e.g. a Pydantic `BaseModel` instance) on `SUCCESS`/`ALREADY_VALID`, a plain `dict` on `PARTIAL`, and `None` on `FAILED`. This is documented in `ContractGuard.repair`'s docstring, but it's a real "read the docs or get a surprising `AttributeError`" trap — calling `.some_field` on a `PARTIAL` result's `repaired_output` works for a dict-schema (via key access syntax mismatch — `dict["x"]` not `dict.x`) and would simply not work as expected if a caller assumed Pydantic-model-style attribute access on a `PARTIAL` result. No test currently exercises "caller naively treats PARTIAL output like SUCCESS output and gets a confusing error" as a documented failure mode — it's implicitly understood from the type annotation, not actively guided.

**Recommendation (M10):** Either (a) a small `result.as_model()` / `result.as_dict()` convenience accessor pair that raises a clear, StateGuard-specific error message when called against the wrong status, or (b) at minimum, a `RepairResult.repaired_output_type` discriminator property so callers can branch without inspecting `status` themselves.

### 4.2 No single "give me a contract for this and tell me if it's fine" one-liner

Today: `guard = ContractGuard.with_pydantic(); result = guard.repair(Model, data)`. This is two lines and is fine, but there's no `stateguard.check(Model, data)` module-level convenience function for the extremely common "I don't need a persistent guard instance, I just want one answer" case. Every example in the README and this audit constructs a `ContractGuard` first.

**Recommendation (M10, low effort):** A thin `stateguard.check(schema, data, **kwargs)` module-level function that constructs an ephemeral guard and delegates. Purely additive sugar; no architecture change.

### 4.3 `DictContractAdapter`'s schema format has no programmatic validation/linting tool

If a user hand-writes a `--schema schema.json` file with a typo (e.g. `"required": "yes"` instead of `true`, or a constraint `"type"` that doesn't exist), they find out via a `ValueError` raised at `extract_contract` time — which the CLI surfaces reasonably (`stateguard error: ...` to stderr, clean exit code 2), but there's no standalone "lint my schema file before I run anything against it" command.

**Recommendation (M10, low effort):** `stateguard validate-schema --schema schema.json` — just calls `DictContractAdapter().extract_contract()` and reports success/failure, no payload required.

### 4.4 `FieldOpType.SET_VALUE` and `SET_DEFAULT` are functionally near-identical at the engine level but semantically distinct to strategy authors

Both end up calling `_set_nested` in `RepairEngine._apply_operation`. The distinction (declared-default vs. arbitrary-forced-value) matters for **why** a strategy proposes one or the other, but nothing in `FieldOperation` itself documents *when a new strategy author should pick one over the other* beyond what's implicit in reading `DefaultValueFillStrategy`'s source. This is a minor "tribal knowledge" gap for anyone implementing a custom `IRepairStrategy`.

**Recommendation:** A short "Writing a custom strategy" section in the README or `docs/architecture.md` covering this distinction explicitly. Documentation-only, no code change.

## 5. Documentation Gaps

1. **No top-level "Writing a custom IRepairStrategy / IContractAdapter" guide.** Both interfaces are richly docstringed individually (`core/interfaces/strategy.py`, `core/interfaces/adapter.py`), and `DictContractAdapter` is a good worked example, but there's no single doc page that says "here's the checklist for adding your own adapter or strategy" end to end (priority numbering conventions, the `can_handle`/`propose` split, stateless-instance requirement, etc.).
2. **`benchmarks/README.md` (new this milestone) does not yet link back from the top-level `README.md`.** Someone reading the main README has no signal that a benchmark suite exists at all.
3. **No documented guidance on choosing `min_confidence_threshold` / `score_collision_margin`.** Both are exposed as `RepairConfig` fields and as CLI flags (`--confidence-threshold`), but there's no worked guidance on what values are appropriate for which risk tolerance (e.g. "lower this for exploratory/dev use, keep the default for anything touching production data").
4. **`RepairHistoryRecorder`'s JSONL schema is documented only in its own module docstring**, not in the README or a dedicated `docs/repair-history.md` — someone wanting to build tooling on top of `~/.stateguard/repairs.jsonl` has to go read source to find the field list.
5. **The M9 nested-depth limitation is documented in README.md and this audit, but not cross-referenced from the Pydantic extractor's own docstring**, which separately documents its own (consistent, but independently-stated) nesting limitations for arrays-of-models and recursive schemas. A reader of `extractor.py` alone wouldn't be pointed at the README's "Nested structures" section.

## 6. Remaining Technical Debt

| Item | Severity | Notes |
|---|---|---|
| 4 `mypy --strict` errors | Low | None are runtime bugs (1403 tests pass, 99% coverage). `violation_mapper.py:183` — pydantic's `ErrorDetails` TypedDict vs `dict[str, Any]` parameter type mismatch (a typing-only issue, not a behavior bug). `guard.py:223` — a now-stale `# type: ignore` comment. `cli.py:56,63` — `_load_json`'s return-type inference through `json.load`. All four are mechanical fixes. |
| 26 `ruff` findings | Low | All `UP*` (pyupgrade modernization, e.g. `datetime.now(timezone.utc)` → `datetime.now(UTC)`) or `SIM*` (simplification suggestions). Zero correctness-affecting findings. Auto-fixable in large part (`ruff check --fix`). |
| Two genuinely-unreachable defensive branches in `cli.py` | Trivial | Marked `# pragma: no cover` this milestone (argparse's `required=True`/mutually-exclusive-group guarantees make them dead code by construction) rather than removed, to preserve defensive-programming intent for future CLI changes. |
| `FuzzyFieldMatchStrategy`'s cross-branch collision risk | Medium | See §2.1. Documented and demonstrated, not silently hidden — but not yet fixed. |
| No perf regression harness | Low-Medium | See §3.3. Correctness is benchmarked; speed is not, beyond this audit's one-time probe. |
| `RepairHistoryRecorder` lacks rotation/size management | Low | An append-only file with no rotation will grow unboundedly under heavy use. Not a correctness issue (JSONL tools handle large files fine) but worth a documented "you may want to rotate this yourself" note. |

## 7. Recommended M10 Priorities

In rough priority order, balancing risk against effort:

1. **Parent-scope-aware fuzzy matching (§2.1)** — the one architecture risk with a demonstrated (not hypothetical) adversarial test case. Highest-value fix given it directly addresses a documented safety gap.
2. **Mechanical cleanup: the 4 mypy errors + `ruff --fix` for the auto-fixable findings (§6).** Near-zero risk, restores a fully clean `mypy --strict` / `ruff check` baseline, cheap to do early before more code accumulates on top of the current state.
3. **`stateguard.check()` convenience function + `RepairResult` status-aware accessor (§4.1, §4.2).** Highest ergonomics-to-effort ratio; both are purely additive.
4. **Soft field-count performance ceiling + documentation (§3.1).** Don't need a full fix yet, but should set expectations before someone hits the O(N²) wall in production and is surprised.
5. **`JSONSchemaAdapter` (§2.3)** — only if real user demand materializes; otherwise lower priority than the above, since `DictContractAdapter` already unblocks the CLI/benchmark use cases this milestone needed.
6. **Documentation pass (§5)** — the "writing a custom strategy/adapter" guide and cross-linking the benchmark README are both cheap, high-leverage trust-building work for anyone evaluating StateGuard as a dependency.
7. **Perf regression harness (§3.3) + `RepairHistoryRecorder` batching/rotation (§3.2, §6)** — defer until there's a concrete server/high-throughput use case; premature otherwise.

None of these are blockers for the M9 deliverable itself. They represent the honest "here's what I'd fix next, in order" view of a codebase that is functionally solid (1403 passing tests, 99% coverage, zero known bugs) but has the kind of edges you only find by actively trying to break it — which is exactly what this audit set out to do.
