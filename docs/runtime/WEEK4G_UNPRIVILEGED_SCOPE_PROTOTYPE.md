# Week 4G — unprivileged executor-scope integration prototype

**Date:** 2026-10-18. **State:** working tree, uncommitted and unpushed. **HEAD:** `b6f7ba0f`.
**Change:** architecture B (unprivileged user + PID namespace) integrated into the existing executor scope as an **explicit, opt-in, experimental** selection — `--executor-scope pid-namespace-unprivileged` / `PidNamespaceScope(..., unprivileged=True)`. Architecture A (`pid-namespace`, the current default) is unchanged.
**History:** 4A–4F (`WEEK4_CLI_EXECUTOR_SCOPE.md` … `WEEK4F_EXECUTOR_SCOPE_ARCHITECTURE_DECISION.md`); contract `RECONCILIATION_CONTRACT_V2.md` §4.4.
**This is a privilege-boundary prototype, not a production-ready security boundary or a general-purpose sandbox.**

Evidence tags: **[verified]** observed by a test/probe on this host; **[verified-in-docker]** observed in a real container here; **[untested]** no environment available; **[analysis]** reasoned from verified facts.

---

## Phase 1 — requirements for Architecture B

B must satisfy every requirement A satisfies (`RECONCILIATION_CONTRACT_V2.md` §4.4; `WEEK4F` R1–R8), differing only in *how* the namespace is created and in the user-identity consequence. Per concern:

1. **User-namespace creation + UID/GID mapping.** `unshare(CLONE_NEWUSER | CLONE_NEWPID)` in one call, then self-map a single id: `setgroups=deny`, `gid_map="0 <gid> 1"`, `uid_map="0 <uid> 1"`. The uid/gid are read **before** the unshare (afterwards the process is unmapped). The server then runs as namespaced root; its host identity is the relay's uid/gid.
2. **PID-namespace creation + init lifecycle.** Unchanged from A: the first fork after the unshare is PID 1; init reaps orphans and exits when the server exits, so the kernel empties the namespace.
3. **Server startup + capability confinement.** Unchanged: `_confine_server` clears ambient caps, sets `no_new_privs`, and — as namespaced root holds `CAP_SETPCAP` — drops `CAP_SYS_ADMIN` from the bounding set, verified from `/proc`; fail-closed (`_exit(126)`) if unconfirmable.
4. **Minimal + configured environment allowlist.** Unchanged: baseline `PATH` + `--env`/`--env-passthrough`, delivered over a private pipe, never argv/helper-env/audit.
5. **Working-directory validation.** Unchanged: neutral `/` or a pre-validated `--server-cwd`; `chdir` failure is fail-closed (`_exit(125)`).
6. **Detached / `setsid` descendants.** Unchanged: all confined to the PID namespace; killed with it.
7. **Namespace teardown + confirmation.** Unchanged: control-pipe EOF, pidfd teardown with a 1 s end-grace, `/proc` emptiness scan by both helper and relay.
8. **Wedged helpers / failed creation / uncertain teardown.** Fail-closed: a failed `unshare` or mapping → `ScopeError`, nothing started; a wedged/killed helper or non-empty scan → `scope_unconfirmed`.
9. **Reconciliation eligibility.** Unchanged: only `scope_ended` (confirmed empty) permits a probe; `scope_unconfirmed` never does.
10. **Shadow / passthrough / no-replay / conservative results.** Unchanged: these are architecture-independent relay logic; B only changes namespace creation. `executionState`/`attribution`/`durability`/`operationOutcome` stay unknown; `treatAsSuccess` is false; an observation never implies attribution, causation, durability, or confirmed success.

**The one semantic change:** B **remaps the server's user identity** to the relay's uid/gid. A file the server writes on a shared mount is owned by that host uid, not root. A is unchanged (server keeps its host identity).

---

## Phase 2 — seccomp feasibility [verified-in-docker]

Evidence: `/root/stateguard-captures/week4g/phase2/` (`seccomp-matrix.txt`, `seccomp-narrow-candidate.json`, `README.txt`). Image `python:3.11-slim`, `--user 65534:65534`, **no** `--cap-add`.

Docker's default profile (`defaultAction: SCMP_ACT_ERRNO`) gates `clone/clone3/mount/setns/unshare` behind `includes.caps=[CAP_SYS_ADMIN]`; without the capability `unshare` falls through to deny. So B is **refused (EPERM)** under the default profile — fail-closed.

