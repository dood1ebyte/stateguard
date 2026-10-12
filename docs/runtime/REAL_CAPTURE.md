# Real MCP capture (shadow mode)

**real_capture provides evidence collection, not proof of reliability
improvement.**

A real capture records the traffic between a real MCP client and a real MCP
server, through the StateGuard stdio relay running in **shadow mode**. It
shows what happened on the wire, what StateGuard observed, and what StateGuard
*would* have done in auto mode. It does not show that auto mode would have
helped. Nothing in it is a rate for another server, workload or period, and
no synthetic benchmark figure (`measure.py`, `replay.py` on `tapes/`,
`recovery.py`, `auth.py`) predicts what a capture will contain.

No real capture has been taken yet. Everything below that runs uses local
fixtures only (the in-process `ScriptedServer` and
`tests/examples/fake_mcp_server.py`), and the tests that label their
recordings `real_capture` say so: they are a harness, not production evidence.

---

## What it is built from

Nothing new on the wire: the T13 recording format
(`stateguard.mcp.recording`, version 1), its one redaction path (`Redactor`,
mask mode), the relay's existing taps and audit events, and the T11 failure
classifier. Added for real capture:

| Piece | Where | What it does |
|---|---|---|
| Provenance families | `recording.py` `Provenance` | `synthetic`, `fault_injection` (generated; may carry ground truth), `replay` (a derivative of its source), `local_development`, `real_capture` (observed; never carry ground truth). |
| Mask required, numbers too | `Recorder.__init__`, `_check_header`, `Redactor` (`mask_numbers`) | A `real_capture` recorder with any redaction but `mask` raises `ValueError` before writing a byte; it always masks numbers; the parser refuses a file that does not say both. |
| No ground truth on observed traffic | `Recorder.fault` / `label`, `parse_recording` | Refused (counted as `ground_truth_refused` in `end`) for `local_development`, `real_capture` and `replay`; a file that has them anyway is malformed. |
| Shadow only | `StdioRelay.__init__`, relay CLI | A `real_capture` recorder in auto or passthrough mode raises `ValueError`; the CLI exits 2 before starting the server. |
| Start time | header `started_at` | UTC, to the second, on live (`deterministic=False`) recordings. |
| Byte identity | `diag.digest` | HMAC-SHA256 (truncated) of each raw line under a random key held only in memory; shows a call reached the server byte-identical without storing it. |
| Server identity | `serverInfo.name` / `.version` | Kept when identifier-shaped; title, description, URLs, icons redacted. |
| Proposal flag | audit `call` event `would_modify` | Whether auto mode would have changed or refused the call. |
| Evidence summary | `adapters/mcp/evidence.py`, `examples/mcp/evidence_report.py` | Reads a recording; separates wire facts, observed failures, StateGuard's observations, proposals and unknown outcomes. |
| Replay | `benchmarks/runtime/replay.py` | Overhead recordings are `replay`, not `synthetic`; a replay of observed traffic says it has no ground truth and claims no prevented or recovered failure. |

## Shadow-only: what the relay does and does not do

In a real capture the client's payload is the payload. StateGuard may
inspect, validate, classify, plan a repair and record. It does not:

| Never | Why it holds | Test |
|---|---|---|
| forward a repaired payload | shadow outcome is `HOLD`; `forward_message` returns the original message; the relay writes the original line | `test_every_call_reaches_the_server_byte_identical` |
| refuse or drop a call | shadow never blocks, including unreadable schemas and internal errors (T2) | same; `client_answers_not_from_server == 0` |
| retry | `_retry_mode` is forced to `shadow`; decisions are logged with `acted: false` | `test_nothing_is_retried_restarted_dropped_or_invented` |
| restart a server | shadow logs `restart_would_attempt` and stops | same |
| drop a duplicate or late answer | shadow logs `response_would_drop` and delivers it | same |
| answer for the server | an unknown outcome is logged with `surfaced: false`; the client gets no relay-made error | `test_auth_and_unknown_semantics_are_intact` |
| retry an unknown outcome | `decide_retry` never retries one, in any mode | same |

