# First real MCP capture: plan (not yet performed)

Status: **plan only, awaiting approval.** No server has been installed, no MCP
traffic has been sent, no recording exists. Procedure and privacy guarantees:
`REAL_CAPTURE.md`. Everything here is subordinate to it.

**real_capture provides evidence collection, not proof of reliability
improvement.**

---

## 1. Recommendation

**`mcp-server-git` 2026.8.18** (the official `modelcontextprotocol/servers`
reference server, the exact version whose tool listing is already in
`tests/adapters/jsonschema/corpus/schemas/mcp-server-git.json`), run over
stdio against a **throw-away fixture repository with synthetic content**,
driven by a **scripted MCP SDK client**, through the relay CLI in
`--shadow --record-provenance real_capture`.

Why it wins:

- A third-party, widely deployed MCP server -- not written by us, not built
  to exhibit drift -- on the official MCP SDK, speaking real stdio JSON-RPC.
- Needs no credentials and no network. It touches only the repository path
  it is given; the fixture repo is created for the capture and deleted after.
- Twelve tools with realistic schema features: required strings, an array
  (`git_add.files`), integer defaults (`context_lines`, `max_count`),
  nullable `anyOf` optionals (`start_timestamp`, `base_branch`, `contains`),
  read tools and state-changing tools (on the fixture only).
- Its results are rich real server output (status text, logs, diffs) -- a
  real test of masking -- yet the content is ours and synthetic.

**Approval needed for one install step:** the server is not installed in this
environment. It must be installed, pinned, into its own venv from PyPI
(`REAL_CAPTURE.md` forbids nothing here, but this task did not install it).

## 2. What "real" means here

| Category | What it is | Evidence about |
|---|---|---|
| Synthetic fault injection | `record_corpus.py` tapes: harness server, injected faults, harness labels | StateGuard's logic under faults we designed |
| Replay | a recording re-driven through the TapeServer model | fidelity/determinism; counterfactuals are predictions |
| In-process test harness | `Rig` + `ScriptedServer`, `fake_mcp_server.py` | relay correctness; nothing about real servers |
| **Local real MCP server** | a real server implementation, real SDK, real stdio transport, real relay process, our machine, our data | how a real server and the real stack behave on the wire |
| External production server | someone's deployed server, real users' traffic | real-world rates |

**Capture #1 is "local real MCP server, scripted workload".** Real: the
server code, its tool definitions, its validation and error behaviour, its
result shapes, the MCP SDK on both ends, the stdio transport, the relay
running as a separate process exactly as a client would launch it, and the
recorder/redaction path. **Not** real: the workload (we choose every call,
not an agent or a user), the data (synthetic fixture), the scale (one
session, ~20 calls). So it is evidence about *real server/stack behaviour*,
not about how often real agents send malformed calls, and nothing in it is a
rate. Phase 1b (below) is manufactured input to a real server; it is kept in
a separate file and labelled here, never in the recording.

## 3. Candidates (top 3, plus excluded)

| | 1. `mcp-server-git` | 2. `examples/mcp/server.py` ("weather-demo") | 3. `mcp-server-time` |
|---|---|---|---|
| Runs as | `mcp-server-git --repository PATH` (stdio) | `python examples/mcp/server.py` (stdio) | `mcp-server-time` (stdio) |
| Genuinely MCP? | yes, official reference server | yes (official SDK `MCPServer`), **but ours**, written to exhibit drift | yes, official reference server |
| Installed? | **no** -- pinned PyPI install needed | yes (repo + SDK 2.3.0 in `.venv`) | no -- install needed |
| Tools | 12: status, diff ×3, commit, add, reset, log, create_branch, checkout, show, branch | 3: get_forecast, search_places, compare_cities | 2: get_current_time, convert_time |
| Schemas | required, array, int defaults, nullable `anyOf`; no enum/bounds | enum, `ge`/`le` bounds, default, two-way ambiguity | three required strings |
| Credentials | none | none | none |
| External services | none (local git) | none | none (local tz database) |
| Local/non-prod data | yes (fixture repo) | yes (no data at all) | yes |
| Usefulness | high: real results, errors, writes, arrays/nullables | medium: real SDK validation errors on drifted input, but drift is staged by us | low: two string tools |
| Privacy risk | low-medium: paths, git text, identity (mitigated, §5) | very low: echoes its arguments | very low |
| Setup | medium: venv + fixture + git isolation | none | low: venv |
| Verdict | **first capture** | good zero-install fallback; weaker "real" claim | too thin to learn much |

