# E4: the HTTP relay's default deadline recovers a hung keyed call (written before any row)

**Why.** Decision 2 (`4c2256d`): `--call-deadline` now defaults to 30 s for recoverable
requests. This batch checks the default itself, with no flag passed: a hung keyed write is
recovered at about 30 s, and a slow keyed success is not cut short.

**Setup.** `driver.py`, notes server, 12 trials, TRIAL_OFFSET=12000, 4 workers; the code is
this commit. Arm `sg_keyed_default` is sg_keyed with no `--call-deadline` (the client waits
40 s); arm `sg_keyed` passes `--call-deadline 0` as the control.

| Cell | Arm | Expectation |
|---|---|---|
| `write/hang_after` | sg_keyed_default | 0/12: replayed at about 30 s |
| `write/hang_after` | sg_keyed (deadline 0, client waits 8 s) | 12/12 unresolved, as in B2 |
| `write/slow` (10 s) | sg_keyed_default | 0/12: below the default, no resend |

**Also expected:** 0 unsafe outcomes, notes written once, 0 record mismatches; hang rows
recorded `recovered` with fault `deadline`, slow rows `answered`.