One relay-made answer exists in every mode, shadow included: a request that
arrives when no server is running is answered "not sent" (it certainly did
not execute; a direct connection would have failed on the dead pipe). It is
counted as `observed.not_sent` and in `wire.client_answers_not_from_server`.

The relay sends its own `tools/list` requests (to know the schemas), including
one before forwarding a call it would modify, to confirm the listing is
current (T3). That is a read, and it delays such a call by one round trip; it
is visible in the summary as `relay_own_requests`.

`--retry auto`, `--restart N`, `--call-deadline` and `--effects` are accepted
with real capture: in shadow they only make StateGuard *log* what it would do
(`would_retry`, `would_restart`, an `outcome_unknown` at the deadline), which
is the point of a capture. The tests pass `retry="auto"` and
`max_restarts=1` on purpose.

## Privacy

The recorder is the safety boundary: redaction runs inside it, before
anything reaches the file, and nothing depends on the operator checking the
file afterwards. It is the same `Redactor` as every other recording, with
one rule set that only real capture is forced to use (`mask_numbers`).

A real capture is **not "fully masked"**: it keeps the classes listed under
"Retained" and "Residual risks" below, by design or as known limits.

### What a real capture never contains

The recorder guarantees, for every `message` and `audit` record and the
header's `config`, at every depth (including JSON sent as a string):

1. **No value under a credential-like key.** `authorization`, `*token*`,
   `*secret*`, `password`, `api_key`, `cookie`, `pin`, `otp`, `passcode`,
   `verification_code`, `mfa_code`, `recovery_code`, ... (any spelling):
   the subtree is replaced, keeping only JSON types (`"[redacted]"`, `0`,
   `false`).
2. **No string containing a recognised credential shape.** Bearer/Basic, JWT,
   AWS/GitHub/Slack/Stripe/`sk-`/Google keys, PEM private keys, `user:pass@`
   URLs, `?token=`-style queries, `password=`-style assignments: replaced
   whole, wherever they are, even where structure would keep them.
3. **No free text.** Every other string is masked shape-preservingly
   (letters to `a`/`A`, digits to `1`), unless it is in "Retained" below.
   A numeric string is never kept on the strength of a numeric schema type
   (a field typed `integer` can hold a PIN).
4. **No number a user could have typed.** Every number outside the
   retained classes below is reduced to its sign and order of magnitude,
   whatever its key: `482913` -> `100000`,
   `25` -> `10`, `3.14159` -> `1.0`, `0.042` -> `0.01`, `-122.4` -> `-100.0`;
   `0` stays `0`. Integers stay integers and floats floats. The same holds
   for a number written as a string in a protocol position (`"482913"` ->
   `"111111"`) and for a dict key that is a number (`{"7351": ...}`).
   This is forced: a `real_capture` recorder masks numbers whatever policy
   it is given, the header lists `mask_numbers`, and the parser refuses a
   `real_capture` file without it.
5. **No `secrets_only` redaction** (refused at construction and by the
   parser) and **no server command line** (not in the header; the recorded
   `relay_started` event names only the program and argument count).

### Retained, on purpose

Kept verbatim because it is protocol or contract, not user data, and the
recording is useless for failure analysis without it:

- **Protocol structure:** `jsonrpc`, methods, message kinds, JSON-RPC ids,
  `error.code`, tool names, protocol versions, content types, `isError`,
  `resultType`, and identifier-shaped values in the `io.modelcontextprotocol/`
  and `io.stateguard/` `_meta` namespaces (not pure numbers).
