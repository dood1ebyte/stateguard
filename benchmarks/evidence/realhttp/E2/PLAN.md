# E2: D10 with the TypeScript keyed server (written before any row)

**Why.** E1 showed D10 on the Python notes server keyed by the injector. The TypeScript preload
(`adapters/typescript/src/executor.ts`) also holds a duplicate of a running key, then answers
"still running" (`-32011`). This batch checks that, after D10, a deadline never turns a slow
keyed success into a failure there either.

**Setup.** `driver.py`:
- tsnotes server, cell `write/slow` (no fault: `add_note` takes 10 s; the fixture gains
  `--slow` in this commit), deadline 2 s, the client waits 18 s;
- arms raw, sg_keyed and sg_keyed_deadline; 12 trials, TRIAL_OFFSET=7000, 4 workers;
- the code under test is this commit.

There is no "before" part: earlier commits' fixture has no `--slow`. E1 shows the before case
on the Python server.

**Expectations:** every arm 0/12, 0 induced, the note written once; 0 record mismatches.
