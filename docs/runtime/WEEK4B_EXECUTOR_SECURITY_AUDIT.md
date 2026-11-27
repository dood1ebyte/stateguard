# Week 4B — executor-scope security audit and privilege isolation

**Date:** 2026-10-17. **State:** working tree, uncommitted and unpushed.
**Builds on:** Week 4A (`docs/runtime/WEEK4_CLI_EXECUTOR_SCOPE.md`), contract `docs/runtime/RECONCILIATION_CONTRACT_V2.md` §4.4.
**Code:** `src/stateguard/adapters/mcp/executor_scope.py` (`24f3658f19935be6`).
**Evidence:** `/root/stateguard-captures/week4b/`. Raw private data stays there; this report carries hashes and aggregates.

Labels: **[measured]** counted from a probe/audit run; **[test]** an automated test; **[implemented]** a description of code; **[assumption]** not enforced by StateGuard.

## 1. Decision

**Retain the executor scope as an experimental, opt-in feature — now with the one demonstrated privilege defect fixed — and keep its boundary scoped to *lifecycle + CAP_SYS_ADMIN denial*, explicitly not a sandbox.**

- **Defect found and fixed:** enabling `--executor-scope pid-namespace` handed the supervised server a live `CAP_SYS_ADMIN` it would not otherwise have. The scope now drops it before the server runs, fails closed if it cannot, and refuses at startup on a kernel where the drop does not work.
- **Not fixed, by design, and now documented as explicit non-guarantees:** the scope shares the host mount/network/user/IPC namespaces, the relay's environment and working directory, and imposes no resource limit. These are architectural and would require a different mechanism (a container/sandbox); the contract must not claim isolation it does not provide.

The feature does **not** become general-purpose sandboxing, and the container compatibility matrix (the Week 4A "next task") stays deferred until this privilege decision is settled — which it now is.

## 2. Threat model and security boundary

**Actors.** An untrusted or compromised MCP server, supervised by the relay with `--executor-scope pid-namespace`. The relay, helper and init are trusted. The client is trusted.

**What the scope is for.** A kernel-enforced *lifecycle* boundary: every process the server generation starts is in one PID namespace, and the relay can confirm the namespace empty before it reports `scope_ended` (the termination evidence reconciliation requires). See Week 4A.

**The boundary this audit establishes:**

| Property | Status | Enforced by |
|---|---|---|
| The server never holds `CAP_SYS_ADMIN` (the capability the feature required) | **guaranteed** | code (drop + `/proc` verify, fail closed) + kernel |
| The server cannot create or join namespaces, mount, etc. via `CAP_SYS_ADMIN` | **guaranteed** | kernel (capability absent) |
| The server cannot re-escalate via a setuid-root exec | **guaranteed** | kernel (`no_new_privs`) |
| The server cannot end or manipulate its own scope's lifecycle | **guaranteed** | kernel (PID-1 signal protection; the helper kills from outside) |
| No relay pipe/socket fd leaks to the server (only its stdio) | **guaranteed** | code (`closerange`) + [measured] |
| The server's effects stay on the host filesystem it shares | **NOT provided** | — (shared mount namespace) |
| The server cannot reach the host network | **NOT provided** | — (shared network namespace) |
| The server cannot exhaust CPU / memory / PIDs / fds | **NOT provided** | — (no cgroup, inherited rlimits) |
| The relay's environment (secrets) is hidden from the server | **NOT provided** | — (inherited environment) |

**This is not a sandbox.** It denies one capability and bounds the process lifetime. A server run as root still has every other root capability and full host access; a non-root server has no capabilities but still shares the host filesystem and network.

## 3. What the server inherits — before and after the fix

Traced with `benchmarks/domains/mcp_server_filesystem/week4b_scope_audit.py` (a scoped probe that reports its own context) and the raw probes in `week4b/phase2/`. Two relay configurations:

- **root** — the relay runs as uid 0 (its capabilities are the full set).
- **uid 65534 + ambient `CAP_SYS_ADMIN`** — an unprivileged relay granted only the one capability the scope needs. This is the sharpest amplification case: without the scope this user has no capabilities at all.

| Inherited item | Before fix (`f16abace…`) | After fix (`24f3658f…`) |
|---|---|---|
| `CAP_SYS_ADMIN`, root relay | in Eff/Prm/Bnd; **live** (`unshare(CLONE_NEWNS)` succeeds) | absent from Eff/Prm/Bnd; **not live** (EPERM) |
| Capabilities, uid-65534 relay | `CAP_SYS_ADMIN` in Eff/Prm/Amb; **live** | **no capabilities at all**; `CAP_SYS_ADMIN` not live (EPERM) |
| `no_new_privs` | 0 | **1** |
| Ambient set | carried `CAP_SYS_ADMIN` (non-root) | **empty** |
| Baseline (same uid-65534, **no scope**) | `CAP_SYS_ADMIN` not available (EPERM) | unchanged — the scope now matches it |
| Open fds | `{0,1,2}` only (no fd ≥3) | `{0,1,2}` only |
| Namespaces | only PID is new; mnt/net/user/ipc/uts/cgroup shared with host | unchanged (not addressed) |
| Environment | full relay environment, incl. a planted secret | unchanged (not addressed) |
| Working directory | the relay's cwd | unchanged (not addressed) |
| Resource limits | inherited; no cgroup `pids.max`; `RLIMIT_AS`/`CPU` unlimited | unchanged (not addressed) |

