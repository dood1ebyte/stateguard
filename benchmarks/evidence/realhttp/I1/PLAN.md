# I1: every relayed request is counted in the incidence records (written before any row)

**Why.** In B1 and B2, 15 and 18 `sg` rows on `hang_after` had no `invocation` record. A relay
wrote a record only when a request finished, and the driver kills the relay (SIGKILL) while the
request still waits on the hung server. The fix (this commit) writes an `invocation_open` line
as each request arrives. `incidence.read_records` turns an open line with no record into an
`unfinished` record, which is unresolved.

**Setup.** `driver.py`:
- notes and tsnotes;
- cells `read/hang_after`, `write/hang_after`, `read/none`, `write/none`;
- arms `raw`, `sg`, `sg_keyed`;
- 12 trials, TRIAL_OFFSET=4000, 4 workers;
- the code under test is the fix commit (`codestate*.json`).

**Expectations:**
- every `sg` and `sg_keyed` row has a `relay_invocation` (records = rows: 96 per server);
- the hang rows the relay never finished are recorded `unfinished`;
- 0 record mismatches against ground truth (`summarize.py`);
- the residual failure rates match B2's cells: the fix changes records only, never traffic.
