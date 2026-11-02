# Week 4F — executor-scope architecture decision

**Date:** 2026-10-18. **State:** working tree, uncommitted and unpushed. **HEAD:** `b6f7ba0f`.
**Question:** can StateGuard's experimental MCP executor-scope + reconciliation mechanism provide its required lifecycle guarantees **without the relay/helper holding host `CAP_SYS_ADMIN`**?
**History:** 4A `WEEK4_CLI_EXECUTOR_SCOPE.md`, 4B `WEEK4B_EXECUTOR_SECURITY_AUDIT.md`, 4C `WEEK4C_ENVIRONMENT_AND_RESOURCE_BOUNDARY.md`, 4D `WEEK4D_DEPLOYMENT_GUIDE.md` + `WEEK4D_RELEASE_GATE.md`, 4E `WEEK4E_RUNTIME_COMPATIBILITY.md`; contract `RECONCILIATION_CONTRACT_V2.md` §4.4.
**This package makes no production code change.** It is an architecture decision backed by new, reproducible evidence. Nothing here claims the feature is production-ready or a general-purpose sandbox.

Evidence tags: **[verified]** observed by a probe on this host; **[verified-in-docker]** observed in a real container here; **[untested]** no environment available; **[analysis]** reasoned from verified facts, not independently demonstrated.

---

## Phase 1 — ground truth

- **Branch/HEAD/tree:** `claude/serene-bohr-o28snr`, HEAD `b6f7ba0f`, working tree dirty and unpushed (the whole Week 1–4 series lives uncommitted). A 49-file frozen snapshot + `tree.sha256` + `head.txt` + `status.txt` is at `/root/stateguard-captures/week4f/baseline/`.
- **No production code or tests changed in 4E or 4F.** `executor_scope.py` and `stdio_proxy.py` carry the same bytes recorded at 4D/4E (`executor_scope.py` sha prefix `a479983a…`, `stdio_proxy.py` `90db679e…`). The only new 4F files are **evidence**, not repository code, under `/root/stateguard-captures/week4f/`.
- **4E evidence re-read and found consistent with its report.** The 4E conclusions rest on captured observations, not assertion: `week4e/phase2/docker-matrix.txt` shows Docker-default fail-closed and `--cap-add SYS_ADMIN` working; `week4e/phase1/capability-op-trace.txt` is the capability trace. The one claim 4E explicitly did **not** demonstrate — a full unprivileged-userns lifecycle (option B) — it correctly labelled *feasible-not-demonstrated*. 4F closes exactly that gap (Phase 3B below).

### What the current scope (architecture A) actually guarantees, and does not

StateGuard's reconciliation contract turns on one piece of evidence: `TerminationEvidence`. Only `kind = scope_ended` is *sufficient* to let a reconciliation probe run; everything else (`not_reported`, `direct_child_reaped`, `scope_unconfirmed`) leaves the postcondition undetermined and blocks the probe (`reconcile.py:80–98`, `587–602`). Crucially, **`scope_ended` is an assertion by the embedder, not proof by StateGuard** (`reconcile.py:93`). The executor-scope feature's whole job is to make that assertion *well-founded* and to **fail closed** when it cannot be.

| Guarantee | Provided by StateGuard today? | Evidence |
|---|---|---|
| Every process of a server generation lives in one scope (one PID namespace) | **Yes** | 4A/4B; `_serve` unshare + init-as-PID-1 |
| A `setsid`/detached descendant cannot escape that scope | **Yes** | 4B, 4E [verified-in-docker]; 4F-B [verified] |
| The scope is confirmed *empty* (via `/proc` ns scan) before any probe | **Yes** | 4B; `executor_scope.py:368` |
| A failed/uncertain teardown → `scope_unconfirmed` → reconciliation blocked | **Yes, fail-closed** | 4B; 4F-B stophelper [verified] |
| The original executor is terminated, or explicitly unconfirmed, before reconcile | **Yes** | contract §4.4; `reconcile.py:587` |
| No replay of a call with an unknown result | **Yes** | contract v2; exhaustively tested in host suite |
| Shadow / no-scope behaviour byte-identical to baseline | **Yes** | host suite, environment-independent |
| Filesystem / network / IPC isolation | **No** — shared host namespaces | 4C/4D §3 |
| Resource containment (fork bomb, CPU, memory) | **No** — deployment's job | 4C/4D |
| Containment of external side effects (APIs, DBs, queues) | **No** — out of boundary | contract `reconcile.py:97` |
| Durability / execution attribution of an observed state | **No** — observation ≠ attribution | contract §4.4 |

