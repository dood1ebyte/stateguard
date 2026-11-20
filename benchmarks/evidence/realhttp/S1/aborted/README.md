# Aborted start (not evidence)

At 09:40 IST on 2026-11-06, four S1 arms started at once raced to create the same worktree. Three
refused it as "not clean" while it was half written. The `new` arm passed and was stopped before
writing a row (0 rows). `run_batch.py` now takes a file lock around worktree creation. S1 was
re-run from scratch.
