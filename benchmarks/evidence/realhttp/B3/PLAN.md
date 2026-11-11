# B3: the HTTP matrix again, on the Session 2–3 code (written before any row)

**Why.** Since B2 (code `1852365`), the HTTP relay gained:
- D9: `invocation_open` lines and `unfinished` records;
- D10: a keyed call reported "still running" is waited for under a deadline;
- D12: a 0.2 s backoff before a resend after a transport loss.

B3 repeats B2 on `51f6fdf` as a regression check of the full fault matrix.

**Same as B2:**
- servers notes, tsnotes and everything, with every default arm;
- 12 trials;
- 60-trial fault-free controls (TRIAL_OFFSET=1000);
- 4 workers.

**Expectations:**
- B2's failure counts, cell for cell:
  - notes and tsnotes: raw 168/168, naive 48/168 (48 duplicates), sg 84/168, sg_keyed 24/168,
    sg_keyed_deadline 0/168;
  - everything: raw 72/84, naive 0/84, sg 12/84, sg_deadline 0/84.
- Fault-free 0; 0 unsafe outcomes and 0 induced in StateGuard arms; 0 record mismatches.
- Every relay row has a record (D9).
- Keyed recoveries after a transport loss take about 0.2 s longer (D12); none should fail.