Excluded: `mcp-server-fetch` (fetches arbitrary URLs: external network,
SSRF-shaped -- never for capture #1); `mcp-server-sqlite` (SQL text as the
only argument, `write_query` can drop tables, package last released
2025.4.25). No other MCP server exists locally: no npm/uvx-installed servers,
no MCP client configuration (`~/.claude.json` has no MCP servers).

## 4. Fault seeding

- **A (pure normal traffic) is capture #1** (`capture-1a`): it shows what
  real traffic looks like before we provoke anything, and every failure in
  it is an observed one.
- **B (a few safe, controlled errors) is useful, as a separate recording**
  (`capture-1b`), run right after 1a against the same fixture: invalid
  revision/branch, string-for-integer, string-for-array, a renamed required
  parameter, an unknown tool, a path outside `--repository`. The *server's*
  responses are real; the *inputs* are manufactured. It answers "does the
  real server behave as its schema claims?" and "what does StateGuard
  propose on real error shapes?". Its results are never pooled with 1a's.
- **C (transport/server failures)** -- killing the server, timeouts,
  restarts -- **reserved for later captures.** Those are faults we would
  inject; in shadow mode they only produce `would_restart` /
  `outcome_unknown` observations, and they are better studied once normal
  traffic is understood.

## 5. Privacy review (against `REAL_CAPTURE.md`)

| Data | Example from this server | In the recording |
|---|---|---|
| Tool arguments | `repo_path`, `message`, `files`, `revision` | **masked** (letters→a, digits→1); `max_count`/`context_lines` numbers **scaled** (sign + order of magnitude) |
| Tool results | status text, log text (hashes, author, dates), diffs (file contents) | **masked**; lengths/line structure kept |
| Free text | commit messages, file contents, server prose | **masked** |
| File paths | `/root/stateguard-captures/capture-1/fixture-repo` | **masked**; depth and lengths visible |
| Identifiers | JSON-RPC ids, method and tool names, protocol version | **retained** (protocol) |
| Schema contents | property names, types, defaults, `required` | **retained**; descriptions masked |
| Enum/const values | none in this server's schemas | (would be retained) |
| Server metadata | `serverInfo` name/version | **retained** when identifier-shaped |
| Errors | git error text, SDK validation messages, `error.code` | text **masked**, codes **retained**, HTTP statuses (none expected) retained |
| Timing | `diag.t_ms`, `started_at`, byte counts | **retained** |
| Audit data (recording) | decisions, `would_modify`, counters | reasons masked; counters/flags retained |
| **Audit log file** | `capture-1a.audit.jsonl` | **NOT redacted** -- private, deleted after review |
| Server stderr | git/SDK log lines | not recorded; goes to the terminal only |

Server-specific risks and their mitigation:

- **Host git identity and signing.** This machine's global git config sets
  `user.name`, `user.email`, `user.signingkey`, `commit.gpgsign` and a custom
  `gpg.ssh.program`. Inherited, `git_commit` would sign with the host key
  (running that program) and write the host identity into results. **Isolate:**
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `HOME=$CAP`, and a
  repo-local synthetic identity with `commit.gpgsign=false`.
- **Any-path access.** The tools take `repo_path`; the workload only ever
  passes the fixture (and, in 1b, one non-existent path inside `$CAP`).
  `--repository` is passed; whether it *enforces* the boundary in 2026.8.18
  is verified by reading the installed source in pre-flight, not assumed.
- **Fixture content** is synthetic (`alpha`/`beta`/...), so even unmasked it
  would be harmless; masking is still on.
- Nothing about this server needs credentials; if anything at pre-flight
  asks for one, stop (§9).

## 6. Architecture

```
workload.py (MCP SDK Client, scripted)
   │ stdio (JSON-RPC lines)
   ▼
stdio_proxy.py --shadow --record ... --record-provenance real_capture   [relay process]
   │ stdio                           │ redacted records → capture-1a.jsonl
   ▼                                 │ raw audit lines  → capture-1a.audit.jsonl
mcp-server-git --repository $REPO   [server process, own venv, isolated git config]
```

Where shadow mode observes (and only observes):

- **Tool listing** -- the relay sends its own `tools/list` (ids
  `stateguard-relay:N`), syncs its registry (`tools_synced`); the client's
  own listing passes through untouched.
