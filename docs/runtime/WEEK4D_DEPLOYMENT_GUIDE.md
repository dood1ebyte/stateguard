# Week 4D — executor-scope deployment guide

**Date:** 2026-10-18. **State:** working tree, uncommitted.
**Scope:** how to deploy `stdio_proxy.py --executor-scope pid-namespace` safely. Companion: `WEEK4D_RELEASE_GATE.md`. Background: `WEEK4_CLI_EXECUTOR_SCOPE.md` (4A), `WEEK4B_EXECUTOR_SECURITY_AUDIT.md` (4B), `WEEK4C_ENVIRONMENT_AND_RESOURCE_BOUNDARY.md` (4C), contract `RECONCILIATION_CONTRACT_V2.md` §4.4.

Every statement is tagged:
- **[verified]** — observed by a test or probe in the tested environment (§1);
- **[env-dependent]** — behaviour that is tested here but depends on the host/kernel/runtime;
- **[unverified]** — a recommendation not experimentally confirmed in this package.

## 0. What the executor scope is — and is not

The executor scope is **process-lifecycle confinement**: each MCP server generation runs as the only child of an `init` (PID 1) in its own PID namespace, so the relay can confirm every process of that generation has ended before it probes (the termination evidence reconciliation requires). As hardening it also **denies the server `CAP_SYS_ADMIN`** and gives it an **explicit minimal environment** and a **neutral working directory**.

It is **not a sandbox.** [verified] The server shares the host **mount, network, IPC and user namespaces**, and there is **no resource limit** (no cgroup, no added rlimit). A server can read host files its user can reach, open host network connections, and consume CPU/memory/processes up to the host/inherited limits. Filesystem, network and resource isolation must come from the **deployment** (§5–§7).

## 1. Tested environment (the only environment where behaviour is [verified])

| | Value |
|---|---|
| Kernel | Linux 6.18.44 (`-fc-v139`, a Firecracker microVM) |
| Python | 3.11.17 |
| User | root (uid 0), with `CAP_SYS_ADMIN` |
| cgroup v2 | **not mounted** (`/sys/fs/cgroup/cgroup.controllers` absent) |
| Inherited rlimits | `RLIMIT_AS`/`CPU`/`FSIZE` unlimited; `RLIMIT_NOFILE` 20000; `RLIMIT_NPROC` 64298 (per-UID) |

No container runtime (Docker/Podman/Kubernetes) and no systemd unit were tested in this package. Everything about them below is **[unverified]** and carries a verification procedure.

## 2. Linux capabilities and how to minimise them

- The relay needs **`CAP_SYS_ADMIN`** to create the PID namespace (`unshare(CLONE_NEWPID)`). This is the single capability the feature requires. [verified]
- **`CAP_SYS_ADMIN` is dropped before the server runs** (ambient cleared, `no_new_privs` set, bounding-set drop for a root exec), and the drop is verified from `/proc`; if it cannot be confirmed the server never execs (fail closed), and the startup capability check refuses a kernel where the drop does not work. [verified, 4B]
- **Minimise:** grant the relay `CAP_SYS_ADMIN` and nothing else. [unverified] The intended way is to run the relay as a **non-root user with ambient `CAP_SYS_ADMIN`** (so even the relay is not full root); this was exercised for the capability check and the no-amplification test [verified, 4B], but a full non-root production run was not end-to-end tested. Do **not** grant other capabilities to the relay; the server inherits the relay's user and (except `CAP_SYS_ADMIN`) its capabilities, so a root relay yields a root server with every other capability. [verified, 4B]

## 3. Environment allowlisting

- In scope mode the server does **not** inherit the relay's environment. It receives a minimal baseline (`PATH`) plus only what you pass explicitly. [verified, 4C/4D]
  - `--env NAME=VALUE` — set a variable to an explicit value.
  - `--env-passthrough NAME` — copy the relay's current value of `NAME` (refused if unset).
