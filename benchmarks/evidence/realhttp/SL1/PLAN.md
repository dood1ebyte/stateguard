# SL1: the A1/A2 stale-listing cells with refresh off, on S4.1's code (written before any row)

**Why (Phase 2 S4.1 acceptance).** In Phase 1, with `--no-listing-refresh`, a stale listing
made the relay "repair" a correct call into a wrong one:
- A1 (stdio): sqlite 12/12 **false successes**, because the server reports the error as text;
  memory 12/12 failed;
- A2 (HTTP): notes and tsnotes 12/12 failed.
Under S4.1, with refresh off the relay neither repairs nor refuses on an unconfirmed listing: it
forwards the call unchanged and logs the proposal as withheld.

**Setup.** The code is S4.1's commit (`290ba5d`), with the cells of Phase 1:
- **realstdio** (`../realstdio/driver.py`): `sqlite` and `memory`, `write/stale_listing`, arms
  `sg_norefresh` and `sg`, 12 trials, TRIAL_OFFSET 16000 (A1's);
- **realhttp** (`driver.py`): `notes` and `tsnotes`, `write/stale_listing`, arms `sg_norefresh`
  and `sg`, 12 trials, TRIAL_OFFSET 20000 (A2's).

**Prediction.**
- `sg_norefresh`: **0/12 on every server, with 0 false successes and 0 false failures.** The call
  is forwarded as the client sent it, and the server, whose true schema it is, runs it once.
- `sg` (refresh on): 0/12, as A1/A2.
- 0 unsafe outcomes.

Synthetic fault injection on controlled and pinned servers; not production traffic.