- **Request** -- each client `tools/call` is recorded on arrival
  (`client_to_relay`).
- **Decision** -- the relay validates the arguments against the server's
  schema and logs `call` with `action` (shadow: `hold`/`forward`),
  `auto_action`, field `decisions` and **`would_modify`**.
- **Proposed repair** -- exists only as `auto_action` + `would_modify` +
  field decisions in the audit event. Before a call it *would* modify, the
  relay re-lists tools once (one extra round trip; T3).
- **Forwarding** -- the original bytes go to the server (`relay_to_server`,
  same digest as `client_to_relay`).
- **Response** -- recorded from the server and to the client, unchanged;
  result-integrity checks (`result_check`) are logged only.
- **Failure classification** -- T11 classifies each answer; auth failures
  log `auth_failure` (none expected here); retry decisions log `acted: false`.
- **UNKNOWN** -- only if the server dies or (with a deadline, not used here)
  times out mid-call: `outcome_unknown`, `surfaced: false`. Not expected in 1a.

## 7. Procedure (do not run until approved)

All commands from a shell in the container. `SG` is the repo checkout at
`main` (`2e000fa` or later).

**Step 0 -- workspace and server install (needs approval: one PyPI install).**

```bash
umask 077
export SG=/home/user/stateguard
export CAP=$HOME/stateguard-captures/capture-1
mkdir -p "$CAP" && chmod 700 "$HOME/stateguard-captures" "$CAP"
uv venv "$CAP/server-venv" --python 3.11
uv pip install --python "$CAP/server-venv/bin/python" "mcp-server-git==2026.8.18"
uv pip freeze --python "$CAP/server-venv/bin/python" > "$CAP/server-freeze.txt"
```

**Step 1 -- pre-flight (static reads only; still no MCP traffic).**

```bash
cd "$SG" && git status --short | wc -l          # expect 0
"$CAP/server-venv/bin/python" -c "import importlib.metadata as m; print(m.version('mcp-server-git'), m.version('mcp'))"
grep -rn "repository" "$CAP"/server-venv/lib/python3.11/site-packages/mcp_server_git/ | head
```

Confirm: version is 2026.8.18; whether `--repository` restricts
`repo_path` (record the answer either way); no import of network clients
other than the MCP SDK; no credential lookups.

**Step 2 -- fixture repository (synthetic content, isolated git config).**

```bash
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
git init -q -b main "$CAP/fixture-repo" && cd "$CAP/fixture-repo"
git config user.name "Capture Fixture"
git config user.email "capture@example.invalid"
git config commit.gpgsign false
printf 'Capture fixture.\nNo real data.\n' > README.txt
mkdir src && printf 'alpha\nbeta\n' > src/items.txt
git add -A && git commit -qm "fixture: initial"
printf 'gamma\n' >> src/items.txt && git commit -qam "fixture: add gamma"
printf 'delta\n' >> src/items.txt      # unstaged change, for git_diff_unstaged
printf 'note one\n' > notes.txt        # untracked, added by the workload
cd "$CAP"
```

**Step 3 -- effect declarations** (operator data; only adds `would_retry`
proposals, which in shadow never act). `$CAP/effects.json`:

```json
{"tools": [
  {"name": "git_status", "effect": "read_only"},
  {"name": "git_log", "effect": "read_only"},
  {"name": "git_show", "effect": "read_only"},
  {"name": "git_branch", "effect": "read_only"},
  {"name": "git_diff", "effect": "read_only"},
  {"name": "git_diff_staged", "effect": "read_only"},
  {"name": "git_diff_unstaged", "effect": "read_only"}
]}
```

**Step 4 -- the workload client.** `$CAP/workload.py` (outside the repo;
prints statuses only, never content):