The lifecycle guarantees in the top block are the feature's contribution. The bottom block is explicitly **out of the executor-scope boundary** and delegated to the deployment or disclaimed. **The cost of the top block today is that the relay and its helper must hold host `CAP_SYS_ADMIN`.**

---

## Phase 2 — the minimum contract reconciliation requires

For reconciliation to proceed *safely*, the lifecycle layer must guarantee all of the following. These are **architecture-independent**: they are what any candidate (A, B, or C) must satisfy to feed a sound `scope_ended` into the contract.

**R1. Termination-before-probe.** A probe runs only after the original executor is either confirmed terminated or explicitly recorded as unconfirmed. (`scope_ended` vs `scope_unconfirmed`.)
**R2. No continuation after confirmed termination.** Once `scope_ended` is asserted, no process or thread of that generation can still run — including `setsid` descendants that left the process group.
**R3. Escape-proof scope.** Detached descendants cannot outlive the scope's teardown.
**R4. Fail-closed on uncertainty.** A failed, partial, or unconfirmable teardown must yield `scope_unconfirmed` and block the probe — never a hopeful `scope_ended`.
**R5. No replay on a lost result.** Independent of the scope; preserved by the contract regardless of architecture.
**R6. Shadow / passthrough unchanged.** The scope must not alter the no-reconcile path.
**R7. Observation ≠ attribution/durability.** A satisfied postcondition never implies the reconciled generation caused it, nor that it is durable, absent independent evidence.
**R8. Out of boundary.** Resource exhaustion and external side effects are explicitly *not* contained by the scope.

### StateGuard-provided vs deployment-delegated

| Requirement | Who must provide it |
|---|---|
| R1, R2, R3, R4 — the scope mechanism and its fail-closed gate | **StateGuard** (the executor-scope layer) *or* an external runtime StateGuard reads a termination signal from (architecture C) |
| R5, R6, R7 — contract invariants | **StateGuard** (architecture-independent; already verified) |
| R8 lower bound — filesystem/network/resource isolation | **Deployment** (container/VM/cgroup); never claimed by the scope |
| The *privilege* needed to create the scope | **Deployment** grants it; the architecture decides *how much* |

The architecture question is therefore narrow: **R1–R4 can be satisfied by three different owners of the scope, at three different privilege costs.** R5–R8 do not vary.

---

## Phase 3 — three architectures against the same contract (R1–R4)

### A. Current — privileged PID namespace (relay/helper hold host `CAP_SYS_ADMIN`)

- **R1–R4: satisfied and verified.** 4B/4D host suite + 4E [verified-in-docker]: detached descendant killed, namespace confirmed empty, `scope_unconfirmed` on a wedged helper.
- **Privilege cost:** the **only** operation needing `CAP_SYS_ADMIN` is `unshare(CLONE_NEWPID)` (`week4e/phase1/capability-op-trace.txt`). It is held by relay → helper → init; **dropped from the server before `execvp`** [verified, 4B]. So the server never holds it, but the **relay does, for the whole session**, and the relay is the process exposed to server-influenced input.
- **Compromise consequence [analysis]:** a relay compromised *before* the per-server drop — or any bug in relay/helper code — runs with `CAP_SYS_ADMIN`, which is close to full root: it permits `mount`, `setns`, `pivot_root`, BPF, and much more. Mitigation already available: run the relay **non-root with ambient `CAP_SYS_ADMIN`** (not full root) [verified, 4B], inside a container that bounds filesystem/network/resources. This shrinks but does not remove the concern: the capability itself is the broad grant.
- **Deployment reality [verified-in-docker]:** works under Docker **only** with `--cap-add SYS_ADMIN`; Docker's *default* seccomp+caps **fail closed** (correct, safe).