**The amplification, stated plainly [measured]:** before the fix, a uid-65534 process that by itself cannot call `unshare(CLONE_NEWNS)` (EPERM) *could* do so once wrapped in the scope — the safety feature granted it `CAP_SYS_ADMIN`. After the fix it gets EPERM again, matching the no-scope baseline.

Raw evidence:
- `week4b/phase2/inheritance-before.txt` — before-fix probes (both relays, with the live-capability demonstration).
- `week4b/phase3/audit-root.json`, `week4b/phase3/audit-nobody.json` — after-fix, from the checked-in audit tool (root run byte-reproducible).

## 4. The fix

[implemented] In the server child of init, before `execvp` (`_confine_server`):
1. clear the ambient capability set (`PR_CAP_AMBIENT_CLEAR_ALL`) — removes the non-root amplification; a non-root server then execs with no capabilities;
2. set `no_new_privs` (`PR_SET_NO_NEW_PRIVS`) — stops re-escalation via a setuid-root exec;
3. for a root exec, drop `CAP_SYS_ADMIN` from the bounding set (`PR_CAPBSET_DROP`) — a root exec is otherwise granted the whole bounding set;
4. verify from `/proc/self/status` that the ambient set is empty, `no_new_privs` is set, and (root) the bounding set excludes `CAP_SYS_ADMIN`. **If verification fails, the server is never executed (`_exit(126)`) — fail closed.**

The verification logic is a pure function (`_confinement_gap`) covered by unit tests for every gap.

**Startup refusal.** The capability probe (`check_pid_namespace_capability`, run before any server starts) now also exercises the drop in a throwaway child. A kernel that cannot drop `CAP_SYS_ADMIN` is refused up front, so the fail-closed `_exit(126)` is a backstop, not the normal path. [measured] the probe reports available on this kernel.

**What the fix deliberately leaves:** a root server keeps root's other capabilities (dropping them all would break legitimate root servers and still would not make a sandbox); the shared namespaces, environment, cwd and resource limits are unchanged (§5).

**Teardown race fixed in passing.** While running the full suite under load, one clean shutdown reported exit 137: the helper SIGKILLed init on control-pipe close in the microsecond window between the server exiting cleanly and init reporting status 0. The helper now gives init a bounded grace (`_END_GRACE_SECONDS`, 1 s, well inside the 5 s end deadline) to exit on its own before forcing it; a clean exit registers in ~1 ms, and only a server that refuses to exit waits the grace. Pinned by `test_a_clean_server_exit_reports_its_status_not_a_kill` (repeated). This preserves Shadow byte-identity and the "end confirmed before probe" guarantee (end still only returns `scope_ended` after the namespace is empty).

## 5. Limits that remain (not fixed — would need a different mechanism)

These are **[assumption]** / architectural and are documented so the contract cannot drift into claiming isolation:

- **Filesystem:** the server shares the relay's mount namespace. It reads and writes the same files the relay's user can. A root server can read `/etc/shadow` [measured]. Confinement would need a mount namespace + pivot/bind policy, or a container.
- **Network:** shared network namespace; the server reaches whatever the host can.
- **Resources:** no cgroup and no tightened rlimit; a server can spawn up to the inherited `RLIMIT_NPROC` (64298 here) and use unlimited CPU/memory [measured]. Confinement would need a cgroup with `pids.max`/`memory.max`/CPU limits.
- **Environment & cwd:** the server inherits the relay's full environment (secrets included) and working directory. Confinement would need the relay to scrub the environment and set a cwd before launching.
- **Syscalls:** no seccomp filter. `no_new_privs` is set (a prerequisite for an unprivileged seccomp filter) but no filter is installed.

A server that needs `CAP_SYS_ADMIN` to function is **incompatible** with the scope (it is now denied); such a server should not be reconciled.

## 6. Adversarial tests

`tests/adapters/mcp/test_executor_scope_security.py` (14 tests) and additions to `tests/adapters/mcp/test_executor_scope.py`. All bounded; no destructive host operations.

