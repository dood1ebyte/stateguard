# Changelog

All notable changes to StateGuard will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
