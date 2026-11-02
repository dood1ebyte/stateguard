# Week 4E — runtime compatibility and privilege feasibility

**Date:** 2026-10-18. **State:** working tree, uncommitted and unpushed. **HEAD:** `b6f7ba0f`.
**Scope:** can `stdio_proxy.py --executor-scope pid-namespace` be deployed safely and practically in real runtimes, given its Linux + `CAP_SYS_ADMIN` requirement? Companion: `WEEK4D_DEPLOYMENT_GUIDE.md`, `WEEK4D_RELEASE_GATE.md`. Background: 4A–4D, contract `RECONCILIATION_CONTRACT_V2.md` §4.4.

Tags: **[verified]** observed by a test/probe here; **[verified-in-docker]** observed in a real container here; **[untested]** no environment available; **[feasible-not-demonstrated]** mechanism shown to work but full guarantees not proven.

## 1. Recommendation

**Retain the feature as experimental (option A + C): keep the `CAP_SYS_ADMIN` requirement, require an isolated deployment, and rely on the existing fail-closed behaviour where the boundary cannot be established.** The feature works correctly in Docker with one explicit added capability and fails closed under Docker's default configuration. **Do not redesign now.** An unprivileged-user-namespace redesign (option B) is **feasible in principle on the tested kernel** and would remove the host `CAP_SYS_ADMIN` requirement, but it remaps the server's uid and its full lifecycle/teardown was not demonstrated; it is recorded as a **separate future proposal** (§6), not undertaken here.

## 2. Capability operation trace (Phase 1)

- The **only** operation requiring `CAP_SYS_ADMIN` is `unshare(CLONE_NEWPID)` — in the startup capability probe and in the helper (`_serve`). `pidfd_open`/`pidfd_send_signal` need no capability; the server's capability drop uses `CAP_SETPCAP` (held by root) and `prctl`.
- Checked by `check_pid_namespace_capability()` in `main()` **before any server starts**; refuses (exit 2), never downgrades.
- Who holds `CAP_SYS_ADMIN`: relay → helper (does the unshare) → init (inherits, unused) → **server: dropped before `execvp`** [verified, 4B]. So the relay and helper hold a near-root capability; the server never does.
- Full note: `/root/stateguard-captures/week4e/phase1/capability-op-trace.txt`.

## 3. Runtime compatibility matrix (Phase 2)

Tested on: kernel **6.18.44** (Firecracker microVM), **Docker 29.8.2**, cgroup **v1** (the host has no cgroup v2). Evidence: `/root/stateguard-captures/week4e/phase2/docker-matrix.txt`, harness `week4e/harness/`.

| Environment | Startup | Scope lifecycle | Detached descendant / emptiness | Env allowlist / cwd | Cap drop | Reconcile / no-replay / v2 | Status |
|---|---|---|---|---|---|---|---|
| **Docker, default config** (no `--cap-add`, default seccomp) | **fail-closed** — capability refused, no server started | — | — | — | — | — | **[verified-in-docker]** safe refusal |
| **Docker `--cap-add SYS_ADMIN`** (default seccomp) | succeeds | `scope_ended`, established, rc 137 | 4 members before end → **namespace empty after** | server env=2 vars, **secret not leaked**, cwd `/` | server `cap_sys_admin_live=false`, `NoNewPrivs=1` | e2e: `observed_satisfied`, `executionState unknown`, `treatAsSuccess false`, move executed once (no replay) | **[verified-in-docker]** works |
| **Docker `--cap-add SYS_ADMIN --security-opt seccomp=unconfined`** | succeeds | `scope_ended`, empty after | — | — | — | — | **[verified-in-docker]** (default seccomp already suffices; unconfined not required) |
| rootless Podman | — | — | — | — | — | — | **[untested]** — Podman not installed |
| rootful Podman | — | — | — | — | — | — | **[untested]** — Podman not installed |
| systemd service | — | — | — | — | — | — | **[untested]** — systemd is not PID 1 here (`System has not been booted with systemd`); no bus |
| Kubernetes | — | — | — | — | — | — | **[untested]** — no cluster available |

