# Week 4D — executor-scope release gate

**Date:** 2026-10-18. **State:** working tree, uncommitted and unpushed. **HEAD:** `b6f7ba0f`.
**Feature:** `examples/mcp/stdio_proxy.py --executor-scope pid-namespace` (CLI reconciliation behind a PID-namespace executor scope).
**Companion:** `WEEK4D_DEPLOYMENT_GUIDE.md`. **History:** 4A (`WEEK4_CLI_EXECUTOR_SCOPE.md`), 4B (`WEEK4B_EXECUTOR_SECURITY_AUDIT.md`), 4C (`WEEK4C_ENVIRONMENT_AND_RESOURCE_BOUNDARY.md`), contract `RECONCILIATION_CONTRACT_V2.md` §4.4.

## 1. Verdict

**Experimental release is justified — opt-in, Linux with `CAP_SYS_ADMIN` only, and only when deployed inside a runtime that supplies filesystem/network/resource isolation.** The feature is off by default, refuses rather than degrades when its preconditions are not met, and never weakens the reconciliation contract. It is **not** a sandbox and must not be deployed as the sole isolation for a hostile server.

## 2. What the feature guarantees [verified by test/probe in the tested environment]

- **Lifecycle confinement:** every process of a server generation runs in one PID namespace; the relay confirms the namespace empty before any reconciliation probe.
- **No `CAP_SYS_ADMIN` for the server:** ambient cleared, `no_new_privs` set, bounding-set drop for a root exec, verified from `/proc`; **fail-closed** if unconfirmable, and refused at startup on a kernel where the drop does not work. A non-root server runs with **zero** capabilities.
- **No inherited relay environment by default (scope mode):** minimal baseline (`PATH`) plus only `--env` / `--env-passthrough`. Values travel over a private pipe — never argv/`/proc/cmdline`, never the helper's (empty) environment, never the audit log (names only).
- **Deterministic working directory:** neutral `/` by default, or an explicit, pre-validated `--server-cwd`.
- **Fail-closed configuration:** malformed/duplicate/conflicting/unset env, bad cwd, missing program, missing capability, or env flags without a scope → exit 2, nothing started.
- **Bounded cleanup / no orphans:** relay or helper death empties the namespace (control-pipe EOF, `PR_SET_PDEATHSIG`); a clean server exit reports its status, not a teardown-race kill.
- **Reconciliation contract intact:** no replay of unknown invocations; no probe before confirmed termination; `executionState`/`attribution`/`durability`/`operationOutcome` stay unknown and `treatAsSuccess` is false; **Shadow byte-identical** to the no-relay baseline; no-scope behaviour unchanged.

## 3. What it explicitly does NOT guarantee

- **Not a sandbox.** Shared host **mount, network, IPC, user** namespaces: the server can read/write host files its user can reach and use the host network.
- **No resource containment.** No cgroup, no added rlimit; a server can fork to the per-UID limit and consume host CPU/memory. (Resource-limit decision: **leave to deployment**, §9 of the deployment guide.)
- **No protection against a root server's other capabilities.** A root relay yields a server with every root capability except `CAP_SYS_ADMIN`.
- **No containment of external side effects** (remote APIs, databases, queues, other local processes).
- **Environment minimisation reduces accidental exposure only** — a secret passed explicitly is still held by the server.

## 4. Environments actually tested

- **[verified]** Linux 6.18.44 (Firecracker microVM), Python 3.11.17, root (uid 0) with `CAP_SYS_ADMIN`; cgroup v2 **not mounted**.
- **[verified, partial]** the capability check and the no-amplification path under uid 65534 with ambient `CAP_SYS_ADMIN` (not a full non-root production run).
- **[not tested]** Docker, Podman, Kubernetes, systemd units, other kernels/distros, seccomp-restricted containers, non-root end-to-end. All deployment examples in the guide are marked unverified with a verification procedure.

## 5. Required deployment controls (before relying on the feature)

1. Run the relay inside a container or systemd unit that applies **cgroup v2 limits** (`pids.max`, `memory.max`, `cpu.max`) and **`ulimit -n`** to the whole relay+server tree.
2. Provide **filesystem isolation** (restricted/read-only root with only needed data) and **network policy** at that layer.
3. Grant the relay **only `CAP_SYS_ADMIN`**; prefer a non-root user with ambient `CAP_SYS_ADMIN`; ensure the runtime's seccomp profile allows `unshare(CLONE_NEWPID)`, `pidfd_open`, `pidfd_send_signal`.
4. Pass server secrets only via `--env`/`--env-passthrough`; launch the relay with a minimal environment.
5. Keep environment values under ~128 KiB each (kernel exec limit).
6. Run the verification procedure in `WEEK4D_DEPLOYMENT_GUIDE.md` §7c in the target environment.

## 6. Known compatibility and security risks

- A server that relied on inheriting environment (credentials, proxy, `HOME`) or the relay's cwd must now be configured explicitly in scope mode (behaviour change, scope mode only; no-scope unchanged).
- A server needing `CAP_SYS_ADMIN` is incompatible with the scope (it is denied).
- `RLIMIT_AS` is **not** used precisely because it breaks common runtimes; do not add it casually (deployment-guide §9).
- An environment value past the kernel exec limit makes the server exit 127 (fail-closed but opaque).
- The documented routing exposure (a client reusing a pending relay-owned id) is unchanged; at most one client answer.

