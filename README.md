# StateGuard

> Runtime contract reliability SDK for AI system components.

[![CI](https://github.com/dood1ebyte/stateguard/actions/workflows/ci.yml/badge.svg)](https://github.com/dood1ebyte/stateguard/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

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

## Tool-call repair

An MCP tool declares its parameters as JSON Schema, and models drift against
it constantly — a renamed parameter, a number sent as a string, an enum in
the wrong case. StateGuard repairs the arguments at the boundary; the server
is unmodified and unaware, and so is the agent.

```python
from stateguard import ContractGuard
from stateguard.adapters.mcp import outcome_for

guard = ContractGuard.with_mcp()

result = guard.repair(tool, {"loc": "Mumbai", "days": "5"})
outcome = outcome_for(result, {"loc": "Mumbai", "days": "5"})
# outcome.action    → MCPAction.FORWARD
# outcome.arguments → {"location": "Mumbai", "days": 5, "unit": "celsius"}
```

`outcome.action` is one of `FORWARD`, `HOLD` (shadow mode — forward what the
model sent, log what would have changed), `ESCALATE` (a repair was found but
not trusted enough to apply unsupervised; the candidates come with it), or
`REFUSE`. Requires no extra dependencies — a tool definition arriving over
the wire is just a `dict`.

A runnable before/after demo, over real MCP stdio, is in
[`examples/mcp/`](examples/mcp/README.md).

## Installation

**Requirements:** Python 3.11+. No runtime dependencies for the core package;
`pydantic>=2.0,<3.0` if you install the `pydantic` extra, and `mcp>=2.0` only
if you want to run the example MCP proxy.

```bash
pip install "sguard[pydantic]"
```

> The brackets must be quoted — on zsh and many Linux shells, an unquoted
> `pip install sguard[pydantic]` is interpreted as a glob pattern and
> fails with `no matches found`. The quoted form works in zsh, bash,
> PowerShell, and `cmd.exe` alike.

The core package (`pip install sguard`) has **zero runtime dependencies**.
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
Every adapter but Pydantic's is also dependency-free, which is what lets the
MCP surface ship without widening the install — a tool definition arriving
over the wire is just a `dict`.

```
User Code
    │
    ▼
ContractGuard          ← orchestrator (guard.py)
    │
    ├── IContractAdapter  ← PydanticAdapter, DictContractAdapter,
    │       │                JSONSchemaAdapter, MCPToolAdapter
    │       │
    │       └── ContractSpec  ← normalised, framework-agnostic contract
    │
    └── RepairEngine      ← core; zero external deps
            │
            ├── ContractValidator
            ├── StrategyRegistry
            └── Strategies: ExactAlias, NormalizedName, FuzzyRename,
                            TypeCoerce, EnumNormalize, DefaultFill
```

`MCPToolAdapter` delegates to `JSONSchemaAdapter`, which is where the real
work is: an MCP tool's `inputSchema` *is* JSON Schema.

## Limitations

- **Nesting depth:** repairs are officially validated up to 3 levels of
  nesting (`root.address.country.code`). Deeper structures generally work
  but aren't part of the tested/supported surface.
- **Cross-branch fuzzy matching:** `FuzzyFieldMatchStrategy` scores
  candidates by full dotted-path similarity, not parent-scope. In
  adversarial cases with similar field *and* branch names, this can block
  a valid repair (StateGuard's safe failure mode) rather than guess wrong.
- **JSON Schema is a supported subset, not the full spec.** The adapter
  implements what tool definitions actually emit and *refuses* what it
  cannot represent (`allOf`, `not`, `if`/`then`/`else`, `patternProperties`,
  …) rather than ignoring it — so a `SUCCESS` is not a claim of JSON Schema
  compliance, and the gap is bounded and visible. See
  [ADR-0001](docs/adr/0001-json-schema-source-of-truth.md). The CLI's
  `--schema` format remains a StateGuard-proprietary equivalent; pass a real
  JSON Schema through `ContractGuard.with_json_schema()`.
- **Tool-call repair covers arguments, not results.** Repairing what a
  server sends *back* (`outputSchema` / `structuredContent`) is not built.

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
