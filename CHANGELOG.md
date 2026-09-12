# Changelog

All notable changes to StateGuard will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Tool-call repair for MCP.** `ContractGuard.with_mcp()` repairs the
  arguments an agent sends to an MCP tool, against the server's declared
  `inputSchema`. Accepts a full tool definition or a bare input schema, in
  either spelling of the key (`inputSchema` on the wire, `input_schema` as
  the Python SDK names it). Extracted contracts are cached on
  `(tool name, schema content)` — not `contract_id`, which collides between
  tools with coincidentally identical signatures — so a proxy fetches
  `tools/list` once, and a server that changes a signature is picked up
  rather than papered over. `stateguard.adapters.mcp.outcome_for` maps a
  `RepairResult` onto `FORWARD` / `HOLD` / `ESCALATE` / `REFUSE`. Zero extra
  dependencies, enforced by an import-isolation test.
- **A runnable MCP demo** in `examples/mcp/` — an ordinary MCP server, a
  `RepairingClient` proxy, and a before/after script that speaks real MCP
  over stdio. Three drifted calls the server rejects now succeed; a fourth
  is refused rather than repaired, because no honest repair exists for it.
- **`ContractGuard.with_json_schema()`** and the `JSONSchemaAdapter` behind
  it: a supported subset of JSON Schema (`MCP_ADAPTER_PLAN.md` §5) with
  `$ref`/`$defs` resolution, cycle *and* depth guards, and a ReDoS screen on
  schema-supplied `pattern` values. Keywords outside the subset raise rather
  than being ignored, so a `SUCCESS` never quietly means "validated less
  than the schema asked". See `docs/adr/0001-json-schema-source-of-truth.md`.

### Fixed
- `ContractValidator` now uses `re.search` for `PATTERN` constraints, not
  `re.match`. JSON Schema defers to ECMA-262 and Pydantic's
  `Field(pattern=)` searches, so anchoring at the start produced *false*
  violations — `"abc123"` against `"[0-9]+"` was reported broken while
  Pydantic, the documented source of truth on that path, accepted it. A
  false violation can trigger a repair of a field that was never broken. An
  unusable pattern now raises an error naming the field instead of a bare
  `re` exception.
- `GuardConfig.strict_mode` composes with a contract's own `strict_mode` as
  a **floor** (strict if either says so) rather than overwriting it in both
  directions. A schema declaring `additionalProperties: false` was being
  silently relaxed by the config default of `False`, contradicting
  `ContractSpec.strict_mode`'s own documented precedence. A contract is
  never loosened by configuration.
- Seven under-validation defects in the JSON Schema adapter, found by
  review before release. Most consequential: for `anyOf: [{$ref}, {null}]`
  — the shape Pydantic emits for every `Optional[X]` — constraints,
  declared defaults, and the unsupported-keyword screen were all read off a
  bare `{"$ref": ...}` and therefore silently dropped; `allOf` behind such a
  reference produced a field that accepted anything. Also: unsupported
  keywords inside union branches and array `items` went unscreened; a deep
  schema raised `RecursionError` instead of a catchable error; an untyped
  `enum` containing `null` produced a false `NOT_NULL` violation; a
  `required` name with no `properties` entry was dropped, so a payload
  missing it was reported valid.
- `FieldType.BYTES` — declared binary fields (e.g. Pydantic `bytes`
  annotations, previously extracted as `ANY`) are now a first-class
  contract type accepting `str | bytes` values, mirroring the lax
  wire-format rule of the frameworks that declare them. The dict-schema
  adapter accepts `"type": "bytes"`.
- `dict`/`list` → string JSON-serialise coercion: `TypeCoercionStrategy`
  now repairs a `TYPE_MISMATCH` on `STRING`/`BYTES` targets by
  `json.dumps`-ing container values (confidence 0.85; refused when the
  container holds non-JSON values). Also applies to `STRING`/`BYTES`
  members of `UNION` targets. Repairs the failure mode of
  openai-python#2702, where an agent harness passes a parsed JSON object
  to a tool argument declared `str`/`bytes`.
- Python 3.14 compatibility: added `__init__.py` to all test directories
  (`tests/`, `tests/core/`, `tests/core/models/`, `tests/core/errors/`,
  `tests/integration/`, `tests/isolation/`, `tests/logging/`). The mixed
  presence of these files caused pytest collection failures under Python
  3.14's stricter import-system behavior. The fix also resolves the same
  latent inconsistency on Python 3.11 / 3.12 on macOS (zsh, case-insensitive
  APFS) and Linux. Discovered during macOS validation on Python 3.14.6.

### Added

#### M0 — Repository Bootstrap
- Project structure with `src/` layout
- `pyproject.toml` with zero runtime dependencies for core and `pydantic` as optional extra
- GitHub Actions CI pipeline: isolation → lint/typecheck/test (parallel)
- Import isolation test suite (`tests/isolation/`) — verifies core never loads pydantic
- `.python-version` pinned to 3.11
- `py.typed` marker for PEP 561 compliance

#### M1 — Domain Enums and Value Objects
- `FieldType` — abstract field type vocabulary used by the core engine
- `FieldConstraintType` — categories of field-level constraints
- `FieldConstraint` — immutable constraint descriptor (frozen dataclass)
- `RepairConfig` — repair engine configuration with `__post_init__` validation
- `GuardConfig` — top-level guard configuration composing `RepairConfig`
- `ViolationType` — categories of detectable contract violations
- `ViolationSeverity` — ERROR / WARNING severity levels
- `ContractViolation` — mutable violation descriptor with auto-generated UUID
- `FieldOpType` — atomic repair operation types
- `FieldOperation` — immutable repair operation proposed by strategies (frozen dataclass)
- Full test suite for all M1 domain objects
