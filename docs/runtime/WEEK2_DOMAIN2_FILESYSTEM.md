# Week 2 — Second MCP ecosystem and multi-step workflow testbed

**Status:** published for review in a pull request. Controlled experimental evidence; no claim about production prevalence or demand.
**Date:** 2026-10-16.
**Branch:** `claude/serene-bohr-o28snr`, at `467396b`. That is the same commit as `origin/main`; the branch was fast-forwarded and has no commits of its own.
**Roadmap:** the revised four-week master roadmap, Week 2 (§2.1–2.4).

This report does not change historical Git/MCP evidence. Every Week 2 artifact has its own provenance, environment record and hashes. Private recordings, audit logs and result files stay outside the repository under `/root/stateguard-captures/domain2-fs/`. Only hashes, metadata, aggregates and redacted examples appear here; server paths are shown as `<path>`.

---

## 1. Executive summary

**What was done**

- **Week 1 closure verified.**
  - `8f94504` is contained in `main` (merge `467396b`).
  - CI run #44 on `467396b` concluded `success`; re-checked through the GitHub API today.
  - The full local suite passes: 5,776 tests before this work and 5,807 after.
  - The four private Git recordings are intact and unchanged.
- **Second ecosystem selected.**
  - Candidates evaluated: `@modelcontextprotocol/server-filesystem`, `@modelcontextprotocol/server-memory` and `mcp-server-sqlite`.
  - Selected: filesystem 2026.8.31, on the TypeScript SDK 1.32.1 with zod 4.6.7 and Node 22.22.0.
  - It is pinned by exact version plus a lockfile with integrity hashes for all 104 packages.
  - sqlite was rejected because its unpinned dependency set does not start. Memory is the documented runner-up.
- **Offline characterization: 39 probes** covering lifecycle, contract and effect/replay behaviour, with on-disk ground truth.
  - Every mutating operation was replayed on a fresh sandbox, which gave an evidence-based effect class for each one.
- **Four-step workflow built**: inspect → approve → file → log.
  - Ground truth is the on-disk state, compared with five oracle checkpoints computed from the fixture text alone.
  - Eight step states and seven workflow states.
  - A fault shim simulates six fault kinds.
- **Workload: 20 tests, 59 runs** across `raw` (no StateGuard), `shadow`, `enforce` and one scoped `restart` configuration.
  - Every run matched the outcome written before execution: 59/59, 0 discrepancies.
  - The sentinel outside the sandbox never changed.
- **Shadow `real_capture`: 10 recorded sessions, 27 tool calls.**
  - The client sees byte-identical traffic with and without the relay.
  - Both directions through the relay are byte-identical in order, checked independently with a digest-only tee.
  - Each recording's own digest comparison agrees: 27/27 calls forwarded unchanged.
  - No fixture value or path appears in any recording.

**Most important result.**

A lost answer leaves the client with identical evidence in three tests (I1, I2, I3), whatever actually happened:

- in I1 the call never executed;
- in I2 the move executed;
- in I3 a non-idempotent append executed.

StateGuard correctly calls all three "outcome unknown" and never re-sends them. The right next action still differs by case, and only an independent state check tells them apart:

- the naive baseline's re-send is correct after I1 (D3);
- after I2 it reports failure for a move that happened (D2);
- after I3 it silently duplicates the side effect (D1).

The duplicate is invisible on the wire. StateGuard cannot connect the re-send to the earlier unknown, because its ledger lives in one relay process and the client reconnected.

That is the concrete, narrow gap Week 3 should close: reconcile an UNKNOWN invocation against an operator-declared postcondition check, and stop otherwise (§14).

---

## 2. Week 1 verification

