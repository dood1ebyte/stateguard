# B6: the HTTP matrix's keyed arms on the final code (written before any row)

**Why.** D15 and D16 changed how the HTTP relay settles a keyed call after a second fault. In
the matrix each call meets at most one fault, so its cells should not change. B6 checks that
on the code they run on.
- D15: an internal error after an unknown attempt is answered "unknown".
- D16: keyed calls get 3 attempts.

**Setup**, as B5's parts, arms sg_keyed and sg_keyed_deadline only (the arms whose retries D16
changes):
- parts `notes` and `tsnotes`: `driver.py SERVER 12 "" sg_keyed,sg_keyed_deadline`,
  TRIAL_OFFSET 0;
- parts `crash_notes` and `crash_tsnotes`: the crash cells (`write/after_begin`,
  `write/after_effect`, `write/after_record`), 12 trials, TRIAL_OFFSET 11000.

**Prediction:** B5's keyed cells, cell for cell.
- sg_keyed 24/168 faulted on each server: its hang cells, with no deadline.
- sg_keyed_deadline 0/168.
- Crash cells: `after_record` 0/12; `after_begin` and `after_effect` 12/12 unresolved.
- 0 unsafe outcomes.

Any other difference is a regression to explain or fix.

Synthetic fault injection on two controlled servers; not production traffic.
