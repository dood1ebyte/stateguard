# M9.1 Release Readiness Report

**Scope:** A stabilization pass over the M9-complete codebase. No new product features, no behavior changes to the repair engine, strategies, or adapters. Every fix below is documentation, packaging, exports, or CI — verified not to alter `RepairEngine`, `IRepairStrategy`, or `IContractAdapter` behavior.

**Baseline going in:** 1403 tests passing, 99.14% coverage (per `M9_AUDIT.md`).
**State coming out:** 1411 tests passing, 99.14% coverage, 0 regressions.

---

## Files Modified

| File | Change |
|---|---|
| `README.md` | Quoted the extras install (`pip install "stateguard[pydantic]"`), added an explicit zsh-glob-expansion warning, corrected the milestone status table (M2–M9 were stale-marked "Pending"), removed the dead `docs/architecture.md` link, corrected the architecture diagram's adapter example (`JSONSchemaAdapter` → `DictContractAdapter`, which actually exists), added a CLI usage section, added a local repair-history usage section, added a Benchmarks section linking to `benchmarks/README.md` |
| `src/stateguard/__init__.py` | Exported `RepairHistoryRecorder` from the top-level package (was previously only reachable via `stateguard.logging.repair_history`) |
| `.github/workflows/ci.yml` | Added `macos-latest` alongside `ubuntu-latest` in the `test` job's matrix (now `os × python-version` = 4 combinations); added CLI and benchmark-harness smoke-test steps to that job |

## Files Created

| File | Purpose |
|---|---|
| `tests/test_public_api.py` | 8 tests locking in the top-level `stateguard` package's export contract — specifically guards against the "fully implemented but not exported" class of bug that `RepairHistoryRecorder` had until this pass |
| `M9_1_RELEASE_REPORT.md` | This report |

No source files in `src/stateguard/core/`, `src/stateguard/adapters/`, or `src/stateguard/cli.py` were modified in this pass beyond the single `__init__.py` export addition above. Repair behavior, confidence scoring, and the CLI's argument/output contract are byte-for-byte unchanged from M9.

---

## Task 1 — macOS Installation Fix

**Fixed.** The single unquoted `pip install stateguard[pydantic]` in `README.md` (the only such occurrence found anywhere in the repo — `CHANGELOG.md`, `benchmarks/README.md`, and the CI workflow already used correct quoting) is now `pip install "stateguard[pydantic]"`, with an explicit callout explaining *why* the quoting matters (zsh glob expansion) and confirming the quoted form is correct across zsh, bash, PowerShell, and `cmd.exe`.

## Task 2 — Cross-Platform CI

**Fixed.** The `test` job's matrix now runs on both `ubuntu-latest` and `macos-latest` (crossed with Python 3.11/3.12, for 4 total runs per push/PR), and each run now also exercises the CLI (`stateguard check`) and the benchmark harness (`python benchmarks/runner.py --no-write`) as smoke-test steps — so the next push to this repository will be the **first time** StateGuard's test suite, CLI, and benchmarks have ever actually executed on macOS, automatically and on every change going forward. Windows was intentionally left out per the explicit instruction not to add it unless trivial — there's no blocking reason it couldn't be added later (see `M9_AUDIT.md` §2/§3 for the prior compatibility audit), but it wasn't free, so it's out of scope for this pass.

## Task 3 — Release Readiness Review

