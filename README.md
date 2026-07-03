# StateGuard

> Runtime contract reliability SDK for AI system components.

[![CI](https://github.com/dood1ebyte/stateguard/actions/workflows/ci.yml/badge.svg)](https://github.com/dood1ebyte/stateguard/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-1411%20passing-brightgreen.svg)](tests/)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen.svg)](M9_AUDIT.md)

StateGuard automatically detects and repairs runtime contract failures between AI system
components — preventing schema drift, field renames, and type mismatches from crashing
your AI workflows.

## Why StateGuard?

LLM tool calls and agent pipelines are wired together by convention, not by a compiler.
A model returns `temp_celsius` when your schema expects `temperature`, or `"31.5"` where
you need a `float` — and without a repair layer, that's an unhandled exception in
production. StateGuard sits between the LLM's output and your typed schema, detects the
drift, and repairs it automatically wherever it can safely infer the fix — falling back
to a clear, structured failure when it can't.

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

## Installation

**Requirements:** Python 3.11+. No runtime dependencies for the core package;
`pydantic>=2.0,<3.0` if you install the `pydantic` extra.

```bash
pip install "stateguard[pydantic]"
```

> The brackets must be quoted — on zsh and many Linux shells, an unquoted
> `pip install stateguard[pydantic]` is interpreted as a glob pattern and
> fails with `no matches found`. The quoted form works in zsh, bash,
> PowerShell, and `cmd.exe` alike.

The core package (`pip install stateguard`) has **zero runtime dependencies**.
Pydantic is an optional extra — the only adapter currently shipped.

## Command-line interface

Validate and repair a JSON payload against a contract without writing any Python:

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

## Repair history (optional)

Keep a local, append-only audit trail of every repair StateGuard performs:

```python
from stateguard import ContractGuard
from stateguard.logging import RepairHistoryRecorder

guard = ContractGuard.with_pydantic(history=RepairHistoryRecorder())
# Appends one JSON line per repair to ~/.stateguard/repairs.jsonl by default.
# Fully optional, fully local — no network calls, no external services.
```

## Architecture

A framework-agnostic core engine with zero external runtime dependencies.
Pydantic is the first supported adapter; future adapters (LangChain,
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

## Limitations

- **Nesting depth:** repairs are officially validated up to 3 levels of
  nesting (`root.address.country.code`). Deeper structures generally work
  but aren't part of the tested/supported surface.
- **Cross-branch fuzzy matching:** `FuzzyFieldMatchStrategy` scores
  candidates by full dotted-path similarity, not parent-scope. In
  adversarial cases with similar field *and* branch names, this can block
  a valid repair (StateGuard's safe failure mode) rather than guess wrong.
- **No JSON Schema adapter yet** — the CLI's `--schema` format is a
  StateGuard-proprietary equivalent, not real JSON Schema.

See [`M9_AUDIT.md`](M9_AUDIT.md) for the full production-readiness audit,
performance characteristics, and recommended next steps.

## Benchmarks

A correctness benchmark suite in [`benchmarks/`](benchmarks/README.md) covers
alias repair, fuzzy renames, type coercion, default-fill, nested structures,
and known-unrecoverable cases:

```bash
python benchmarks/runner.py --verbose
```

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

See [`CHANGELOG.md`](CHANGELOG.md) for release history.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
