# O1: clean-machine time from install to the first protected call (Phase 2 S5.2; written before the run)

**What.** `scripts/clean_machine.sh` times, from nothing, the steps a new user takes:
1. a fresh `HOME`, a new venv, and pip and npm with empty caches (`PIP_NO_CACHE_DIR=1`, a new
   npm cache directory);
2. `pip install` of the release-candidate wheel (`dist/rc/`). Once published this would be
   `pip install sguard`; it is not published (A5);
3. `npm install @modelcontextprotocol/server-filesystem@2026.8.31` into a new directory: the
   server being protected, downloaded as a first-time user would;
4. `stateguard doctor -- <server>`;
5. **the first protected call:** a client sends `write_file`, then `read_text_file`, through
   `stateguard relay stdio --retry auto --effects ...`, and the content is checked.

**Measured:** the wall-clock seconds of each step and their total. Network installs from PyPI and
npm go through this environment's proxy.

**Prediction:** total ≤ 300 s (the S5 acceptance: 5 minutes or less); the call succeeds.

**Limits.** A container, not a new laptop:
- Python 3.13 and Node 22 are already installed;
- the network is this environment's;
- the time a person spends reading the snippet is not counted.
