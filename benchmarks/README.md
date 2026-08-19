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

- **Total cases**, **Passed / Failed**
- **Repair rate** — `repaired_correctly / repairable_cases`, i.e. recall.
  A case is *repairable* when its own `expected_result` says so; cases
  expecting `already_valid` have nothing to repair, and the case expecting
  `failed` exists to prove StateGuard refuses to guess. Dividing by
  `total_cases` counted both as repair failures, which made 77.8% the
  *maximum* achievable score on a suite where every case behaved correctly.
  A case counts in the numerator only when it **passed**, so a degradation
  from `success` to `partial` moves the number.
- **Precision** — of the cases StateGuard chose to repair, how many it
  should have. A case that repairs when its `expected_result` says `failed`
  is a false positive: a silently wrong repair, which is the failure mode
  that actually costs users data.
- **Average trust** — mean of every applied operation's trust score. Note
  this is a *mix* statistic, not a quality one: declared repairs score 1.00
  by construction, so the average mostly reports which strategies the suite
  happens to exercise. The calibration harness below is what says whether
  the number means anything.

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
| 10 | `json_encoded_object` | Structured fields returned as JSON text (parse, don't wrap) |
| 11 | `enum_value_normalization` | Enum members echoed back in prose casing |

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


---

# Calibration harness

`benchmarks/runner.py` answers *"does StateGuard behave correctly on these
scenarios"*. It cannot answer the question the trust score actually makes:
**when StateGuard says trust 0.85, is it right about 85% of the time?**

`benchmarks/calibrate.py` answers that one, against a labelled corpus in
`benchmarks/calibration/`.

```bash
python benchmarks/calibrate.py
python benchmarks/calibrate.py --verbose      # explain each known gap
```

## Why it is separate

The bands in `stateguard.core.trust` were *fitted* to a handful of required
outcomes. Fitting says "these five cases land on the right side of the line";
calibration says "the number means what it claims across a corpus you did not
tune it on". Only the second is a defensible claim, and it needs labelled data
the case suite does not have.

## Corpus

Three families, ~80 cases, each labelled with a verdict and a ground-truth
payload:

| File | Verdict | Means |
|---|---|---|
| `must_apply.json` | `repair` | A specific repair is correct and should be applied unsupervised |
| `must_abstain.json` | `abstain` | A repair is findable but must be surfaced, not applied |
| `must_refuse.json` | `refuse` | **The false-positive block.** A repair is *possible* and doing nothing is correct |

`expected_payload` is the ground truth for every verdict, not just `repair` —
for `refuse` it is identical to `payload`. Having one ground-truth payload per
case is what makes per-operation scoring possible.

The `refuse` block is the point. It holds field pairs that are lexically close
and semantically opposite — `created_at`/`updated_at`, `min_price`/`max_price`,
`is_active`/`is_archived` — where a rename is entirely plausible and entirely
wrong. The 11-case benchmark suite has zero of these.

## What it reports

- **Precision** over applied operations — an operation is correct when the
  *final* payload at its `target_path` matches ground truth. Judging the final
  state rather than the operation's immediate effect is deliberate: a rename
  that exposes a type mismatch leaves `"31.5"` behind for the next pass to
  coerce, and scoring it against that intermediate value would mark a correct
  field correspondence wrong.
- **A reliability curve** — applied operations bucketed by trust, with the
  accuracy of each bucket against its midpoint. A calibrated model has
  accuracy ≈ midpoint.
- **Expected calibration error** — the weighted mean gap. The single number
  that turns "trust score" from a rename into a claim.

## Known gaps

A case may carry `"known_gap": "<why this is currently accepted>"`. Those
cases do **not** fail the run — a documented gap failing is the corpus doing
its job. What *does* fail the run is an undocumented failure (a regression) or
a documented gap that has started passing (a stale marker, which would let the
next real regression hide behind it).

That split is what lets the harness be a CI gate and an honest record at the
same time. Without it the only options are deleting the cases the engine gets
wrong, or leaving the build red forever.