## 7. Exact verification commands

Run from the repository root in the tested environment.

```
# Full suite
.venv/bin/python -m pytest -o addopts="" -q

# Focused security / scope / environment-policy suites
.venv/bin/python -m pytest -o addopts="" -q \
  tests/adapters/mcp/test_executor_scope.py \
  tests/adapters/mcp/test_executor_scope_security.py \
  tests/examples/test_cli_executor_scope.py \
  tests/examples/test_cli_environment_policy.py

# Repeated combined invariants (race/concurrency)
for i in 1 2 3; do .venv/bin/python -m pytest -o addopts="" -q -p no:randomly \
  tests/adapters/mcp/test_executor_scope.py \
  tests/adapters/mcp/test_executor_scope_security.py \
  tests/examples/test_cli_executor_scope.py \
  tests/examples/test_cli_environment_policy.py \
  tests/examples/test_reconciliation_lifecycle.py ; done

# Lint / format / types
.venv/bin/ruff check src tests examples/mcp/stdio_proxy.py benchmarks/domains/mcp_server_filesystem benchmarks/runtime
.venv/bin/ruff format --check src tests examples/mcp/stdio_proxy.py benchmarks/domains/mcp_server_filesystem benchmarks/runtime
.venv/bin/mypy src
MYPYPATH=benchmarks:benchmarks/domains/mcp_server_filesystem .venv/bin/mypy --strict \
  src/stateguard/adapters/mcp/executor_scope.py examples/mcp/stdio_proxy.py \
  tests/adapters/mcp/test_executor_scope.py tests/adapters/mcp/test_executor_scope_security.py \
  tests/examples/test_cli_executor_scope.py tests/examples/test_cli_environment_policy.py

# Prior-evidence manifests
/root/stateguard-captures/week4a/verify_prior.sh          # weeks 1-3
(cd /root/stateguard-captures/week4a && sha256sum -c --quiet EVIDENCE_SHA256.txt)
(cd /root/stateguard-captures/week4b && sha256sum -c --quiet EVIDENCE_SHA256.txt)
(cd /root/stateguard-captures/week4c && sha256sum -c --quiet EVIDENCE_SHA256.txt)
(cd /root/stateguard-captures/week4d && sha256sum -c --quiet EVIDENCE_SHA256.txt)
```

## 8. Gate results (2026-10-18, this package) [verified]

| Gate | Result |
|---|---|
| Full suite | **6052 passed**, 0 failed |
| Focused security/scope/env suites | 70 passed |
| Repeated combined invariants ×3 | 84 passed each |
| `ruff check` / `ruff format --check` | clean |
| `mypy src` | clean (67 files) |
| `mypy --strict` (executor, relay, scope/env tests) | clean |
| Week 4A / 4B / 4C manifests | re-verified, unchanged |
| Weeks 1–3 verifier | 39/39 ok |
| New-defect fixes this package | env-send fail-closed (`ScopeError`), with regression tests |

## 9. Decision

**Ship as experimental, opt-in**, with the deployment controls in §5 mandatory and the "not a sandbox" statement (§3) prominent. Resource limits remain the deployment's responsibility (no code added). The container/runtime compatibility matrix remains the next task and must be completed before any non-experimental claim.

---

## Appended 2026-10-18 (Week 4E) — runtime-compatibility decision note

This note is append-only; nothing above is changed. Full evidence: `docs/runtime/WEEK4E_RUNTIME_COMPATIBILITY.md`.

- **Docker is now a [verified-in-docker] environment.** Docker 29.8.2 on kernel 6.18.44 (cgroup v1): default config → **fail-closed** (capability refused, no server started); `--cap-add SYS_ADMIN` (default seccomp) → the scope works fully — detached descendant killed, namespace confirmed empty, `CAP_SYS_ADMIN` dropped from the server, environment allowlist holds (planted secret not leaked), neutral cwd, and a reconciliation ran end-to-end with `treatAsSuccess: false` and no replay. `seccomp=unconfined` is **not** required.
- **Still untested:** Podman (not installed), systemd units (systemd is not PID 1 here), Kubernetes (no cluster). The §7 example configs in the deployment guide remain unverified for those runtimes.
- **Privilege decision:** retain the `CAP_SYS_ADMIN` requirement with mandated isolated deployment (option A), relying on the existing fail-closed behaviour (option C). An unprivileged-user-namespace redesign (option B) is feasible in principle on the tested kernel but remaps the server's uid and is not demonstrated end-to-end; it is recorded as a separate future proposal, not adopted.
- **Packaging requirement (new, must be in release notes):** install StateGuard as a normal package in `site-packages`. The scope helper runs with an empty environment, so an editable/`PYTHONPATH`-only install is not importable by the helper and the scope fails closed with `no 'started' report in time`.
- **Release posture unchanged:** experimental, opt-in, Linux + `CAP_SYS_ADMIN`, inside an isolated deployment. Docker with one added capability is the first verified such deployment.