### B. Unprivileged `CLONE_NEWUSER` + `CLONE_NEWPID` (no host `CAP_SYS_ADMIN`)

Full lifecycle demonstrated this package — not just namespace creation. Prototype + driver + outputs: `/root/stateguard-captures/week4f/phase3/` (`userns_scope.py`, `driver.py`, `B_normal.json`, `B_stop.json`, `B_stophelper.json`, `B_docker.txt`, `README.txt`). Run as uid 65534 with **no** capabilities.

| Invariant | Observation [verified] |
|---|---|
| Namespace creation unprivileged | `unshare(NEWUSER\|NEWPID)` rc=0 as uid 65534; `init_pid` real, `ns pid:[…]` |
| UID/GID mapping | `setgroups=deny`, `gid_map`/`uid_map = "0 65534 1"`; server sees `inner_uid=0` |
| Server launch | server reports, runs as namespaced root |
| **R3** detached-descendant termination | `setsid` double-fork descendant **killed with the namespace**; `descendant_acted_after_teardown=false` (normal/stop) |
| **R2** namespace-empty confirmation | `members_before_end=3 → members_left=0 → members_after_end=0`, `scope_confirmed_empty=true` |
| teardown race | pidfd teardown with a 1s end-grace; clean `scope_ended status=-9` |
| **R4** fail-closed (wedged helper) | stophelper: `ended=null`, `members_after_end=3`, `reconcile_gate=scope_unconfirmed`, and `descendant_acted_after_teardown=TRUE` — the gate blocks precisely the genuinely-unsafe case |
| **R1** reconcile gating | `reconcile_gate=scope_ended` only when confirmed empty; else `scope_unconfirmed` |

**B satisfies R1–R4 with the same mechanism and the same fail-closed gate as A, with no host `CAP_SYS_ADMIN`.** Its distinct costs:

1. **Semantic change — uid remapping.** The server's host identity becomes uid 65534, not its original uid. Files it writes on a shared mount are **owned by 65534** (`file_owner_uid=65534` [verified]). 4A/4B deliberately avoided this. It changes file ownership, permission checks, and anything keyed on the server's host uid.
2. **Environment dependence.** Requires unprivileged user namespaces enabled (sysctl/distro can disable them) **and** the runtime's seccomp to permit `unshare`. [verified] on this kernel; **[verified-in-docker]** that Docker's **default** seccomp **blocks** it (EPERM) while `seccomp=unconfined` with **zero** added capabilities allows it (`CapEff=0` in both — B needs no capability, only seccomp relaxed). This is the mirror image of A: A needs a capability under default seccomp; B needs seccomp relaxed and no capability.
3. **Prototype only.** `userns_scope.py` is evidence, not the production helper: it has **no** capability-drop/verify, env policy, cwd policy, confinement checks, or test suite. Claiming B-equals-A for release requires porting all of 4B/4C/4D through it and re-running the full gate.

Per the brief, **equivalence with A is not claimed** beyond R1–R4, which are the invariants demonstrated here.

### C. External supervisor / runtime owns the scope

StateGuard creates **no** namespace; it launches each server generation as a unit of an external runtime (a container, a systemd scope, a microVM) and reads that runtime's termination signal as its `scope_ended`/`scope_unconfirmed`.

