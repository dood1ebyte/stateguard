# Week 4A — CLI executor confinement, safe reconciliation and evidence audit

**Date:** 2026-10-17. **State:** working tree, uncommitted and unpushed.
**Contract:** `docs/runtime/RECONCILIATION_CONTRACT_V2.md`, revised 2026-10-17: §4.4 (new), §5.1 and §7.
**Evidence:** `/root/stateguard-captures/week4a/`. Raw private data is kept there; this report carries hashes and aggregates only.

Labels used throughout:
- **[measured]** — counted from stored raw results by `week4_summary.py`;
- **[test]** — an automated test;
- **[implemented]** — a description of code;
- **[assumption]** — not enforced by StateGuard.

## 1. Verdict

**Ready for experimental opt-in, on Linux with `CAP_SYS_ADMIN` only.**

- `stdio_proxy.py --executor-scope pid-namespace` runs every server generation as the only child of init of its own PID namespace.
- It confirms the namespace empty itself before it reports `scope_ended` for that generation's identity.
- It reconciles only after that.
- Everywhere else (any other platform, no permission, a missing prerequisite) it refuses at startup, before any server starts; nothing is downgraded.

The CLI without the flag still refuses `reconcile` blocks, as before.

What it does **not** establish, and never claims:
- that the lost call executed, or did not;
- that it caused what was observed;
- that the state persists;
- that the operation succeeded;
- anything about external systems.

Every reconciled answer remains a JSON-RPC error (`-32000`) carrying `operationOutcome: unknown` and `treatAsSuccess: false`.

## 2. Environments

| Environment | Status | Basis |
|---|---|---|
| Linux 6.18.44 (Firecracker VM), root, Python 3.11.17, Node 22.22.0 | **supported, experimental** | [measured] batch; [test] full suite |
| The same, as uid 65534 with only `CAP_SYS_ADMIN` (ambient) | the capability check passes; root is not required | [test] `test_cap_sys_admin_alone_is_enough_root_is_not_required` |
| The same, as uid 65534 without capabilities | refused at startup: `unshare(CLONE_NEWPID): Operation not permitted … needs CAP_SYS_ADMIN …` | [test] and [measured] refusal case |
| Linux without `/proc/self/ns/pid`, without pidfd (kernel < 5.3), or where seccomp blocks `unshare` (Docker default profile) | refused at startup, with the reason | [test] for `/proc` (simulated); **pidfd and seccomp were not tested on a real system** |
| macOS, Windows, other OS | refused at startup: "PID namespaces need Linux; this platform is …" | [test] simulated platform, in-process `main()`. **Not run on a real macOS or Windows host.** |
| Other kernels, distributions, container runtimes, rootless setups | **untested — not claimed** | — |

## 3. Invocation

```sh
python examples/mcp/stdio_proxy.py \
  --effects effects.json \             # with reconcile blocks (contract §6)
  --executor-scope pid-namespace \
  --restart 2 --restart-policy after-unknown \
  --retry off --audit audit.jsonl \
  -- npx @modelcontextprotocol/server-filesystem /data
```

**Checked before anything starts.** Each failure exits with status 2 and starts no server [test] [measured 10/10]:
- reconcile blocks without `--executor-scope pid-namespace`;
- reconcile blocks in `--shadow` or `--passthrough`;
- reconcile blocks without `--restart N ≥ 1` and `--restart-policy after-unknown`;
- a server program not on `PATH`;
- a failed capability check.

The capability check (`check_pid_namespace_capability`) creates a real PID namespace containing a `setsid` descendant, kills its init, and requires the namespace to be seen empty and pidfd to be available. It takes at most 5 s.

`--executor-scope pid-namespace` without reconcile blocks is allowed and changes nothing client-visible [measured: N1, and shadow fidelity below].

## 4. Architecture and actual guarantees

Full detail: contract §4.4. [implemented]: `src/stateguard/adapters/mcp/executor_scope.py`; the `main()` integration in `examples/mcp/stdio_proxy.py`.

**Per generation:** relay → helper (outside the namespace) → init (PID 1 inside) → server.
- The helper and init close their copies of the server's stdio, so no wrapper holds the pipes.
- To end a scope: the relay closes the control pipe; the helper SIGKILLs init through a pidfd; the kernel kills every other member; the helper reaps init, counts the members left and reports; the relay rescans `/proc`.
- The result is `scope_ended`, bound to the identity `pidns-<inode>-<helper pid>-<n>`, only if all of that happened within 5 s. Anything else is `scope_unconfirmed`, which never allows a probe.

**Explicitly not used:**
- `PR_SET_CHILD_SUBREAPER`;
- process-group kill;
- unprivileged user namespaces, which would change the server's identity.

