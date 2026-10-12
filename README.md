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
is unmodified and unaware, and so is the agent. No extra dependencies — a
tool definition arriving over the wire is just a `dict`.

```python
from stateguard import ContractGuard
from stateguard.adapters.mcp import MCPAction, repair_tool_call

tool = {  # one entry from the server's tools/list
    "name": "get_forecast",
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 14},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"], "default": "celsius"},
        },
        "required": ["location", "days"],
    },
}

guard = ContractGuard.with_mcp()
outcome = repair_tool_call(guard, tool, {"loc": "Mumbai", "days": "5"})

outcome.action      # MCPAction.FORWARD
outcome.arguments   # {"location": "Mumbai", "days": 5, "unit": "celsius"}
if outcome.should_forward:
    ...             # send outcome.arguments to the server
```

### What the proxy should do

`outcome.action` is one of four, and `outcome.arguments` is what to send
whenever `outcome.should_forward` is true:

| Action | When | `arguments` |
|---|---|---|
| `FORWARD` | Valid as sent, or repaired with enough evidence | the (repaired) arguments |
| `HOLD` | Shadow mode only: auto mode would have done something else (repaired, escalated or refused) | the arguments **as received**; `outcome.auto_action` says what auto would do, `outcome.preview` holds its repair |
| `ESCALATE` | Auto mode only: a repair was found but not trusted enough to apply unsupervised | `None` — re-prompt with the candidates, or ask a person |
| `REFUSE` | Auto mode only: no safe repair exists (missing required value, out of range, unknown enum value, …) | `None` — the server would reject it anyway |

Shadow mode never blocks a call: its outcomes are only ever `FORWARD` or
`HOLD`. `outcome.auto_action` is what auto mode does with the same call, in
either mode, and `outcome.proposed_arguments` the payload it would send.

A tool whose schema StateGuard cannot read (`allOf`, `not`, a remote `$ref`,
…) gets no outcome at all: `repair_tool_call` raises `JSONSchemaError` (a
`ValueError`). Whether such a tool is blocked or passed through unvalidated
is your policy, decided once per tool — StateGuard will not pretend to have
checked it.

### What happened to this call?

`outcome.explain()` answers that field by field, from the evidence the
engine recorded. It contains no payload values, so it is safe to log:

```text
FORWARD: Repaired 2 field(s) before forwarding.
status: success (auto)
  repaired    location <- loc  [rename, trust 0.85, INFERRED]
              Fuzzy name match: 'loc' -> 'location'.
                - jaro-winkler similarity 0.854
  repaired    days  [coerce, trust 1.00, REVERSIBLE]
              Coerce str at 'days' to integer.
                - str -> int round-trips at 1.00
```

When the evidence is not decisive, nothing is guessed:

```text
ESCALATE: A repair was found but not trusted enough to apply unsupervised (1 ambiguous field(s): 'account_id'). Not forwarded.
status: ambiguous (auto) -- no payload produced
  held        account_id <- account  [rename, trust 0.70, INFERRED]
              trust 0.70 is below the INFERRED threshold of 0.75
              candidate rename <- account  trust 0.70, INFERRED
                - jaro-winkler similarity 0.940
                - closest alternative 'account_name' (another field also wanted this key)
  unresolved  account_name
              missing_required_field (error)
  unresolved  account
              unexpected_field (warning)
```

`outcome.to_dict()` is the same record as JSON-ready data — original input,
every decision with its trust and evidence, every attempt, and the payloads —
for an audit store. `outcome.result.field_decisions()` is the typed form.

### Shadow mode

Run with `ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW))`
(`from stateguard.core.models.config import GuardConfig, RepairMode`)
before turning repair on. Every call is forwarded exactly as the model sent
it — including calls auto mode would refuse or escalate. A call auto mode
would have handled differently comes back as `HOLD`: `outcome.auto_action`
records the decision, and `outcome.preview` holds precisely what auto mode
would send when that decision was a repair.

### Try it on your own tools

```bash
stateguard check --tool tool.json --payload arguments.json          # explanation
stateguard check --tool tool.json --payload arguments.json --json   # full record
```

`tool.json` is a tool definition from `tools/list` (or a bare
`inputSchema`); `arguments.json` is a call's `arguments`. Add `--shadow` to
see the `HOLD` view.

### What it deliberately does not repair

- **Anything it would have to invent.** A missing required parameter with no
  declared default, a value outside `minimum`/`maximum`, an enum value that
  is not a re-spelling of a member, a word where a number is declared — all
  refused.
- **A scalar where a string is declared** (`42` for `"42"`). That is a
  type disagreement, not an encoding slip, and is refused.
- **Drift on an optional parameter.** `maxLength` for an optional
  `max_length` is not renamed; the key is forwarded as sent, and nothing is
  written in its place -- the server decides what it makes of it. It is
  listed as `unresolved` in the explanation. A missed repair, never a wrong
  one.
