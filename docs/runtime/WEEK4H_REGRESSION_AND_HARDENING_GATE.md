# Week 4H — regression triage and Architecture-B hardening gate

**Date:** 2026-10-18. **State:** working tree, uncommitted and unpushed. **HEAD:** `b6f7ba0f`.
**Purpose:** resolve the one full-suite failure reported in Week 4G (`test_differential_unique_items`) and set the next evidence-based gate for the unprivileged executor-scope prototype (Architecture B). **No feature expansion.**
**History:** 4A–4G; `WEEK4G_UNPRIVILEGED_SCOPE_PROTOTYPE.md`, `WEEK4F_EXECUTOR_SCOPE_ARCHITECTURE_DECISION.md`.

Evidence tags: **[demonstrated]** observed by a probe/test here; **[verified-in-docker]**; **[analysis]**; **[unverified]** no environment / not shown.

---

## Phase 1 — baseline preserved

Branch `claude/serene-bohr-o28snr`, HEAD `b6f7ba0f`, weeks 4A–4G uncommitted and intact. The 4G failure evidence (`/root/stateguard-captures/week4g/gates/`) and the Hypothesis example database (`.hypothesis/examples/`) were left untouched; the failure was reproduced by **replaying the preserved example and by calling the code path directly**, never by a fresh randomized search that could overwrite it. No reset/clean/commit/push.

---

## Phase 2 — `uniqueItems` failure: disposition

**Counterexample (recovered exactly):** field `{"type":"array","uniqueItems":true}`, value
`[[[true,true,true],null],[[true,true,1],null],[[true,true,true],null]]`.
Reproduction: `.venv/bin/python -m pytest tests/adapters/jsonschema/test_2020_12_conformance.py::test_differential_unique_items` (deterministic once the example is in `.hypothesis/examples`; also reproducible by calling `_agree` directly). Evidence: `/root/stateguard-captures/week4h/phase2/uniqueitems_investigation.txt`.

**Determinism:** the result is a pure function of the input; it reproduces on every call. [demonstrated]

**Correct answer per JSON Schema 2020-12.** `uniqueItems` requires all items distinct under JSON equality; JSON booleans are **not** JSON numbers (`true != 1`), `1 == 1.0`. Elements 0 and 2 are the identical JSON value `[[true,true,true],null]`, so the array is **not** unique → the correct result is **invalid** (`is_valid = False`).

| Validator | result | correct? |
|---|---|---|
| **StateGuard** | `is_valid = False` (flags the duplicate) | **yes** |
| reference `jsonschema` | `is_valid = True` (claims unique) | **no** |
| sound ground truth | invalid (duplicate present) | — |

**Root cause (where the discrepancy is):** the **reference validator**, and therefore the **differential test's assumption** that the reference is ground truth. `jsonschema._utils.uniq` sorts the list then compares only **adjacent** items. Python's `<` treats `True` and `1` as equal, so the sort interleaves element 1 (`[[true,true,1],null]`) **between** the two identical elements 0 and 2; the adjacent-only scan never compares the duplicate pair and reports "all unique". StateGuard uses a sound JSON-equality key (`core/validator.py:_json_key`, which tags booleans separately from numbers and normalises `1`/`1.0`) plus set-based dedup (`_all_distinct`), and is correct. [demonstrated]

**Does it reproduce on the baseline revision?** **Yes.** In an isolated `git worktree` at committed `b6f7ba0` (current tree untouched), the same input gives reference `True` / StateGuard `False`, and the test itself fails when the preserved example is replayed. The adapter (`core/validator.py`) and the test are byte-identical at `b6f7ba0`. The discrepancy is **pre-existing and independent of weeks 4A–4G**; the 4G executor-scope diff touches neither the JSON Schema adapter nor this test. It surfaced in 4G only because Hypothesis's randomized search first sampled this input class that run (4D ran the identical code at "6052 passed, 0 failed"). [demonstrated]

**Disposition — test-harness defect, not a product defect.** StateGuard's `uniqueItems` is spec-correct and was **not** changed. The fix is in the test: `test_differential_unique_items` now compares StateGuard against a **sound, independent oracle** (`_unique_items_expected`, O(n²) JSON-equality, derived from the spec — not from StateGuard's implementation) instead of the reference's unsound `uniq()`. This **does not weaken conformance** — every generated value is still checked, now against the correct answer. The exact counterexample is pinned with `@example`, and a dedicated regression test (`test_unique_items_bool_number_duplicate_where_reference_is_unsound`) documents the reference's limitation so the oracle choice is never silently reverted. Neighboring conformance tests pass (25 in the file).

---

## Phase 3 — Week 4G claims, re-verified