| Item checked | Finding | Action |
|---|---|---|
| Broken README links | `docs/architecture.md` was linked but the file doesn't exist (`docs/` is empty) | Removed the dead link; the inline ASCII diagram immediately above it already conveys the same architecture summary |
| Stale documentation | Milestone status table showed M2–M8 as "🔲 Pending" despite being complete, and M9 wasn't listed at all | Updated to reflect actual M0–M9 completion; added a pointer to `M9_AUDIT.md` |
| Missing exports | `RepairHistoryRecorder` (a complete, tested M9 feature) was not importable from `stateguard` directly — only via `stateguard.logging.repair_history` | Exported from `stateguard/__init__.py`; added regression test (`tests/test_public_api.py`) |
| Missing public API documentation | The README never mentioned the CLI (`stateguard check`) or `RepairHistoryRecorder` at all, despite both being complete, tested M9 features | Added a CLI usage section and a repair-history usage section to the README |
| Packaging issues | Built a real wheel via `python -m build --wheel` and inspected its manifest | **None found.** All real source files present, `py.typed` marker included, no test files leaked in, `entry_points.txt` correctly declares the `stateguard` console script, `METADATA` correctly lists all three extras (`pydantic`, `dev`, `all`) and embeds the (now-corrected) README as the long description |
| Install issues | See Task 4 below | Verified clean on both editable and wheel install paths |
| Benchmark documentation issues | `benchmarks/README.md` itself had no broken links or install commands needing fixes; the gap was the *main* README never linking to it | Closed via the new Benchmarks section in `README.md` |

Two items were explicitly identified and **intentionally left alone** as outside the "under 30 minutes / no behavior change" bar:
- The 4 `mypy --strict` findings and 26 `ruff` findings catalogued in `M9_AUDIT.md` §6 are unchanged (re-verified at identical counts in this pass) — all are pre-existing, non-functional (typing/style only), and fixing them was explicitly out of scope for M9.1 per the "no feature work" instruction; they remain a clean, itemized M10 task.
- `CHANGELOG.md` still only documents M0–M1 in detail. Bringing it fully up to the M0/M1 level of per-file detail for M2–M9 would exceed the 30-minute bar for a single item; left as a known gap rather than risking a rushed, inaccurate changelog entry.

## Task 4 — Version Readiness

All four checks performed against **genuinely isolated environments** — fresh `python -m venv`, verified empty (`pip list` showed only `pip` itself) before each install, binaries invoked by full path rather than `source`/`activate` (which aren't available in this tool's non-interactive shell) to guarantee no contamination from the pre-existing development install.

