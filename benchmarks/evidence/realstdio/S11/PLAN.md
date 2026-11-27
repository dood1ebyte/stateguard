# S11: the stdio matrix on S4.1's code (written before any row)

**Why.** S4.1 changed the relays in two places:
- with refresh off, a repair or refusal is withheld;
- a refused repair falls back to the client's own call.
The default matrix runs with refresh on and has no repaired calls, so its cells should not
change. S11 checks that S4 does not regress the matrix.

**Setup.** S10's parts and commands exactly, on S4.1's commit `290ba5d`, through
`stateguard relay stdio`:
- sqlite, memory and filesystem: every default arm, 12 trials, TRIAL_OFFSET 22000;
- memory_idempotent: TRIAL_OFFSET 22000;
- the slow parts: TRIAL_OFFSET 23000;
- keys_auto on each server: TRIAL_OFFSET 24000.

**Prediction:** S10, cell for cell, in every StateGuard arm (filesystem against
`filesystem_r2`), with 0 unsafe outcomes. A difference in a baseline arm is reported as
run-to-run variation.

Synthetic fault injection on pinned servers; not production traffic.