| Profile (no CAP_SYS_ADMIN) | `unshare(NEWUSER\|NEWPID)` | `keyctl` (discriminator) | Verdict |
|---|---|---|---|
| Docker **default** | Operation not permitted | Operation not permitted | B refused, fail-closed |
| `seccomp=unconfined` | OK | Invalid argument (reaches kernel) | works, but removes **all** ~44 blocked syscalls |
| **Narrow candidate** (default + `unshare`/`clone3`) | OK | Operation not permitted | works **and** keyctl/mount/setns/… stay blocked |

- A **narrow, operator-supplied** profile (default + `unshare`/`clone3`) is **feasible** and demonstrably tighter than `unconfined` (keyctl stays blocked under narrow, reachable under unconfined).
- It is applied by the **deployment operator** (`docker run --security-opt seccomp=profile.json` or equivalent). **StateGuard does not modify host security policy and does not ship this as a default.**
- When the operation is denied, B **fails closed** — the scope never starts, the contract is not weakened.
- `seccomp=unconfined` is **not** recommended as the default; the narrow profile is the recommended path where B is used.
- Caveats: "narrow" = default + `unshare`/`clone3`, not a from-scratch minimal allowlist; `unshare` is allowed unconditionally (flag-bitmask arg-filtering is coarse and was not required to show feasibility); verified on Docker only ([untested] elsewhere). If no narrow profile can be applied in a target runtime, that is a **deployment blocker** for B there — not a reason to relax the contract.

---

## Phase 3 — the integration (smallest maintainable change)

Files changed: `src/stateguard/adapters/mcp/executor_scope.py`, `examples/mcp/stdio_proxy.py`. New tests: `tests/adapters/mcp/test_executor_scope_unprivileged.py`, additions to `tests/examples/test_cli_executor_scope.py`.

