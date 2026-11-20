# D1: schema drift, category A (written before any row)

**Why.** No Phase 1 batch has run the `drift` op, so the Phase 1 RFR says nothing about
category A (`PHASE1_METRICS.md`). The lost iter9c batch did (12 trials): raw 12/12 failed, and
every relay arm 0/12 (`KEY_INJECTION_AND_HTTP_RELAY.md`).

**Setup.** `driver.py`:
- notes and tsnotes, cell `drift/none`: `add_note` with its field named `Text`, not `text`; no
  transport fault;
- every default arm;
- 60 trials, TRIAL_OFFSET=3000, 4 workers;
- the commit is in `codestate*.json`.

**Scoring.** `phase1_table.py` (family `drift`). Ground truth is the notes file: a repaired
call succeeds only if the note exists once.

**Expectations:**
- raw and naive fail every row: the server rejects the call, and a blind resend repeats it;
- `sg`, `sg_keyed` and `sg_keyed_deadline` repair the field: 0 failures (upper bound 6.0% at
  n = 60);
- 0 unsafe outcomes and 0 induced failures.

This is one drift (a case-only rename); it covers category A only for that case.
