# S10: the stdio matrix through `stateguard relay stdio`, and `--keys auto` (written before any row)

**Why.** Phase 2 S1 moved the stdio relay into the package (`stateguard.relay.stdio`) and runs
it as a package command: `stateguard relay stdio [opts] -- <server>` (`python -m stateguard
relay stdio ...` in the driver). The relay also gained `--keys auto` (off by default), which
starts a Python server under the injector and a Node server with the preload. With `--keys`
off nothing else changed, so S8's and S9's cells should not change. S10 checks that, and runs
the `--keys auto` arm on the cells it can run.

**Setup.** `../realstdio/driver.py` (default 4 workers), the driver now starting the relay as
`python -m stateguard relay stdio ...`. Parts, one after another:
- `sqlite`, `memory`, `filesystem`: S8's command, every default cell and arm (raw, naive, sg,
  sg_keyed), 12 trials, TRIAL_OFFSET 22000;
- `memory_idempotent`: S8's command (`sg_keyed_idempotent`, its 8 write cells), TRIAL_OFFSET
  22000;
- `slow_sqlite`, `slow_memory`, `slow_filesystem`: S9's command (`write/slow`, sg_keyed),
  12 trials, TRIAL_OFFSET 23000;
- `keys_auto_sqlite`, `keys_auto_memory`, `keys_auto_filesystem` (new): arm `sg_keys_auto`,
  cells `write/none,write/after_begin,write/after_effect,write/after_record`, 12 trials,
  TRIAL_OFFSET 24000. The arm is sg_keyed's relay flags plus `--keys auto --keys-db
  <run>/keys.db --keys-scope <server>`, with the plain server command (no adapter variables,
  no preload, no injector): the relay wraps it. sqlite is the mcp 1.x `mcp-server-sqlite`
  console script (Python, `#!` line: the injector); memory and filesystem are Node (the
  preload). The TEST-ONLY crash faults are set in the environment as in the other arms. The
  arm runs only cells without a shim fault: `--keys auto` reads the server command, and with
  the fault shim in front it would see the shim, not the server.

**Prediction:**
- S8's cells: per server raw 132/132; naive sqlite 48, memory 0, filesystem 48 (of 132); sg
  84/132; sg_keyed 24/132 (S9), only the two crash windows, unresolved; memory
  sg_keyed_idempotent 0/84; fault-free 0 in StateGuard arms;
- slow: 0/12 per server (S9);
- keys_auto, per server, as sg_keyed on the same cells: `write/none` 0/12,
  `write/after_record` 0/12 (replayed), `write/after_begin` and `write/after_effect` 12/12
  unresolved (surfaced unknown, never success or failure);
- 0 unsafe outcomes in StateGuard arms; 0 relay-record mismatches.

**Comparison.** Cell by cell against S8, and against S9 for sg_keyed and the slow parts.
StateGuard arms must match exactly; any difference is a regression to explain or fix. The raw
and naive arms never run the relay, so a difference there is reported as run-to-run variation
of the baseline, never hidden. keys_auto is compared with S10's own sg_keyed cells.

Synthetic fault injection on three pinned servers; not production traffic.

---

**Run log (added after rows; the plan above is unchanged).**
- 2026-11-18 about 10:10 IST: the container restarted during part `filesystem`, after 240 of
  its 624 rows. They are kept as `rows_filesystem_interrupted.jsonl` and `.err`, with no
  codestate or summary, and are not counted. In those 240 rows: 0 driver errors, 0 unsafe
  outcomes in StateGuard arms. The part is re-run complete, same command and TRIAL_OFFSET, as
  `filesystem_r2`; that run is the one compared. The remaining parts run after it,
  unchanged.