- The server's environment is delivered over a **private pipe**, never on the argv and never in the helper's own environment; **no value appears in `/proc/<pid>/cmdline`**, in the helper's environment (which is empty), or in the audit log (names only). [verified, 4D]
- Misconfiguration (malformed `--env`, a duplicate or conflicting name, an unset passthrough, any of these flags without a scope) is **refused before any server starts**. [verified, 4C]
- **Operational guidance:** pass secrets only with `--env`/`--env-passthrough`, and only to servers that need them. Launch the relay itself with a minimal environment if the server is untrusted, so there is less to pass through by mistake. Treat any variable name as potentially sensitive — the policy withholds **everything** unless named, so there is no reliance on name matching. [verified, 4D]
- **Limit:** a single environment value larger than the kernel's `MAX_ARG_STRLEN` (~128 KiB on x86-64) makes the server's `execvpe` fail (E2BIG); the server exits 127 and the scope ends cleanly — fail-closed and bounded, but opaque. Keep environment values well under 128 KiB. [verified, 4D]

## 4. Working directory and filesystem access

- In scope mode the server's working directory defaults to a neutral **`/`**, overridable with `--server-cwd DIR` (validated to exist before start; a chdir failure in the server child fails closed). [verified, 4C/4D]
- The relay resolves the server **program to an absolute path**, so a relative command still runs under the neutral directory. [verified, 4C]
- **This is not filesystem isolation.** [verified] The server shares the host mount namespace and can read/write any path its user can reach, regardless of cwd. The neutral cwd only removes *accidental* exposure of the relay's directory to relative paths. For real filesystem confinement, run the relay in a container or a mount namespace with a restricted root (§7). [unverified]
- Filesystem-server roots passed as absolute arguments are unaffected by the cwd policy. [verified, 4C]

## 5. Network access and external side effects

- [verified] The server shares the host **network namespace**: it can make any outbound (and, with privilege, inbound) connection the host allows. The scope does nothing to the network.
- [verified, 4A] Reconciliation and the scope say **nothing** about external systems. A side effect the server hands to a remote API, database, queue, or another local process is outside the scope and outside every reconciliation verdict (`operationOutcome: unknown`, `treatAsSuccess: false`).
- **Deployment responsibility:** apply network policy (egress firewall, a network namespace, or a container network) at the deployment layer if the server must be network-restricted. [unverified]

## 6. CPU, memory, process-count and file-descriptor limits

- [verified] The scope imposes **none** of these. Inherited rlimits apply (`RLIMIT_NPROC` is per-UID and shared with the relay); there is no cgroup. A compromised server can fork up to the per-UID process limit and consume CPU/memory up to the host's.
- **Resource-limit decision (Phase 4): leave all resource limits to deployment (outcome A).** No resource limit is implemented in this package. Rationale and the rejected alternative are in §9.
- **Deployment responsibility:** bound the whole relay+server process tree with **cgroup v2** (`pids.max`, `memory.max`, `cpu.max`) via the container runtime or a systemd unit, and set per-process `ulimit`s (`nofile`) there. cgroup v2 is **not even mounted** in the tested environment [verified], so StateGuard cannot assume it exists; the runtime that provides and delegates cgroups is the right owner. [unverified for any specific runtime]

## 7. Recommended deployment — container or systemd (all [unverified])

> **None of the configurations below has been tested in this package.** They are starting points. Each is followed by a verification procedure you must run in your environment before relying on it. Do not treat them as known-good.

### 7a. Container (illustrative, [unverified])

Run the whole relay inside a container that provides the filesystem, network and resource isolation the scope does not:

```
# ILLUSTRATIVE ONLY — verify before use (§7c).
# - a read-only root with only the data the server needs mounted;
# - an egress network policy;
# - resource limits on the container (pids/memory/cpu);
# - CAP_SYS_ADMIN granted to the relay (needed for the PID namespace),
#   which also requires the runtime's seccomp profile to allow unshare(CLONE_NEWPID),
#   pidfd_open and pidfd_send_signal.
```

The default seccomp profile of common runtimes **blocks `unshare` without `CAP_SYS_ADMIN`**; granting `CAP_SYS_ADMIN` is a broad grant, so prefer a minimal purpose-built profile that allows exactly `unshare(CLONE_NEWPID)`, `pidfd_open`, `pidfd_send_signal`. Whether a given runtime/profile permits this is **[unverified]** here.

### 7b. systemd unit (illustrative, [unverified])

A unit can apply cgroup limits and ambient capabilities:

```
# ILLUSTRATIVE ONLY — verify before use (§7c).
# [Service]
# AmbientCapabilities=CAP_SYS_ADMIN
# CapabilityBoundingSet=CAP_SYS_ADMIN
# TasksMax=…            # process-count ceiling (cgroup pids)
# MemoryMax=…           # RSS ceiling (cgroup memory)
# CPUQuota=…            # CPU ceiling
# LimitNOFILE=…         # per-process fd ceiling
# Delegate=yes          # only if the relay itself needed to manage cgroups (it does not)
```

