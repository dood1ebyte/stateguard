# P5: HTTP relay overhead on the code after D15 and D16, paired and uncontended (written before any row)

**Why.** P4 (code `16e26b9`) measured the HTTP relay's per-session overhead on the extra
session's final code at +0.009 to +0.058 s. D15 and D16 since then change only fault paths. P5
repeats P4 exactly, on the final code, to check the fault-free path did not get slower.

**Setup:** P4's commands, one worker, no other load.
- `driver.py notes 20 read/none,write/none raw,sg,sg_keyed_default 1`, and the same for
  tsnotes;
- `driver.py everything 20 read/none raw,sg 1`;
- TRIAL_OFFSET 18000.

**Measure:** `paired_overhead.py`, the per-trial difference against raw.

**Prediction:** P4's figures within their noise: at most 0.07 s, and at most 0.15 s everywhere.
