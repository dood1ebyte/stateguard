# Week 4C — environment isolation and resource-boundary decision

**Date:** 2026-10-17. **State:** working tree, uncommitted and unpushed.
**Builds on:** Week 4A (`WEEK4_CLI_EXECUTOR_SCOPE.md`) and Week 4B (`WEEK4B_EXECUTOR_SECURITY_AUDIT.md`); contract `RECONCILIATION_CONTRACT_V2.md` §4.4.
**Code:** `src/stateguard/adapters/mcp/executor_scope.py` (`ddea6b3f1f7239d5`), `examples/mcp/stdio_proxy.py` (`90db679e819d73e2`).
**Evidence:** `/root/stateguard-captures/week4c/`.

Labels: **[measured]** from a probe/run; **[test]** an automated test; **[implemented]** a description of code; **[assumption]** not enforced by StateGuard.

The PID-namespace executor scope is a **lifecycle boundary with CAP_SYS_ADMIN denial** (Week 4B). It is **not a sandbox**. This package reduces accidental exposure of the relay's environment and working directory to the supervised server, and decides where resource containment belongs. It does not add cgroups or any new privileged mechanism.

## 1. Release decision

- **Environment:** in executor-scope mode the server no longer inherits the relay's environment. It gets a **minimal baseline (`PATH`) plus only what the operator passes explicitly** (`--env NAME=VALUE`, `--env-passthrough NAME`). Conflicts, malformed entries and missing passthroughs are refused before any server starts. Outside scope mode nothing changes. **Ship (experimental, with the scope).**
- **Working directory:** in scope mode the server runs in a **neutral default `/`**, overridable with `--server-cwd DIR` (validated to exist before start). Outside scope mode the server inherits the relay's directory, as before. **Ship.**
- **Resource containment:** **does not belong in StateGuard.** It requires a delegated, privileged cgroup v2 subtree that is not even present in the tested environment; rlimits alone are per-UID and leaky. **Recommend boundary B (the container/runtime/deployment supervisor).** Not implemented here.

No sandboxing is claimed. The filesystem, network, IPC and user namespaces remain shared with the host (Week 4B §5); this package does not change that.

## 2. Phase 1 — what reached the server (before)

[measured] `week4c/phase1/env-inheritance-before.json` — a scoped probe reporting its own context, relay environment seeded with planted secrets:

- **161** environment variables reached the server, **22 credential-like**, including `AWS_SECRET_ACCESS_KEY`, `GH_TOKEN`, `GITHUB_TOKEN`, `CLOUDSDK_AUTH_ACCESS_TOKEN`, `CLAUDE_CODE_MESSAGING_TOKEN`, and the planted `MY_API_TOKEN` / `DATABASE_PASSWORD`.
- Proxy configuration (18 vars), `HOME`, `PATH`, and application config all passed through.
- **Working directory:** the relay's repository (`/home/user/stateguard`).

Trace of the mechanism [implemented]: the helper was launched with `subprocess.Popen(...)` with no `env=`/`cwd=`, so it inherited the relay's environment and directory; init and the server inherited them in turn (no `chdir`, no environment edit anywhere). The server therefore ran with the relay's full environment and directory.

## 3. Phase 2 — environment policy

### 3.1 Design