**Findings:**
- Docker's **default** profile drops `CAP_SYS_ADMIN` and its seccomp gates `unshare` on it, so the scope **fails closed** — the safe outcome, no server started. [verified-in-docker]
- Docker with **one** added capability (`--cap-add SYS_ADMIN`) works fully: capability check passes, the scope runs, a **detached `setsid` descendant is killed with the namespace** (confirmed empty), the server has **no `CAP_SYS_ADMIN`**, the environment is the minimal allowlist (a planted secret did **not** leak), the working directory is the neutral `/`, and a full reconciliation ran end-to-end with the v2 semantics intact (`treatAsSuccess: false`, the move executed exactly once — **no replay**). [verified-in-docker]
- **Default seccomp is sufficient** with the capability; `seccomp=unconfined` is not required.
- **Resource / filesystem / network isolation inside the container** is the container's, not the scope's: the scope shares the container's mount/network namespaces (it only adds a PID namespace). cgroup v1 limits (pids/memory/cpu) would be applied by `docker run` flags; not separately re-verified here.

### 3.1 Exact commands

```
# image: python:3.11-slim ; stateguard mounted into site-packages so the empty-env helper can import it
docker run --rm -v <src>/stateguard:/usr/local/lib/python3.11/site-packages/stateguard:ro \
  -v <harness>:/harness:ro python:3.11-slim python /harness/driver.py          # D1 default -> fail-closed
docker run --rm --cap-add SYS_ADMIN  <same mounts>  python:3.11-slim python /harness/driver.py   # D2 -> works
docker run --rm --cap-add SYS_ADMIN --security-opt seccomp=unconfined <mounts> ... /harness/driver.py  # D3
# reconciliation e2e: a combined site-packages (venv deps + real stateguard) mounted at the default path
docker run --rm --cap-add SYS_ADMIN -v <staged-site-packages>:/usr/local/lib/python3.11/site-packages:ro \
  -v <repo>:/repo:ro -v <harness>:/harness:ro python:3.11-slim python /harness/recon_client.py
```

### 3.2 A real packaging requirement surfaced by the container test [verified-in-docker]

The scope helper runs with an **empty environment** (the 4C env policy). It must therefore be able to import `stateguard` with **no environment variables** — i.e. the package must be **installed into `site-packages`**, not provided via `PYTHONPATH` or an **editable/dev install** whose finder points at a path that does not exist in the container. The first container reconcile attempt failed (`no 'started' report in time`) precisely because the venv's editable install was not importable in the container with an empty env. This is a deployment constraint, not a scope defect: **install StateGuard as a normal package in the image.**

## 4. Verified guarantees vs assumptions in a container

- **[verified-in-docker]** in Docker `--cap-add SYS_ADMIN`: capability detection, scope start, detached-descendant termination, namespace-emptiness confirmation, `scope_ended`, `CAP_SYS_ADMIN` drop + `no_new_privs`, environment allowlist (no secret leak), neutral cwd, and a full reconciliation with no replay and `treatAsSuccess: false`.
- **[verified-in-docker]** Docker default → fail-closed.
- **[assumption]** cgroup/filesystem/network isolation is supplied by the container runtime (not re-measured per §6 of the deployment guide).
- **Shadow byte-identity, concurrency, the full reconciliation adversarial matrix** are environment-independent relay logic, exhaustively verified by the host suite; only the scope's kernel operations are environment-dependent, and those are now [verified-in-docker]. The full host suite was re-run this package (§7).

## 5. Failures and false-refusal cases

- **No false refusals observed.** Docker default refuses because `CAP_SYS_ADMIN` genuinely is not present — correct. With the capability, no spurious refusal occurred across the runs.
- **Opaque-but-safe:** Docker default's refusal reason is the generic "`unshare(CLONE_NEWPID)`: Operation not permitted … needs `CAP_SYS_ADMIN`", which is accurate.
- **Packaging pitfall (§3.2):** an editable/PYTHONPATH install yields `no 'started' report in time` — a fail-closed but non-obvious error. Documented.