**What each layer establishes:**

| Layer | Established by | Does not establish |
|---|---|---|
| Process confinement | kernel PID namespace | anything outside it |
| Scope termination | init reaped + two empty scans + helper exit, within 5 s | whether the call executed |
| Predicate observation | read-only probes (§5 of the contract) | causality, durability, success |
| Operation outcome | nothing | always `unknown`, `treatAsSuccess: false` |
| External effects | nothing | X1 (below) shows this |

**Defects found and fixed in this package** [test, each shown failing without the fix]:
1. **The client leaving during a restart left the relay running forever.** `pump_client` closed the *old* server's stdin, the restart then connected a new server that nobody closed, and the relay never stopped. This affected the unscoped CLI too. It is fixed in `_on_server_lost`, which closes the new server's stdin and sets the link down when the client is gone. Tests: `test_shutdown_while_termination_is_being_confirmed_answers_once`, `test_a_termination_that_never_comes_is_answered_within_the_budget`.
2. **The kernel reuses a freed namespace's inode number at once** (20/20 trials). The end-of-scope scans could therefore count the *next* generation's members, turning every confirmed end into `scope_unconfirmed` (fail-closed). The relay now holds a descriptor of the namespace, checked to be init's by its parent PID, until its scan. Afterwards there was no reuse in 20/20 trials. Test: `test_the_namespace_is_held_so_its_end_is_never_judged_by_a_newer_one`.
3. **A `stateguard` package planted in the working directory ran as the scope helper.** The cause was `python -m`, which puts the working directory first on `sys.path`. The helper now runs with `-P`. Test: `test_a_package_planted_in_the_working_directory_never_runs_as_the_helper`.
4. Earlier in this package:
   - after `unshare(CLONE_NEWPID)` a process cannot create threads, so the helper now uses a single-threaded pidfd poll loop;
   - the reference embedder's wrapper held stdio (the T3 liveness gap); in this implementation no wrapper holds it.

## 5. Result semantics and SDKs

**Envelope unchanged.**
- Reconciliation answers are always JSON-RPC errors `-32000`.
- New key `io.stateguard/treatAsSuccess: false`, alongside `executionState`, `attribution`, `durability`, `operationOutcome`, `observation` (window, predicates with strength, `content_checked`) and `reconciliation` (`establishes` / `does_not_establish`).

**One overall budget.**
- `max_seconds`: default 30, maximum 60, validated so that `timeout_seconds × probes ≤ max_seconds`.
- It caps the evidence wait (≤ 12 s), discovery (≤ 20 s) and each probe; the previous summed worst case of 150 s is gone.
- Measured worst case in the batch: **20.1 s**, from a restarted server that never lists its tools (S12e). The scope-unconfirmed path took 5.1 s.

**Clients** [measured, `week4a/clients/client_semantics.json`, CLI with the scope, real server]:
- The raw JSON-RPC, Python `mcp` SDK and TypeScript SDK clients all saw an **error, never a result**, both for a lost answer after execution (`observed_satisfied`) and for a request that never arrived (`observed_not_satisfied`): 6/6.
- `io.stateguard/treatAsSuccess` was present in the error data in 6/6.
- Termination was `scope_ended` and applied in 6/6.

## 6. Comparative batch (Phase 6–7)

**Batch facts:**
- Location: `/root/stateguard-captures/week4a/batch/`.
- Code under test: `code-under-test.sha256`, checked unchanged after the batch, the client run and the fidelity run.
- Size: 22 scenarios, 47 (scenario, configuration) cells, 5 repetitions each (S12e: 3), **233 trials** plus 10 refusal cases.
- Excluded, incomplete or re-run trials: **none**.

Summary: `week4_summary.py results.json clients/client_semantics.json summary.json`. Two runs gave byte-identical output.

**Configurations:**

| Name | What it is |
|---|---|
| `cli_scope` | CLI + scope + reconcile |
| `cli_raw` | CLI, no reconcile |
| `cli_naive` | `cli_raw`, and the client re-sends once |
| `emb_direct` | embedded relay seeing only a reaped direct child (negative control) |
| `cli_shadow` / `cli_shadow_scope` | Shadow Mode, without and with the scope |
| `direct` | no relay |

**Scenario map:**

| # | Situation | Scenarios |
|---|---|---|
| 1 | normal | N1 |
| 2 | lost, not executed | S2 |
| 3 | lost, executed | S1 |
| 4 | detached descendant | T1 |
| 5 | late descendant or survivor | T2, T3 |
| 6 | scope not emptied | U1 (helper SIGSTOPped) |
| 7 | restart fails | S9 |
| 8 | predicate already held | S4b |
| 9 | another actor | S4a |
| 10 | wrong content | S4c (presence check), C2 (content check) |
| 11 | change between observation and answer | S4d, S6d, C3 |
| 12 | malformed or misleading probe | S6b, S6e, S7a |
| 13 | concurrent calls, one lost | M1 |
| 14 | shutdown race | SR1, S12e |
| 15 | shadow, scope absent and present | N1, S1, S2 × shadow configurations |
| — | external effect | X1 |

