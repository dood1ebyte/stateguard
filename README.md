# StateGuard

> Runtime contract reliability SDK for AI system components.

StateGuard automatically detects and repairs runtime contract failures between AI system
components — preventing schema drift, field renames, and type mismatches from crashing
your AI workflows.

## Status

✅ **V1 Complete (M0–M9)**

| Milestone | Status |
|-----------|--------|
| M0 — Repository Bootstrap | ✅ Complete |
| M1 — Domain Models | ✅ Complete |
| M2 — Contract + Result Models | ✅ Complete |
| M3 — Interfaces + Test Infrastructure | ✅ Complete |
| M4 — Core Validator | ✅ Complete |
| M5 — Repair Strategies | ✅ Complete |
| M6 — Repair Engine | ✅ Complete |
| M7 — Pydantic Adapter | ✅ Complete |
| M8 — Public API | ✅ Complete |
| M9 — Production Hardening, CLI, Benchmarks | ✅ Complete |

See [`M9_AUDIT.md`](M9_AUDIT.md) for the full production-readiness audit,
including known limitations and recommended next steps.

## Quick Start

```bash
pip install "stateguard[pydantic]"
```

> **Note on quoting:** the brackets around `pydantic` must be quoted. On
> macOS's default shell (zsh) and many Linux shells, an unquoted
> `pip install stateguard[pydantic]` is interpreted as a glob pattern and
> fails with `no matches found: stateguard[pydantic]`. The quoted form
> above works correctly in zsh, bash, PowerShell, and `cmd.exe` alike.

```python
from stateguard import ContractGuard
from pydantic import BaseModel

class Weather(BaseModel):
    temperature: float
    humidity: int

guard = ContractGuard.with_pydantic()

# Tool returned the wrong field name — StateGuard repairs it automatically.
result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})
# result.status        → RepairStatus.SUCCESS
# result.repaired_output → {"temperature": 31.5, "humidity": 80}
```

### Command-line interface

StateGuard also ships a `stateguard check` CLI for validating and repairing
a JSON payload against a contract without writing any Python:

```bash
# Against a Pydantic model
stateguard check --model mypackage.models:Weather --payload payload.json

# Against a plain JSON schema (no pydantic required)
stateguard check --schema contract.json --payload payload.json

# Machine-readable output, for piping into other tools
stateguard check --schema contract.json --payload payload.json --json
```

Exit codes: `0` = success/already valid, `1` = partially repaired, `2` =
failed (or a usage error). Run `stateguard check --help` for the full flag
reference, including `--strict`, `--max-attempts`, and
`--confidence-threshold`.

### Local repair history (optional)

To keep a local, append-only audit trail of every repair StateGuard
performs, pass a `RepairHistoryRecorder` when constructing your guard:

```python
from stateguard import ContractGuard
from stateguard.logging import RepairHistoryRecorder

guard = ContractGuard.with_pydantic(history=RepairHistoryRecorder())
# Appends one JSON line per repair to ~/.stateguard/repairs.jsonl by default.
# Fully optional, fully local — no network calls, no external services.
```

## Architecture

StateGuard is built on a **framework-agnostic core engine** with zero external runtime
dependencies. Pydantic is the first supported adapter; future adapters (LangChain,
LangGraph, JSON Schema) can be added without modifying the engine.

```
User Code
    │
    ▼
ContractGuard          ← orchestrator (guard.py)
    │
    ├── IContractAdapter  ← PydanticAdapter, DictContractAdapter, ...
    │       │
    │       └── ContractSpec  ← normalised, framework-agnostic contract
    │
    └── RepairEngine      ← core; zero external deps
            │
            ├── ContractValidator
            ├── StrategyRegistry
            └── Strategies: ExactAlias, FuzzyRename, TypeCoerce, DefaultFill
```

## Nested structures

StateGuard repairs nested objects in addition to flat fields. **V1 is
officially validated up to 3 levels of nesting** — i.e. a path shape like
`root.address.country.code` (two nested `OBJECT` fields plus a leaf field).
This depth is covered end-to-end by the test suite across every layer:
`ContractValidator`, the Pydantic extractor, every repair strategy
(`ExactAliasStrategy`, `FuzzyFieldMatchStrategy`, `TypeCoercionStrategy`,
`DefaultValueFillStrategy`), and the full `RepairEngine` repair loop,
including mixed-failure and partial-repair scenarios at that depth.

Beyond 3 levels: the underlying path-walking code (`_get_nested`,
`_set_nested`, dotted-path lookups in each strategy) has no hard depth
limit and will generally continue to work, but deeper structures are not
part of StateGuard's tested or supported surface in V1 — use at your own
risk, and please report issues if you rely on greater depth.

**Known limitation — cross-branch fuzzy matching.** `FuzzyFieldMatchStrategy`
scores candidates purely by full dotted-path string similarity; it has no
explicit awareness of "same parent object" scope. In practice this is safe
because differing branch prefixes (e.g. `address.` vs `billing.`) naturally
suppress cross-branch similarity scores. In adversarial cases where two
different branches have *both* similar field names *and* similar branch
names, this can produce either an ambiguous match (correctly blocked by
collision detection — StateGuard's safe failure mode) or, in principle, a
wrong-branch rename. See `M9_AUDIT.md` for a full writeup and the
recommended M10 fix (parent-scoped matching).

## Benchmarks

A correctness benchmark suite lives in [`benchmarks/`](benchmarks/README.md),
covering alias repair, fuzzy renames, type coercion, default-fill, nested
structures, and known-unrecoverable cases. Run it with:

```bash
python benchmarks/runner.py --verbose
```

See [`benchmarks/README.md`](benchmarks/README.md) for the full case
format and current results.

## Development

```bash
# Install with all dev dependencies
pip install -e ".[pydantic,dev]"

# Run isolation tests first (must always pass)
pytest tests/isolation/ -v

# Run full test suite
pytest tests/ --cov=stateguard

# Type check
mypy src/

# Lint
ruff check src/ tests/
```

## License

MIT