```python
import os, sys, anyio
from mcp import Client, StdioServerParameters

CAP, SG, PHASE = os.environ["CAP"], os.environ["SG"], sys.argv[1]   # "1a" or "1b"
REPO = f"{CAP}/fixture-repo"
RELAY = [
    f"{SG}/examples/mcp/stdio_proxy.py", "--shadow",
    "--record", f"{CAP}/capture-{PHASE}.jsonl", "--record-provenance", "real_capture",
    "--audit", f"{CAP}/capture-{PHASE}.audit.jsonl", "--effects", f"{CAP}/effects.json",
    "--", f"{CAP}/server-venv/bin/mcp-server-git", "--repository", REPO,
]
ENV = {"HOME": CAP, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

CALLS = {
    "1a": [  # normal traffic: every call valid per the published schema
        ("git_status", {"repo_path": REPO}),
        ("git_log", {"repo_path": REPO}),
        ("git_log", {"repo_path": REPO, "max_count": 1}),
        ("git_log", {"repo_path": REPO, "max_count": 3, "start_timestamp": None}),
        ("git_show", {"repo_path": REPO, "revision": "HEAD"}),
        ("git_branch", {"repo_path": REPO, "branch_type": "local"}),
        ("git_diff_unstaged", {"repo_path": REPO}),
        ("git_diff_unstaged", {"repo_path": REPO, "context_lines": 1}),
        ("git_diff_staged", {"repo_path": REPO}),
        ("git_create_branch", {"repo_path": REPO, "branch_name": "capture-1"}),
        ("git_checkout", {"repo_path": REPO, "branch_name": "capture-1"}),
        ("git_add", {"repo_path": REPO, "files": ["notes.txt"]}),
        ("git_diff_staged", {"repo_path": REPO}),
        ("git_commit", {"repo_path": REPO, "message": "capture: add notes"}),
        ("git_log", {"repo_path": REPO, "max_count": 2}),
        ("git_diff", {"repo_path": REPO, "target": "main"}),
        ("git_branch", {"repo_path": REPO, "branch_type": "all"}),
        ("git_status", {"repo_path": REPO}),
    ],
    "1b": [  # MANUFACTURED inputs to the real server -- labelled here, never in the file
        ("git_show", {"repo_path": REPO, "revision": "no-such-revision"}),
        ("git_checkout", {"repo_path": REPO, "branch_name": "no-such-branch"}),
        ("git_branch", {"repo_path": REPO, "branch_type": "sideways"}),
        ("git_log", {"repo_path": REPO, "max_count": "3"}),
        ("git_diff_unstaged", {"repo_path": REPO, "context_lines": "2"}),
        ("git_add", {"repo_path": REPO, "files": "notes.txt"}),
        ("git_status", {"repo": REPO}),
        ("git_status", {"repo_path": f"{CAP}/not-a-repo"}),
        ("git_no_such_tool", {}),
    ],
}

async def main() -> None:
    params = StdioServerParameters(command=f"{SG}/.venv/bin/python", args=RELAY, env=ENV, cwd=CAP)
    async with Client(params) as client:
        listing = await client.list_tools()
        print("tools listed:", len(listing.tools))
        for n, (name, arguments) in enumerate(CALLS[PHASE], 1):
            try:
                result = await client.call_tool(name, arguments)
                print(n, name, "tool error" if result.is_error else "ok")
            except Exception as exc:  # a JSON-RPC error: record its type only
                print(n, name, "raised", type(exc).__name__)

anyio.run(main)
```

**Step 5 -- run 1a, check it, then (only if 1a passes) run 1b.**

```bash
cd "$CAP"
"$SG/.venv/bin/python" workload.py 1a
"$SG/.venv/bin/python" "$SG/examples/mcp/evidence_report.py" capture-1a.jsonl; echo "exit $?"
# only if 1a passed every check below:
"$SG/.venv/bin/python" workload.py 1b
"$SG/.venv/bin/python" "$SG/examples/mcp/evidence_report.py" capture-1b.jsonl; echo "exit $?"
```

The session ends when the client leaves `async with`: it closes the relay's
stdin, the relay closes the server's, both exit, the recorder writes `end`.
The SDK allows 2 s before killing the process tree; a kill leaves the file
without `end` (report exit 1) -- then the capture is incomplete and is
re-run, not analysed.

**Step 6 -- checks before looking at any content.** The report must show:

- exit status 0 (complete, not malformed);
- `provenance real_capture`, `mode shadow`, `redaction mask`, header rules
  include `mask_numbers`;
- `integrity.ground_truth_records 0`, `recorder_failures 0`;
- `wire.tool_calls` = 18 (1a) / 9 (1b) -- more only if the SDK client
  re-sent a call after `HEADER_MISMATCH` (its own behaviour; explain it, do
  not discard it); `tool_calls_forwarded_changed 0`,
  `tool_calls_not_forwarded 0`, `tool_calls_compared_by {"digest": N}`;