**Results by configuration** [measured]:

| Measure | `cli_scope` | `cli_raw` | `cli_naive` | `emb_direct` | `cli_shadow` | `cli_shadow_scope` | `direct` |
|---|---|---|---|---|---|---|---|
| Trials | 108 | 35 | 15 | 30 | 15 | 15 | 15 |
| Executed (ground truth; incl. external worker) | 68 (73)/108 | 20/35 | 10/15 | 20 (25)/30 | 10/15 | 10/15 | 10/15 |
| Predicate true at answer | 73/108 | 25/35 | 10/15 | 15/30 | 10/15 | 10/15 | 10/15 |
| observed_satisfied / not_satisfied / undetermined / none | 50 / 25 / 28 / 5 | — / — / — / 35 | 15 none | 0 / 0 / 30 / 0 | 15 none | 15 none | 15 none |
| Observation correct at answer | **65/75** | — | — | — | — | — | — |
| Trials with a false claim of execution, attribution, durability or success | **0/108** | 0/35 | 0/15 | 0/30 | 0/15 | 0/15 | 0/15 |
| Duplicate side effects | 0/108 | 0/35 | 0/15 | 0/30 | 0/15 | 0/15 | 0/15 |
| Relay resends of the original | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Client resends (that executed) | 0 | 0 | 10 (5) | 0 | 0 | 0 | 0 |
| Requests answered exactly once | 113/113 | 40/40 | 25/25 | 30/30 | 5/15 | 5/15 | 5/15 |
| Termination evidence | 103 `scope_ended`; **5 `scope_unconfirmed` (U1)** | — | — | 30 `direct_child_reaped` (insufficient) | — | — | — |
| Probes (not completed) | 170 (20) | 0 | 0 | 0 | 0 | 0 | 0 |
| Latency median / p95 / max (s) | 0.388 / 5.124 / 20.125 | 0.026 / 0.035 / 0.039 | 0.024 / 0.026 / 0.026 | 0.006 / 0.035 / 0.051 | 0.024 / 0.029 / 0.029 | 0.027 / 0.041 / 0.041 | 0.009 / 0.017 / 0.017 |
| Orphan processes after the 10 s bound | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Late effects after the answer | 5 (X1 external worker) | 0 | 0 | 15 (T1 rollback, T2 late move, X1) | 0 | 0 | 0 |
| Audit content leaks / sentinel changed | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

**Reading the table:**

- **The 10 incorrect observations** are S6d (state changed between the two probes) and S6e (the probe server lies), 5 each. They are the contract's stated limits: an observation is a snapshot, and a lying server cannot be detected. Each said "observed", with execution, attribution, durability and outcome `unknown` and `treatAsSuccess: false`. None was a false claim of execution or success.
  - In 10 of the 50 `observed_satisfied` trials the predicate no longer held at the end; these are the same trials. That is why `durability: not_established` is required.
- **`undetermined` (28)** is never success:
  - U1: the scope was unconfirmed, nothing was probed (5);
  - S9: the restart failed (5);
  - S6b: a malformed probe (5);
  - S7a: a probe timeout (5);
  - SR1: the client closed during reconciliation, answered at end of stream (5);
  - S12e: never listed (3).
- **The 5 "none"** for `cli_scope` are N1: no fault, so the server's real result passed through untouched.
- **T1, T2, T3:** with the scope, the detached descendant or the stdout-closed survivor was killed with the namespace before any probe. The verdicts matched ground truth (T1 and T3 satisfied, T2 not satisfied), and no late effect followed in 15/15.
  - The negative control (`emb_direct`) refused to probe in 15/15. The descendant then acted: 5 rollbacks (T1) and 5 late moves (T2).
- **X1:** the scope ended (5/5), the probes saw the move not yet made, and the external worker made it after the answer (5/5). Local process termination says nothing about an external actor, and the answer did not claim it did.
- **Answers.**
  - Answering configurations: 208/208 requests answered exactly once.
  - All configurations: 253/253 at most once.
  - The 30 unanswered requests are the shadow and no-relay pass-through of a closed pipe (S1 and S2 × 3 configurations), identical to having no relay.
