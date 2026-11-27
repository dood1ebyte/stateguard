# S7: the stdio matrix on a third pinned server, server-filesystem (written before any row)

**Why.** This session's focus includes testing against more third-party MCP servers. The stdio
matrix has run on sqlite (Python) and memory (TypeScript).
`@modelcontextprotocol/server-filesystem` 2026.8.31 (TypeScript, keyed by the preload) adds a
write that cannot duplicate but can fail when repeated: `move_file`. A second move finds its
source gone.

**Setup.** `realstdio/driver.py`, server `filesystem`, every default cell (fault-free, the 4
transport faults on read and write, the 3 crash points on write), arms raw, naive, sg and
sg_keyed; 12 trials, TRIAL_OFFSET=15000, 4 workers; the code is this commit.
- read: `read_text_file` of a seeded file, declared read_only;
- write: `move_file` of `src-T.txt` to `dst-MARKER.txt`, declared non_idempotent;
- ground truth: the destination exists.

**Expectations:**
- raw: every faulted row fails (132/132), with false failures where the move happened;
- naive: no duplicate is possible. A blind resend of a move that happened is answered with an
  error, so there are false failures (unsafe) where sqlite showed duplicates;
- sg: S4's shape (about 84/132), unresolved, never resent, 0 unsafe;
- sg_keyed: the two crash windows unresolved (24/132); everything else recovered, 0 unsafe;
- fault-free 0; 0 record mismatches.

A shape different from S4 is a finding to explain, not a failure to tune away.
