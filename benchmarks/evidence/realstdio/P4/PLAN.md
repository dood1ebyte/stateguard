# P4 (stdio): per-call overhead of a running relay on the final Phase 1 code (written before any row)

**Why.** P3 (code `0cee55c`) measured under 1 ms per call for the relay and about 6–7 ms more
for a keyed write. Since then the stdio relay has gained:
- decision 1 (the deadline is when to try again);
- D13 (conformance check);
- open lines (D9, already in P3).
P4 repeats P3 on the final code.

**Setup.** `realstdio/per_call.py`, sqlite and memory, arms raw, sg and sg_keyed (relay flags as
`driver.py`: `--call-deadline 2`, now with the default scope `recoverable`), 20 rounds × 50
calls, TRIAL_OFFSET=19000, no other load. Scored by `per_call_summary.py`.

**Expectation:** P3's figures within their intervals: sg under 1 ms per call, sg_keyed writes
+6 to +7 ms.