- **Shadow byte fidelity** (the client's whole stream compared with `direct`): `cli_shadow` 15/15, `cli_shadow_scope` 15/15.
  - The Week 2 workload fidelity pass on the final code, scope absent (`week4a/shadow-fidelity/fidelity.json`), gave raw == shadow == tee in 10/10 tests.
  - Its 10 "leaked value" hits are all the `relay_started` audit event naming the server command's workspace paths, as in Week 3 — paths, not contents.
- **Refusals:** 10/10 exited with status 2, started no server, and printed an actionable message.
- **The naive retry caused no duplicate here** only because of the fixture: re-sending a `move_file` whose original had executed (S1) fails with ENOENT. Its 5 resends that executed are S2, where the original never arrived. This is a property of `move_file`, not evidence that blind retry is safe.

**What this does not show.**
- These are controlled fixtures on one machine. **No production-reliability percentage can be derived**, and none is claimed.
- The number of observations is not a recovery rate: nothing was recovered, and the original call was never resent.
- Untested:
  - other kernels and container runtimes;
  - real seccomp-restricted containers;
  - a process in uninterruptible sleep at end of scope (only simulated, through a stopped helper and a monkeypatched scan);
  - load and many concurrent relays on one host;
  - a real macOS or Windows host.

## 7. Timeouts and cleanup

| Step | Bound |
|---|---|
| Capability probe | 5 s |
| Scope start | 5 s |
| Scope end | 5 s, plus ≤ 5 s to kill the helper on the unconfirmed path |
| Evidence wait | ≤ 12 s |
| Discovery | ≤ 20 s |
| Probe | `timeout_seconds` |
| Overall | all of the above capped by `max_seconds` (≤ 60) |
| Shutdown | waits ≤ 2 s for answers being written |

**Measured cleanup:** 0 orphans in 233 trials [measured].

**Abrupt endings** [test]:
- relay killed with SIGKILL: no process left;
- helper killed: the namespace empties (PDEATHSIG);
- helper stopped: `scope_unconfirmed`, then cleaned up.

The scope creates no files, mounts or cgroups.

## 8. Trust boundary, risks and limitations

**[assumption]: what StateGuard does not enforce:**
- The probe server reports state honestly.
- The server's effects stay on this host, inside processes in the namespace. Anything handed to another local process (a daemon over a socket, D-Bus, a container runtime, `at`) or a remote system is outside the scope, and stays outside every answer.
- `/proc` shows the relay's PID namespace (checked by the capability probe).

**Privilege:**
- The relay needs `CAP_SYS_ADMIN`.
- The server runs with the relay's credentials, exactly as without a scope.
- **Superseded 2026-10-17 (Week 4B — `docs/runtime/WEEK4B_EXECUTOR_SECURITY_AUDIT.md`):** the statement that "StateGuard adds none and drops none" was a defect. Enabling the scope forced the relay to hold `CAP_SYS_ADMIN` and handed it to the server. The scope now **drops `CAP_SYS_ADMIN`** (and all ambient capabilities, with `no_new_privs`) before the server runs, failing closed if it cannot. The server never holds `CAP_SYS_ADMIN`; a non-root server runs with no capabilities. The scope remains a lifecycle boundary, not a sandbox.

**Not limited:** CPU, memory and process count (no cgroup). A fork bomb inside the scope is not contained.

**False `scope_unconfirmed`:** possible under heavy load (an end slower than 5 s) or with a process in uninterruptible sleep. This is fail-closed: the answer is `undetermined`.

**Remaining routing exposure** (unchanged, contract §8): a client reusing the exact id of a pending relay-owned request loses the first response with that id. It still gets at most one answer [test].

## 9. Before broader support

- Each additional platform needs its own confinement mechanism, with a provable end:
  - cgroup v2 `cgroup.kill` plus `populated=0` on Linux without `CAP_SYS_ADMIN`;
  - job objects with kill-on-close on Windows.
  - There is no comparable kernel primitive on macOS.
- That mechanism needs tests and a batch on that platform. Support stays unclaimed until then.
- A container and seccomp matrix on Linux.
- Resource limits (cgroups) if hostile servers are in scope.
- A load test with many relays per host, to measure false `scope_unconfirmed` rates.

## 10. Evidence index

| Item | Location |
|---|---|
| Raw results | `/root/stateguard-captures/week4a/batch/results.json` (233 trials + 10 refusals) |
| Derived summary | `batch/summary.json`, `batch/summary.txt` |
| Code hashes | `batch/code-under-test.sha256` |
| Environment | `batch/environment.txt` |
| Clients | `clients/client_semantics.json` |
| Shadow fidelity | `shadow-fidelity/fidelity.json` |
| Metric audit | `metric-audit/audit-final.json` |
| Gates | `gates/` (pytest, junit, checks, mypy-strict, repeat, verify_prior_final) |
| Manifest | `EVIDENCE_SHA256.txt` |
