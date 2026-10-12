# The MCP adapter

StateGuard's MCP adapter sits between an MCP client and an MCP server and
answers one question for every `tools/call`: **do I forward these
arguments, and in what form?** It validates each call against the tool's
current `inputSchema`. It repairs drift only when the evidence is strong
enough (a renamed parameter, a number sent as a string, an enum in the
wrong case). Otherwise it says exactly why it won't. It also tracks the
server's tool definitions as they change and reports when a change could
break calls that used to work.

It is a library, not a service. It needs no network or MCP SDK, and adds no
runtime dependencies: a tool definition arriving over the wire is a `dict`,
and so is everything the adapter returns.

- [Where it sits](#where-it-sits)
- [Repairing one call](#repairing-one-call)
- [Outcomes](#outcomes): the four actions, and the [truth table](#the-outcome-truth-table)
- [Auto mode and shadow mode](#auto-mode-and-shadow-mode)
- [Ambiguity](#ambiguity)
- [Schemas StateGuard cannot read](#schemas-stateguard-cannot-read)
- [The tool registry](#the-tool-registry)
- [Schema changes](#schema-changes): identity, normalisation, diff
- [Caching](#caching)
- [The JSON-RPC boundary](#the-json-rpc-boundary)
- [Explanations and audit records](#explanations-and-audit-records)
- [Command line](#command-line)
- [The stdio relay example](#the-stdio-relay-example)
- [Bounds on hostile input](#bounds-on-hostile-input)
- [What it does not do](#what-it-does-not-do)
- [API reference](#api-reference)

The supported JSON Schema subset is documented separately, in
[`jsonschema-adapter.md`](jsonschema-adapter.md). An MCP `inputSchema` *is*
JSON Schema, and this adapter delegates all schema reading to that one. How
the pieces fit together is in [`mcp-architecture.md`](mcp-architecture.md).

---

## Where it sits

```
MCP client ──tools/list──▶ ┌──────────────────────────┐ ──tools/list──▶ MCP server
 (agent)   ◀── result ──── │ your proxy / gateway     │ ◀── result ────
           ──tools/call──▶ │   ToolRegistry           │ ──tools/call──▶  (only if
           ◀── result ──── │   (StateGuard)           │ ◀── result ────   forwarded)
                           └──────────────────────────┘
```

Your code owns the transport. StateGuard is called at two points:

1. **When a `tools/list` result arrives.** Hand the listing to
   `ToolRegistry.sync`. The registry now holds the server's current
   definitions and returns a report of what changed.
2. **When a `tools/call` request arrives.** Call
   `ToolRegistry.process_call`. Act on the returned `ToolCallOutcome`:
   forward its `arguments`, or answer the client yourself.

Results flow back untouched. StateGuard never builds, reads or rewrites a
tool's result.

If you hold the tool definition yourself and have no registry,
`repair_tool_call(guard, tool, arguments)` is the same decision for a
single call.

---

## Repairing one call

```python
from stateguard import ContractGuard
from stateguard.adapters.mcp import MCPAction, repair_tool_call

tool = {  # one entry from tools/list
    "name": "get_forecast",
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 14},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"], "default": "celsius"},
        },
        "required": ["location", "days"],
    },
}

guard = ContractGuard.with_mcp()
outcome = repair_tool_call(guard, tool, {"loc": "Mumbai", "days": "5"})

outcome.action       # MCPAction.FORWARD
outcome.arguments    # {"location": "Mumbai", "days": 5, "unit": "celsius"}
```

The model's input is not limited to a dict. `arguments` may be:

- `None` (no arguments), which is treated as `{}`;
- a JSON *string* holding the object, which models sometimes double-encode;
- anything else, which is refused because the root is not an object.

`repair_tool_call` never mutates the arguments you pass in.

---

## Outcomes

| `action` | Mode | Meaning | `arguments` |
|---|---|---|---|
| `FORWARD` | both | Valid as sent, or repaired with enough evidence | the payload to send: repaired, with declared defaults filled |
| `HOLD` | shadow only | Auto mode would *not* have sent the arguments as received: it would repair, escalate or refuse them | the arguments **as received** |
| `ESCALATE` | auto only | A repair was found but not trusted enough to apply unsupervised | `None` |
| `REFUSE` | auto only | No safe repair exists | `None` |

The rule a proxy needs is this:

```python
if outcome.should_forward:
    send(outcome.arguments)          # FORWARD or HOLD
else:
    answer_client(outcome.reason)    # ESCALATE or REFUSE
```

Fields on every `ToolCallOutcome`:

| Field | What it holds |
|---|---|
| `action` | What to do now, in the guard's mode |
| `auto_action` | What auto mode does with this same call. In auto mode it is equal to `action`. In shadow mode it is the decision being measured |
| `arguments` | What to send when `should_forward`; otherwise `None` |
| `original_arguments` | What the model sent (`None` becomes `{}`) |
| `proposed_arguments` | The payload auto mode would send, or `None` if auto would not forward |
| `preview` | Shadow only: the repaired payload being withheld. It is never on `arguments` |
| `reason` | One line, safe to show to a model or a person |
| `result` | The full `RepairResult`, for everything else |

### The outcome truth table

Every row below is pinned by `tests/adapters/mcp/test_outcome_truth_table.py`
in both modes. That includes the payload forwarded, the preview, and
determinism. The tool is the `get_forecast` example above, plus an
`options.hourly` boolean. "Ambiguous rename" uses a tool that requires both
`account_id` and `account_name`, called with `account`. The no-argument rows
use a tool whose schema is `{"type": "object"}`.

| Scenario | Auto: `action` | Auto: forwards | Shadow: `action` | Shadow: forwards | `auto_action` |
|---|---|---|---|---|---|
| valid, nothing to fill | `FORWARD` | as received | `FORWARD` | as received | `FORWARD` |
| valid, declared default filled | `FORWARD` | with `unit` filled | `HOLD` | as received | `FORWARD` |
| repairable: rename + coercion | `FORWARD` | repaired | `HOLD` | as received | `FORWARD` |
| repairable: nested coercion | `FORWARD` | repaired | `HOLD` | as received | `FORWARD` |
| repairable: JSON-encoded argument object | `FORWARD` | decoded object | `HOLD` | the string, as received | `FORWARD` |
| ambiguous rename | `ESCALATE` | nothing | `HOLD` | as received | `ESCALATE` |
| invalid: out of range | `REFUSE` | nothing | `HOLD` | as received | `REFUSE` |
| invalid: missing required | `REFUSE` | nothing | `HOLD` | as received | `REFUSE` |
| invalid: root is not an object | `REFUSE` | nothing | `HOLD` | as received | `REFUSE` |
| no-argument tool, `arguments=None` | `FORWARD` | `{}` | `FORWARD` | `{}` | `FORWARD` |
| no-argument tool, `arguments={}` | `FORWARD` | `{}` | `FORWARD` | `{}` | `FORWARD` |
| schema StateGuard cannot read | raises `JSONSchemaError` | — | raises `JSONSchemaError` | — | — |

Three things follow from the table:

- **Shadow is `HOLD` exactly when auto would not send the arguments
  unchanged.** Filling a declared default counts as a change, so a
  defaulted call is `HOLD` in shadow mode.
- **`preview` is set only when shadow is `HOLD` *and* auto would forward.**
  It is equal to `proposed_arguments`.
- **The decision does not depend on the mode.** `auto_action` and
  `proposed_arguments` are identical for the same call in either mode. They
  are computed once, and the shadow view is derived from them.

### The forwarding gate

Before any outcome is returned, it passes a final check. If the check
fails, `ForwardingInvariantError` (a `RuntimeError`) is raised instead of
returning the outcome. The check enforces four things:

- `arguments` is set exactly when the call is forwarded, so `ESCALATE` and
  `REFUSE` never carry a payload;
- shadow mode never blocks a call;
- shadow mode only ever forwards the original arguments;
- in auto mode, every `FORWARD` payload is **re-validated against the
  tool's schema** (in `repair_tool_call`, and therefore in `process_call`).
  This costs one extra validation pass against an already-cached contract.

These invariants hold by construction, and the check exists so that a
future bug fails loudly instead of forwarding something wrong. Because the
error signals a StateGuard bug, never a property of the input, it is not
mapped to an action. The relay example fails open on it in both modes: it
forwards the client's original call, unrepaired, and logs the failure as
category Z (StateGuard-induced). The client's arguments are not what
failed; StateGuard's payload is.

---

## Auto mode and shadow mode

```python
from stateguard import ContractGuard, GuardConfig
from stateguard.core.models.config import RepairMode

auto   = ContractGuard.with_mcp()                                          # the default
shadow = ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW))
```

**Auto mode** applies repairs it trusts, escalates the ones it does not,
and refuses calls it cannot fix.

**Shadow mode** is for measuring StateGuard before trusting it. Every call
is forwarded exactly as the model sent it, including calls auto mode would
refuse. That matters most when StateGuard *wrongly* refuses a call, which is
precisely the error a shadow rollout exists to surface. Each outcome still
records what auto mode would have done (`auto_action`, `proposed_arguments`,
the field decisions). Logging `to_dict()` for every call therefore gives you
an exact record of what turning auto on would change.

A sensible rollout:

1. Run shadow mode and log `outcome.to_dict()` (or the value-free
   `explain()`) for each call.
2. Review the `HOLD` records grouped by `auto_action`, using
   `stateguard replay --shadow` (see [Command line](#command-line)) or your
   own log tooling. Look hardest at the `REFUSE` and `ESCALATE` records:
   each one is a call auto mode would have blocked.
3. Switch to auto mode.

Nothing about the decision changes between steps 1 and 3. Only what is
forwarded changes.

---

## Ambiguity

`ESCALATE` is not a weaker `REFUSE`. It means a candidate repair exists, but
the evidence did not clear the trust threshold. `outcome.result.ambiguous`
carries the candidates with their evidence, so an agent can re-prompt with
them, a reviewer can pick one, or a UI can ask.

Things that escalate rather than repair:

- **Two declared fields competing for one key.** For example, `account`
  when the tool requires both `account_id` and `account_name`.
- **A name that swaps one word for another**, even when the names look
  alike. `max_price` is not renamed to `min_price`, and `updated_after` is
  not renamed to `created_after`.
- **A fuzzy match below the threshold for its risk band.**

Things that are refused:

- a missing required parameter with no declared default;
- a value outside `minimum`/`maximum` (or the exclusive bounds);
- an enum value that is not a re-spelling of a member;
- a word where a number is declared;
- a scalar where a string is declared (`42` for `"42"`);
- a violation inside an array element (StateGuard validates array elements
  but never repairs them).

A key StateGuard declines to move is left exactly where the model put it. A
property test checks that an escalated key is never moved.

---

## Schemas StateGuard cannot read

Some schemas use JSON Schema features StateGuard refuses rather than ignores
(`allOf`, `not`, `if`/`then`/`else`, `patternProperties`, remote `$ref`, …).
Others are not valid JSON Schema at all. Neither kind ever produces an
outcome:

| Error | Base class | Meaning |
|---|---|---|
| `UnsupportedSchemaError` | `JSONSchemaError` (a `ValueError`) | Valid JSON Schema that StateGuard cannot represent faithfully |
| `MalformedSchemaError` | `UnsupportedSchemaError` | Not valid JSON Schema: `"type": "strng"`, `"required": "a"`, a `$ref` to nothing, `exclusiveMinimum: "0"` |
| `InvalidToolDefinitionError` | `MCPAdapterError`, `MalformedSchemaError` | The tool definition itself is broken: no `inputSchema`, an `inputSchema` that is not an object, or no usable `name` |
| `SchemaReferenceError` | `JSONSchemaError` | A `$ref` StateGuard will not follow: remote, recursive, too deep, or over the expansion budget. `MalformedReferenceError` (also a `MalformedSchemaError`) is a `$ref` to nothing |

**What to do with such a tool is your policy**, decided once per tool:

- **Pass them through unvalidated.** This is the relay example's default
  (`--unreadable forward`). A host in the call path should fail open: the
  server still validates, and StateGuard not being able to read a schema is
  not evidence that the call is wrong.
- **Block its calls** (`--unreadable refuse`). This is a deliberate
  fail-closed choice that blocks calls the server may accept.

StateGuard will not claim to have checked a call it could not check, and it
will not validate against a looser schema it invented.

Some schemas can only be read loosely. Examples are an
`additionalProperties` given as a schema object and tuple-form `items`. For
these, extraction succeeds and emits a `SchemaFeatureWarning` naming the
field.

- **Registry users** should check `RegisteredTool.warnings`. The registry
  captures the warnings at registration, so warning filters do not affect
  it.
- **Direct callers** (`guard.repair`, `repair_tool_call`) can make the
  warnings fatal with
  `warnings.simplefilter("error", SchemaFeatureWarning)`. The warning is
  then raised from the first extraction of that schema, which happens on a
  cache miss. It is a `UserWarning`, not a `JSONSchemaError`.

---

## The tool registry

In a running proxy, tool definitions are state. They are learned from
`tools/list` and changed by later listings. `ToolRegistry` holds that state,
reports how it changed, and validates calls against it.

```python
from stateguard.adapters.mcp import ToolRegistry

registry = ToolRegistry()                       # or ToolRegistry(guard) for shadow etc.

report = registry.sync(tools_list_result["tools"], server="github")
print(report.explain())
```

```text
tools/list (github): 0 added, 0 removed, 1 updated, 1 unchanged; 1 breaking
  UPDATED   github/search_issues  BREAKING
    INCOMPATIBLE
      ! constraint_changed             limit: 'maximum' tightened: 100 -> 50
      ! field_added                    repo: new required parameter: calls that omit it are now rejected
      ~ enum_changed                   state: added ['all']
```

```python
outcome = registry.process_call("search_issues", arguments, server="github")
```

### Identity

A tool is identified by `ToolKey(name, server)`. Two servers can expose a
`search` tool, so a name alone is not an identity. `server` is any string
you choose, such as a configured server name or a connection id. `None` is
its own namespace, and it suits a proxy in front of one server.

Lookups never guess across servers. `get("search")` means the `search` with
no server, never "whichever server has one". If the name is registered
under other servers, `ToolNotFoundError.elsewhere` lists them and the
message says to pass `server=`.

### Operations

| Method | Does |
|---|---|
| `sync(tools, *, server=None)` | Replaces a server's whole listing **atomically** and returns a `SyncReport`. Tools absent from the listing are removed. If a name appears twice, that name is registered once, as `INVALID`, because the registry cannot tell which definition the server serves |
| `register(tool, *, server=None)` | Adds or replaces one definition and returns a `ToolChange` |
| `compare(tool, *, server=None)` | Returns the `ToolChange` that `register` *would* return, without changing anything |
| `remove(name, *, server=None)` | Removes one tool and returns its `ToolChange` |
| `clear(*, server=…)` | Forgets one server's tools; with no argument, all of them |
| `get` / `find` | Look up one tool. `get` raises `ToolNotFoundError`; `find` returns `None` |
| `tools(*, server=…)` / `servers()` | List the registered tools / servers |
| `process_call(name, arguments, *, server=None)` | Validates against the **current** definition and returns a `ToolCallOutcome` |

`process_call` raises in two cases:

- **`ToolNotFoundError`** (a `LookupError`) when nothing is registered
  under that key.
- **`ToolSchemaUnusableError`** (a `ValueError`; its `.cause` is the schema
  error) when the tool is registered but its current schema cannot be read.

**A stale schema is never used.** Registering a new definition replaces the
old one unconditionally, even when the new one cannot be read.

### What a registered tool records

`RegisteredTool` is a frozen snapshot with these fields:

- `definition` and `input_schema` (copies; mutating them does not affect
  calls);
- `state`: `READY`, `INVALID` or `UNSUPPORTED`;
- `error`;
- `fingerprint` (document and contract);
- `definition_fingerprint`;
- `warnings`;
- `revision`;
- `registered_at` and `updated_at`;
- `description` and `annotations`.

`to_dict()` is JSON-ready.

### Change reports

`ToolChange.kind` is `ADDED`, `REMOVED`, `UPDATED` or `UNCHANGED`. An
`UPDATED` change carries these fields:

- **`schema`**: a `SchemaDiff` (see below). Its status is `UNCHANGED` when
  only metadata changed. (`schema` is `None` only for `ADDED` and
  `REMOVED`.)
- **`changed_fields`**: which top-level definition fields changed
  (`description`, `annotations`, `title`, …).
- **`description_changed`**: reported even when the schema did not change.
  An agent reads tool descriptions, and a description that changes after
  approval is a known MCP attack pattern. StateGuard reports the change. It
  does not judge it.
- **`breaking`**: true when calls that used to be valid can now be
  rejected. That covers an incompatible schema change, a tool removed, or a
  tool that became unreadable.

`SyncReport` groups changes as `added`, `removed`, `updated`, `unchanged`,
`unusable` and `breaking`, and provides `explain()` and `to_dict()`.

Re-syncing an unchanged listing is cheap: when a definition's fingerprint
is unchanged, the existing entry is reused (about 0.6 ms for the 35-tool
benchmark listing).

### Thread safety

The registry is thread-safe. Every read and write happens under one lock,
entries are immutable, and `process_call` validates outside the lock
against the snapshot it took. One caveat: recording extraction warnings
uses `warnings.catch_warnings`, which is process-global on most builds.

---

## Schema changes

Three functions in `stateguard.adapters.jsonschema` let you reason about a
schema's identity and how it changed. They are useful with or without the
registry:

```python
from stateguard.adapters.jsonschema import diff_schemas, fingerprint_schema, normalize_schema
```

### Identity and normalisation

`fingerprint_schema(schema)` returns `SchemaFingerprint(document, contract)`.
Both are SHA-256 hex digests:

- **`document`** covers the schema exactly as written, in canonical JSON
  (sorted keys, no insignificant whitespace). Any edit changes it,
  including one to a `description`.
- **`contract`** covers `normalize_schema(schema)`, which keeps only what
  decides whether a call is accepted. Normalisation:
  - drops annotations (`title`, `description`, `examples`, `$comment`, …)
    and `x-*` extensions;
  - resolves local `$ref`s;
  - sorts `required`, `type`, `enum`, `anyOf` and `oneOf`;
  - unifies equivalent spellings: `1.0` becomes `1`, `["string"]` becomes
    `"string"`, and empty `required`/`properties`,
    `additionalProperties: true` and draft-4 `exclusiveMinimum: false` are
    dropped.

  If two schemas have the same contract fingerprint, they accept exactly
  the same payloads.

### Diff

`diff_schemas(before, after)` returns a `SchemaDiff`. Its `status` is one
of:

| Status | Meaning |
|---|---|
| `UNCHANGED` | Identical documents |
| `EQUIVALENT` | The documents differ but the contracts do not (annotations, ordering, `$ref` layout) |
| `COMPATIBLE` | The contract changed, and **every payload valid before is still valid** |
| `INCOMPATIBLE` | At least one change can reject a previously valid payload |
| `INVALID` | The new schema is not valid JSON Schema |
| `UNSUPPORTED` | The new schema is valid, but StateGuard cannot represent it |

`COMPATIBLE` is a guarantee. A change is marked non-breaking only when a
rule *proves* it cannot reject a previously valid call. Anything the module
cannot prove safe is marked breaking: a `pattern` edit, a `oneOf` branch
added, an unknown keyword. Property tests generate mutated schemas and
check every non-breaking verdict against the validator.

`compatible` is about acceptance, not behaviour. A changed `default`, or an
optional parameter removed from an open schema, is reported as
non-breaking, with a `detail` explaining what changes for callers.

Each `SchemaChange` has these fields:

- `kind`: one of `field_added`, `field_removed`, `required_added`,
  `required_removed`, `type_changed`, `enum_changed`, `const_changed`,
  `constraint_changed`, `default_changed`, `items_changed`,
  `additional_properties_changed`, `union_changed`, `keyword_changed`,
  `schema_replaced`;
- `path`;
- `breaking`;
- `keyword`;
- `before` and `after` (`ABSENT` when missing);
- `detail`.

The diff does not infer renames. A removed `city` and an added `location`
are reported as a removal and an addition. Whether they mean the same thing
is the judgement the repair engine makes per call, with evidence.

---

## Caching

`ContractGuard.with_mcp()` caches extracted contracts in a thread-safe LRU
(`SchemaCache`, `DEFAULT_CACHE_SIZE` entries unless you pass
`with_mcp(cache_size=…)`). The cache is keyed on the tool name plus the
schema's **document fingerprint**.

- **A changed schema is never served stale.** The key is the schema's
  content, not its name, so a new schema is a cache miss.
- **Nothing needs invalidating.** Superseded entries age out of the LRU.
- **An unreadable schema is not cached.** It raises again on every call.

The cache key uses the document fingerprint, not the contract
fingerprint, on purpose. Two documents that differ only in a `description`
produce the same contract, but each contract points back at its own source
document.

The registry extracts every definition at registration and skips the cache
lookup, so that each registration records its own `SchemaFeatureWarning`s.
The result is then stored in the cache, so `process_call`, which goes
through the guard, normally finds it there.

---

## The JSON-RPC boundary

A proxy that handles raw MCP messages should change exactly one thing in a
`tools/call` request, `params.arguments`, and preserve everything else:

- the request `id`;
- `params._meta` (progress tokens, tracing, the protocol version);
- any parameter a later protocol revision adds.

The helpers below do that:

```python
from stateguard.adapters.mcp import (
    ToolCallRequest, ToolListAccumulator, forward_message, refusal_response,
)

request = ToolCallRequest.parse(message)     # raises MalformedMessageError
outcome = registry.process_call(request.name, request.arguments, server=server)

to_server = forward_message(request, outcome)
if to_server is not None:
    send_to_server(to_server)
else:
    send_to_client(refusal_response(request, outcome))
```

| Helper | Behaviour |
|---|---|
| `ToolCallRequest.parse(message)` | Validates a `tools/call` request and keeps a deep copy of it. Raises `MalformedMessageError` when the message is unusable; answer it with `error_response(request_id(message), INVALID_PARAMS, …)` |
| `forward_message(request, outcome)` | Returns `None` when the call is not forwarded. Returns the **original message, unmodified** when the arguments are unchanged (`HOLD`, or a `FORWARD` that changed nothing). Otherwise returns the original with only `params.arguments` replaced |
| `refusal_response(request, outcome)` | For `ESCALATE`/`REFUSE`: a `CallToolResult` with `isError: true` and the reason, so the *model* sees why and can correct itself. Adds `resultType: "complete"` when the request declares protocol `2026-07-28` or later |
| `tool_error_response(request, text)` | The same response shape, with your own text |
| `ToolListAccumulator` | Collects a paginated `tools/list` into one complete listing. Syncing a single page would remove the tools listed on the other pages |

`stateguard.adapters.mcp.jsonrpc` also provides `error_response` (for
genuine protocol errors), `is_request`, `is_notification`, `is_response`,
`request_id`, `protocol_version`, and the method-name constants.

A correctable argument problem is reported as a *tool error* (in the
result, where the model reads it), not as a JSON-RPC *protocol error*
(which clients usually handle without showing the model). MCP draws the
same line.

---

## Explanations and audit records

`outcome.explain()` renders the decision field by field. It **contains no
argument values**, so it is safe to log:

```text
FORWARD: Repaired 2 field(s) before forwarding.
status: success (auto)
  repaired    query <- qeury  [rename, trust 0.94, INFERRED]
              Fuzzy name match: 'qeury' -> 'query'.
                - jaro-winkler similarity 0.940
  repaired    state  [set_value, trust 1.00, INFERRED]
              Normalise the value at 'state' to the declared enum member 'open'.
                - received value normalises onto the declared member 'open'
```

`outcome.to_dict()` is the full record as JSON-ready data. It contains:

- `action`, `auto_action`, `mode` and `reason`;
- `arguments`, `proposed_arguments` and `preview` (the arguments as received
  are under `result.original_input`);
- every field decision, with its trust and evidence;
- every repair attempt, including discarded ones.

The record **does contain values**, so store it where argument values are
allowed to go.

`outcome.result.field_decisions()` is the typed form. Each decision is
`REPAIRED`, `HELD`, `DISCARDED` or `UNRESOLVED`, with a path, an
operation, trust and evidence.

Every value in a forwarded payload either was sent by the model, was
changed by a recorded repair, or is a declared default. A property test
checks this over generated drifted calls.

---

## Command line

```bash
# One call: what would a repairing proxy do?
stateguard check --tool tool.json --payload arguments.json [--shadow] [--json]

# Two versions of a tool, a bare schema, or a whole tools/list
stateguard diff before.json after.json [--json]

# Replay recorded calls against a tools/list
stateguard replay --tools tools.json --calls calls.jsonl [--shadow] [--json]
```

**`diff`** exits `0` when nothing is breaking and `1` when something is, so
it can gate CI on a server's published tool list:

```text
tools/list: 0 added, 0 removed, 1 updated, 1 unchanged; 1 breaking
  UPDATED   search_issues  BREAKING
    INCOMPATIBLE
      ! constraint_changed             limit: 'maximum' tightened: 100 -> 50
      ! field_added                    repo: new required parameter: calls that omit it are now rejected
      ~ enum_changed                   state: added ['all']
```

**`replay`** takes calls as a JSON array or JSON lines. Each entry is
either a `{"name", "arguments"}` object or a raw JSON-RPC `tools/call`
message, so a captured transcript works as-is. It prints each call's
explanation and then a summary:

```text
#4 delete_issue
  UNKNOWN_TOOL: no tool 'delete_issue' in the list

Summary: 3 forward, 1 refuse, 1 unknown_tool
```

With `--shadow`, every readable call is `FORWARD` or `HOLD`, so a second
line gives the counts by what auto mode would have done instead:

```text
Summary: 1 forward, 3 hold, 1 unknown_tool
Auto mode would: 3 forward, 1 refuse, 1 unknown_tool
```

`--json` carries both, as `summary` and `auto_summary`. Together they show
what switching to auto mode would change, which is the review step of a
shadow rollout.

---

## The stdio relay example

[`examples/mcp/stdio_proxy.py`](../examples/mcp/stdio_proxy.py) is a
complete MCP stdio relay in 400 lines, with no MCP SDK. An MCP client
launches it in place of the server:

```bash
python examples/mcp/stdio_proxy.py [--shadow | --passthrough] [--audit FILE] [--unreadable forward|refuse] -- python my_server.py
```

What the relay does:

- **Passes every message through byte for byte**, except `tools/call`
  requests it repairs or answers itself.
- **Keeps its registry current.** It syncs from `tools/list` responses
  (including paginated ones), and re-lists on
  `notifications/tools/list_changed`, holding new calls until the refresh
  lands.
- **Writes a value-free JSON-lines audit log.**

It works with both protocol generations:

- the `initialize` handshake;
- the stateless `2026-07-28` revision, in which every request carries its
  protocol version in `params._meta`.

It is tested end to end against the MCP Python SDK client and server. See
[`examples/mcp/README.md`](../examples/mcp/README.md#on-the-wire-the-stdio-relay).

`--record FILE` writes a redacted, versioned recording of everything that
crosses the relay, for replay measurement (see
[Recording traffic](#recording-traffic)).

It is an example of where StateGuard sits on the wire, not a production
gateway. It serves one server process at a time.

### Recording traffic

`stateguard.adapters.mcp.recording` holds the recording format, the
redactor, a `Recorder` and the parser:

```python
from stateguard.adapters.mcp import Provenance, Recorder, read_recording

recorder = Recorder(open("session.jsonl", "w"), provenance=Provenance.LOCAL_DEVELOPMENT,
                    session="dev-1")
recorder.message("client_to_relay", line)   # every wire line, any direction
recorder.audit(event)                        # StateGuard decisions
recorder.close()                             # writes "end"; complete only if nothing failed

recording = read_recording("session.jsonl")  # raises RecordingError, never anything else
```

- **Format.** `stateguard.mcp.recording` version 1: JSON lines, keys sorted.
  - It starts with a `header` (provenance, session, redaction policy, host
    settings) and ends with `end`.
  - In between: `message`, `audit`, `fault` and `label` records, in a
    strictly increasing `seq`.
  - Timing and line lengths live under `diag` and are never needed to
    replay. Timestamps are written only with `deterministic=False`.
  - Unknown versions are refused (`UnsupportedRecordingVersionError`).
- **Redaction.** It runs before anything is written. It is deterministic and
  rule-based, not a detector.
  - Credential key names remove the whole value at any depth: in
    arguments, `_meta`, results and errors.
  - Credential value shapes are removed wherever they appear: bearer and
    basic schemes, JWTs, cloud and VCS keys, PEM private keys, URL
    userinfo, secret query parameters and `password=` assignments.
  - In the default `mask` mode, every other free-form string is masked
    shape-preservingly.
  - Values are kept verbatim only for protocol structure, and for arguments
    the tool's own schema vouches for (an enum member or a near miss of one,
    a numeric string in a numeric field).
- **Failure.** A recorder never raises into its host. If it fails, it stops,
  and the file is marked incomplete; replay refuses incomplete files by
  default.
- **Provenance.** `synthetic`, `fault_injection`, `replay`,
  `local_development` or `real_capture`. Only `real_capture` is real
  traffic, and the replay report says so on its first line. Only
  `synthetic` and `fault_injection` may carry ground truth (`fault`,
  `label`); the recorder refuses it for the others, and the parser refuses a
  file that has it. `real_capture` requires mask redaction, always masks
  numbers (`RedactionPolicy.mask_numbers`: sign and order of magnitude only,
  protocol and contract values excepted), and the relay records it in shadow
  mode only (`docs/runtime/REAL_CAPTURE.md`).
  `summarize_evidence(recording)` separates observed facts from
  StateGuard's hypothetical proposals and from unknown outcomes.

---

## Bounds on hostile input

Tool definitions and arguments both come from outside your process. These
are the limits StateGuard enforces:

| Input | Bound | Beyond it |
|---|---|---|
| Subschemas walked while reading a schema | 10,000 resolutions per schema (`DEFAULT_MAX_EXPANSIONS` in `stateguard.adapters.jsonschema.refs`), `$ref` expansions included | `SchemaReferenceError`. This stops "billion laughs" schemas built from nested `$ref`s. A schema with more than 10,000 subschemas in total is refused for the same reason |
| `$ref` recursion | refused | `SchemaReferenceError` |
| `pattern` | screened before compilation for nested unbounded quantifiers (`(a+)+`, …). This is a heuristic: it does not catch overlapping alternation such as `(a\|a)+` | `UnsupportedSchemaError` |
| Argument nesting depth | 100 levels (`MAX_PAYLOAD_DEPTH`), checked iteratively | `REFUSE`, with a value-free reason. Never a `RecursionError` |
| JSON-RPC message nesting | the same bound, in `ToolCallRequest.parse` | `MalformedMessageError` |
| A tool definition mutated after registration | the registry holds private copies | no effect on calls |

Two checks were run out of tree during development, and are not part of the
test suite. A fuzz run of 20,000 random registry inputs produced zero
unexpected exceptions. A differential run of about 6,000 generated payloads
against the `jsonschema` reference validator, over every corpus tool,
produced zero disagreements in either direction. In-tree, hostile input is
covered by `test_hostile_payloads.py` and `test_refs.py`.

---

## What it does not do

These are deliberate non-claims:

- **No transport, retries, reconnection or timeouts.** The SDK or your
  gateway owns the connection.
- **No authentication, authorization or security policy.** StateGuard
  checks that arguments match a schema. It does not decide whether a call
  *should* be made, and passing validation does not make a call safe. It
  reports a changed tool description, but does not judge it.
- **No result repair.** `outputSchema` / `structuredContent` are not read
  or modified, and responses pass through untouched.
- **No semantic correctness.** A call that matches the schema can still be
  the wrong call. A `SUCCESS` means "conforms to the schema subset
  StateGuard supports" (see [ADR-0001](adr/0001-json-schema-source-of-truth.md)),
  not "does what the user meant".
- **No guessing.** Drift on an *optional* parameter is not renamed, because
  nothing marks it as missing. Array elements are validated but not
  repaired. A rename between different words is escalated, not applied.
- **No full JSON Schema.** It supports the subset MCP tools use, and refuses
  the rest. See [`jsonschema-adapter.md`](jsonschema-adapter.md).
- **No persistence or sharing.** The registry lives in memory for one
  process and is rebuilt from `tools/list`.

---

## API reference

Everything below is importable from `stateguard.adapters.mcp` unless noted.

| Name | Kind |
|---|---|
| `repair_tool_call(guard, tool, arguments)` | function → `ToolCallOutcome` |
| `outcome_for(result, original_arguments)` | function → `ToolCallOutcome` (from a `RepairResult` you already have) |
| `ToolCallOutcome`, `MCPAction` | outcome model |
| `ToolRegistry`, `RegisteredTool`, `ToolKey`, `ToolState` | registry |
| `ToolChange`, `ToolChangeKind`, `SyncReport` | change reports |
| `ToolCallRequest`, `ToolListAccumulator`, `forward_message`, `refusal_response`, `tool_error_response` | JSON-RPC boundary |
| `stateguard.adapters.mcp.jsonrpc`: `error_response`, `is_request`, `is_notification`, `is_response`, `request_id`, `protocol_version`, `INVALID_PARAMS`, `PROTOCOL_VERSION_META_KEY`, `TOOLS_CALL`, `TOOLS_LIST`, `TOOLS_LIST_CHANGED` | lower-level JSON-RPC helpers |
| `MCPToolAdapter`, `split_tool_definition`, `SchemaCache`, `DEFAULT_CACHE_SIZE` | adapter plumbing |
| `MCPAdapterError`, `InvalidToolDefinitionError`, `ToolNotFoundError`, `ToolSchemaUnusableError`, `MalformedMessageError`, `ForwardingInvariantError` | errors |
| `stateguard.adapters.jsonschema`: `fingerprint_schema`, `document_fingerprint`, `normalize_schema`, `SchemaFingerprint`, `diff_schemas`, `SchemaDiff`, `SchemaChange`, `SchemaChangeStatus`, `ChangeKind` | identity and diff |
| `stateguard.adapters.jsonschema`: `JSONSchemaError`, `UnsupportedSchemaError`, `MalformedSchemaError`, `SchemaReferenceError`, `MalformedReferenceError`, `SchemaFeatureWarning` | schema errors |
