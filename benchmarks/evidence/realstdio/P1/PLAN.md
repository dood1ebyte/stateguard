# P1 (stdio): relay overhead, paired and uncontended (written before any row)

`realstdio/driver.py`, one worker, arms interleaved trial by trial, 20 trials per cell,
TRIAL_OFFSET=2000, read/none and write/none, servers sqlite and memory, arms raw, sg, sg_keyed.

**Measure:** the per-trial difference in session latency against raw. On stdio this includes
starting the relay process, which a host pays once per server process; the per-call cost is
not separated here.

**Expectation:** about +0.2 s for sg and +0.3 to +0.4 s for sg_keyed (S2 and S4 means, contended).
