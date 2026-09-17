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
| `server.py` | An ordinary MCP server with two tools. Knows nothing about StateGuard. |
| `proxy.py` | `RepairingClient` — caches `tools/list`, repairs `arguments`, forwards. The reusable piece. |
| `demo.py` | Runs the same four calls twice, with and without the proxy. |

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

==============================================================================
WITH StateGuard -- the same calls through the repairing proxy
==============================================================================
Loaded 2 tool definition(s): get_forecast, search_places

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
| 4 | `limit: 999` | **none** | — |

Every operation carries its evidence. `result.attempts[i].applied_operations`
holds the strategy, the rationale, the trust score, and what the score was
built from — so a repair is auditable after the fact, not just applied.

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
forwards the model's arguments **untouched** and reports what it would have
changed:

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
  would have sent: {'location': 'Mumbai', 'days': 5, 'unit': 'celsius'}
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
* **Double-encoded `arguments`** — a model emitting the whole argument
  object as a JSON string — is not handled here yet.

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
        print(call.outcome.action, call.outcome.reason)
```

`call.outcome.action` is one of `FORWARD`, `HOLD`, `ESCALATE`, `REFUSE`.
`ESCALATE` is the one worth handling deliberately: it means a repair *was*
found but the evidence did not justify applying it unsupervised, and
`call.outcome.result.ambiguous` carries the candidates — so an agent can
re-prompt with them rather than guessing.
