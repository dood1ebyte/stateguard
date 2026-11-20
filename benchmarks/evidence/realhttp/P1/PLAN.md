# P1: HTTP relay overhead, paired and uncontended (written before any row)

`driver.py`, one worker, arms interleaved trial by trial, 20 trials per cell, TRIAL_OFFSET=2000,
fault-free cells only:
- notes and tsnotes: read/none and write/none; arms raw, sg, sg_keyed_deadline;
- everything: read/none; arms raw, sg, sg_deadline.

**Measure:** the per-trial difference in session latency against raw on the same trial (client
process start included; the relay's own start is not).

**Expectation:** within 0.15 s of raw (iter9c, lost: +0.04 to +0.08 s on notes).
