# S4: the stdio matrix in one batch on the current commit (written before any row)

**Why.** S2 (code `6181cc2`) and S3 (code `a8ed63d`, the `-32603` change) together imply a keyed
residual of 24/132 per server, but they are two batches at two commits. S4 runs S2's full
matrix once, at the commit in `codestate*.json`.

**Same as S2:** the harness, the servers (sqlite, memory), the 13 cells, the arms (raw, naive,
sg, sg_keyed), 12 trials, and 60-trial fault-free controls.

**Expectations, per server:**
- sg_keyed: 24/132 faulted, all of it after_begin and after_effect, reported in doubt or
  unknown, never success.
- sg: read/transient now recovers, so 84/132: every unkeyed write cell except write/none fails.
- raw 132/132. naive 48/132 with 48 duplicates on sqlite, and 0/132 on memory.
- 0 unsafe outcomes in the relay arms; fault-free 0; 0 invocation-record mismatches.
