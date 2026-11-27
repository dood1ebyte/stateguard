# E3: a slow declared read under a deadline (written before any row)

**Why.** This is part of the evidence for a default deadline (queue item 2). The HTTP relay
re-requests a recoverable read at the deadline and keeps both attempts open; the first answer
wins. The read should never be cut short. The cost is a second read on the server.

**Setup.** `driver.py`:
- notes server, cell `read/slow` (no fault: `list_notes` takes 10 s via the fixture's new
  `--slow-read`), the client waits 18 s;
- arms raw, sg (no deadline) and sg_deadline (2 s); `list_notes` is recoverable through its
  `readOnlyHint` (`--trust-read-only-hints`);
- 12 trials, TRIAL_OFFSET=8000, 4 workers; the code is this commit.

**Measure:** strict success, plus the reads that reached the server (`server_calls_of_target`,
from the fault proxy's log).

**Expectations:**
- every arm 0/12, with 0 induced;
- raw and sg: 1 read per row; sg_deadline: 2;
- sg_deadline's records read `recovered` with fault `deadline`. That is an incidence caveat: a
  slow call past the deadline counts as a fault.
