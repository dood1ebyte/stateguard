# P5 (stdio): per-call overhead of a running relay after D15–D17 (written before any row)

**Why.** P4 (stdio) measured under 1 ms per call for the relay, and about 4.5–5 ms more for a
keyed write. D15, D16 and D17 since then change fault paths, plus one check per deadline tick
for each keyed resend. P5 repeats P4 exactly, on the final code.

**Setup:** P4's command, `realstdio/per_call.py SERVER 20 50` for sqlite and memory,
TRIAL_OFFSET 19000, no other load. Scored by `per_call_summary.py`.

**Prediction:** P4's figures within their intervals.