Re-checked against the actual code, tests, and evidence (all **[demonstrated]** by the 4G/4H focused suites unless noted):

| Claim | Status |
|---|---|
| Explicit selection `--executor-scope pid-namespace-unprivileged` | holds (argparse choice; unknown value rejected) |
| No silent fallback to A or unscoped execution | holds (`--userns` only on B's helper argv; failed B start → `ScopeError`, never A) |
| Fail-closed: userns disabled / creation denied | holds ([verified-in-docker] default seccomp EPERM; capability refused before start) |
| Fail-closed: UID/GID mapping failure | **now directly tested** (Phase 4) — 4G had this by inspection only |
| Server capability confinement (`CAP_SYS_ADMIN` dropped as namespaced root) | holds (`CapBnd` bit 21 = 0, `CapAmb=0`, `NoNewPrivs=1`) |
| Environment allowlist + working-directory controls | holds (shared with A; secret non-disclosure test) |
| Detached-descendant termination + namespace-empty confirmation | holds |
| `scope_unconfirmed` blocks reconciliation | holds (wedged-helper end-to-end) |
| No replay of unknown-outcome calls | holds (B reconcile: the move executes once, never resent) |
| Shadow Mode / passthrough unchanged | holds (architecture-independent; A's byte-identity suite green) |
| Accurate audit label / error reporting | holds (`relay_started.executor_scope` = exact selected name) |

**Seccomp profile — actual, audited.** `/root/stateguard-captures/week4h/phase3/seccomp_profile_audit.txt`. The narrow candidate (`week4g/phase2/seccomp-narrow-candidate.json`) is the moby v24.0.7 default (`defaultAction: ERRNO`) plus **exactly one** added group — `{"names":["unshare","clone3"],"action":"SCMP_ACT_ALLOW"}` — and removes nothing. The 4G report's description ("default + `unshare`/`clone3`") **matches the tested policy exactly.**
Precise scope of what it permits: `unshare`/`clone3` are allowed **unconditionally** (not arg-filtered to specific namespace flags), so it permits creating user namespaces (and nested namespaces within them) without `CAP_SYS_ADMIN`; it does **not** grant host-privileged namespace powers because the kernel still gates mount/net/etc. on the capability (`mount` stayed EPERM in Phase 2), and `keyctl` plus the other ~43 default-denied syscalls remain blocked (narrower than `unconfined`). It is **not** described as least-privilege: it is a narrow, operator-supplied candidate, not a from-scratch minimal allowlist, and `unshare` is not arg-filtered. 4G did not overclaim this. [verified-in-docker]

**Gaps recorded (not assumed proven by the standalone prototype):**
- "User-namespace support disabled via sysctl" is **[unverified]** directly (cannot change host sysctls non-destructively); it is covered only by the *equivalent* fail-closed code path (`_create_namespace` returns a reason → helper exits 3 → `ScopeError`) and by the Docker-default-seccomp denial.
- B behind a **non-root relay in a real deployment** remains shown only as uid 65534 here (representative, not a deployment). [unverified]
- The narrow seccomp profile is **[verified-in-docker]** only; other runtimes are [unverified].

---

## Phase 4 — Architecture-B edge-case gate

Focused, reproducible tests against the real integrated implementation. **131 passed** (`/root/stateguard-captures/week4h/gates/edge_case_gate.txt`): the B scope suite, A scope suite, security suite, CLI-scope suite, environment-policy suite, reconciliation-lifecycle suite, and JSON Schema conformance.

| Highest-risk failure mode | Coverage |
|---|---|
| User-namespace support disabled | [analysis] + equivalent path (Docker-default seccomp denial [verified-in-docker]); direct sysctl disable [unverified] |
| Namespace creation denied by seccomp | [verified-in-docker] (default profile → EPERM, fail-closed) |
| **UID/GID mapping failure** | **[demonstrated] new 4H tests** — a real second `uid_map` write fails → `_map_userns_identity` returns a reason; `_create_namespace` surfaces it (fail-closed, no server forked) |
| Helper startup / communication failure | [demonstrated] (shared helper: missing-permission → `ScopeError`; env-send fail-closed in the security suite) |
| Wedged helper / uncertain teardown | [demonstrated] (B wedged-helper → `scope_unconfirmed`, bounded, cleaned up) |
| Detached descendants / repeated lifecycle | [demonstrated] (setsid descendant killed; 4 generations, distinct identity, ns empty each) |
| Reconciliation blocked when unconfirmed | [demonstrated] (B end-to-end: `undetermined`, no probe, nothing resent) |
| No replay (incl. concurrent / response-loss) | [demonstrated] for the B scope path (move executes once); concurrent/response-loss are architecture-independent relay logic covered by the broader reconciliation suite |
| Shadow Mode / passthrough unchanged | [demonstrated] (architecture-independent; A's byte-identity suite green) |
| Environment + capability confinement | [demonstrated] (allowlist, secret non-disclosure, `CAP_SYS_ADMIN` dropped) |

Throughout, an observed postcondition is kept distinct from proof of execution/causation: every B reconciliation leaves `executionState`/`attribution`/`durability`/`operationOutcome` unknown and `treatAsSuccess` false.

---

## Phase 5 — decision gate

**Decision: (2) B needs specific, enumerated additional hardening before any deployment experiment — but the Week 4G regression is fully resolved and no new blocker was found.**

The `uniqueItems` failure is dispositioned (reference-validator unsoundness; StateGuard correct; test corrected; full suite green at **6075 passed, 0 failed**). The B prototype's integrated lifecycle, fail-closed behaviour (now including the previously inspection-only UID/GID mapping path), and seccomp story are verified as described. This is **not** option 3 (no seccomp/UID-GID/lifecycle blocker was found) and **not** option 1 (a green suite alone is not production readiness — see the open items).

Specific hardening required before a controlled deployment experiment (unchanged in spirit from 4G, now sharpened):
1. **UID/GID remap product decision** — accept 65534-owned files for target servers, or provide an identity mapping that preserves needed ownership. Gating.
2. **Narrow seccomp profile proven in ≥1 non-Docker runtime** (rootless Podman or a systemd user service), and a decision that StateGuard ships it as documentation only (never installs host policy). Currently [verified-in-docker] only.
3. **Direct "unprivileged userns disabled" fail-closed test** in an environment where the sysctl can be toggled (currently [unverified]; only the equivalent code path is shown).
4. **B behind a non-root relay in a real (containerised) deployment**, end-to-end, not just as uid 65534 in this host.
5. Keep `treatAsSuccess` false and the "experimental / not a sandbox / not production-ready" framing until 1–4 close.

An external supervisor (4F option C) is **not** reconsidered now: nothing in 4H weakened B's standing, and B remains the cheaper privilege reduction if items 1–2 resolve. If the UID remap proves unacceptable **and** no non-Docker narrow profile can be applied, revisit C.

---

## Validation [demonstrated]

| Gate | Result |
|---|---|
| Full suite | **6075 passed, 0 failed** (was 6071/1 in 4G) |
| B edge-case gate (scope A+B / security / CLI / env / reconciliation / conformance) | 131 passed |
| JSON Schema conformance file | 25 passed (incl. the corrected differential + new regression test) |
| New 4H tests | 3 (sound-oracle regression + 2 UID/GID mapping fail-closed) |
| `ruff check` / `ruff format --check` | clean (231 files) |
| `mypy src` | clean (67 files) |
| `mypy --strict` (`test_executor_scope_unprivileged.py`) | clean |
| Failing tests | none |
| Skipped tests | the scope tests skip only where the kernel capability is absent (not here); no new skips introduced |
| Production code changed | **none** (executor_scope.py, stdio_proxy.py, core/validator.py byte-identical to 4G) |
| Prior manifests (weeks 1–3, 4A–4G) | re-verified, unchanged |

**Note on `mypy --strict` and the conformance file:** that file is not in the repo's `--strict` set; its only `--strict` note is a pre-existing missing `types-jsonschema` stub, present at baseline `b6f7ba0` and unrelated to 4H.

---

## Files changed (Week 4H)

- `tests/adapters/jsonschema/test_2020_12_conformance.py` — `test_differential_unique_items` now asserts against a sound oracle (`_json_equal`, `_unique_items_expected`); the counterexample pinned with `@example`; new `test_unique_items_bool_number_duplicate_where_reference_is_unsound`.
- `tests/adapters/mcp/test_executor_scope_unprivileged.py` — `test_uid_gid_mapping_failure_fails_closed_with_a_reason`, `test_create_namespace_reports_mapping_failure`.

No production code changed. The JSON Schema adapter's (correct) `uniqueItems` behaviour was not modified.

---

## Next engineering package (recommendation)

**Week 4I — B deployment-prerequisite closure:** (a) make the UID/GID remap decision and, if an identity-preserving mapping is wanted, prototype it; (b) prove the narrow seccomp profile in a non-Docker runtime and settle the ship-as-docs-only question; (c) add the direct "userns disabled" fail-closed test where the sysctl is togglable; (d) run B behind a non-root relay in a container end-to-end. Only after (a)–(d) is a controlled deployment experiment (4G/4H option 1) justified.