- **`executor_scope.py`:** added `_CLONE_NEWUSER`; `_map_userns_identity(uid, gid)` (fail-closed id self-map); `_create_namespace(libc, *, unprivileged)` doing A's `unshare(CLONE_NEWPID)` or B's `unshare(CLONE_NEWUSER|CLONE_NEWPID)`+map. `PidNamespaceScope.__init__` gained `unprivileged: bool = False` (adds a valued `--userns` flag to the helper argv only for B; **A's argv is byte-for-byte unchanged**). `_serve` creates the namespace via `_create_namespace`. `check_pid_namespace_capability(*, unprivileged=False)` dispatches `probe` vs `probe-userns`; a new `_probe_userns` mirrors `_probe` but creates the user+PID namespace unprivileged and confines as namespaced root. **A's `_probe` is untouched.**
- **`stdio_proxy.py`:** `--executor-scope` gained the choice `pid-namespace-unprivileged`; `scoped`/`unprivileged` derived from it; the reconcile-requirement, env-flag, capability-probe, scope construction, error messages, and the `relay_started` audit label (`executor_scope` = the exact selected name) all thread the selection through. Reconcile blocks are satisfied by **either** scope.

Invariants honoured:
- **A unchanged** [verified]: A's code paths, argv, and probe are identical; A's full existing suite passes.
- **No silent fallback** [verified]: the architecture is chosen explicitly and never substituted; B's helper argv carries `--userns`, A's does not (`test_b_helper_argv_carries_the_userns_flag_and_a_does_not`); a failed B start raises, it never becomes A, direct-child reaping, or unscoped execution.
- **Fail-closed** [verified]: failed `unshare`/mapping → `ScopeError`/exit 3; wedged helper → `scope_unconfirmed`; disabled userns / denied unshare → capability refused before any server starts.
- **Server cannot keep `CAP_SYS_ADMIN`** [verified]: dropped from the bounding set even as namespaced root (`CapBnd` bit 21 = 0, `CapAmb=0`, `NoNewPrivs=1`).
- **No secrets in argv/logs** [verified]: unchanged env-over-pipe path; `--userns` carries no secret.
- **Unambiguous API/CLI** [verified]: three explicit `--executor-scope` choices; an unknown one is rejected by argparse.

**Not exposed as a production-ready security boundary:** help text, the module docstring, and this document all state B is experimental, remaps identity, and is not a sandbox.

---

## Phase 4 — lifecycle & safety invariants, against the integrated implementation

Evidence: `/root/stateguard-captures/week4g/phase4/` + the test suites. "Integrated" = the real `PidNamespaceScope`/CLI, not the 4F standalone prototype.

| Invariant | Result | Where |
|---|---|---|
| Normal startup + clean teardown → `scope_ended` | **[verified]** rc preserved, members_after=0 | `B_integrated_normal.json`; `test_a_generation_runs_in_its_own_namespace…` |
| Detached `setsid` descendant cannot outlive scope | **[verified]** killed with ns; `descendant_acted=false` | `B_integrated_normal.json`; `test_a_detached_descendant…` |
| Namespace-empty confirmation | **[verified]** helper + relay scans agree | `test_a_member_still_visible…` (A), B lifecycle tests |
| Wedged helper + teardown timeout → `scope_unconfirmed` | **[verified]** fail-closed, bounded | `B_integrated_wedged_helper.json`; `test_a_stopped_helper…` |
| Namespace creation denied (default seccomp) | **[verified-in-docker]** EPERM, refused | Phase 2 `seccomp-matrix.txt` |
| User-namespace support disabled | **[analysis]**/[verified-in-docker] default seccomp denies `unshare` → capability refused; same code path as a disabled-userns sysctl | Phase 2; `_probe_userns` refusal text |
| UID/GID mapping failure | **[verified]** `_map_userns_identity` returns a reason → fail-closed (unit path) | code + `test_…` fail-closed asserts |
| Server startup failure | **[verified]** exit 127 inside the scope (shared with A) | A `test_a_program_that_cannot_be_executed…` (shared helper) |
| Environment allowlist + secret non-disclosure | **[verified]** policy env only; relay secret not inherited | `test_env_allowlist_reaches_the_server_and_no_other` |
| Working-directory behaviour + failure | **[verified]** neutral `/`; chdir failure fail-closed (shared with A) | shared `_init` path |
| Server capability confinement | **[verified]** `CAP_SYS_ADMIN` dropped as namespaced root | `test_the_server_cannot_keep_cap_sys_admin`; `B_integrated_normal.json` |
| Reconciliation only after required termination confirmation | **[verified]** `scope_ended` only; `scope_unconfirmed` blocks | `test_b_reconcile_end_to_end…`, `test_b_scope_that_cannot_be_confirmed_empty…` |
| No original-call replay on unknown outcome | **[verified]** move executed once, never resent | `test_b_reconcile_end_to_end…` (`_calls` = one move) |
| Shadow / passthrough unchanged | **[verified]** architecture-independent; A's shadow byte-identity suite unchanged | existing suite |
| Concurrent calls + teardown races | **[verified]** clean-exit status not 137, ×5 repeats | `test_a_clean_server_exit_reports_its_status_not_a_kill` |
| Repeated starts/stops, no orphaned descendants | **[verified]** 4 generations, distinct identity, ns empty each time | `test_repeated_starts_and_stops…` |
| **No host `CAP_SYS_ADMIN`** (the point of B) | **[verified]** as uid 65534 A refused, B works | `capability_contrast.txt`; `test_b_works_unprivileged_where_a_is_refused`, `test_b_full_lifecycle_unprivileged…` |

**Observed state vs attribution:** every B reconciliation leaves `executionState`/`attribution`/`durability`/`operationOutcome` unknown and `treatAsSuccess` false, exactly as A — an observed postcondition is never read as proof the original call executed, caused the state, or is durable.

---

## Phase 5 — A vs B

| Dimension | A (`pid-namespace`, default) | B (`pid-namespace-unprivileged`, opt-in) |
|---|---|---|
| Host capability on relay | **`CAP_SYS_ADMIN`** (near-root) | **none** [verified] |
| Seccomp requirement | works under Docker default **with** `--cap-add SYS_ADMIN` [verified-in-docker, 4E] | **refused** under Docker default; needs a narrow operator profile (default + `unshare`) or `unconfined` [verified-in-docker] |
| Lifecycle / teardown | init-PID-1, pidfd teardown, `/proc` scan | **identical** [verified] |
| Fail-closed behaviour | wedged helper / denied op → `scope_unconfirmed` / refused | **identical** [verified] |
| Reconciliation safety | sound; `scope_ended` only | **identical** [verified] |
| UID/GID + filesystem | server keeps host identity; files owned normally | **server→namespaced root = relay's host uid; files owned by that uid** [verified] |
| Server capabilities | host-root server has real host caps **minus `CAP_SYS_ADMIN`** | namespaced-root server has **namespaced** caps (no host authority) minus `CAP_SYS_ADMIN`; can re-enter nested userns → caps there are still namespaced [analysis] |
| Packaging / installation | install in `site-packages` (empty-env helper, 4E §3.2) | **same** |
| Deployment burden | operator adds one capability | operator enables unprivileged userns **and** applies a narrow seccomp profile; accepts uid remap |
| Remaining boundary limits | not a sandbox: shared mount/net/IPC; no resource containment | **same**, plus: server is root *within* its userns; narrow seccomp unverified outside Docker |

**B is not "safer overall" merely for lacking host `CAP_SYS_ADMIN`.** It removes a broad host grant from the relay (a real reduction in the relay's blast radius), but it (a) remaps the server's identity, changing file ownership and anything keyed on uid; (b) runs the server as root *within* its user namespace, which can re-enter nested user namespaces (namespaced caps only, no host authority, but worth stating); (c) shifts the requirement from a capability to a seccomp-policy + sysctl prerequisite that is itself a deployment burden and, outside Docker, unverified. The complete trade-off is: **less host privilege on the relay, in exchange for an identity remap and a different (narrower-but-unproven-elsewhere) deployment prerequisite.** Neither is a sandbox.

---

## Validation [verified]

| Gate | Result |
|---|---|
| New B scope suite (`test_executor_scope_unprivileged.py`) | 17 passed |
| New B CLI tests (`test_cli_executor_scope.py`) | 3 added, pass |
| Focused scope(A+B)/security/env/CLI/reconciliation ×1 | **104 passed** |
| Full suite | **6071 passed, 1 failed** — the one failure is unrelated to 4G (see below) |
| `ruff check` / `ruff format --check` | clean (231 files) |
| `mypy src` | clean (67 files) |
| `mypy --strict` (executor, relay, scope/env/CLI tests + new B tests) | clean (7 files) |
| Architecture A behaviour | unchanged (A suite green; A argv/probe byte-identical) |
| Prior manifests (weeks 1–3, 4A–4F) | re-verified, unchanged |

**The one full-suite failure is not a Week 4G regression.**
`tests/adapters/jsonschema/test_2020_12_conformance.py::test_differential_unique_items`
is a Hypothesis differential property test (`max_examples=500`) over the JSON Schema
adapter — it loads **neither** `executor_scope` nor `stdio_proxy`, and 4G's diff is
confined to those two modules plus test files. Hypothesis **discovered a new
counterexample this run** (`[[[True,True,True],None],[[True,True,1],None],[[True,True,True],None]]`):
the reference `jsonschema` validator returns `is_valid=True` for a list whose
elements 0 and 2 are literally identical, while StateGuard flags the duplicate. It
is a **pre-existing latent `uniqueItems` divergence** (nested bool/int equality),
surfaced by the randomized search — 4D ran the byte-identical adapter at "6052
passed, 0 failed". Per this package's brief it is **reported, not masked**: it was
not fixed (out of 4G scope; fixing the adapter would touch contract behaviour) and
the counterexample was not deleted to force green. Evidence + full analysis:
`/root/stateguard-captures/week4g/gates/README.txt`. All executor-scope,
reconciliation, Shadow, security, environment, and CLI gates pass.

---

## Recommendation

**Continue hardening B as the path to a non-privileged executor scope, keeping A as the default experimental option — B stays opt-in and experimental, not promoted, until the open items below are closed.**

B now demonstrably delivers A's lifecycle and reconciliation guarantees **with no host `CAP_SYS_ADMIN`**, inside the real implementation, behind an explicit opt-in that never downgrades A and never silently falls back. That is the privilege reduction 4F identified as worth pursuing. It is not yet a production boundary:

What would change the recommendation / remains to close before any non-experimental claim:
1. **Decide the uid-remap semantics** for target servers (accept 65534-owned files, or provide an id mapping that preserves needed identity). This is the gating product question.
2. **Prove the narrow seccomp profile in ≥1 non-Docker runtime** (rootless Podman, a systemd user service) — currently [untested] there; and settle whether StateGuard ships the profile as documentation only (it must not install host policy).
3. **A fleet check** that unprivileged user namespaces are enabled widely enough in target environments to rely on; where they are not, B is a deployment blocker and A (or an external supervisor, 4F option C) remains.
4. Keep `treatAsSuccess` false and the "not a sandbox / not production-ready" framing until 1–3 are resolved.

If the uid remap proves unacceptable for target servers and no narrow seccomp profile can be applied in the target runtime, fall back to retaining A (4F option 1) or delegating to an external supervisor (4F option C). Stopping the feature line is not warranted: a working unprivileged boundary now exists in the implementation.

### Standalone (4F) vs integrated (4G) vs unverified

- **4F standalone prototype demonstrated:** the user+PID-namespace lifecycle in a throwaway script (`userns_scope.py`) — creation, mapping, detached-descendant kill, emptiness, fail-closed on a wedged helper.
- **4G integrated implementation now demonstrates:** the same, inside the real `PidNamespaceScope`/CLI, wired to the real reconciliation contract — capability probe, env/cwd/capability controls, `scope_ended`/`scope_unconfirmed` gate, end-to-end CLI reconciliation with no replay and the correct audit label, A unchanged, no silent fallback, under the full gate suite; plus the seccomp feasibility matrix (a narrow operator profile).
- **Remains unverified:** B under non-Docker runtimes; a minimal from-scratch seccomp profile; B behind a non-root relay in a real production deployment (shown here as uid 65534, representative but not a deployment); the uid-remap product decision; wide availability of unprivileged userns in target fleets.
