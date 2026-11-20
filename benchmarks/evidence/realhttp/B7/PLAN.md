# B7: the HTTP matrix through `stateguard relay http` (written before any row)

**Why.** Phase 2 S1 moved the HTTP relay into the package (`stateguard.relay.http`) and runs
it as a package command: `stateguard relay http --upstream URL [opts]`
(`python -m stateguard relay http ...` in the driver). The relay's code is unchanged except
its entry point (the upstream URL may now be given as `--upstream`). Its cells should not
change. B7 checks that, through the new command, on the commit it runs on.

**Setup.** B5's parts and commands exactly, `driver.py` (default 4 workers), the driver now
starting the relay as `python -m stateguard relay http ... --upstream URL`:
- `notes`, `tsnotes`, `everything`: every default arm and cell, 12 trials, TRIAL_OFFSET 0;
- `notes_control`, `tsnotes_control` (`read/none,write/none`) and `everything_control`
  (`read/none`): 60 trials, TRIAL_OFFSET 1000;
- `crash_notes`, `crash_tsnotes`: `write/after_begin,write/after_effect,write/after_record`,
  every default arm, 12 trials, TRIAL_OFFSET 11000.
The parts run one after another, as B5's did.

**Prediction:** B5's counts cell for cell, and B6's for the keyed arms:
- notes and tsnotes: raw 168/168, naive 48/168, sg 84/168, sg_keyed 24/168 (its hang cells,
  no deadline), sg_keyed_deadline 0/168;
- everything: raw 72/84, naive 0/84, sg 12/84, sg_deadline 0/84;
- crash cells: keyed `after_record` 0/12; `after_begin` and `after_effect` 12/12 unresolved;
  unkeyed arms 36/36;
- fault-free 0 in StateGuard arms (controls included);
- 0 unsafe outcomes and 0 induced failures in StateGuard arms; 0 relay-record mismatches.

**Comparison.** Cell by cell (server, arm, cell: failures/trials) against B5, and against B6
for sg_keyed and sg_keyed_deadline. StateGuard arms must match exactly; any difference is a
regression to explain or fix. The raw and naive arms never run the relay, so the command
cannot change them; a difference there is reported as run-to-run variation of the baseline,
never hidden (B5 itself had one naive fault-free failure on tsnotes).

Synthetic fault injection on three controlled servers (two local test servers and the pinned
server-everything); not production traffic.
