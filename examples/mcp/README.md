# Repairing MCP tool calls

A server updated its tool signature. The agent is still calling the old
shape — and doing what models do besides: sending a number as a string,
getting an enum's capitalisation wrong, omitting an optional.

Without StateGuard, every one of those is a rejected call and another turn
of the agent loop spent on a failure. With it, they are repaired at the
boundary — the server is unmodified and unaware, and so is the agent.

## Run it

```bash
pip install 'sguard[mcp]'
```

```bash
python examples/mcp/demo.py
```

That launches `server.py` as a real subprocess and speaks MCP over stdio, so
what you see is the actual protocol rather than a simulation of it.

## What's here

| File | What it is |
|---|---|
| `server.py` | An ordinary MCP server with three tools. Knows nothing about StateGuard. |
| `proxy.py` | `RepairingClient` — an in-process wrapper around an MCP SDK `Client`: caches `tools/list`, repairs `arguments`, forwards. |
| `stdio_proxy.py` | A **stdio relay** — no SDK. Put it where a client expects a server command; it sits on the wire between them. See [below](#on-the-wire-the-stdio-relay). |
| `demo.py` | Runs the same five calls three times: directly, through the proxy, and through the proxy in shadow mode. |

## Output

Real output, captured from the command above:

```text
==============================================================================
WITHOUT StateGuard -- the agent calls the server directly
==============================================================================

renamed parameter + stringified number
  sent: {'loc': 'Mumbai', 'days': '5'}
  SERVER ERROR: Error executing tool get_forecast: 1 validation error for get_forecastArguments

enum in the wrong case
  sent: {'location': 'Delhi', 'days': 3, 'unit': 'Celsius'}
  SERVER ERROR: Error executing tool get_forecast: 1 validation error for get_forecastArguments

abbreviated parameter, optional omitted
  sent: {'q': 'Bengaluru'}
  SERVER ERROR: Error executing tool search_places: 1 validation error for search_placesArguments

out of range -- not repairable, and should not be
  sent: {'query': 'Chennai', 'limit': 999}
  SERVER ERROR: Error executing tool search_places: 1 validation error for search_placesArguments

one city where two are expected -- found, but not trusted
  sent: {'city': 'Delhi'}
  SERVER ERROR: Error executing tool compare_cities: 2 validation errors for compare_citiesArguments

==============================================================================
WITH StateGuard -- the same calls through the repairing proxy
==============================================================================
Loaded 3 tool definition(s): get_forecast, search_places, compare_cities

renamed parameter + stringified number
  sent by agent: {'loc': 'Mumbai', 'days': '5'}
  StateGuard: FORWARD -- Repaired 2 field(s) before forwarding.
  sent to server: {'days': 5, 'location': 'Mumbai', 'unit': 'celsius'}
  OK: Mumbai: 5-day forecast, temperatures in degrees C.

enum in the wrong case
  sent by agent: {'location': 'Delhi', 'days': 3, 'unit': 'Celsius'}
  StateGuard: FORWARD -- Repaired 1 field(s) before forwarding.
  sent to server: {'location': 'Delhi', 'days': 3, 'unit': 'celsius'}
  OK: Delhi: 3-day forecast, temperatures in degrees C.

abbreviated parameter, optional omitted
  sent by agent: {'q': 'Bengaluru'}
  StateGuard: FORWARD -- Repaired 1 field(s) before forwarding.
  sent to server: {'query': 'Bengaluru', 'limit': 5}
  OK: Found up to 5 places matching 'Bengaluru'.

out of range -- not repairable, and should not be
  sent by agent: {'query': 'Chennai', 'limit': 999}
  StateGuard: REFUSE -- Could not bring the arguments up to the tool's schema (1 violation(s) remain: 'limit'). Not forwarded.
  (not sent)

one city where two are expected -- found, but not trusted
  sent by agent: {'city': 'Delhi'}
  StateGuard: ESCALATE -- A repair was found but not trusted enough to apply unsupervised (1 ambiguous field(s): 'city_b'). Not forwarded.
  held: city_b -- trust 0.65 is below the INFERRED threshold of 0.75
  (not sent)

==============================================================================
SHADOW MODE -- observe only: every call reaches the server as sent
==============================================================================
Loaded 3 tool definition(s): get_forecast, search_places, compare_cities

renamed parameter + stringified number
  sent by agent: {'loc': 'Mumbai', 'days': '5'}
  StateGuard: HOLD -- Shadow mode: 2 repair(s) determined and withheld. Forward the original arguments and review the preview.
  would have sent: {'days': 5, 'location': 'Mumbai', 'unit': 'celsius'}
  SERVER ERROR: Error executing tool get_forecast: 1 validation error for get_forecastArguments

enum in the wrong case
  sent by agent: {'location': 'Delhi', 'days': 3, 'unit': 'Celsius'}
  StateGuard: HOLD -- Shadow mode: 1 repair(s) determined and withheld. Forward the original arguments and review the preview.
  would have sent: {'location': 'Delhi', 'days': 3, 'unit': 'celsius'}
  SERVER ERROR: Error executing tool get_forecast: 1 validation error for get_forecastArguments

abbreviated parameter, optional omitted
  sent by agent: {'q': 'Bengaluru'}
  StateGuard: HOLD -- Shadow mode: 1 repair(s) determined and withheld. Forward the original arguments and review the preview.
  would have sent: {'query': 'Bengaluru', 'limit': 5}
  SERVER ERROR: Error executing tool search_places: 1 validation error for search_placesArguments

out of range -- not repairable, and should not be
  sent by agent: {'query': 'Chennai', 'limit': 999}
  StateGuard: HOLD -- Shadow mode: auto mode would refuse this call (1 violation(s) remain: 'limit'). Forwarded as received.
  auto mode would refuse; forwarded anyway (shadow)
  SERVER ERROR: Error executing tool search_places: 1 validation error for search_placesArguments

one city where two are expected -- found, but not trusted
  sent by agent: {'city': 'Delhi'}
  StateGuard: HOLD -- Shadow mode: auto mode would escalate this call -- a repair was found but not trusted enough to apply ('city_b'). Forwarded as received.
  auto mode would escalate; forwarded anyway (shadow)
  SERVER ERROR: Error executing tool compare_cities: 2 validation errors for compare_citiesArguments

==============================================================================
Three calls that the server rejected now succeed. The fourth is still
refused -- 'limit: 999' violates the schema's maximum and there is no
honest repair for it, so StateGuard does not invent one. The fifth has
a repair, but 'city' fits 'city_a' and 'city_b' equally well, so it is
escalated for the agent to restate rather than guessed. In shadow mode
every call reaches the server exactly as sent, and StateGuard only says
what it would have done.
==============================================================================
```

## What each repair actually was

| Call | Drift | Repair | Trust |
|---|---|---|---|
| 1 | `loc` → `location` | fuzzy name match | 0.854 (jaro-winkler) |
| 1 | `"5"` → `5` | type coercion | 1.0 (round-trips exactly) |
| 1 | `unit` absent | filled from the schema's `default` | — (declared, not inferred) |
| 2 | `"Celsius"` → `"celsius"` | enum normalisation | 1.0 (normalises onto a declared member) |
| 3 | `q` → `query` | fuzzy name match | — |
| 3 | `limit` absent | filled from the schema's `default` | — |
| 4 | `limit: 999` | **none** — refused | — |
| 5 | `city` for `city_a` / `city_b` | **held** — escalated | 0.65: the two fields tie, so the margin pulls trust below the bar |

Every operation carries its evidence — the strategy, the rationale, the
trust score, and what the score was built from — so a repair is auditable
after the fact, not just applied. `outcome.explain()` prints it per field;
`outcome.to_dict()` is the full record. To replay one call from the shell:

```bash
stateguard check --tool tool.json --payload arguments.json
```

## The fourth call is the point

`limit: 999` violates the schema's `maximum: 10`, and there is no honest
repair for it: any value StateGuard picked would be one the model did not
ask for. So it refuses, and the call is never sent.

That matters more than the three that succeed. A repair layer that always
finds an answer is one you cannot trust with the answers it finds — and the
failure that damages trust most is not a missed repair, it is a confident
wrong one. Refusing also costs less than failing: the server would have
rejected the call anyway, so forwarding it spends a round trip to learn what
StateGuard already knew.

## Shadow mode

Before turning on automatic repair, run the same proxy in shadow. It
forwards **every** call untouched — including the ones auto mode would refuse
or escalate — and reports what auto mode would have done (the third section
of the output above):

```python
from stateguard import ContractGuard
from stateguard.core.models.config import GuardConfig, RepairMode

proxy = RepairingClient(
    client,
    guard=ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW)),
    on_event=print,
)
```

```text
  StateGuard: HOLD -- Shadow mode: 2 repair(s) determined and withheld. ...
  would have sent: {'days': 5, 'location': 'Mumbai', 'unit': 'celsius'}
  ...
  StateGuard: HOLD -- Shadow mode: auto mode would refuse this call (...). Forwarded as received.
```

The preview is exactly what auto mode would have sent — that is enforced by
a test, because a shadow diff you cannot trust to match tells you nothing.

## What it does not do

* **Repairs arguments, not results.** `outputSchema` / `structuredContent`
  — repairing what a server sends *back* — is a deliberate non-goal for v1.
  Argument drift is where the model is, and the model is what drifts.
* **A `SUCCESS` is not a claim of JSON Schema compliance.** StateGuard
  validates with its own validator against the subset of JSON Schema that
  tool definitions actually use. Keywords outside that subset raise rather
  than being ignored, so the gap is bounded and visible. See
  [ADR-0001](../../docs/adr/0001-json-schema-source-of-truth.md).
* **Tools whose schema StateGuard cannot read** (`allOf`, a remote
  `$ref`, …). `call_tool` raises `JSONSchemaError` and sends nothing — this
  demo fails closed. Passing such tools through unvalidated instead is a
  per-tool policy for your deployment, not something decided per call.

A whole argument object sent as a JSON string *is* handled: it is parsed
before repair, and the reason says so.

## On the wire: the stdio relay

`proxy.py` needs the MCP SDK and sits *inside* your client. `stdio_proxy.py`
needs neither: it is a separate process an MCP client launches in place of
the server, and it relays newline-delimited JSON-RPC between them.

```bash
# where a client config says:   python my_server.py
# put instead:
python examples/mcp/stdio_proxy.py --audit stateguard.jsonl -- python my_server.py
```

Every message passes through byte for byte except three:

| Message | What the relay does |
|---|---|
| `tools/list` responses (every page) | Syncs a `ToolRegistry`; the response still reaches the client untouched |
| `notifications/tools/list_changed` | Passes it on and re-lists the tools itself, so calls are never validated against a replaced listing |
| `tools/call` requests | `FORWARD`: sends the original request, with only `params.arguments` replaced if repaired. `HOLD` (shadow): sends it unchanged. `ESCALATE`/`REFUSE`: answers the client itself with a `CallToolResult` carrying `isError: true` and the reason — the server never sees it |

Results are never touched: the relay writes the server's response bytes as
they arrived. Its decisions go to a JSON-lines audit log — every listing
change (added, removed, breaking, description changed) and every call
(action, what auto mode would do, field decisions with trust) — with field
names, never argument values.

The relay is in the call path, so it **fails open**: when StateGuard cannot
decide, the call goes to the server exactly as the client sent it. A call to
a tool the relay has no definition for is forwarded for the server to
answer. A call to a tool whose schema StateGuard cannot read is forwarded
unvalidated (`--unreadable refuse` blocks it instead). An internal
StateGuard error forwards the original call and is logged as category `Z`;
it never reaches the client as an error and never stops the relay.
`--shadow` observes without changing anything; `--passthrough` turns
StateGuard off entirely, which is the baseline for measurement.

Before the relay changes or blocks a call, it re-lists the tools and decides
again on a listing requested after the call arrived. Under protocol
`2026-07-28` a server announces `tools/list_changed` only on
`subscriptions/listen` streams, which the relay does not hold, so it cannot
rely on being told that a schema changed. Calls that pass unchanged cost no
extra round trip. If no fresh listing arrives, the call is forwarded
unchanged. `--no-listing-refresh` turns this off.

`--record FILE` (off by default) writes a redacted, versioned JSON-lines
recording of every line in all four directions, plus the relay's decisions.
`benchmarks/runtime/replay.py` replays it against an in-process server built
from the recording, never against the original.

- **Redaction runs before anything is written.** Credentials are removed by
  key name and by value shape. Other free-form text is masked (letters to
  `a`, digits to `1`). The server command line is never written.
- **Recording only observes.** It records a line after it was written or
  read, so it cannot add, drop or reorder anything. A recorder that fails
  stops and is logged once as `recording_stopped`; traffic carries on
  unchanged.
- **Provenance is part of the file.** `--record-provenance` defaults to
  `local_development`. Claim `real_capture` only for real use; it requires
  `--shadow` and mask redaction, and the relay refuses to start otherwise
  (see `docs/runtime/REAL_CAPTURE.md`). `--record-redaction secrets_only`
  keeps free-form text and is meant for traffic known to hold no personal
  data.
- **Evidence report.** `python examples/mcp/evidence_report.py FILE` keeps
  what happened (wire, observed failures), what StateGuard observed, what it
  would have done (hypothetical in shadow), and unknown outcomes apart.

**Auth failures** are recognised and never retried. A server answer that
deterministically signals one is logged as category K (`auth_failure`) and
is never sent again, even for a tool declared safe to repeat. It reaches the
client unchanged. The signals are:

- an HTTP 401/403 status or an RFC 6750 OAuth code in `error.data`;
- `401 Unauthorized`, `HTTP 403` or `error="invalid_token"` in the error
  text;
- a message that is just `Unauthorized` or `Forbidden`.

Auth-like words alone ("permission", "token", "access") never count.
StateGuard never handles credentials.

`--restart N` (off by default) restarts a server process that exits, at most
N times.

- **When the relay restarts.** Only when the server dies idle, with nothing
  in flight. The relay re-runs the `initialize` handshake if the session used
  one, refusing a server that changed protocol version. It also lists the
  tools again, since the old listing describes a process that no longer
  exists.
- **Requests in flight are never re-sent.** Each one is answered "outcome
  unknown". By default the relay then does *not* restart, because the dead
  process may have left work running. `--restart-policy after-unknown`
  restarts anyway, after answering them.
- **Requests that arrive mid-restart** wait for it. If they still cannot be
  sent, they are answered "not sent", because they certainly did not
  execute.
- **A tool call is decided only against a live, listed server.** After a
  restart, a `tools/call` waits until the new server's tools have been
  listed, so it is repaired or refused against the new schemas. It is never
  decided against the dead server's schemas, and never forwarded
  unvalidated because a restart was under way.
  - If the new server cannot list its tools, the call is answered "not
    sent".
  - A decision is only ever written to the server process it was made for.
- **When restarts run out,** the relay stops, as it would without
  StateGuard.

It speaks both protocol generations: the `initialize` handshake, and the
stateless revision (`2026-07-28`) in which every request carries its
protocol version in `params._meta` — which the relay preserves on forwarded
calls, copies onto its own `tools/list` requests, and reflects in the
results it synthesizes (`resultType`). Tested end to end against the MCP
Python SDK 2.x client and server (`tests/examples/test_stdio_relay.py`), and
byte by byte against a scripted server that changes a schema mid-session
(`tests/examples/test_stdio_relay_protocol.py`).

## Using the proxy in your own code

`RepairingClient` is about eighty lines and has no StateGuard-specific
concepts in its interface:

```python
from mcp import Client, StdioServerParameters
from proxy import RepairingClient

async with Client(server_params) as client:
    proxy = RepairingClient(client)
    call = await proxy.call_tool("get_forecast", {"loc": "Mumbai", "days": "5"})

    if call.called:
        print(call.response.content)
    else:
        print(call.outcome.explain())  # why, field by field
```

The proxy is a thin wrapper over `stateguard.adapters.mcp.repair_tool_call`,
which is the piece to reuse if you already have your own client loop.

`call.outcome.action` is one of `FORWARD`, `HOLD`, `ESCALATE`, `REFUSE`.
`ESCALATE` is the one worth handling deliberately: it means a repair *was*
found but the evidence did not justify applying it unsupervised, and
`call.outcome.result.ambiguous` carries the candidates — so an agent can
re-prompt with them rather than guessing.
