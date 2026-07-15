# Changelog

All notable changes to StateGuard will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
