# A1: adversarial cells for categories J, H, C and B (written before any row)

**Why.** The Phase 1 RFR covers D, E, F, I and A (`PHASE1_METRICS.md`). This batch adds four
more taxonomy categories with new shim modes. The call always runs:
- J (`duplicate`: the answer is sent twice);
- H (`garbage`: the answer is replaced by a line that is not JSON; `truncated`: the answer is
  cut off half-way);
- C (`nonconformant`: the answer says `"jsonrpc": "1.0"`);
- B (`stale_listing`: the first two tools/list answers show the tool with its first input
  property capitalised, a schema the server no longer serves).

**Setup.** `realstdio/driver.py`, sqlite and memory, 12 trials, TRIAL_OFFSET=16000, 4 workers;
the code is this commit.
- Part `adv_<server>`: cells `{read,write}/{duplicate,garbage,truncated,nonconformant}` and
  `write/stale_listing`; arms raw, sg and sg_keyed.
- Part `norefresh_<server>`: `write/stale_listing`, arm sg_norefresh (the relay with
  `--no-listing-refresh`).

**Expectations, from a one-trial dry run on sqlite:**
- J: every arm 0/12.
- H: raw fails (no answer); sg recovers reads (re-read at the deadline); sg_keyed recovers
  writes (replay); an unkeyed write stays unresolved (it cannot be resent).
- C: every arm fails, the relay included. It treats the non-conformant answer as the answer;
  the client rejects it and nothing is resent. If confirmed, this is a defect to fix (a part on
  the fix follows).
- B: raw, sg and sg_keyed 0/12 (the relay refreshes its listing before acting). sg_norefresh
  "repairs" the call to the stale schema, and sqlite then answered success without writing: a
  **false success**. This is the risk of turning refresh off.
- 0 unsafe outcomes in raw, sg and sg_keyed.
