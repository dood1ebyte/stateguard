# REPRO-20261127: one matrix part reproduced from a clean checkout, by the documented commands only

**How:**
- `git clone` of this repository at `609bfa5`, into a new directory;
- then, there, only the two documented commands (`docs/runtime/REPRODUCE.md`), with a new
  `SG_PREFIX` and nothing pre-installed in it:

```
SG_PREFIX=/tmp/repro-prefix PYTHON=python3.13 scripts/reproduce.sh setup     # 46 s (setup.log)
SG_PREFIX=/tmp/repro-prefix scripts/reproduce.sh run realhttp crash_notes B7
```

The part is B7's `crash_notes` (HTTP, notes server, three crash points × 12, five arms).
`run` read its command from B7's `codestate_crash_notes.json`: driver `driver.py`, trial offset
11000, the same arguments. The plan was written by the script before the run.

**Result: identical to B7 cell for cell in all five arms** (`summary_crash_notes.compare.txt`):
- sg 36/36;
- sg_keyed 24/36;
- sg_keyed_deadline 24/36;
- raw 36/36;
- naive 36/36.
0 unsafe outcomes in the StateGuard arms.

`codestate_crash_notes.json` records commit `609bfa5`, `harness_dirty: false`, Python 3.13.18,
mcp 2.3.0 and Node v22.23.4.

Synthetic fault injection; not production traffic.
