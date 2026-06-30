# StateGuard Benchmarks

A lightweight harness for evaluating StateGuard's repair engine against a
curated set of real-world-shaped schema-drift scenarios. This is **not**
a performance/speed benchmark — it's a *correctness and behavior*
benchmark: does StateGuard repair what it should, and correctly refuse to
guess when it shouldn't?

## Running

```bash
# From the repository root, with StateGuard installed (editable or otherwise):
python benchmarks/runner.py

# Verbose output (includes each case's description):
python benchmarks/runner.py --verbose

# Custom case/results directories:
python benchmarks/runner.py --cases-dir my_cases/ --results-dir my_results/

# Print the summary without writing a results file:
python benchmarks/runner.py --no-write
```

The runner exits `0` if every case's actual status matches its expected
status (and, where specified, its minimum confidence), and `1` otherwise
— making it safe to wire into CI as a regression gate.

## What gets measured

For each case, the runner records:

- **`expected_status` vs `actual_status`** — does StateGuard's
  `RepairStatus` match what the case author asserted it should be
  (`success`, `partial`, `failed`, or `already_valid`)?
- **`min_confidence`** *(optional)* — if set, every applied operation's
  confidence in that case must meet or exceed this floor, or the case is
  marked failed even if the status matched. Use this to catch silent
  confidence regressions (e.g. a fuzzy-matching tweak that still produces
  the right repair but at a meaningfully lower confidence).

Aggregated across the whole suite:

- **Total cases**
- **Passed / Failed** cases
- **Repaired cases** — count of cases whose `actual_status` is `success`
  or `partial` (i.e. StateGuard did *something* to the payload)
- **Repair rate** — `repaired_cases / total_cases`
- **Average confidence** — mean of every applied operation's confidence
  across the whole run (not just repaired cases — `already_valid` and
  `failed` cases simply contribute zero operations to this average)

## Case format

Each file in `benchmarks/cases/` is one JSON object:

```json
{
  "name": "short_unique_identifier",
  "description": "Human-readable explanation of what this case proves.",
  "expected_schema": {
    "fields": [
      {"path": "temperature", "type": "float"},
      {"path": "humidity", "type": "integer"}
    ]
  },
  "broken_payload": {
    "temp_celsius": 31.5,
    "humidity": 80
  },
  "expected_result": {
    "status": "success",
    "min_confidence": 0.7
  }
}
```

- **`expected_schema`** uses StateGuard's own simple JSON contract format
  (`DictContractAdapter` — see
  `src/stateguard/adapters/dict_adapter.py` for the full spec, including
  nested objects, constraints, and arrays). This format is deliberately
  framework-agnostic so cases don't require Pydantic to be installed.
- **`broken_payload`** is the (possibly malformed) input data.
- **`expected_result.status`** is one of `"success"`, `"partial"`,
  `"failed"`, `"already_valid"`.
- **`expected_result.min_confidence`** is optional; omit it if the case
  isn't about confidence calibration (e.g. `failed`/`already_valid`
  cases, which apply no operations at all).

## Current case set

| # | Case | Proves |
|---|------|--------|
| 01 | `exact_alias` | Declared-alias repair (`ExactAliasStrategy`) |
| 02 | `fuzzy_rename` | The canonical `temp_celsius` → `temperature` schema-drift scenario |
| 03 | `type_coercion` | Safe string→numeric casts (`TypeCoercionStrategy`) |
| 04 | `default_fill` | Missing-field-with-declared-default repair |
| 05 | `nested_2level` | Fuzzy repair through one level of nested `OBJECT` |
| 06 | `nested_3level` | Fuzzy repair through StateGuard's officially validated max nesting depth |
| 07 | `unrecoverable` | Safe refusal — no plausible candidate exists |
| 08 | `already_valid` | Zero-overhead baseline — clean input stays untouched |
| 09 | `partial_repair` | Mixed fixable + unfixable fields within one payload |

## Adding a new case

1. Create a new `benchmarks/cases/NN_short_name.json` file following the
   format above (the `NN_` numeric prefix just keeps the case list
   ordered when listed alphabetically — it has no semantic meaning).
2. Run `python benchmarks/runner.py --verbose` and confirm your case
   reports `passed: true` for the *expected* behavior — i.e. verify
   StateGuard's actual behavior matches your assertion, don't just assume
   it will.
3. If your case is meant to capture a *known limitation* (the way the
   cross-branch fuzzy-matching edge case is documented in
   `M9_AUDIT.md`), say so explicitly in `"description"` so future
   readers don't mistake it for a bug report.

## Results files

Each run writes a timestamped JSON file to `benchmarks/results/`
(`run_<ISO-8601-timestamp>.json`) containing the full summary plus a
per-case breakdown — useful for diffing behavior across StateGuard
versions. These files are not checked into version control by default
(see `.gitignore`); only `benchmarks/results/.gitkeep` is tracked, to
preserve the directory structure.
