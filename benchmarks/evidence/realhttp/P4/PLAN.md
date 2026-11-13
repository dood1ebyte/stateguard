# P4: HTTP relay overhead on the final Phase 1 code, paired and uncontended (written before any row)

**Why.** P1 (code `1852365`) measured the relay's session overhead at +0.006 to +0.066 s. Since
then the relay has gained:
- D9 (open lines);
- D10 and decision 2 (a 30 s default deadline, whose race path answers recoverable calls as one
  JSON message);
- D12 (resend backoff);
- D13 (conformance check).
P4 repeats P1 on the final code, with the product defaults.

**Setup.** `driver.py`, one worker, arms interleaved trial by trial, 20 trials per fault-free
cell, TRIAL_OFFSET=18000, no other load:
- notes and tsnotes, `read/none` and `write/none`: arms raw, sg (`--call-deadline 0`) and
  sg_keyed_default (keyed, the default deadline);
- everything, `read/none`: arms raw and sg.

**Measure:** `paired_overhead.py`, the per-trial difference against raw.

**Expectation:** within P1's range (≤ 0.07 s), and ≤ 0.15 s everywhere.
