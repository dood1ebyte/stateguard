# Reproducing the Phase 1/2 matrices (Phase 2 S5.4)

From a clean checkout, two commands rebuild the environment and re-run any recorded matrix part,
then compare it with its reference cell for cell.

```
scripts/reproduce.sh setup                      # into $SG_PREFIX (default ./.repro)
scripts/reproduce.sh run SUITE PART REFERENCE   # e.g. run realhttp crash_notes B7
eval "$(scripts/reproduce.sh env)"              # the variables, for running drivers by hand
```

**Requirements:**
- git, Python 3.11 or later, and Node 22.18 or later with npm;
- network access to PyPI and npm.
Nothing is written outside `$SG_PREFIX` and the checkout.

**`setup` installs, at the evidence's pinned versions:**

| Piece | Version |
|---|---|
| this repository, editable, with `[pydantic,mcp,dev]` | the checkout |
| mcp 1.x environment: `mcp`, `mcp-server-sqlite`, `pydantic` | 1.9.4, 2025.4.25, 2.11.7 |
| `mcp-server-git`, `mcp-server-fetch` | 2026.8.18, 2026.10.10 |
| `@modelcontextprotocol/server-memory`, `-filesystem`, `-everything` | 2026.8.31 |
| the TypeScript adapter's dependencies | `npm ci` from its lockfile |

**`run SUITE PART REF`:**
- **Command:** it reads REF's `codestate_PART.json` (driver, arguments, trial offset), so the
  part runs exactly as recorded.
- **Plan:** it writes the plan before any row, into `benchmarks/evidence/SUITE/REPRO-<date>/`.
- **Code:** it runs `run_batch.py` on the checkout's HEAD, on a clean worktree.
- **Comparison:** it compares the summary with REF's, using `compare_cells.py`.
- **Re-running the whole matrix:** run each part of B7 (HTTP), S10 (stdio), R1 and R2 the same
  way. The parts of each batch are its `codestate_*.json` files.

**Shown:** B7's `crash_notes` reproduced cell for cell from a fresh clone, in all five arms
(`benchmarks/evidence/realhttp/REPRO-20261127/`).

**Limits:**
- Timing-sensitive cells (deadlines, slow answers) depend on the machine's load.
- The matrices ran on a 4-CPU Linux container. A loaded or much slower machine can move a
  deadline race, which shows as a different cell, never a hidden one.
