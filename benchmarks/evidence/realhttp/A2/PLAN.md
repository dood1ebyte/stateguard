# A2: adversarial cells J, H, C and B under the HTTP relay (written before any row)

**Why.** A1 covered the taxonomy's J, H, C and B on stdio. A2 does the same on HTTP, with the
fault proxy's new modes (`db99799`). The call always runs; the server's answer is then altered:
- J (`dup_answer`): the answer event is sent twice;
- H (`garbage_answer`, `truncated_answer`): the answer is replaced by non-JSON, or cut off
  half-way;
- C (`nonconformant_answer`): the answer says `"jsonrpc": "1.0"`;
- B (`stale_listing`): the client's first listing shows a schema the server has since changed.

**Setup.** `driver.py`, notes and tsnotes, 12 trials, TRIAL_OFFSET=20000, 4 workers; the code is
this commit.
- Part `adv_<server>`: cells `{read,write}/{dup_answer,garbage_answer,truncated_answer,
  nonconformant_answer}` and `write/stale_listing`; arms raw, sg and sg_keyed (both
  `--call-deadline 0`).
- Part `norefresh_<server>`: `write/stale_listing`, arm sg_norefresh.

**Expectations, from one-trial dry runs:**
- J: 0 everywhere.
- H: raw 24/24 (writes told a parse error for a write that happened: false failures); sg reads
  recovered, unkeyed writes unknown (12/24); sg_keyed 0/24.
- C:
  - sg_keyed 0/24 (D13);
  - sg 12/24: unkeyed writes, passed through. The client's SDK turns the answer into a parse
    error, so it is told failure for a write that happened, which is **a false failure in a
    StateGuard arm**.
  - If confirmed, this is a defect: a non-conformant answer that cannot be resent should be
    answered "outcome unknown", not passed through. A part on the fix follows.
- B: raw, sg and sg_keyed 0/12; sg_norefresh 12/12 (the server rejects the stale field).
- 0 record mismatches except where the client's view differs from the relay's (named if any).