## 6. Privilege feasibility (Phase 3)

The risk: the feature requires the **relay and helper** to hold `CAP_SYS_ADMIN`, which is close to full root (it permits mount, namespace and many other operations). Although the *server's* capabilities are dropped, a relay compromised **before** the drop, or a bug in the relay/helper, runs with `CAP_SYS_ADMIN`. The relay is the process that launches an MCP server, i.e. it is exposed to server-influenced input. This is the central privilege concern.

| Option | Finding | Verdict |
|---|---|---|
| **A. Keep `CAP_SYS_ADMIN` + isolated deployment** | Works today [verified-in-docker, D2]. Mitigate by running the relay **non-root with ambient `CAP_SYS_ADMIN`** (not full root) [verified, 4B] and inside a container that bounds filesystem/network/resources. | **Recommended** |
| **B. Unprivileged user namespaces** | On the tested kernel an unprivileged user (uid 65534, no `CAP_SYS_ADMIN`) **can** create `userns+pidns` (`unshare --user --map-root-user --pid --fork` → euid 0 mapped from host 65534, full caps *within* the userns) [verified]. So the PID-namespace lifecycle could be obtained **without granting the relay host `CAP_SYS_ADMIN`** — a real privilege reduction. **But:** (1) it **remaps the server's uid** (host 65534), a semantic change 4A/4B deliberately avoided; (2) it is **environment-dependent** (unprivileged userns can be disabled by sysctl/distro); (3) the **full scope lifecycle and teardown were not demonstrated** under it. | **[feasible-not-demonstrated]** → separate proposal |
| **C. Keep experimental, unavailable where the boundary cannot be established** | Already the behaviour: fail-closed on Docker default, on kernels without PID namespaces/pidfd, and without the capability. The code uses **only `CLONE_NEWPID`, never `CLONE_NEWUSER`** (0 occurrences) — **no silent fallback** to userns or to process-group/direct-child termination [verified]. | **In effect** |

**Separate future proposal (not undertaken here):** an unprivileged-userns executor scope. It would remove the host `CAP_SYS_ADMIN` requirement (large security win) at the cost of a uid-remapping semantic change, require unprivileged userns to be enabled, and must demonstrate the **complete** lifecycle (init-as-PID1, detached-descendant kill, pidfd teardown, emptiness confirmation, capability drop) and teardown before claiming equivalence. Per the brief, equivalence is **not** claimed until that demonstration exists.

## 7. Gate results (this package) [verified]

| Gate | Result |
|---|---|
| Full suite | see `week4e/gates/pytest.txt` |
| Repeated security/lifecycle/concurrency/shadow ×3 | see `week4e/gates/repeat.txt` |
| ruff check / format | clean |
| mypy src / strict | clean |
| Week 4A/4B/4C/4D manifests + weeks 1–3 | re-verified, unchanged |

No production code changed in Week 4E; the matrix and feasibility work is evidence and documentation only.

## 8. Deployment risks and trade-offs

- Granting `CAP_SYS_ADMIN` to the relay is a broad grant; mitigate with non-root + ambient capability and container isolation, and keep the relay's own attack surface minimal.
- Default-config container runtimes refuse the feature (fail-closed) — operators must consciously add the capability, which is appropriate for an experimental feature.
- The feature still provides **no** filesystem/network/resource isolation itself; that remains the deployment's job (4C/4D).
- The packaging requirement (§3.2) must be in release notes: install as a real package.

## 9. Conditions for any future (non-experimental) release

1. A tested matrix across **≥2 runtimes** (Docker verified; add rootless/rootful Podman and a real systemd unit, and Kubernetes if available) with the §3 checks all passing.
2. A decision on the `CAP_SYS_ADMIN` requirement: either accept it with mandated isolated deployment (A), or complete the option-B unprivileged-userns proposal with a full lifecycle/teardown demonstration.
3. Resource/filesystem/network isolation verified at the deployment layer (4D guide §7c) for each named runtime.
4. Release notes stating the packaging requirement and the "not a sandbox" boundary.
