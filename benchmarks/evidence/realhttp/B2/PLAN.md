# B2: the HTTP matrix again, on the current code (written before any row)

**Why.** Since B1 (code `237a332`), the HTTP relay gained:
- the 1 s pool idle timeout (D4);
- the `-32603` retry where safe (D8);
- the same response path through `_settle` and `_stream`, extended.

B2 repeats B1 on the commit in `codestate*.json`, as a regression check of those changes on the
full fault matrix.

**Same as B1:**
- servers notes, tsnotes and everything, with every default arm (`driver.py`);
- 12 trials;
- 60-trial fault-free controls (TRIAL_OFFSET=1000);
- 4 workers.

**Expectations:** B1's numbers, cell for cell.
- notes and tsnotes: raw 168/168, naive 48/168 (48 duplicates), sg 84/168, sg_keyed 24/168,
  sg_keyed_deadline 0/168.
- everything: raw 72/84, naive 0/84, sg 12/84, sg_deadline 0/84.
- Fault-free 0; 0 unsafe outcomes and 0 induced failures in StateGuard arms; 0 record mismatches.

None of B1's faults injects a `-32603`, and the fault proxy closes every connection, so neither
change should move a cell. Any cell that moves is a regression to explain.