### Wheel install (non-editable, real end-user path)
```
python -m build --wheel  →  stateguard-0.1.0-py3-none-any.whl
pip install "stateguard-0.1.0-py3-none-any.whl[pydantic]"   (into a fresh, verified-empty venv)
```
| Check | Result |
|---|---|
| `import stateguard` | ✅ `version: 0.1.0` |
| `from stateguard import ContractGuard` | ✅ |
| CLI entrypoint (`stateguard --version`, `stateguard check ...`) | ✅ both schema-mode and model-mode `check` runs succeeded, correct exit codes |
| Repair history (`RepairHistoryRecorder`) | ✅ constructs and reports `enabled`/`path` correctly |
| Dict-schema mode (`ContractGuard.with_dict_schema()`) | ✅ `ALREADY_VALID` on clean input |
| Pydantic mode (`ContractGuard.with_pydantic()`) | ✅ `SUCCESS`, correct fuzzy-repaired output on the canonical `temp_celsius` scenario |
| Benchmark runner (via the venv's interpreter against the source tree — `benchmarks/` is intentionally not packaged into the wheel) | ✅ 9/9 cases pass |

### Editable install (development path)
```
pip install -e ".[pydantic,dev]"   (into a separate fresh, verified-empty venv)
```
| Check | Result |
|---|---|
| Install completes | ✅ |
| Full test suite from this venv | ✅ **1411 passed** |
| Coverage from this venv | ✅ **99.14%** |
| Isolation tests from this venv | ✅ 7/7 |
| Console script (`stateguard --version`, `stateguard check --json`) | ✅ |
| `mypy --strict` / `ruff check` finding counts | ✅ identical to the M9 baseline (4 / 26) — confirms M9.1 introduced no new type or lint debt |

**Conclusion: both install paths work correctly, verified from genuinely clean environments, not just by re-running the pre-existing development install.**

## Task 5 — Final Verification

| Check | Result |
|---|---|
| Full test suite | **1411 passed**, 0 failed |
| Coverage | **99.14%** (1587 statements / 516 branches; 11 misses, all pre-existing defensive/unreachable lines already itemized in `M9_AUDIT.md`) |
| Isolation tests | **7/7 passed** — `stateguard.core` still loads zero external packages; the new top-level `RepairHistoryRecorder` export does not import pydantic anywhere in its chain |
| CLI smoke test | ✅ `stateguard check --schema ... --payload ...` → correct `ALREADY_VALID`/`SUCCESS` output, exit code `0` |
| Benchmark harness | ✅ 9/9 cases pass, 77.8% repair rate, 0.928 average confidence — identical to the M9 baseline |

---

## Remaining Known Issues

Carried forward, unchanged, from `M9_AUDIT.md` (none of these were in scope for M9.1; none block release per the reasoning in that document):

1. **Fuzzy-matching cross-branch collision risk** (Medium, architectural) — demonstrated but not exploited by any shipped test; current behavior fails safe (blocks ambiguous repairs rather than guessing wrong).
2. **O(N²) fuzzy-matching cost** for payloads with hundreds of simultaneously-broken fields (Low-Medium, performance) — irrelevant for realistic tool-output payload sizes; documented, not yet bounded.
3. **4 `mypy --strict` findings / 26 `ruff` findings** (Low, mechanical) — none affect runtime behavior; itemized fix list already exists in `M9_AUDIT.md` §6.
4. **No `JSONSchemaAdapter`** (Low, scope) — `DictContractAdapter`'s proprietary format covers the CLI/benchmark use cases this milestone needed; real JSON Schema support remains a credible, additive M10 candidate.
5. **`CHANGELOG.md` only details M0–M1** — accurate as far as it goes, just incomplete; not misleading, since the README's status table (now corrected) is the canonical "what's done" reference.
6. **Windows is unverified** — no known blocker per the prior compatibility audit (zero `win32`/`pywin32`/path-separator issues found), but genuinely untested by CI or by hand, and intentionally out of scope for this pass.

None of these are new. M9.1 did not surface any previously-unknown defect during this verification pass.

---

## Release Recommendation

### Can StateGuard v0.1.0 be publicly released today?

# YES

**Justification:**

- **Functional correctness is thoroughly verified, not assumed.** 1411 tests passing at 99.14% coverage, across the full milestone history (M0–M9) plus this stabilization pass, including end-to-end integration tests, nested-repair hardening tests, and an adversarial test that *deliberately tries to break* the one known architectural soft spot (cross-branch fuzzy matching) and confirms it fails safely rather than silently.
- **The actual release artifact has been tested, not just the source tree.** A real wheel was built via `python -m build`, its manifest was inspected file-by-file, and it was installed into a genuinely clean, freshly-created virtual environment with no prior state — where every primary user-facing surface (`import stateguard`, `ContractGuard`, both adapter modes, the CLI, repair history, and the benchmark harness) was independently exercised and passed. The editable/development install path was verified the same way, in a separate clean environment.
- **Every issue raised in `M9_AUDIT.md` that was both real and fixable within this pass's constraints has been fixed**, and verified fixed (re-running mypy/ruff after the fix confirms no new debt, the new export has a regression test, the new CI matrix has been syntax-validated and its exact steps manually verified to succeed). The macOS install footgun — a real, first-five-minutes failure for anyone on the default macOS shell — is closed. CI will now catch macOS regressions automatically starting with the very next push, closing the gap between "designed to be cross-platform" and "verified to be cross-platform" that the prior audit flagged as the single biggest reason for hesitation.
- **What's left unfixed is honestly disclosed, not hidden, and is genuinely non-blocking for a v0.1.0.** The remaining items (mechanical type-hygiene cleanup, an architectural edge case that already fails safely, an unbounded-but-realistic-case-irrelevant performance ceiling, Windows being untested) are the kind of well-understood, well-documented trade-offs that are completely normal and expected for a `0.1.0` release — they're exactly the sort of thing a `0.1.0` version number exists to signal, and they're already written down candidly in `M9_AUDIT.md` for any adopter to evaluate before depending on this in their own production system.

A `0.1.0` release does not need to be "finished" — it needs to be **honest, working, and safe to fail when it doesn't know the answer**. StateGuard, as of this pass, is all three.
