# S9: the stdio matrix's keyed arm on the final code (written before any row)

**Why.** D15, D16 and D17 changed how the stdio relay settles a keyed call after a second
fault, and D17 changed when a keyed resend meets the deadline. In the matrix each call meets
at most one fault, so its cells should not change. S9 checks that on the code they run on.
- D15: an internal error after an unknown attempt is answered "unknown".
- D16: keyed calls get 3 attempts.
- D17: a keyed resend with attempts left is tried again at its own deadline.

Slow keyed writes are added: D17 must not turn a slow call into extra resends or a failure.

**Setup**, as S8's parts, arm sg_keyed only:
- parts `sqlite`, `memory`, `filesystem`: `../realstdio/driver.py SERVER 12 "" sg_keyed`,
  TRIAL_OFFSET 22000;
- part `memory_idempotent`: S8's command (`sg_keyed_idempotent`, its 8 write cells);
- part `slow`: `write/slow` (the answer 5 s late, past the 2 s deadline) on each server,
  sg_keyed, 12 trials, TRIAL_OFFSET 23000.

**Prediction:**
- S8's cells: sg_keyed 24/132 faulted on each server, only the two crash windows, unresolved;
  memory sg_keyed_idempotent 0/84.
- Slow: 0/12 per server. The resend is answered "in progress" and the relay waits for the
  original.
- 0 unsafe outcomes.

Any other difference is a regression to explain or fix.

Synthetic fault injection on three pinned servers; not production traffic.