Note: `NoNewPrivileges=yes` at the unit level would be **incompatible** — the relay needs to create a namespace; the server (not the relay) gets `no_new_privs` set by the scope itself. Whether this unit works is **[unverified]**.

### 7c. Verification procedure (run before trusting any of the above)

1. Start the relay in the target deployment with `--executor-scope pid-namespace` and a trivial server.
2. Confirm the capability check passes and a tool call completes:
   `printf '<initialize>\n<initialized>\n<tools/call>\n' | stdio_proxy ... --executor-scope pid-namespace -- <server>`.
3. Confirm the server does **not** hold `CAP_SYS_ADMIN`: have the server read `/proc/self/status` (`CapEff`) or attempt `unshare(CLONE_NEWNS)` and expect `EPERM`.
4. Confirm resource limits bind the **whole tree**: from inside the server, read `/sys/fs/cgroup/.../pids.max`/`memory.max` and confirm they are the deployment's values, not `max`.
5. Confirm the environment is minimal: have the server dump `sorted(os.environ)` and confirm only the baseline plus what you passed.
6. Confirm no orphan survives: kill the relay and confirm no server/helper/init process remains (the scope's abrupt-death handling, [verified, 4A/4B], but re-confirm under the runtime).

## 8. Logging, credentials, upgrades, operational failure

- **Logging:** the audit log records environment-variable **names** and `server_cwd`, never values [verified, 4D]. The server command is logged (program + args); do not put secrets in the server's argv — pass them via `--env`.
- **Credentials:** see §3. The policy withholds all relay environment unless named.
- **Upgrades:** the scope is experimental; the CLI refuses reconcile without a scope and refuses a scope where the capability/drop check fails. A kernel or runtime change that removes PID-namespace or pidfd support, or blocks the capability drop, causes a **startup refusal** (fail closed), not a silent downgrade [verified, 4A/4B].
- **Operational failure behaviour** [verified]:
  - capability/drop/program/env/cwd misconfiguration → exit 2, nothing started;
  - a scope that cannot be confirmed empty → `scope_unconfirmed` → reconciliation `undetermined`, no probe;
  - relay killed → helper (control-pipe EOF) and `PR_SET_PDEATHSIG` empty the namespace, no orphan;
  - oversized env value → server exits 127, scope ends cleanly;
  - teardown race → an end-grace lets a clean exit report its status rather than a kill.
- **Never** disable the capability check or run reconcile without a scope to "make it work"; both are fail-closed by design.

## 9. Resource-limit decision (Phase 4 detail)

Evaluated, per the brief, without implementing cgroups.

| Control | Limits | Does **not** limit | Inheritance / privilege | Compatibility risk | If ever enabled |
|---|---|---|---|---|---|
| `RLIMIT_NOFILE` | open fds of **one** process | aggregate fds across the tree; memory; CPU; process count | per-process, inherited; lowering soft needs no privilege, raising hard needs `CAP_SYS_RESOURCE` | a server legitimately opening many fds hits `EMFILE`; hard to pick a safe default | explicit opt-in flag with an operator value; never a default; test a high-fd server |
| `RLIMIT_AS` | **virtual** address space of one process | RSS (real memory); the tree; CPU; fds | per-process, inherited | **high** — Go, JVM, Node/V8, ASAN, io_uring reserve huge virtual space far above RSS; a "generous" limit still crashes them opaquely at startup | explicit opt-in only, heavily caveated per runtime; cgroup `memory.max` is the correct tool |

**Both are per-process, not per-tree**, so neither bounds the real threat (a compromised server that forks). The aggregate controls (pids/memory/cpu) require cgroup v2, which must come from deployment and is not even mounted here.

**Decision: outcome A — leave all resource limits to deployment.** No critical gap requires StateGuard to add per-process rlimits: the deployment's cgroups bound the whole tree and its `ulimit`s bound per-process fds. `RLIMIT_AS` in particular is an active footgun. Outcome B (an opt-in `--rlimit-nofile`/`--rlimit-as` as a *future* task) is **not recommended now**; if a specific need arises it would need its own design decision, per-runtime caveats, and tests (a high-fd server, each major runtime under `RLIMIT_AS`, failure/cleanup) before being enabled even as opt-in.