- **A declared default for an omitted optional parameter.** A call that
  leaves out an optional parameter is valid; the server applies its own
  default, so StateGuard forwards the call as sent. Only a missing *required*
  parameter with a declared default is filled -- a recorded repair.
- **A different word, even when the names look alike.** `max_price` is not
  renamed to `min_price`, nor `updated_after` to `created_after`: the names
  share a word but swap another, which is a different field, not a typo. At
  most the call escalates with the candidate. Abbreviations (`usr_id`,
  `src_tz`) and typos (`recieve_date`) still repair.
- **Inside array elements.** `files[0].path` is validated, and a violation
  there is refused, not repaired.
- **Tool results.** Only arguments are repaired, not `outputSchema` /
  `structuredContent`.

### Tracking a server's tools

In a running proxy the tool definitions are state: learned from
`tools/list`, changed by later listings. `ToolRegistry` holds them, says
what changed, and validates each call against the *current* definition:

```python
from stateguard.adapters.mcp import ToolRegistry

registry = ToolRegistry()
report = registry.sync(listing["tools"], server="github")   # atomic; returns a SyncReport
outcome = registry.process_call("search_issues", arguments, server="github")
```

```text
tools/list (github): 0 added, 0 removed, 1 updated, 1 unchanged; 1 breaking
  UPDATED   github/search_issues  BREAKING
    INCOMPATIBLE
      ! constraint_changed             limit: 'maximum' tightened: 100 -> 50
      ! field_added                    repo: new required parameter: calls that omit it are now rejected
      ~ enum_changed                   state: added ['all']
```

`COMPATIBLE` is a guarantee — every call the old schema accepted, the new
one accepts — and anything not provably safe is reported as breaking. A
tool whose new schema StateGuard cannot read becomes unusable rather than
silently keeping the old one. The same diff is available on its own as
`diff_schemas`, and from the command line as `stateguard diff`.

For a proxy that handles raw JSON-RPC, `ToolCallRequest`,
`forward_message` and `refusal_response` change only `params.arguments`
and preserve the request `id` and `_meta`. `examples/mcp/stdio_proxy.py`
puts all of it together as an SDK-free stdio relay.

**The full guide is [`docs/mcp-adapter.md`](docs/mcp-adapter.md)** —
outcome truth table, registry, schema changes, the JSON-RPC boundary, the
bounds on hostile input, and what StateGuard deliberately does not do. How
the layers fit is in [`docs/mcp-architecture.md`](docs/mcp-architecture.md).

A runnable before/after demo, over real MCP stdio, is in
[`examples/mcp/`](examples/mcp/README.md). The supported JSON Schema subset,
what refuses versus warns, and the limitations are documented in
[`docs/jsonschema-adapter.md`](docs/jsonschema-adapter.md) — verified against
21 tool schemas captured from four public MCP servers.

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
Pydantic is an optional extra — the only adapter that needs a dependency;
the JSON Schema and MCP adapters use the standard library alone.

## Command-line interface

Validate and repair a JSON payload against a contract without writing any Python:

```bash
# Against a Pydantic model
stateguard check --model mypackage.models:Weather --payload payload.json

# Against a StateGuard schema file (no pydantic required)
stateguard check --schema contract.json --payload payload.json

# Against an MCP tool definition: what a repairing proxy would do with the call
stateguard check --tool tool.json --payload arguments.json

# Machine-readable output, for piping into other tools
stateguard check --schema contract.json --payload payload.json --json

# Two versions of a tool list (or one tool, or a bare schema): what could break?
stateguard diff tools-v1.json tools-v2.json        # exit 1 if anything is breaking

# Replay recorded calls against a tool list (add --shadow for the rollout view)
stateguard replay --tools tools.json --calls calls.jsonl
```

Exit codes for `check`: `0` = success/already valid, `1` = partially
repaired, `2` = failed (or a usage error). `diff` exits `1` when a change is
breaking; `replay` exits `0`, or `2` when its input cannot be read. Run `stateguard check --help` for the full flag
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
  [ADR-0001](docs/adr/0001-json-schema-source-of-truth.md) for the decision
  and [the adapter guide](docs/jsonschema-adapter.md) for the subset itself.
  The CLI's `--schema` format remains a StateGuard-proprietary equivalent;
  pass a real JSON Schema through `ContractGuard.with_json_schema()`.
- **Tool-call repair covers arguments, not results.** Repairing what a
  server sends *back* (`outputSchema` / `structuredContent`) is not built.
- **Drift on an *optional* parameter is not repaired.** Fuzzy renaming pairs
  an unexpected key with a missing *required* field, and an optional field is
  never missing — so a one-character typo on an optional parameter goes
  uncorrected. It is a missed repair, never a wrong one; nothing is written
  into a declared field. 29% of the parameters in the real-schema corpus are
  optional.

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