- **R1–R4 via the runtime [verified-in-docker]:** `C_docker_supervisor.txt` — a container whose server left a SIGTERM-ignoring detached `setsid` descendant was ended by `docker kill`; container exited **137**, **no** descendant survived on the host. Same kernel mechanism (a PID namespace torn down by an uncatchable signal), but **the runtime owns it, so StateGuard holds no `CAP_SYS_ADMIN`.**
- **Cost shift [analysis]:** StateGuard can no longer run its own `/proc` emptiness scan inside the namespace; its gate must map the runtime's state → evidence ("container exited" → `scope_ended`; "cannot confirm gone" → `scope_unconfirmed`). It also adds an operational dependency (a runtime per server generation) and must define the mapping fail-closed. The privilege moves entirely into the runtime, which is designed to hold it.
- **[untested]** systemd scopes (systemd is not PID 1 here), Podman, Kubernetes — no environment. Only Docker-as-supervisor is demonstrated.

### Side-by-side (R1–R4 + privilege)

| | A: privileged pidns | B: unpriv userns+pidns | C: external runtime |
|---|---|---|---|
| R1 termination-before-probe | ✓ [verified] | ✓ [verified] | ✓ via runtime [verified-in-docker] |
| R2 no continuation | ✓ | ✓ | ✓ |
| R3 escape-proof | ✓ [verified-in-docker] | ✓ [verified] | ✓ [verified-in-docker] |
| R4 fail-closed | ✓ | ✓ [verified] | ✓ (mapping must be written fail-closed) [analysis] |
| Host `CAP_SYS_ADMIN` on relay | **required** | **not required** | **not required** (runtime holds privilege) |
| Server host uid preserved | ✓ | ✗ (remapped to 65534) | ✓ (runtime's choice) |
| Works under Docker default config | ✗ (needs `--cap-add SYS_ADMIN`) | ✗ (needs `seccomp=unconfined`) | n/a (StateGuard isn't unsharing) |
| Production-complete in StateGuard today | ✓ (A *is* the shipped code) | ✗ (prototype) | ✗ (not implemented) |

---

## Phase 4 — deployment-consequences matrix

Columns: host caps needed on the relay; feasible under Docker default vs least-privilege; rootless/rootful; kernel/sysctl deps; UID/GID + filesystem effect; process-tree termination; teardown-uncertainty handling; reconciliation safety; failure mode; packaging; operational burden; claim status.

| Dimension | A (shipped) | B (prototype) | C (external runtime) |
|---|---|---|---|
| Host capability on relay | `CAP_SYS_ADMIN` (near-root) | **none** | none (runtime privileged) |
| Docker **default** config | **fail-closed** [verified-in-docker] | **fail-closed** (EPERM) [verified-in-docker] | runtime launches normally [verified-in-docker] |
| Docker **least-privilege** that works | `--cap-add SYS_ADMIN`, default seccomp [verified-in-docker] | `seccomp=unconfined`, **no** cap, `CapEff=0` [verified-in-docker] | default Docker (no special flags) |
| Rootless feasibility | relay non-root **with ambient `CAP_SYS_ADMIN`** [verified, 4B partial] | fully rootless (uid 65534) [verified] | rootless if runtime supports it ([untested] rootless Podman) |
| Rootful feasibility | yes | yes | yes |
| Kernel / sysctl deps | PID ns + pidfd (≥5.3) | + unprivileged userns **enabled** (sysctl/distro can disable) [verified here] | runtime's own deps |
| UID/GID + filesystem | server keeps host uid; files owned normally | **server→uid 65534; files owned by 65534** [verified] | runtime's uid policy |
| Process-tree termination | whole pidns killed [verified-in-docker] | whole pidns killed [verified] | whole container killed, exit 137 [verified-in-docker] |
| Teardown uncertainty | `scope_unconfirmed` on wedged helper [verified] | `scope_unconfirmed` on wedged helper [verified] | must map "can't confirm" → `scope_unconfirmed` [analysis] |
| Reconciliation safety | sound; fail-closed | sound; fail-closed | sound **iff** the mapping is fail-closed |
| Dominant failure mode | relay holds broad cap for whole session | server identity remapped; userns may be disabled | extra runtime dependency + mapping correctness |
| Packaging | install in `site-packages` (empty-env helper) [verified-in-docker, 4E §3.2] | same | runtime image owns the server |
| Operational burden | operator must add a capability consciously | operator must relax seccomp; accept uid remap | operator runs a container/scope per generation |
| Claim status | **[verified-in-docker]** end-to-end incl. reconcile | **[verified] R1–R4 only**; rest unported | **[verified-in-docker]** termination only; gate mapping **[analysis]** |

**[untested] everywhere:** Podman (not installed), systemd units (systemd not PID 1 here), Kubernetes (no cluster). Those rows stay untested — **no support is inferred from a binary being installed.**

---

## Phase 5 — decision

**Recommendation: (2) prototype the unprivileged user-namespace design (B) next — as the path toward removing the host `CAP_SYS_ADMIN` requirement — while A remains the only shipped, experimental option in the meantime (status unchanged from 4E).**

Rationale:

- The central privilege concern is real and unchanged: A requires the **relay** to hold `CAP_SYS_ADMIN` for the whole session, and the relay is exposed to server-influenced input. That is the thing worth removing.
- B is **no longer merely feasible** — its complete lifecycle, including the escape-proof scope and the fail-closed gate, is now **[verified]** unprivileged (Phase 3B). It removes the host capability entirely (`CapEff=0`). This is the strongest available evidence that the required invariants (R1–R4) do **not** depend on host `CAP_SYS_ADMIN`.
- B is **not** adopted now, and A is **not** declared equivalent-to-B, because B carries an unshipped semantic change (uid remap) and is a prototype missing every 4B/4C/4D control. Promoting B is a *scoped follow-up package*, not this one — the brief forbids a broad redesign here.
- C stays a viable alternative, especially where a container runtime is already mandated (which 4D already requires for isolation). If deployments must run inside a privileged-enough runtime anyway, delegating the scope to it (C) may be simpler than B. C is kept open as the fallback if B's uid remap or userns-availability proves unacceptable.
- Option (4) stop — is rejected: a viable unprivileged boundary now **has** been demonstrated (B), so there is no basis to stop.

**This does not change the release posture:** experimental, opt-in, Linux, inside an isolated deployment. Today that still means A with `CAP_SYS_ADMIN`. **No prerequisite in the 4D release gate changes**, so no gate amendment is appended this package (the gate's §5 controls and §3 "not a sandbox" boundary all still hold; B/C are future paths, not current prerequisites).

### What evidence would change the decision

- **Toward adopting B:** a production B helper that ports the 4B capability drop/verify, 4C env + cwd policy, and 4D combined-invariant tests, passing the full host suite **and** a container matrix — *plus* an accepted answer to the uid-remap semantic change (either it is acceptable for target servers, or a mapping that preserves needed identity). Absent that, B stays a prototype.
- **Toward C:** a fail-closed mapping from a real runtime's termination state to `TerminationEvidence`, demonstrated across ≥2 runtimes, with `scope_unconfirmed` proven on an un-confirmable container stop.
- **Toward retaining A as-is (option 1):** evidence that unprivileged userns is too often disabled in target fleets to rely on (making B impractical) **and** that mandating a runtime (C) is unacceptable — which would make A's capability the least-bad option, to be retained with the 4D isolation prerequisites.
- **Toward stopping (option 4):** discovery that none of A/B/C can satisfy R4 (fail-closed) under a realistic teardown race — not observed in any run here.

**Not claimed:** that the feature is production-ready, that any architecture is a general-purpose sandbox, or that B/C are implemented. B's evidence covers R1–R4 only.

---

## Validation

No production code or tests were changed in Week 4F (confirmed against the 4D/4E byte snapshots in Phase 1), so the full suite + lint + format + typing are **not** re-required by the brief for this package. The architecture comparison is itself the evidence. The two new scripts under `/root/stateguard-captures/week4f/phase3/` are evidence, not repository code. Prior-evidence manifests (weeks 1–3 and 4A–4E) are re-verified in the final report; a fresh `week4f` manifest covers this package's captures.