- **Tool schemas from listings** (the server's published contract),
  including their numbers (`maximum`, `enum`, `default`) and samples --
  except prose (masked) and samples of credential-named properties
  (scrubbed).
- **Argument values the tool's schema publishes:** a string equal to (or
  within two edits of) an `enum`/`const` member, a boolean word in a boolean
  field, a number equal to an `enum`/`const` member.
- **HTTP statuses (100-599) under the status keys of `error.data`**
  (`status`, `statusCode`, ...): what the T11 classifier reads, so the
  recording classifies as the traffic did. Other numbers in `error.data` are
  scaled.
- **Auth signal phrases** in text (`401 Unauthorized`, `invalid_token`, ...).
- **The server's `serverInfo` name and version** when identifier-shaped.
- **The relay's own numbers:** audit counters (`generation`, `attempt(s)`,
  `restarts_used`, `in_flight_unknown`, `trust`, listing change counts) and
  the operator settings in `config`.
- **Booleans and nulls** (one bit each).
- **Diagnostics:** byte counts, `t_ms` timing, `started_at`, and the keyed
  `digest` (useless without the key, which is never written).

### Residual risks

- **The relay's audit log** (`--audit FILE`, default stderr) is **not
  redacted**: decision reasons and exception text can quote values. Treat it
  as sensitive local data; keep it private, never share it, delete it after
  review, or send it to `/dev/null`. Only the recording is redacted.
- **Scale, length and shape.** A masked number keeps its sign, type and order
  of magnitude (a 6-digit code becomes `100000`: its digit count is
  visible); a masked string keeps its length and punctuation.
- **Published-value matches.** A value equal to a schema `enum`/`const`
  member (or a string within two edits of one) is kept. It reveals only that
  the user chose a published value -- unless a server publishes secrets in
  its schema.
- **Server-chosen protocol values.** JSON-RPC ids, `error.code` and schema
  contents are kept whatever a client or server puts there.
- **Digits inside identifier-shaped protocol strings** (e.g. a `_meta` value
  `otp-482913` in the `io.modelcontextprotocol/` namespace, a tool name) are
  kept; only pure numbers are masked there.
- **Word-like dict keys** are kept (non-numeric, at most 40 characters, at
  most 3 digits); a credential used *as a key* is kept unless it has a
  recognised shape.
- **Credentials of unrecognised shape** under innocuous keys are masked like
  any text -- unreadable, but their length remains.
- **Booleans** are kept.
- **Timing and volume** are kept.
- The file is not encrypted. Its protection is the redaction and the file's
  permissions.
- **Other provenances are unchanged:** `synthetic`, `fault_injection`,
  `replay` and `local_development` keep numbers below 1,000,000 (larger ones
  scaled) and vouch for numeric strings in numeric fields, unless a library
  caller sets `RedactionPolicy(mask_numbers=True)`. That is acceptable for
  generated traffic; `local_development` through real servers can hold real
  values -- use `real_capture` (or that policy) for anything sensitive.

## Evidence semantics

`examples/mcp/evidence_report.py FILE [--json]` prints
`summarize_evidence(read_recording(FILE))` (schema
`stateguard.mcp.evidence/1`). Sections, kept apart on purpose:

