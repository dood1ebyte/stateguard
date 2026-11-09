# P3 (stdio): per-call overhead of a running relay (written before any row)

**Why.** P1's stdio overhead (+0.20 to +0.37 s per session) includes starting the relay
process, which a host pays once per server process, not per call. This batch separates the
per-call cost.

**Setup.** `realstdio/per_call.py`, after I1 ends (no other load):
- servers sqlite and memory; arms raw, sg, sg_keyed, configured as in `driver.py`;
- 20 rounds × 50 calls, TRIAL_OFFSET=5000;
- each round runs the arms one after another, each as one SDK stdio session: initialize, list
  tools, then 50 calls alternating the read and the write. The shim passes everything.
- Writes carry a distinct marker per call. In `sg` the write is declared non-idempotent and
  unkeyed; in `sg_keyed` the injector or preload keys it.

**Measure.** Per-call latency (the client's `call_tool`, end to end) and its difference from
raw on the same (round, call index, op), with a normal 95% interval (`per_call_summary.py`).
The first read and write of each session are reported apart (warm-up).

**Expectations** (from a 1-round dry run, not evidence):
- warm calls: `sg` within about +2 ms of raw;
- `sg_keyed` writes about +5 to +10 ms (the key store's commit before and after the effect);
- 0 failed calls and 0 session errors.
