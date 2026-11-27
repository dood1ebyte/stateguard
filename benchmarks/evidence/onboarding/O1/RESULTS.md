# O1 results: clean-machine time from install to the first protected call

**Run:** `scripts/clean_machine.sh dist/rc/sguard-0.2.0rc1-py3-none-any.whl`, twice, on
2026-11-27, against the release-candidate wheel built from `d49df75`. The plan, `PLAN.md`, was
committed in `d49df75`, before the run. The rows are `run1.json` and `run2.json`.

| Step | Run 1 | Run 2 |
|---|---|---|
| New venv + `pip install` of the wheel (no cache) | 3.5 s | 3.4 s |
| `npm install` of server-filesystem 2026.8.31 (empty cache) | 5.4 s | 6.8 s |
| `stateguard doctor -- <server>` | 0.6 s | 0.6 s |
| First protected call: `write_file`, then `read_text_file`, through `stateguard relay stdio --retry auto` | 0.5 s | 0.5 s |
| **Total** | **10.1 s** | **11.4 s** |

- **Doctor:** "ready" in both runs.
- **The write and the read:** both succeeded; the content read back is the content written.
- **As predicted:** well under 300 s.

**Limits:**
- A container, with Python 3.13 and Node 22 already installed and this environment's network.
- The wheel is installed from a local file; the package is not published (A5).
- The time to read the guide and edit a client's configuration is not counted. The snippets
  are in `docs/runtime/ONBOARDING.md`.
