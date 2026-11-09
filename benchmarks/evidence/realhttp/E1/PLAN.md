# E1: a slow keyed call under a deadline, before and after D10 (written before any row)

**Why.** D10 found that a keyed call slower than deadline + the server's 5 s duplicate wait +
deadline reached its client as "still running" (`-32011`), unresolved. This was a
StateGuard-induced failure. This batch measures it at the batch level, before and after the
fix. It is also the first evidence on whether a deadline can be on by default for
recoverable requests (queue item 2).

**Setup.** `driver.py`, notes server, 4 workers, TRIAL_OFFSET=6000, 12 trials. The harness is
this commit; the code under test is named per part in `codestate*.json`:
- part `old`, code `1852365` (before D10): cell `write/slow`, arms raw, sg_keyed and
  sg_keyed_deadline;
- part `new`, code `68b5594` (D10): the same cell and arms, plus `write/hang_after` for
  sg_keyed_deadline, as a regression check that recovery from a hang is unchanged.

`write/slow` injects no fault: `add_note` takes 10 s (`--slow 10`) and the client waits 18 s.
The deadline is 2 s.

**Expectations:**
- old: sg_keyed_deadline 12/12 failed, all `-32011`, 12 induced against raw; raw and sg_keyed
  0/12;
- new: every arm 0/12 on `write/slow`, 0 induced; `write/hang_after` 0/12;
- the note exists once wherever the client was told success.