- `wire.client_answers_not_from_server 0`;
- `proposed.acted` all zero;
- a spot-check (not the safety boundary): none of `Capture Fixture`,
  `capture@example.invalid`, `alpha`, `gamma`, `fixture-repo`, the host's git
  user name or email appears in `capture-1a.jsonl`.

Then: keep `capture-1a.jsonl` (and `-1b`) out of the repository; delete the
audit files, the fixture repo and the server venv once reviewed.

## 8. Evidence questions

From the evidence report (1a unless noted), never from guesses:

1. Real tool calls: `wire.tool_calls`; plus client-originated resends (the
   SDK re-sends once on `HEADER_MISMATCH`) -- counted, not attributed to us.
2. Schema mismatches observed: `call` events with field decisions other than
   valid, and `would_modify_call`. Expected ~0 in 1a (valid calls); a
   non-zero count there is a finding about StateGuard (a false positive) or
   the schema.
3. Repair proposals: `proposed.would_repair_call` (and all payload changes:
   `would_modify_call`).
4. Proposal kinds: `would_modify_by_kind` (repair / default_fill / refuse /
   escalate); schema mismatches: `calls_with_schema_violations`.
5. Actual tool failures: `observed.by_failure_class` minus `none`.
6. Failure classes: `observed.by_failure_class`, `by_signal`.
7. UNKNOWN outcomes: `unknown.outcome_unknown` (expected 0).
8. Auth/authz failures: `stateguard_observed.auth_failures` (expected none).
9. Transport/server failures: `observed.server_lost` (1 at shutdown is
   normal end-of-session, not a failure), `transport_closed`, `send_failed`.
10. Traffic vs declared schema: in 1a by construction 0; in 1b, per call,
    whether StateGuard's proposal matched what the server then accepted or
    rejected.
11. Server vs its own definitions (1b): did it reject what its schema
    rejects (string `max_count`, string `files`, missing `repo_path`), accept
    what it should, and report errors as `isError` results or JSON-RPC
    errors?
12. Taxonomy categories with zero real evidence: expected B (no schema
    change), C, D, E, F, H, I, J, K in 1a -- state it, do not treat absence as
    rarity.
13. Unanticipated patterns: anything in result shapes, listing behaviour
    (pagination, `_meta`), error formats or relay decisions not in our
    synthetic corpora.

**Not computed:** failures prevented, failures recovered, or any rate. A
shadow proposal is not a prevented failure; there is no ground truth.

## 9. Stop conditions (abort before or during capture)

- Installing the server needs anything beyond the pinned PyPI package, or
  its version/dependencies differ from what was approved.
- The server, its dependencies or pre-flight show any need for credentials,
  tokens or network access beyond `localhost` stdio.
- The server can touch anything but the fixture repo in a way the workload
  cannot prevent, or would modify state outside `$CAP`.
- Git isolation fails: the host identity or signing configuration is visible
  to the server (e.g. `git -C fixture-repo config user.email` without the
  isolation env shows the host value being used, or a commit is signed).
- Any command line or file in the capture path contains a credential.
- The relay would need auto mode, passthrough, `secrets_only`, or any flag the
  CLI refuses for real capture -- never work around a refusal.
- The working tree of `$SG` is not clean, or `$SG` is not at a `main` commit
  containing `0aaa3ec`.
- After 1a: any check in Step 6 fails -- report it as a defect; do not run 1b
  and do not analyse the file.
- The recording would hold data we would not be willing to keep (anything
  but the synthetic fixture).

## 10. Follow-up captures (if #1 succeeds)

- **#2 -- agent-generated traffic.** The same server and fixture, but driven
  by an LLM agent (an MCP client configured with the relay as the server
  command) on a scripted task ("summarise the history", "commit the notes").
  This is the first capture that can say anything about *how real model
  calls drift from schemas*. Needs a decision on which client and how the
  task prompt (masked anyway) is handled.
- **#3 -- breadth and lifecycle.** A second server with constrained schemas
  (`mcp-server-time`, or a filesystem-style server sandboxed to a temp
  directory), and a longer session with `--restart 1 --call-deadline` set
  well above observed latency, so shadow records `would_restart` /
  `outcome_unknown` if the server dies or stalls on its own. Still no
  injected faults; observed lifecycle only.
- Later: a server the operator runs for real work, with real (but
  operator-owned, non-production) data -- only after #1-#3 show the
  redaction holds on real result shapes.