| Check | Result | Evidence |
|---|---|---|
| Branch / HEAD / remote | `claude/serene-bohr-o28snr` at `467396bc94f4…`, equal to `origin/main`. Remote: `github.com/dood1ebyte/stateguard`. Before this work the tree was clean apart from the new untracked files listed in §12. | `git rev-parse`, `git status` |
| `8f94504` in main | Yes | `git merge-base --is-ancestor 8f94504 origin/main` |
| Merge | [dood1ebyte/stateguard#17](https://github.com/dood1ebyte/stateguard/pull/17), merge commit `467396b` | GitHub |
| CI on the merged commit | Run #44 (id 38550827385): `push` to `main`, head `467396b`, `completed` / `success` | `actions_get get_workflow_run`, re-queried 2026-10-16 |
| Local suite on `467396b` | 5,776 passed, 0 failed, 0 skipped; ruff, format and mypy clean | `pytest`, `ruff check src tests`, `ruff format --check src tests`, `mypy src` |

The four private Git recordings, in mask redaction, unchanged since 2026-10-12. Their sha256 prefixes match the values recorded during Week 1 closure in this session.

| Recording | sha256 (16) | Provenance / mode | Complete | Records | Tool calls (unchanged) |
|---|---|---|---|---|---|
| capture-1a | `5952c1898ede45ce` | real_capture / shadow | yes | 125 | 18 (18) |
| capture-1a re-run | `44d4798335097769` | real_capture / shadow | yes | 110 | 18 (18) |
| capture-1b | `636880d507888b38` | real_capture / shadow | yes | 263 | 34 (34) |
| capture-zt | `bdc65c64ecc0de41` | real_capture / shadow | yes | 72 | 8 (8) |

`evidence_report.py --json` exits 0 on each one.

**Invocation ledger and recovery logic against the roadmap lifecycle.** Files: `ledger.py`, `effects.py`, `stdio_proxy.py`.

| Roadmap requirement | In code? | Where |
|---|---|---|
| Distinguish not-sent from sent | Yes. `PENDING → SENT` is recorded *before* the write. "Not sent" is answered only from `_answer_not_sent`, on paths where nothing was written. | `ledger.sent`, `_send_tracked`, `_send_decided` |
| Execution unknown on transport loss or deadline | Yes. A failed send, a closed pipe or a passed deadline makes the request UNKNOWN, and UNKNOWN never becomes COMPLETED. A late answer is dropped. | `transport_closed`, `expire`, `_resolve_unknown` |
| No replay of unknown | Yes. `decide_retry` never retries an unknown outcome, auth errors or undeclared tools; the budget is 1. A restart never re-sends in-flight work, and `idle-only` refuses to restart while anything is in flight. | `effects.decide_retry`, `_on_server_lost` |
| Result invalid as a state | **No.** Result checks are logged (category H) but do not change the ledger state; the request is COMPLETED. | `check_tool_result` |
| Recovered / escalated / aborted / reconciled states | **No** | — |
| Postcondition verification, checkpoints, reconciliation | **No** | — |
| Invocation identity across relay processes | **No.** The ledger is in memory, one per relay process. | — |

One latent issue was found. It is not exercised by this workload and was not changed:

- `_answer_not_sent` tells the client "not sent, did not execute" whether or not `ledger.answered_locally` accepted the transition.
- `_finish` only records an anomaly when the request is no longer PENDING.
- Every current caller runs before `ledger.sent`, so the claim is true today. It rests on caller ordering, not on the ledger's verdict.
- Recommended for Week 3: `answered_locally` returns whether it applied, and a refused transition answers "unknown".

**Unresolved Week 1 board items.**

- *Confirm merged commit and CI status on `main`*: **done** (above).
- *Finalize invocation lifecycle and recovery eligibility table*:
  - The lifecycle table above, together with the eligibility analysis in §11, is a draft of it.
  - It is **partial** until you review it, because the board must not be ticked merely because a document exists.

---

## 3. Candidate comparison

All three candidates were installed in private directories. Each was listed through an SDK client with no relay in the path, with `PATH` and `HOME` only.

| Criterion | **filesystem** 2026.8.31 | **memory** 2026.8.31 (serverInfo 0.6.3) | **sqlite** 2025.4.25 (PyPI) |
|---|---|---|---|
| Runs reproducibly? | Yes (`npm ci --ignore-scripts`, lockfile) | Yes (npm, lockfile) | **No.** Its unpinned `mcp` dependency resolved to 2.3.0 and startup crashed with `AttributeError: 'Server' object has no attribute 'list_resources'`. |
| SDK / implementation | TypeScript SDK 1.32.1 + zod 4.6.7 (Git: Python SDK 1.30.0) | Same TypeScript SDK | Python SDK; not usable as resolved |
| Tools | 14: 9 read-only, 4 mutating, plus the `dryRun` variant | 9: 3 read, 6 mutating | not listed (crash) |
| Schema dialect | draft-07 `$schema` on every input and output schema | draft-07 | — |
| `outputSchema` / `structuredContent` | On every tool, but trivial: `{content: string}`, equal to the text | **Rich**: structured entity and relation objects | — |
| Error format | `isError` results, including SDK validation errors (text "MCP error -32602 …") | Same SDK conventions | — |
| Lifecycle | Roots protocol (`roots/list`) can replace the sandbox; `listChanged: true` | `resources` plus `tools.listChanged` | — |
| Effect classes available | read-only, idempotent overwrite, **non-idempotent edit whose replay duplicates**, **move whose replay fails**, destructive, `dryRun` | create, add, delete — mostly idempotent or reported duplicates | — |
| Independent ground truth | **OS-level**: hash files directly, no server involved | The server's own JSON store file, which must be parsed | — |
| Isolation boundary | Built-in allowed-directory check; escape attempts are observable against a sentinel | None (one data file) | — |
| Credentials / production data | None | None | None |

**Selection: filesystem.**

It adds the most that Git does not:

- independent ground truth from the OS rather than from the server;
- the full spread of replay behaviours (no-op, failing, duplicating) in one small workflow;
- a sandbox boundary with observable denials;
- a different SDK implementation, with different lifecycle and error conventions.

**Trade-off accepted.** Its `outputSchema` is trivial, so result-integrity checks against a schema carry little information here (R2 has to *simulate* a schema break). Memory is the stronger candidate for structured-result integrity and is the recommended third domain if Week 3 needs one.

**Rejected:**

- **sqlite** — it does not start as resolved, which is a reproducibility risk in its own right.
- **memory** — its effect semantics give weaker replay contrast and its ground truth comes from the server's own store.

---

## 4. Environment specification

| Item | Value |
|---|---|
| Server | `@modelcontextprotocol/server-filesystem` **2026.8.31** (serverInfo `secure-filesystem-server` 0.2.0) |
| SDK and validator | `@modelcontextprotocol/sdk` **1.32.1**, `zod` **4.6.7** (from the lockfile) |
| Runtime | Node **22.22.0**, npm **10.9.4**; Linux 6.18 container |
| Client, relay and harness | The repository `.venv`, Python 3.11.17. The harness uses its own JSON-RPC client (`rawclient.py`), not an MCP SDK. |
| Pin files (new) | `benchmarks/domains/mcp_server_filesystem/env/package.json`: exact version and `engines`, sha256 `c8cd7146ba218537`. `env/package-lock.json`: 104 packages with integrity hashes, sha256 `404c089bc4b2d2b8`. |
| Install | `npm ci --ignore-scripts` into a private directory (`/root/stateguard-captures/domain2-fs/server`) |
| Configuration | One allowed directory: `WORKSPACE/sandbox`. No client roots are sent. Environment: `PATH`, `HOME=WORKSPACE`. |
| Capabilities negotiated | `tools.listChanged: true`; protocol `2025-11-25` |
| Startup | `mcp-server-filesystem WORKSPACE/sandbox`, newline-delimited JSON-RPC on stdio |
| Shutdown | Client closes stdin; server exits 0 (L8) |
| Fixture | `fixture.FILES`, synthetic: an order file (`inbox/order-001.txt`), a log with an `END` marker, an empty `done/`, and a two-section `config/settings.ini`. A sentinel sits *outside* the sandbox. |
| Reset | `fixture.reset(sandbox)`: remove the sandbox and rebuild it. Runs before every test run, and is asserted equal to checkpoint 0. |
| Network | None needed at run time. The server's `dist/` has no HTTP or socket calls (grep). npm was used only for the install. |

**Pinning risk.** Node and npm are not vendored, only declared in `engines`. A different Node major version could change behaviour; the probe suite can detect that.

Exact rebuild and rerun commands are in §12 and in `benchmarks/domains/mcp_server_filesystem/README.md`.

---

## 5. Tool and operation inventory

**Schemas.**

- 14 tools. Every input and output schema declares draft-07.
- No `additionalProperties` anywhere, so all schemas are open.
- Every tool has an `outputSchema` of `{content: string}` (required).
- Annotations are present on all 14 tools: `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint: false`.
- StateGuard reads all 14 schemas; no tool is unreadable.

**Results.**

- Success: `{content: [text], structuredContent: {content: <same text>}}`.
- Error: `{content: [text], isError: true}`, with no `structuredContent`.
- Probe lines are numbered L (lifecycle), C (contract) and E (effect/replay).

### Validation and contract behaviour (offline facts, with no StateGuard in the path)

| Probe | Input | Server behaviour |
|---|---|---|
| C2 / C3 / C5 / C8 / C13 / E11 | Missing required field / integer path / numeric string `head` / bad enum / scalar for array / edit item missing `newText` | `isError` with text `MCP error -32602: Input validation error …`. A **tool result, not a JSON-RPC error**. |
| C4 | Undeclared extra key | Accepted, silently ignored |
| C6 | `head: 1.5` | **Accepted and executed**: returns 2 lines |
| C7 | `head: 2.0` | Accepted: 2 lines |
| C12 | Unknown tool | `isError` with text "MCP error -32602: Tool … not found". Not a protocol error. |
| C14 | `read_multiple_files` with one bad path | **Success** result; the error is embedded in the text |
| C15 | Relative path | Resolved against the allowed directory; success |
| C10 / C16 / E10 | Path outside the sandbox, `..` traversal, write outside | `isError` "Access denied …"; nothing changed. The message echoes full paths. |
| C18 | `head` and `tail` together | `isError`, enforced in the handler, not the schema |
| L1 / L3 | `tools/list` before `initialize`, or before `notifications/initialized` | **Answered** (Git rejects both with -32602) |
| L4 / L5 | `server/discover`, unknown method | JSON-RPC **-32601** (Git: -32602) |
| L6 | Malformed JSON line | Silently ignored; no error response |
| L9 | Old protocol `2024-11-05` | Negotiated, not refused |
| L10 | Client declares roots | The server requests `roots/list`. The client's root **replaces** the command-line sandbox: reading the sandbox is now denied and the other root is readable. `list_allowed_directories` text was still the original. |

### Effect classification

Each mutation was run once and then replayed identically on a fresh sandbox, with OS-level snapshot diffs (E1–E11) plus source inspection.

| Operation | Annotation hints | Observed first effect → replay | Class (evidence) | Facts vs assumptions |
|---|---|---|---|---|
| `read_*`, `list_*`, `directory_tree`, `search_files`, `get_file_info`, `list_allowed_directories` | readOnly | No change (C-probes, S1 runs) | **read-only** | Fact for the probed tools and inputs; others from source |
| `write_file`, new or overwrite (E1, E2) | destructive, idempotent | Creates or replaces → identical replay leaves identical bytes, success | **idempotent at content level for identical content; destructive** (overwrites) | Fact: content hash. Assumed: metadata. Source: `'wx'` exclusive create, else temp file plus rename, so the inode changes on replay. |
| `edit_file`, replace once (E3) | destructive, non-idempotent | Edits → replay gets `isError` "Could not find exact match", no change | **non-idempotent, self-guarding**: the replay fails, so a client is told "failed" for work that was done | Fact |
| `edit_file`, insert before a marker (E4) | destructive, non-idempotent | Edits → **replay succeeds and duplicates** the line | **non-idempotent, duplicating** | Fact. Whether a replay guards itself depends on the edit's *shape*, not on the tool. |
| `edit_file` with `dryRun: true` (E5) | (annotated as for `edit_file`) | No change, twice | **read-only in effect** | Fact. Per-tool annotations cannot express an effect that depends on an argument. |
| `edit_file`, ambiguous match (E8) | — | Replaces the **first** match ([a]); the replay replaces the next ([b]) | **non-idempotent; semantically ambiguous** | Fact |
| `edit_file`, one bad edit of two (E9) | — | `isError`, no change | **atomic** for this case | Fact for this input only |
| `move_file` (E6) | destructive, non-idempotent | Moves → replay `isError` "Destination already exists", no change | **non-idempotent, self-guarding** | Fact |
| `create_directory` (E7) | idempotent | Creates both levels → replay success, no change | **idempotent** | Fact |
| Anything outside the sandbox (E10) | — | Denied | **known pre-execution failure**: `validatePath` runs before every handler | Fact on disk; source for the ordering |
| `read_media_file`, `search_files` (large trees), symlinks | — | Not probed | **unknown** | — |

**Evidence quality.**

- One probe per row, on a single fixture.
- Replays were done in the same session. A second session was used only in the D-tests.
- Metadata (mtime, inode) was deliberately not part of ground truth.

---

## 6. Workflow specification

The intended outcome is that order 001 is approved, filed into `done/` and logged exactly once. Nothing else in or outside the sandbox changes.

| Step | Call | Depends on | Precondition (on disk) | Postcondition (on disk) | May change |
|---|---|---|---|---|---|
| S1 inspect | `read_text_file inbox/order-001.txt` | — | Order equals the fixture (pending) | Disk = checkpoint 1 (unchanged) **and** the returned text equals the file on disk | nothing |
| S2 approve | `edit_file` pending → approved | S1 | Order in inbox, "status: pending" exactly once, not approved | Disk = checkpoint 2 | the order |
| S3 file | `move_file` inbox → done | S2 | Approved order in inbox, `done/` slot free | Disk = checkpoint 3 | the order, its new location |
| S4 log | `edit_file` insert "approved order 001" before `END` | S3 | Filed, line absent | Disk = checkpoint 4 (line exactly once) | the log |

- **Ground truth.** `fixture.snapshot` hashes every file and directory, plus the sentinel. `fixture.checkpoints()` computes the five intended states from the fixture text with Python string operations. Neither uses the server or StateGuard.
- **Checkpoints.** A snapshot is taken before and after every attempt.
- **Invariants.**
  - The sentinel never changes.
  - No step changes a path outside its own row.
  - The final state equals checkpoint 4, or the run is not "completed".
- **Stop conditions.**
  - The first step that is not `completed` or `recovered` stops the run.
  - So does a precondition that fails.
  - The harness never retries, except in the explicitly labelled naive-retry *baseline* (D-tests).

**Step states.** These are derived from what the client was told (wire class) combined with ground truth:

- `not_attempted`
- `known_not_executed`: the relay refused it or answered not-sent, or the client's write failed (EPIPE)
- `failed_reported`
- `completed`
- `recovered`: completed although the call was faulty, because the relay repaired it before execution
- `execution_unknown`
- `result_invalid`: malformed, off-schema, or contradicting the disk
- `postcondition_violated`: success-looking

**Workflow states:**

- `completed` / `recovered`
- `partially_completed_stopped`: clean, the disk is at the checkpoint the client believes
- `not_started_stopped`
- `stopped_execution_unknown` / `stopped_result_invalid` / `stopped_postcondition_violated`: `disk_checkpoint` records what reconciliation would find
- `failed_unrecovered`: the disk matches no checkpoint (a wrong or duplicate effect), or the client was told a step failed or never ran and the disk moved on anyway

**Repeatability.** V1 ran 3× in each configuration. Decisions and outcomes were identical across repetitions.

---

## 7. Test matrix

Configurations:

- `raw` — client → [shim →] server
- `shadow` — relay `--shadow`
- `enforce` — relay in auto mode, `--retry off`, no restart
- `restart` — enforce plus `--restart 1`, with the default `idle-only` policy

Notes on the method:

- "Live" means a real server with injected *client inputs*. "Sim" means a fault simulated by `shim.py` in front of the real server.
- Expected outcomes were written into `workload.py` before execution.
- **Every run matched: 59/59.**

| ID | Hypothesis | Fault and stage | Live/Sim | Expected = observed (raw / shadow / enforce) | Raw server behaviour | StateGuard decision |
|---|---|---|---|---|---|---|
| V1 ×3 | Correct workflow completes | none | live | completed / completed / completed | 4 successes | forward, none; no finding |
| C1 | `edits` sent as one object | S2 args, pre-execution | live | stopped clean at 1 / same / **recovered** | validation `isError` | shadow HOLD (would repair: coerce); enforce repaired → executed → verified |
| C2 | `dryRun: "false"` | S2 args, pre-execution | live | as C1 | validation `isError` | as C1 |
| C3 | `edits` as JSON text | S2 args, pre-execution | live | as C1 | validation `isError` | as C1 |
| U1 | `destination` as a one-element list | S3 args, pre-execution | live | stopped clean at 2 (failed_reported) / same / stopped clean at 2 (**known_not_executed**) | validation `isError` | shadow HOLD (would escalate); enforce ESCALATE, never sent |
| U2 | `oldText` matches both sections; intent is [b] | execution semantics | live | postcondition_violated ×3 | **success**; edited [a] | forward, none; **no finding** |
| P1 | `edits` missing | S2 args, pre-execution | live | stopped clean at 1 / same / stopped clean (**known_not_executed**) | validation `isError` | shadow HOLD (would refuse); enforce REFUSE |
| P2 | Destination outside the sandbox | S3, server path check | live | stopped clean at 2 ×3; sentinel intact; no escape file | `isError` access denied | forward (schema-valid) |
| P3 | Server exits idle before S3 | before send | sim | known_not_executed ×3 (client write EPIPE) | process gone | `server_lost`; relay stops (restart 0). The relay's own `not_sent` answer is **not** reached on this path. |
| I1 | S3 swallowed, server exits | after send, not executed | sim | stopped_execution_unknown ×3; disk at **cp 2** | no answer | shadow: `outcome_unknown` logged, not surfaced; client sees EOF. Enforce: `-32000`, executionState `unknown`. |
| I2 | S3 executed, answer lost | after execution | sim | stopped_execution_unknown ×3; disk at **cp 3** | no answer | as I1 |
| I3 | S4 (append) executed, answer lost | after execution | sim | stopped_execution_unknown ×3; disk at **cp 4** | no answer | as I1 |
| D1 | Baseline re-sends S4 after I3 (new session) | I3 + client re-send | sim | failed_unrecovered (raw, enforce); **log line twice**; retry told *success* | success twice | 2nd relay process: forward, none; **no link to the unknown** |
| D2 | Baseline re-sends S3 after I2 | I2 + client re-send | sim | failed_unrecovered; told "Destination already exists"; disk at cp 3 | `isError` | as D1 |
| D3 | Baseline re-sends S3 after I1 | I1 + client re-send | sim | completed | success | as D1 |
| R1 | S1 text rewritten "approved" (schema-valid) | result content | sim | stopped_result_invalid ×3 (read-back ≠ disk) | — | **no finding** (result passes `outputSchema`) |
| R2 | S2 `structuredContent` `{content: 42}` | result structure | sim | stopped_result_invalid ×3; disk at **cp 2** (executed) | — | `result_check` category **H**, delivered unchanged |
| S1 | S3 answered "success", never executed | fabricated success | sim | stopped_postcondition_violated ×3; disk at cp 2 | — | **no finding** |
| RS1 | Idle exit after S2; `--restart 1` | before send | sim | completed | — | `server_lost` → `server_restarted` → `reinitialized`; S3 gated, then sent to the new process |
| RS2 | S3 answer lost; `--restart 1`, idle-only | after execution | sim | stopped_execution_unknown; cp 3 | — | `outcome_unknown` surfaced, **`restart_refused`**, nothing re-sent |

**Aggregates (59 runs, 179 step attempts).**

- Step states:

  | State | Count |
  |---|---|
  | completed | 126 |
  | execution_unknown | 16 |
  | failed_reported | 15 |
  | postcondition_violated | 8 |
  | result_invalid | 6 |
  | known_not_executed | 5 |
  | recovered | 3 |

- Workflow states:

  | State | Count |
  |---|---|
  | partially_completed_stopped | 18 |
  | completed | 12 |
  | stopped_execution_unknown | 10 |
  | stopped_result_invalid | 6 |
  | failed_unrecovered | 4 |
  | recovered | 3 |
  | postcondition_violated (U2) | 3 |
  | stopped_postcondition_violated | 3 |

**Latency (V1, median per step, 3 runs each; indicative only).**

| Configuration | S1 / S2 / S3 / S4 | Total |
|---|---|---|
| raw | 6 / 5 / 4 / 4 ms | 19 ms |
| shadow | 19 / 8 / 5 / 5 ms | 37 ms |
| enforce | 16 / 7 / 6 / 5 ms | 34 ms |

The S1 cost is first-call overhead.

**Limitations.**

- Simulated faults are injected at the shim, not inside the server.
- `drop_response` relies on this server writing before it answers.
- A delayed answer, `--call-deadline`, a stale listing (`tools/list_changed`), partial writes and concurrent clients were **not tested**.
- Auth failures are not applicable: the server has no auth.

---

## 8. Shadow-capture report

**Configuration.**

- Command: `stdio_proxy.py --shadow --record <rec> --record-provenance real_capture --audit <audit> --retry off -- mcp-server-filesystem <sandbox>`.
- `real_capture` forces mask redaction and number masking; the relay refuses to start otherwise.
- Workload: the 7 live tests (V1 ×3, C1–C3, U1, U2, P1, P2), giving 10 sessions and 27 tool calls.
- Workspace: `/root/stateguard-captures/domain2-fs/capture-final`.
- Time: 2026-10-16 05:45:15Z–05:45:29Z.

**Before capturing, confirmed:**

- the allowed directory is only the sandbox;
- `HOME` is the workspace;
- no credentials or network are involved;
- no client roots are sent (L10: roots would change the sandbox);
- in shadow mode, `forward_message` returns the original message and the relay writes the original line;
- in shadow mode, restart and retry are log-only (`_on_server_lost`, `_retry_mode`);
- the repository status was identical before and after the capture.

**Fidelity** (`fidelity.py`, all 10 sessions, three passes in one workspace so that paths are byte-comparable):

| Check | Result |
|---|---|
| Lines the client sent and received, with relay vs without (sha256 per line, in order) | **identical, 10/10** |
| Same, with the tee shim in the path | identical, 10/10 |
| Client → server through the relay (tee pass), byte-identical and in order | **10/10** |
| Server → client through the relay, byte-identical and in order | **10/10** |
| Extra server-side lines | only the relay's own `tools/list` requests (15) and their answers, never delivered to the client |
| Same ground truth (workflow state, disk checkpoint) in all three passes | 10/10 |
| Recording's own HMAC digest comparison (evidence report) | 27 tool calls compared by digest, 27 forwarded unchanged, 0 changed, 0 not forwarded; 37 client answers, 0 not from the server |

Byte equality implies semantic JSON equality, so no separate semantic check was needed.

**Recording metadata.**

- All 10 recordings: `real_capture`, `shadow`, `mask`, complete, `exit 0`.
- 266 records and 863 values redacted.
- One server generation per session.
- Evidence counts:
  - `calls_with_schema_violations` 5 (C1, C2, C3, U1, P1);
  - `would_modify_call` 5 (repair 3, escalate 1, refuse 1);
  - `retry_decisions.reject` 6;
  - result findings 0; unknown outcomes 0.

**Privacy.**

- The 10 recordings and 10 shadow audit logs were searched for 18 fixture values: file lines, the log line, the sentinel text, the workspace and sandbox paths, and server message phrases.
- **Recordings: 0 hits.**
- Audit logs: the workspace and sandbox paths appear **only** in `relay_started`. That event records the server command line, which is operator configuration, not traffic.
  - The audit log is private by rule and is not part of the recording.
  - It is a residual exposure worth noting: an operator's paths land in audit logs.
- One needle, `[a]`, maps to itself under the mask (letters become `a`) and so cannot be checked. Its single hit is masked output, confirmed by the character-class distribution of the string.
- Mask mode preserves string length and punctuation, as documented. That structural leakage is accepted.

**Hashes** (sha256, first 16 hex):

- Manifest of the 10 recordings: `cef453720aac8b7e`. Its lines are `<name> <sha256>`, sorted by name.
- Individual recordings:

  | Recording | sha256 (16) |
  |---|---|
  | C1 | `9c3cf4fb7ac1ad13` |
  | C2 | `11817702ac300bbc` |
  | C3 | `b6eb46b3aa81ee9c` |
  | P1 | `f1a881371ce6ea49` |
  | P2 | `00149f9def1f2aae` |
  | U1 | `e8cc21f3b1176ed8` |
  | U2 | `0781f3e693b2416b` |
  | V1-0 | `4e08fc1af5f2c644` |
  | V1-1 | `b365138017a4c1fa` |
  | V1-2 | `734cb8e1a15a1d8c` |

- Audit manifest: `d9d1facfb090fbb2`.
- Results:

  | File | sha256 (16) |
  |---|---|
  | `results-raw.json` | `28a9beb529b42d9b` |
  | `results-shadow.json` | `097a7763becf561a` |
  | `results-tee.json` | `d701de99240a782d` |
  | `fidelity.json` | `57697bcd7beaaaf2` |
  | probe results | `025b4d5513f8b638` |
  | workload results (59 runs) | `5eae0b567c5ecf84` |

**Limitations.**

- Only the shadow pass is `real_capture`.
- The fault-injection runs (§7) were not recorded as `real_capture` and must not be.
- The audit logs are not redacted by design.
- Earlier iterations of the same workload (`capture-2`, `workload-1`, run before a lint-only refactor) remain in the private directory. They are superseded and not cited.

**Capability gaps found:** none blocking. The capture system supported this server without changes.

---

## 9. Cross-domain comparison: Git/MCP versus filesystem, by mechanism

**Labels:**

- **[FS]** observed in filesystem
- **[GIT]** previously observed in Git/MCP
- **[BOTH]** reproduced in both
- **[HYP]** hypothesized, untested
- **[N/O]** not observable with the current harness

| Mechanism | Finding | Label |
|---|---|---|
| Contract / input integrity | Single-value wrap, string-to-boolean and JSON-text-to-array repairs are applied correctly, and the step is verified on disk afterwards (C1–C3). Container-to-string and list-to-path casts are held (U1), the R1 boundary from Week 1. | [BOTH]: the Git boundary was set from Capture 1b / Z-T; the filesystem results confirm it on a different SDK |
| Contract / input integrity | Open schemas: an undeclared key is accepted and ignored (C4); StateGuard flags it only as a warning | [BOTH] |
| Schema/runtime disagreement | `head: 1.5` is schema-valid (number) and executed as 2 lines: the schema says number, the handler acts on an integer | [FS] |
| Schema/runtime disagreement | Constraints enforced only in the handler (`head` with `tail`, C18) | [FS]; Git had analogous runtime-only rejections [GIT] |
| Schema dialect | Filesystem declares draft-07 on every schema; Git declares none (12 tools, `$schema` absent). StateGuard reads both. | [FS] / [GIT] |
| Protocol / lifecycle | Both negotiate protocol 2025-11-25 and declare annotations on every tool, so these are **not** contrasts. | [BOTH] |
| Protocol / lifecycle | Filesystem answers requests before initialize; Git rejects them (-32602). Unknown method: filesystem -32601, Git -32602. Malformed line: filesystem silent. | [FS] / [GIT] |
| Protocol / lifecycle | Client roots silently replace the command-line sandbox; `list_allowed_directories` text goes stale | [FS] |
| Protocol / lifecycle | Idle restart: replay `initialize`, re-list, gate calls, then forward (RS1). In-flight: refused under idle-only (RS2). | [FS] live; Git: synthetic relay tests only |
| Invocation-state uncertainty | A lost answer gives the client identical evidence whether or not the call executed (I1/I2/I3) | [FS] (simulated). Git: synthetic relay tests only; never against the real Git server [HYP for Git] |
| Error classification | Validation failures are `isError` tool results. StateGuard classifies them as `tool` failures (category F) and cannot tell "rejected before execution" from "executed and failed". | [BOTH]: consistent with capture-1b, where all tool-call failures were `tool`-class answers (22) alongside 19 schema-violating calls, and no tool call got a JSON-RPC error |
| Error classification | Relay refusals carry no machine-readable execution state, only lead text; not-sent and unknown answers do carry it | [FS] (relay property; the same in Git) |
| Result integrity | A structural break is detected (category H, R2). A schema-valid wrong result is not (R1). The filesystem `outputSchema` is too weak to carry meaning. | [FS] |
| Success-looking semantic failure | A first-match edit reports success on the wrong section (U2). Partial failure inside a success result (C14). Fabricated success (S1, simulated). | [FS]; Git's JSON-text timestamp returned the wrong history with success [GIT] → mechanism [BOTH] |
| Side effects / idempotency | Replays fail for edit and move, silently duplicate for an insert edit, and are a no-op for write and create. An edit's class depends on the shape of its arguments; annotations are per tool. | [FS]. Git mutations (`git_add`, `git_checkout`) were never replayed, so this is not established for Git [HYP] |
| Recovery eligibility | Only pre-execution repair (C1–C3) and idle restart (RS1) are eligible and were verified. Nothing after send is eligible without reconciliation. | [FS] |
| Independent postcondition verification | Filesystem: OS-level hashes against computed checkpoints. Git: fixture repository inspection. Both are harness-side; StateGuard has no postcondition hook. | [BOTH] (harness-only) |
| Shadow-mode fidelity | Byte-identical in both directions; zero intervention | [BOTH] (Git via digests; filesystem via digests plus an independent tee) |
| Cross-session identity | A re-send after reconnect reaches a fresh ledger; the duplicate is undetectable to StateGuard | [FS]; [HYP] for Git |
| Harness and measurement limits | Delayed answers, stale listings, concurrency, partial writes and auth were not tested in the filesystem domain. Real crashes inside the server are not observable with the shim. | [N/O] |

Two servers do not license claims about MCP servers in general. Where nothing was found, that is not evidence of safety.

---

## 10. Failure analysis: what StateGuard detected, missed or could not know

- **Detected and acted on correctly:**
  - The contract faults in C1–C3: proposed in shadow, repaired in enforce and verified on disk.
  - Unsafe casts: held (U1).
  - Unrepairable calls: refused (P1).
  - Lost answers: UNKNOWN, surfaced in enforce, logged in shadow, never re-sent (I1–I3, RS2).
  - The structural result break: category H (R2).
  - Idle restart (RS1); the in-flight restart refusal (RS2).
- **Detected but not reflected in state.** R2's result break is logged and the invocation is still COMPLETED. The ledger has no `result_invalid`.
- **Missed** (schema-valid, so invisible at the contract layer):
  - the ambiguous edit (U2);
  - the wrong content (R1);
  - the fabricated success (S1);
  - the fractional `head` (C6);
  - the partial failure embedded in a success (C14);
  - the duplicate after reconnect (D1).
- **Could not know:**
  - Whether an UNKNOWN executed (I1 versus I2).
  - Whether a server-reported validation error ran nothing. It did run nothing here, but StateGuard has no basis to say so.
  - That D2's "Destination already exists" means the earlier move succeeded.
- **Harness artifacts, kept separate:**
  - In shadow mode and with no restart, the relay exits on server loss. In P3 the client's own write failure (EPIPE) is therefore the evidence of "not sent"; StateGuard's not-sent answer is not involved.
  - `server_lost` at every normal session end is the relay noting the expected exit; it is not a failure.

---

## 11. Safety analysis

- **Execution unknown.** There were 16 unknown step attempts; 6 of them were raw, with no StateGuard in the path. In all 10 relayed ones (shadow 3, enforce 6, restart 1) StateGuard logged `outcome_unknown` and did not re-send. Shadow did not surface it; enforce and restart answered `-32000` with executionState `unknown`. Retries were off; under the code's existing `decide_retry` rule StateGuard would not re-send with retries on either. No false "completed" or "not executed" claim was made.
- **Replay risk** depends on the shape of the call:

  | Re-send after an unknown | Outcome |
  |---|---|
  | D3 (call never executed) | correct |
  | D2 (move already executed) | misleading failure |
  | D1 (append already executed) | silent duplicate |

  No rule based on the wire, the schema or the annotations separates these. Only a postcondition check does: "is the order in `done/`?", "is the line present?".
- **Duplicates.** One duplicate effect was produced, deliberately, by the baseline client (D1). It was detected only by the oracle; the server's answer was success. Not one was caused by StateGuard: 0 StateGuard-caused failures across 59 runs.
- **False recovery claims.** Zero.
  - The 3 `recovered` runs are pre-execution repairs, verified against checkpoint 4.
  - No run was marked recovered without that check.
  - The harness classifies every success as `postcondition_violated` unless the disk agrees (8 instances caught).
- **Eligibility, as observed:**
  - Eligible and verified: pre-execution repair of a proven-safe cast; idle restart without in-flight work; and, inferred, a re-send after `known_not_executed`, because no attempt reached the server.
  - **Not eligible without reconciliation**: anything after send.
  - Never eligible: unknown non-idempotent work re-sent blindly; semantically ambiguous edits.
- **Containment.** Sandbox escapes (C10, C16, E10, P2) were denied and the sentinel stayed intact in every run. Roots (L10) can move the sandbox, so a client that sends roots weakens isolation. The harness never sends roots.

---

## 12. Reproducibility

Files added by this change. No existing file was modified. The repository `.gitignore` ignores `env/` (meant for virtualenvs), so the two pin files under `env/` are committed explicitly with `git add -f`; a later edit to them must be staged the same way.

```
benchmarks/domains/mcp_server_filesystem/README.md
benchmarks/domains/mcp_server_filesystem/env/package.json
benchmarks/domains/mcp_server_filesystem/env/package-lock.json
benchmarks/domains/mcp_server_filesystem/fixture.py      (sha256 ee2e73d167fb86fe)
benchmarks/domains/mcp_server_filesystem/rawclient.py    (6589c75d3ee6d726)
benchmarks/domains/mcp_server_filesystem/workflow.py     (7da087a2bce1411a)
benchmarks/domains/mcp_server_filesystem/shim.py         (c9e936fecaa3fea0)
benchmarks/domains/mcp_server_filesystem/workload.py     (40a4ada1d0ab0268)
benchmarks/domains/mcp_server_filesystem/fidelity.py     (50a7744a5309961c)
benchmarks/domains/mcp_server_filesystem/probe.py        (b6a8f349c057b5e0)
tests/test_domain2_filesystem_testbed.py                 (717ff67dbde43b69; 31 tests, no Node needed)
docs/runtime/WEEK2_DOMAIN2_FILESYSTEM.md                 (this report)
```

Commands run, from the repository root (`D=/root/stateguard-captures/domain2-fs`):

```sh
# environment
mkdir -p $D/server && cp benchmarks/domains/mcp_server_filesystem/env/package*.json $D/server/
(cd $D/server && npm ci --ignore-scripts)
SRV=$D/server/node_modules/.bin/mcp-server-filesystem; P=benchmarks/domains/mcp_server_filesystem
# characterization, workload, capture
.venv/bin/python $P/probe.py    $SRV $D/probe-ws        $D/probe-results.json
.venv/bin/python $P/workload.py $SRV $D/workload-final  $D/workload-final/results.json
W=$D/capture-final
.venv/bin/python $P/workload.py $SRV $W $W/results-raw.json    --configs raw    --live-only
.venv/bin/python $P/workload.py $SRV $W $W/results-shadow.json --configs shadow --live-only --record real_capture
.venv/bin/python $P/workload.py $SRV $W $W/results-tee.json    --configs shadow --live-only --tee
.venv/bin/python $P/fidelity.py $W $W/fidelity.json
# checks
.venv/bin/python -m pytest -q                       # 5807 passed, 0 failed, 0 skipped (165 s)
.venv/bin/python -m pytest tests/test_domain2_filesystem_testbed.py -q   # 31 passed
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests # clean (178 files)
.venv/bin/mypy src                                                       # clean (65 files)
.venv/bin/ruff check benchmarks/domains/mcp_server_filesystem/ && .venv/bin/mypy --strict benchmarks/domains/mcp_server_filesystem/ tests/test_domain2_filesystem_testbed.py   # clean
```

Results:

- `probe.py`: 39 probes.
- `workload.py`: 59 runs, 0 discrepancies. The three capture passes ran 10 each, with 0 discrepancies.
- `fidelity.py`: fidelity ok, 0 recording leaks.

---

## 13. Week 2 exit criteria

| Roadmap item | Status | Evidence |
|---|---|---|
| 2.1 Inspect candidates and document the comparison | **complete** | §3 (3 candidates, 2 runnable) |
| 2.1 Select one and explain the contrast with Git | **complete** | §3, §9 |
| 2.1 Pin the full environment | **complete** (Node not vendored) | §4 |
| 2.1 Document operations, side effects and reset before testing | **complete** | §5, §6, README |
| 2.2 Tool and schema inventory; validation and dialect | **complete** | §5 |
| 2.2 Result formats and output-schema behaviour | **complete** | §5 (trivial schema noted) |
| 2.2 Error conventions and success-looking failures | **complete** | §5 (C12, C14), §7 (U2) |
| 2.2 Listing freshness and lifecycle | **partial**: lifecycle and restart done; stale listing (`list_changed`) not tested | §5 L1–L10, RS1, RS2 |
| 2.2 Evidence-based effect classification | **complete** for the probed operations; `read_media_file`, search and symlinks unknown | §5 |
| 2.2 Ground truth defined before execution | **complete** | `workload.py` expectations, §7 |
| 2.3 Outcome and invariants; safe reset; detects wrong results; small enough to inspect | **complete** | §6; U2, R1, S1 caught |
| 2.4 real_capture provenance and redaction; private raw evidence; isolation; unchanged workload; Shadow fidelity; no retries, restarts or mutations beyond the fixture | **complete** | §8 |
| 2.4 Classify every case against ground truth; separate StateGuard / server / harness | **complete** | §7, §10 |
| **Exit**: second ecosystem pinned and characterized | **complete** | §4, §5 |
| **Exit**: evidence integrity and privacy verified | **complete** | §8 |
| **Exit**: cross-domain comparison (contract, lifecycle, error, result, safety) | **complete** | §9 |
| **Exit**: multi-step workflow reproducible with independent ground truth | **complete** | §6, §7, §12 |
| **Exit**: unsupported or unobservable recovery classes recorded | **complete** | §7 limitations, §9 [N/O], §15 |

**Proposed status-board update.** The roadmap file is not in the repository, so this is proposed for your edit.

- *Week 1*:
  - "Confirm merged commit and CI status on `main`" → [x].
  - "Finalize invocation lifecycle and recovery eligibility table" stays [ ] until you review §2 and §11.
- *Week 2*:
  - [x] for: compare candidates; select and pin; design bounded workflow; Shadow real capture; cross-domain comparison.
  - **Offline characterization**: [x], with the stale-listing caveat; leave it unticked if you require `list_changed` coverage.

---

## 14. Week 3 readiness: the smallest justified next package

**Recommendation.** Begin Week 3 with **one** primitive: reconciliation of an UNKNOWN invocation against an operator-declared, read-only postcondition probe. Everything else stays as it is. The scope:

1. **Ledger states.**
   - `UNKNOWN → RECONCILED_EXECUTED | RECONCILED_NOT_EXECUTED | RECONCILE_FAILED`.
   - `COMPLETED` and `RESULT_INVALID` kept apart (from category H).
   - Attempts, outcomes and verification are recorded separately, never overwritten. This is the roadmap's 3.1 "no conflation".
2. **Operator-declared checks.**
   - `--effects` gains an optional per-tool postcondition: a read-only tool call plus a predicate over its *result*, declared by the operator, never inferred.
   - No generic predicates beyond what the testbed needs: "file exists / absent", "text contains exactly N times".
3. **Decision rule:**

   | Reconciliation result | Action |
   |---|---|
   | executed | report completed-by-reconciliation; no re-send |
   | not executed **and** the tool is declared safe to re-send | one re-send within the existing budget, then verify |
   | anything else | escalate "unknown" |

   There is no compensation and no replanning. Non-idempotent work is never re-sent without a not-executed verdict.
4. **Ledger fix.** `answered_locally` returns whether it applied, and the not-sent answer depends on it (§2 latent issue). Add a regression test.
5. **Evaluation** on this testbed with identical seeds:
   - raw, versus the naive-retry baseline (D1–D3), versus StateGuard reconciliation.
   - Pass condition: D1 → no duplicate; D2 → completed-by-reconciliation; D3 → re-sent and verified; I1–I3 → correct verdicts.
   - Plus the roadmap 3.4 adversarial list, including a delayed answer via `--call-deadline` and an ambiguous probe result → escalate.

**Why this, and not something else.**

- It is the only failure class here where (a) StateGuard is already correct but stuck at "unknown", and (b) independent evidence demonstrably resolves it (`disk_checkpoint` in I1, I2 and I3).
- Contract repair already works (C1–C3).
- Result-integrity extensions are blocked on servers with meaningful output schemas, which argues for memory as domain 3.
- Semantic ambiguity (U2) needs intent the runtime does not have.

**Explicitly out of scope:**

- cross-process ledger persistence. D1 is recorded as a limitation; a reconnecting *client* must carry its own idempotency key. That is a separate decision for you.
- generic retries;
- compensation;
- result repair.

---

## 15. Remaining unknowns

**Observed facts:** everything in §5, §7 and §8, for this server, version, fixture and workload only.

**Inferences** (supported by the evidence, but not directly tested):

- Pre-execution validation in the TypeScript SDK runs nothing. Source inspection and E11/C-probe snapshots support this; it was not proven for every tool.
- `validatePath` precedes every handler. Source and P2/E10 support this.
- A re-send after `known_not_executed` is safe, because no attempt reached the server.

**Hypotheses (untested):**

- Git mutations would show similar replay classes.
- Real server crashes mid-write could leave partial files. The atomic temp-file rename in `write_file` suggests not for that tool; `edit_file` is unknown.
- Stale listings after `tools/list_changed`.
- Behaviour under concurrent clients.
- Delayed answers past a deadline.
- Behaviour under another Node major version.

**Not observable with the current harness:**

- in-server faults;
- OS-level metadata effects (mtime, inode);
- symlink races;
- anything requiring auth.

No claim here concerns production prevalence, other MCP servers or commercial demand.