| Area | Test | Result |
|---|---|---|
| Verification logic (pure) | `test_confinement_gap_*` (4), `test_an_unconfined_process_fails_verification` | every gap flagged [test] |
| Fail closed | `test_confine_propagates_a_verification_gap_so_the_server_never_execs` | a simulated gap → reason returned → server child exits 126 [test] |
| Capability drop, root | `test_a_root_scoped_server_never_holds_cap_sys_admin` | no `CAP_SYS_ADMIN` in Eff/Prm/Bnd; `no_new_privs`=1 [test] |
| Capability not live | `test_cap_sys_admin_is_not_live_in_a_scoped_server` | `unshare(CLONE_NEWNS)` → EPERM [test] |
| No amplification, non-root | `test_enabling_the_scope_does_not_amplify_a_nonroot_servers_privilege` | baseline EPERM; scoped EPERM; zero caps [test] |
| Lifecycle interference | `test_a_scoped_server_cannot_kill_its_own_init` | SIGKILL/SIGTERM to PID 1 ignored by the kernel; scope still ends from outside [test] |
| Inherited fds | `test_a_scoped_server_holds_only_its_stdio` | no pipe/socket fd ≥3 [test] |
| Filesystem limit (honest) | `test_the_scope_shares_the_host_mount_namespace` | shares the relay's mount ns; only PID ns is new [test] |
| Resource limit (honest) | `test_the_scope_imposes_no_resource_limit` | server inherits the relay's rlimits unchanged [test] |
| Startup refusal incl. the drop | `test_the_capability_check_includes_the_privilege_drop` | capability available only when the drop works [test] |
| Clean-exit teardown race | `test_a_clean_server_exit_reports_its_status_not_a_kill` | clean exit reports its status, never 137 (repeated ×5 × 2 codes) [test] |

Preserved invariants, re-run: the Week 4A scope lifecycle, CLI, reconciliation-lifecycle and audit suites, and the Shadow byte-identity test (§7). No-replay, exact correlation, and "no probe until the scope is confirmed ended" are unchanged — the fix touches only the server child's own credentials and the teardown grace.

## 7. Quality gates

[measured] on `executor_scope.py` = `24f3658f19935be6`:

- **Full test suite:** see `week4b/gates/pytest.txt` (re-run after the teardown-race fix).
- **Repeated scope suites:** `test_executor_scope.py` + security + CLI, run ×2 → 50 passed each.
- **Clean-exit regression:** ×3 → 2 passed each.
- **Lint / format:** `ruff check` and `ruff format --check` clean on `src tests examples/mcp/stdio_proxy.py benchmarks/domains/mcp_server_filesystem benchmarks/runtime`.
- **Types:** `mypy src` clean; `mypy --strict` clean on the executor, the relay, and the scope/security tests.
- **Prior evidence:** the Week 4A manifest and the Week 1–3 verifier re-verified unchanged (§8).

## 8. Evidence index and historical preservation

- `week4b/baseline/` — HEAD `b6f7ba0f`, the Week 4A working-tree snapshot and its `tree.sha256` (the starting point, frozen).
- `week4b/phase2/inheritance-before.txt` + probe fixtures — before-fix inheritance.
- `week4b/phase3/audit-root.json`, `audit-nobody.json`, `code.sha256` — after-fix, reproducible.
- `week4b/gates/` — pytest, junit, checks.
- `week4b/EVIDENCE_SHA256.txt` — manifest of the above.

**Historical evidence unchanged:** the Week 4A manifest (`/root/stateguard-captures/week4a/EVIDENCE_SHA256.txt`) and the Weeks 1–3 verifier are re-run and pass; no file under them is modified. The Week 4A reports are not rewritten; the contract's §4.4.4 Privilege subsection is updated in place (the contract is a living document, not evidence) with a dated Week 4B note, and the Week 4A report keeps a short dated pointer to this audit.

## 9. Residual risks and deployment implications

- **Run the server as non-root where possible.** The scope then yields a server with zero capabilities. A root server keeps root's other capabilities and full host access — the scope does not contain it.
- **The scope is not a security sandbox.** Do not deploy it as the sole isolation for a hostile server. For real isolation, run the whole relay inside a container/VM that provides filesystem, network and resource limits; the scope then adds the lifecycle boundary and `CAP_SYS_ADMIN` denial within it.
- **Secrets in the relay's environment reach the server.** Launch the relay with a minimal environment if the server is untrusted.
- **Experimental, Linux + `CAP_SYS_ADMIN` only**, as in Week 4A; unchanged.

## 10. Next task

Decide whether the scope should additionally **scrub the environment, set a neutral working directory, and attach a cgroup with `pids.max`/`memory.max`/CPU limits** before launching the server — the smallest set of changes that would move it from "lifecycle + capability denial" toward a resource boundary — and whether that belongs in StateGuard at all or in the container the relay should run inside. This decision gates (and may replace) the deferred container compatibility matrix.
