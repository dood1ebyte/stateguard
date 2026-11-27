# S5: crash windows of a declared idempotent tool (written before any row)

**Why.** In S4, the keyed relay's whole residual on the memory server was the two crash windows
(`after_begin`, `after_effect`): 24/36, in doubt, never re-run. D11 lets the operator declare a
keyed tool idempotent (`STATEGUARD_IDEMPOTENT_TOOLS`, exclusive store). The next server
process then runs an abandoned attempt again instead of leaving it in doubt. The memory
server's `add_observations` drops an observation it already has, so running it twice leaves
one.

**Setup.** `realstdio/driver.py`:
- memory server; every write cell: `none`, transport `kill_before`, `lost_exit`, `hang`,
  `transient`, crash `after_begin`, `after_effect`, `after_record`;
- arms raw, sg_keyed and sg_keyed_idempotent (sg_keyed plus the declaration and an exclusive
  store; the relay's own configuration is unchanged);
- 12 trials, TRIAL_OFFSET=9000, 4 workers; the code under test is this commit.

**Ground truth:** how many times the marker appears in the memory file.

**Expectations:**
- sg_keyed: as S4 (`after_begin` and `after_effect` 24/24 unresolved; everything else 0);
- sg_keyed_idempotent: 0/96. The two crash windows are answered `executed`, with the marker
  once. Nothing else moves.
- 0 unsafe outcomes and 0 duplicates in StateGuard arms; 0 record mismatches.

The result holds only for a tool whose repeat is harmless. sqlite's `INSERT` is not
idempotent, so its crash windows stay in doubt.
