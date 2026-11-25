# B8: the HTTP matrix on S4.1's code (written before any row)

**Why.** S4.1 changed the relays in two places:
- with refresh off, a repair or refusal is withheld;
- a refused repair falls back to the client's own call.
The default matrix runs with refresh on and has no repaired calls, so its cells should not
change. B8 checks that S4 does not regress the matrix.

**Setup.** B7's parts and commands exactly (B5's), on S4.1's commit `290ba5d`, through
`stateguard relay http`:
- notes, tsnotes and everything: 12 trials, TRIAL_OFFSET 0;
- the controls: 60 trials, TRIAL_OFFSET 1000;
- crash_notes and crash_tsnotes: TRIAL_OFFSET 11000.

**Prediction:** B7, cell for cell, in every StateGuard arm, with 0 unsafe outcomes. A
difference in a baseline arm, which runs no relay, is reported as run-to-run variation, never
hidden.

Synthetic fault injection; not production traffic.