| | Before | After (scope mode only) |
|---|---|---|
| Source | the relay's entire environment, inherited silently | an explicit set built by the relay |
| Baseline | — | `PATH` only (`_ENV_BASELINE`), needed to exec the program and its children; not a credential |
| Additions | — | `--env NAME=VALUE` (explicit value), `--env-passthrough NAME` (copy the relay's current value) |
| Secrets | all inherited | none, unless named explicitly |
| Outside scope mode | unchanged | **unchanged** (full inherit) |

**Determinism and refusals, all before any server starts** [test]:
- `--env` without `=`, or with an empty name → refused.
- the same `NAME` given twice, or via both `--env` and `--env-passthrough` → refused (one source per variable).
- `--env-passthrough NAME` where `NAME` is unset in the relay → refused (never silently dropped).
- any of `--env` / `--env-passthrough` / `--server-cwd` without `--executor-scope pid-namespace` → refused.
- an explicit value overrides the baseline deterministically.

**Transport and separation of helper vs server** [implemented]:
- The server's environment is delivered to the helper over a **private pipe** (`--env-fd`), never on the argv (so no value appears in `/proc/<pid>/cmdline`) and never in the helper's own environment.
- When a policy is in force the **helper runs with an empty environment of its own** (`env={}`); it needs none to create and confirm the namespace ([measured]: the helper imports and the capability check passes under `env -i`). init inherits that empty environment; it applies the server's environment only at `execvpe`, so helper/init and server environments are separate.
- A non-ASCII-safe or non-string environment fails closed in the helper (nothing is exec'd).

**No secret values are logged.** `relay_started` records `server_env_names` (sorted keys) and `server_cwd` only [test: the audit log contains neither `MY_API_TOKEN`'s value nor `DATABASE_PASSWORD`'s]. The sensitivity of a variable is irrelevant to the policy: **everything** is withheld unless named, so a credential whose name is not on any list is withheld exactly like any other variable. No name-pattern guessing is relied on for safety.

### 3.2 Behaviour [measured] (`week4c/phase2/env-after.txt`)

| Invocation | env vars reaching the server | secrets leaked | cwd |
|---|---|---|---|
| `--executor-scope pid-namespace` | 2 (`PATH`, plus `LC_CTYPE` the child Python sets itself) | none | `/` |
| … `--env MCP_ROOT=/data --env-passthrough MY_API_TOKEN` | 4 (`PATH`, `MCP_ROOT`, `MY_API_TOKEN`, `LC_CTYPE`) | only `MY_API_TOKEN`, explicitly permitted | `/` |
| no scope (unchanged default) | 161 | all (`MY_API_TOKEN`, `DATABASE_PASSWORD`, `GH_TOKEN`, `GITHUB_TOKEN`) | the relay's repo |

`LC_CTYPE` is set by the child CPython via PEP 538 C-locale coercion, not inherited from the relay; it carries no relay data.

### 3.3 Adversarial tests

`tests/examples/test_cli_environment_policy.py` [test]:
- planted secrets do not reach a scoped server; the baseline `PATH` does;
- explicitly configured variables (`--env`, `--env-passthrough`) do reach it; an unpermitted secret does not;
- malformed / conflicting / missing-passthrough configurations are refused before start (5 cases); env flags without a scope are refused;
- no environment **value** appears in the audit log, while the **names** and `server_cwd` do;
- the no-scope default still inherits the full environment (unchanged).

## 4. Phase 3 — working-directory policy

**Decision: a documented neutral default of `/`, overridable with `--server-cwd DIR`.** Rationale from actual usage, not aesthetics: the pinned filesystem server and the test servers all take their roots as **absolute arguments** and do not depend on cwd, so a neutral directory keeps every known server working while removing the accidental exposure of the relay's repository. The relay's directory is still available by passing `--server-cwd` to it explicitly (the pre-4C behaviour, now opt-in).

[implemented] / [test]:
- The effective cwd is deterministic: `/` by default, or exactly the `--server-cwd` value.
- `--server-cwd` is validated to be an existing directory **before the server starts**; otherwise refused. A chdir failure in the server child also fails closed (`_exit(125)`), never a silent wrong directory.
- A **relative** server command (e.g. `.venv/bin/python`) still runs: the relay resolves `command[0]` to an absolute path before launching, so changing the server's cwd to `/` does not break it. (This was a real interaction found and fixed here.)
- Configured filesystem-server roots are absolute arguments and are unaffected; relative paths a server opens now resolve against `/`, well-defined, instead of the relay's repo.

**Not a filesystem sandbox.** The server shares the host mount namespace (Week 4B), so a neutral cwd reduces *accidental* exposure but does not prevent a server from reading absolute host paths. No filesystem isolation is claimed.

## 5. Phase 4 — resource-containment architecture decision (no implementation)

[measured] `week4c/phase4/resource-probe.json` (a bounded, non-destructive probe; `week4c_resource_probe.py`):
- Inherited rlimits: `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE` unlimited; `RLIMIT_NOFILE` 20000; `RLIMIT_NPROC` 64298 (**per-UID**, shared with the relay).
- **cgroup v2 is not mounted** in this environment (`/sys/fs/cgroup/cgroup.controllers` absent), so no child cgroup could be created and no `pids.max` written.
- A bounded fork of 8 children hit no ceiling imposed by the scope.

**Requirements to enforce each limit:**

| Limit | Mechanism | What it needs |
|---|---|---|
| max process count | cgroup v2 `pids.max` | a writable, delegated cgroup subtree; `RLIMIT_NPROC` is per-UID (bounds the relay too) and unsuitable |
| memory | cgroup v2 `memory.max` | same; `RLIMIT_AS` is per-process and does not bound a tree |
| CPU | cgroup v2 `cpu.max` | same; `RLIMIT_CPU` is per-process seconds, not a rate |
| file descriptors | `RLIMIT_NOFILE` (per-process, cheap) **or** cgroup | the one limit StateGuard could set directly and soundly |
| termination/cleanup under exhaustion | the existing scope end | already bounded (Week 4A/4B); a cgroup would also allow `cgroup.kill` |

**Boundary A — StateGuard-managed limits.** To set cgroup limits StateGuard would have to: create a child cgroup under a **delegated** subtree, enable controllers via `cgroup.subtree_control`, write the limit files, and place init's PID into `cgroup.procs`. That needs a writable cgroup v2 hierarchy delegated to the relay (root with a writable cgroupfs, or systemd `Delegate=yes`, or a cgroup namespace with delegation). **It is a separate privileged subsystem**, and in the tested environment the hierarchy is not even mounted. Per-process rlimits (`RLIMIT_NOFILE`, `RLIMIT_AS`) could be set cheaply but bound a single process, not the server's whole tree, and `RLIMIT_NPROC` is per-UID.

**Boundary B — external container/runtime/deployment supervisor.** A container runtime (Docker/Podman/Kubernetes) or systemd unit already owns and delegates cgroups and applies `pids`/`memory`/`cpu` limits to the whole relay+server tree, with `cgroup.kill` cleanup. This is where the mechanism, the privilege and the mount already live.

**Recommendation.** Resource containment belongs in **deployment configuration (B)**, not in StateGuard. StateGuard owns the lifecycle boundary and capability/credential/working-directory minimisation; the container owns filesystem, network and resource isolation. If a future need arises, the smallest defensible StateGuard addition would be **per-process `RLIMIT_NOFILE`/`RLIMIT_AS` on the server child behind an explicit, documented flag** — described as a partial, per-process guard, never as containment. cgroup management should not be built into StateGuard as a privileged subsystem.

## 6. Phase 5 — core contract preserved

All invariants hold; the changes touch only the scoped server's environment, working directory and program-path resolution:
- no automatic replay of an unknown-outcome invocation;
- no probe before confirmed scope termination;
- no execution/causation/durability/success claim from observed state;
- fail-closed when the scope cannot be confirmed (and now also on a bad cwd or unreadable env);
- **Shadow remains byte-identical** to the no-relay baseline [test: `test_shadow_with_and_without_the_scope_is_byte_identical`];
- no silent fallback to weaker isolation (env/cwd refusals fail closed);
- no credentials in logs or evidence (names only).

Re-run [measured]: capability-drop and teardown-race regressions, executor-scope tests, reconciliation lifecycle/concurrency — ×3, 53 passed each. Full suite — `week4c/gates/pytest.txt`.

## 7. Compatibility risks for existing MCP servers

- A server that **reads a credential or proxy variable from the environment** (e.g. `GITHUB_TOKEN`, `HTTPS_PROXY`) must now be given it explicitly with `--env-passthrough`/`--env` in scope mode. Silent reliance on inheritance no longer works — by design. This is a behaviour change **for scope mode only**; the non-scope default is unchanged.
- A server that **depends on the relay's working directory** must pass `--server-cwd`. Known servers use absolute arguments and are unaffected.
- A server that needs variables beyond `PATH` (e.g. `HOME` for caches, `LANG`) must have them passed explicitly; the baseline is deliberately minimal because not every server runs on the same minimal set.
- No change for anyone not using `--executor-scope`.

## 8. Guarantees, assumptions, non-guarantees

**Guarantees** (scope mode) [test]: the server's environment is exactly baseline + explicit; no relay environment value reaches it otherwise; no value is logged; the working directory is deterministic; misconfiguration fails closed before start.

**Assumptions** [assumption]: the operator names the variables a server needs; `PATH` is an acceptable baseline (it is not a credential).

**Non-guarantees:** no filesystem/network/IPC isolation (shared namespaces, Week 4B); no resource containment (Phase 5); the environment policy reduces *accidental* exposure — a server given a secret explicitly still has it, and a server can still read host files its user can reach.

## 9. Evidence index and historical preservation

- `week4c/baseline/` — HEAD `b6f7ba0f`, the pre-4C working tree snapshot + `tree.sha256`, and the 4B changed-file list.
- `week4c/phase1/env-inheritance-before.json` — 161 vars, 22 credential-like, cwd = repo.
- `week4c/phase2/env-after.txt` — scope default / explicit / no-scope.
- `week4c/phase4/resource-probe.json` — rlimits, cgroup v2 absent, bounded fork.
- `week4c/gates/` — pytest, junit, checks, repeat.
- `week4c/EVIDENCE_SHA256.txt` — manifest.

**Historical evidence unchanged:** the Week 4A and 4B manifests and the Weeks 1–3 verifier re-run and pass; nothing under them is modified. The contract's §4.4.4 is updated in place (living document) with a dated Week 4C note.

## 10. Next task

Decide whether to add an **opt-in per-process `RLIMIT_NOFILE`/`RLIMIT_AS` guard** on the scoped server child (a small, documented, partial measure — not containment), or to leave all resource limits to the deployment, and in either case write the **deployment guide** that states plainly: run the relay inside a container/unit that applies `pids`/`memory`/`cpu` limits and provides filesystem/network isolation, because the executor scope provides neither. This supersedes the deferred container compatibility matrix as the immediate priority.