| Section | Kind | Meaning |
|---|---|---|
| `integrity` | fact | complete? recorder failures, values redacted, ground-truth records (must be 0) |
| `servers` | fact | `serverInfo` per server generation |
| `wire` | fact | client requests, tool calls forwarded unchanged / changed / not at all (`tool_calls_compared_by`: `digest` on live recordings), client answers not from the server |
| `observed` | fact | failure class of each server answer to a tool call (T11 classifier: `authentication`, `authorization`, `tool`, `protocol`, ...), calls never answered, server lost |
| `stateguard_observed` | StateGuard's observation | auth failures it classified, result-integrity findings, listings synced, breaking schema changes, internal errors, `calls_with_schema_violations` (calls with a field StateGuard found invalid) |
| `proposed` | **hypothetical** (shadow) | `would_modify_call`, by kind (`would_modify_by_kind`: `repair` / `default_fill` / `refuse` / `escalate`; `unclassified` in recordings older than the relay's `proposal` field), `would_repair_call` (repairs only), `would_retry`, `would_restart`, `would_drop_response`; `acted` must be all zero in shadow. A `default_fill` is a valid call whose omitted optionals auto mode would write out from their declared defaults: not a repair, not drift, not a failure |
| `unknown` | neither | outcomes the relay could not determine, by reason; `answer_seen_later` when a late answer arrived; never counted as success or failure |
| `not_claimed` | — | what no number here supports |

- A **proposal is not an observed failure**, and a call auto mode would have
  repaired is **not a prevented failure**. Where the server rejected the
  call as sent, that rejection is an observed tool error; whether the repair
  would have succeeded is unknown.
- A tool error, an auth failure, a transport disconnect or a server that went
  away are observed failures.
- An unknown outcome stays unknown. A late answer shows the call finished;
  it does not turn the earlier "unknown" into a success.
- In an auto-mode recording (`local_development`), the `proposed` section is
  marked `hypothetical: false`: those were decisions StateGuard applied.

Provenance in the header is the only source of truth for what a file is.
`fault` and `label` records exist only in `synthetic` and `fault_injection`
recordings.

## Procedure: the first real capture (not yet performed)

Use a stdio MCP server you control, with non-production data and
credentials you can rotate. Do not point it at a server you do not operate.

1. **Prepare a private directory.**

   ```bash
   umask 077
   mkdir -p ~/stateguard-captures && chmod 700 ~/stateguard-captures
   ```

2. **Wrap the server command in the client's MCP configuration.** Where the
   client runs `<server command>`, have it run instead:

   ```bash
   uv run --directory /path/to/stateguard python examples/mcp/stdio_proxy.py \
     --shadow \
     --record ~/stateguard-captures/capture-1.jsonl \
     --record-provenance real_capture \
     --audit ~/stateguard-captures/capture-1.audit.jsonl \
     -- <server command>
   ```

   Optional, still shadow-only (they add proposals, never actions):
   `--effects effects.json` (operator effect declarations, for
   `would_retry`), `--restart 1` (for `would_restart`),
   `--call-deadline SECONDS` (set well above the tool's normal latency).

   The relay exits 2 without starting the server if `--shadow` or `--record`
   is missing, `--passthrough` is given, or redaction is not `mask`.

3. **Use the client normally**, then close it. The relay writes the `end`
   record when the server exits.

4. **Check the file before anything else.**

   ```bash
   uv run python examples/mcp/evidence_report.py ~/stateguard-captures/capture-1.jsonl
   ```

   Exit 0 = complete; 1 = incomplete (recorder stopped early: read
   `failure_types`); 2 = malformed. Check `provenance real_capture`, `mode
   shadow`, `ground_truth_records 0`, `tool_calls_forwarded_changed 0`,
   `client_answers_not_from_server 0` and `acted` all zero. Any non-zero
   there is a defect: stop and report it.

5. **Spot-check (optional; not the safety boundary).** The recorder's
   redaction is the guarantee; this only catches a defect in it. Search the
   recording for values you know were in the session (your token prefixes,
   email address, file paths, a distinctive string or number you typed):

   ```bash
   grep -c -F -f my-known-values.txt ~/stateguard-captures/capture-1.jsonl
   ```

   Expect 0; anything else is a redaction defect to report. Then delete or
   lock away the audit file.

6. **Replay (optional).** Copy the recording alone into a directory and
   replay it against the model server built from it (it never contacts the
   original):

   ```bash
   uv run python benchmarks/runtime/replay.py --tapes ~/stateguard-captures/replay-in
   ```

   For observed traffic the report measures fidelity and determinism only;
   with no ground truth it counts nothing as prevented or recovered.

7. **Report** with the provenance line and the `not_claimed` lines verbatim.
   One capture is one session of one server.
