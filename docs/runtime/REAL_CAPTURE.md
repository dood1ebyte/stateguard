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
| Mask required | `Recorder.__init__`, `_check_header` | A `real_capture` recorder with any redaction but `mask` raises `ValueError` before writing a byte; the parser refuses such a file. |
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

Redaction runs before anything is written, through the same `Redactor` as
every other recording -- a test checks a real capture's records are
identical to a fault-injection recording's of the same traffic.

**Protected in the recording file:**

- Values under credential-like keys, at any depth (`authorization`,
  `*token*`, `*secret*`, `password`, `api_key`, `cookie`, `passcode`,
  `verification_code`, `mfa_code`, ...): replaced, keeping only JSON types.
- Strings containing a recognised credential shape (Bearer/Basic, JWT,
  AWS/GitHub/Slack/Stripe/`sk-`/Google keys, PEM private keys, `user:pass@`
  URLs, `?token=` queries, `password=...`): replaced whole, wherever they are,
  even where structure would keep them.
- All other free text (tool arguments, results, error messages, audit
  reasons, server descriptions): masked shape-preservingly (letters to `a`,
  digits to `1`). Exceptions: protocol structure (methods, tool names,
  protocol versions, content types), the server's name and version when
  identifier-shaped, argument values the tool's own schema vouches for
  (enum members, small numbers or booleans in numeric/boolean fields), and the
  fixed-vocabulary auth signals (`401 Unauthorized`, `invalid_token`, ...)
  that T11 classifies on.
- The server command line: not in the header, and masked in the recorded
  `relay_started` event.
- `secrets_only` is not available for real capture.

**Not protected -- know these before a first capture:**

- **The relay's audit log** (`--audit FILE`, default stderr) is **not
  redacted**. It holds decision reasons and exception text that can quote
  values. For `real_capture` the `relay_started` event names the server
  program and its argument count only, not its arguments. Treat the audit
  file as sensitive local data: keep it private, never share it, delete it
  after the capture is reviewed (or send it to `/dev/null`).
- **Numbers.** Integers below 1,000,000 and floats are kept in mask mode
  (larger integers are reduced to their order of magnitude). A short numeric
  code sent as a number under an innocuous key (`{"code": 482913}`) is kept.
- **Lengths and shapes.** Masking keeps string length and punctuation.
- **Word-like keys.** A dict key that looks like an identifier is kept; a
  credential used *as a key* is kept unless it has a recognised shape.
- **Credentials of unrecognised shape** under innocuous keys are masked
  like any text (letters/digits replaced) -- not removed, but no longer
  readable.
- **Timing and volume.** `diag.t_ms`, byte counts and `started_at` are kept.
- The relay does not encrypt the file. Its protection is the redaction and
  the file's permissions.

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
| `stateguard_observed` | StateGuard's observation | auth failures it classified, result-integrity findings, listings synced, breaking schema changes, internal errors |
| `proposed` | **hypothetical** (shadow) | `would_modify_call` (by `repair` / `refuse` / `escalate`), `would_retry`, `would_restart`, `would_drop_response`; `acted` must be all zero in shadow |
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

5. **Look for leaks yourself.** Search the recording for values you know were
   in the session (your token prefixes, email address, file paths, a
   distinctive string you typed):

   ```bash
   grep -c -F -f my-known-values.txt ~/stateguard-captures/capture-1.jsonl
   ```

   Expect 0. Then delete or lock away the audit file.

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
